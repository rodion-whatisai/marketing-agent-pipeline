# -*- coding: utf-8 -*-
"""
FB Audience Report — штатная команда: полный аудит активных объявлений страницы.
Порт сессионного пайплайна client-a.example 2026-07-17 в движок (решение Rodion'а:
«сканер должен давать те же аутпуты, что сессионный код»).

Конвейер:
  A. Листинг всех активных → полные ad-records (плейсменты/формат/даты/тексты;
     конец скролла — футер System status). Опция --har добавляет records из
     HAR-файла залогиненного браузера (даёт impressions_text).
  B. Deep-scan модалок (fb_scan.deep_scan_one_ad: клик-фикс + GraphQL ad_details
     сайдкары) — идемпотентный resume, паузы, бэкофф, перезапуск браузера.
  C. Расчёты: зомби-флаг, %М, топ-возраст/страна, агрегаты (coverage-first).
  D. Выходы в scans/<save_as>/: ads_v4.csv, aggregates.csv, demography_by_country.csv.

Запуск:
    python fb_audience_report.py client-a --save-as client-a.example --top 10
    python fb_audience_report.py client-a.example                      # полный, все активные
    python fb_audience_report.py client-a --har путь\к\файлу.har --skip-deep
"""
import sys
import csv
import json
import time
import argparse
from datetime import datetime, date
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from utils import setup_console
setup_console()
from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header

from fb_page_finder import find_brand_pages
from fb_ads_listing import collect_active_ad_records, parse_har_ad_records
from fb_scan import deep_scan_one_ad

PLACEMENT_LABEL = {"FACEBOOK": "FB", "INSTAGRAM": "IG", "AUDIENCE_NETWORK": "AudNet",
                   "MESSENGER": "Msgr", "THREADS": "Threads", "WHATSAPP": "WA",
                   "OCULUS": "Oculus"}
AGE_ORDER = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]

PACE_OK_S = 8      # пауза после успешного объявления (не дразним rate-limit)
PACE_FAIL_S = 45   # бэкофф после отказа
COOLDOWN_S = 300   # остывание при серии отказов
RESTART_EVERY = 60 # профилактический перезапуск браузера


# ─── Шаг A: листинг ─────────────────────────────────────────────────────────

def _merge_records(base: dict, extra: dict):
    """Мёрж records: непустые значения extra поверх base (in-place)."""
    for lid, rec in extra.items():
        slot = base.setdefault(lid, {})
        for k, v in rec.items():
            if v not in (None, "", []):
                slot[k] = v


def step_a_listing(target: str, save_as: str, har_path: str | None,
                   verbose: bool = True) -> dict:
    """Находит страницу, собирает ad-records листинга активных.
    Источники по нарастанию свежести: кэш link_map.json (накопленный прошлыми
    прогонами/HAR'ами) → живой листинг → HAR. Итог пишется обратно в кэш."""
    log_step("ШАГ A: листинг активных объявлений", emoji="📜")
    cache_path = Path("scans") / save_as / "fb_deep" / "link_map.json"
    records: dict = {}
    if cache_path.exists():
        try:
            _merge_records(records, json.loads(cache_path.read_text(encoding="utf-8")))
            log_info(f"кэш листинга: {len(records)} records из {cache_path}")
        except Exception as e:
            log_warn(f"кэш листинга не читается: {e}")

    pages = find_brand_pages(target, verbose=verbose, find_delegate=False)
    alive = [p for p in pages if p.get("alive")]
    if not alive:
        log_error("нет живых FB страниц — стоп")
        return {"records": records}
    page0 = alive[0]
    log_success(f"страница: @{page0['handle']} → '{page0['display_name']}'")
    url = page0["ads_library_urls"]["active"]
    res = collect_active_ad_records(url, verbose=verbose)
    _merge_records(records, res["records"])
    if har_path:
        _merge_records(records, parse_har_ad_records(har_path))
    log_info(f"итого records (кэш+листинг+HAR): {len(records)}")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(records, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        log_debug(f"step_a_listing: кэш обновлён → {cache_path}")
    except Exception as e:
        log_warn(f"кэш листинга не записался: {e}")
    return {"page": page0, "records": records, "reached_footer": res.get("reached_footer")}


# ─── Шаг B: deep-scan ───────────────────────────────────────────────────────

def step_b_deepscan(lib_ids: list, save_as: str, top_n: int, verbose: bool = True) -> dict:
    """Deep-scan модалок: сайдкары <id>_graphql.json + parsed <id>.json.
    Идемпотентно: готовые пропускаются. Паузы/бэкофф — уроки прогона 2026-07-17
    (rate-limit прилетел на ~145-м объявлении без пауз; с паузами 0 ошибок)."""
    from playwright.sync_api import sync_playwright
    deep_dir = Path("scans") / save_as / "fb_deep" / "active"
    todo = [l for l in lib_ids
            if not ((deep_dir / f"{l}.json").exists()
                    and (deep_dir / f"{l}_graphql.json").exists())]
    if top_n:
        todo = todo[:top_n]
    log_step(f"ШАГ B: deep-scan {len(todo)} объявлений "
             f"(пропущено готовых: {len(lib_ids) - len(todo)})", emoji="🖱")
    if not todo:
        return {"scanned": 0, "errors": []}

    stats = {"scanned": 0, "errors": []}
    with sync_playwright() as pw:
        def make_browser():
            b = pw.chromium.launch(headless=True)
            ctx = b.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US", viewport={"width": 1400, "height": 900})
            return b, ctx.new_page()

        browser, page = make_browser()
        consec_fail = 0
        for i, lid in enumerate(todo, 1):
            try:
                res = None
                for attempt in (1, 2):
                    res = deep_scan_one_ad(page, lid, "active", save_as, verbose=False)
                    if res.get("success"):
                        break
                    log_warn(f"[{i}/{len(todo)}] {lid} попытка {attempt} упала: "
                             f"{str(res.get('error'))[:60]}")
                    time.sleep(PACE_FAIL_S)
                if not res.get("success"):
                    raise RuntimeError(res.get("error", "deep_scan_failed"))
                stats["scanned"] += 1
                consec_fail = 0
                if verbose and i % 10 == 0:
                    log_info(f"[{i}/{len(todo)}] ok")
                time.sleep(PACE_OK_S)
            except Exception as e:
                consec_fail += 1
                stats["errors"].append(f"{lid}: {str(e)[:100]}")
                time.sleep(PACE_FAIL_S)
                if consec_fail >= 5:
                    log_warn(f"серия отказов — перезапуск браузера + остывание {COOLDOWN_S}с")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    time.sleep(COOLDOWN_S)
                    browser, page = make_browser()
                    consec_fail = 0
            if i % RESTART_EVERY == 0:
                try:
                    browser.close()
                except Exception:
                    pass
                browser, page = make_browser()
        browser.close()
    log_success(f"deep-scan: {stats['scanned']} ok, {len(stats['errors'])} ошибок")
    return stats


# ─── Шаг C: расчёты ─────────────────────────────────────────────────────────

def _load_sidecar(deep_dir: Path, lid: str) -> dict | None:
    p = deep_dir / f"{lid}_graphql.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["data"]["ad_library_main"]["ad_details"]
    except Exception as e:
        log_debug(f"_load_sidecar: {lid} не читается: {e}")
        return None


def _start_date(rec: dict) -> date | None:
    su = (rec or {}).get("start_date_unix")
    if su:
        try:
            return datetime.fromtimestamp(int(su)).date()
        except Exception:
            pass
    return None


def _vertical(link_url: str | None) -> str:
    if not link_url:
        return ""
    parts = [x for x in urlparse(link_url).path.split("/") if x]
    return "/".join(parts[:2]) if len(parts) >= 2 else ""


def step_c_compute(save_as: str, records: dict, zombie_days: int,
                   zombie_reach: int) -> dict:
    """Per-ad строки + агрегаты из сайдкаров + листинг-records. Только наблюдённое;
    отсутствие данных — 'no_data', покрытие считается и репортится."""
    log_step("ШАГ C: расчёты (зомби, демография, агрегаты)", emoji="🧮")
    deep_dir = Path("scans") / save_as / "fb_deep" / "active"
    lib_ids = sorted(p.name.replace("_graphql.json", "")
                     for p in deep_dir.glob("*_graphql.json"))
    today = date.today()

    rows = []
    gender_tot = Counter()
    age_by_gender = {"male": Counter(), "female": Counter(), "unknown": Counter()}
    country_agg = defaultdict(lambda: [0, 0, 0])   # (страна, возраст) → [м, ж, неизв]
    agg_v = {}
    cover = Counter()
    zombies = 0

    for lid in lib_ids:
        det = _load_sidecar(deep_dir, lid)
        if det is None:
            continue
        tbl = det.get("transparency_by_location") or {}
        eu = tbl.get("eu_transparency") or {}
        uk = tbl.get("uk_transparency") or {}
        pb = ((det.get("aaa_info") or {}).get("payer_beneficiary_data") or [{}])[0]
        rec = records.get(lid) or {}

        m = f = u = 0
        ages, countries = Counter(), Counter()
        for c in (eu.get("age_country_gender_reach_breakdown") or []):
            for b in (c.get("age_gender_breakdowns") or []):
                mm, ff, uu = b.get("male") or 0, b.get("female") or 0, b.get("unknown") or 0
                m += mm; f += ff; u += uu
                ar = b.get("age_range") or "Unknown"
                ages[ar] += mm + ff + uu
                countries[c.get("country")] += mm + ff + uu
                age_by_gender["male"][ar] += mm
                age_by_gender["female"][ar] += ff
                age_by_gender["unknown"][ar] += uu
                key = (c.get("country"), ar)
                country_agg[key][0] += mm
                country_agg[key][1] += ff
                country_agg[key][2] += uu
        gender_tot["male"] += m; gender_tot["female"] += f; gender_tot["unknown"] += u
        tot = m + f + u

        eu_reach = eu.get("eu_total_reach")
        uk_reach = uk.get("total_reach")
        sd = _start_date(rec)
        age_days = (today - sd).days if sd else None
        zombie = bool(sd and age_days >= zombie_days
                      and ((eu_reach or 0) + (uk_reach or 0)) < zombie_reach)
        zombies += 1 if zombie else 0

        pp = rec.get("publisher_platform") or []
        pps = "+".join(PLACEMENT_LABEL.get(p, p) for p in pp) if pp else "no_data"
        fmt = rec.get("display_format") or "no_data"
        vert = _vertical(rec.get("link_url"))
        te = (det.get("aaa_info") or {}).get("targets_eu")
        cover["placements"] += 1 if pp else 0
        cover["format"] += 1 if fmt != "no_data" else 0
        cover["start_date"] += 1 if sd else 0
        cover["impressions"] += 1 if rec.get("impressions_text") else 0

        rows.append({
            "Library ID": lid,
            "Ссылка": f"https://www.facebook.com/ads/library/?id={lid}",
            "Старт": str(sd) if sd else "",
            "EU Reach": eu_reach if eu_reach is not None else "",
            "UK Reach": uk_reach if uk_reach is not None else "",
            "%Муж": round(100 * m / tot, 1) if tot else "",
            "Топ возраст": ages.most_common(1)[0][0] if ages else "",
            "Топ страна": countries.most_common(1)[0][0] if countries else "",
            "targets_eu": "да" if te is True else "нет данных",
            f"Зомби (≥{zombie_days} дн, reach<{zombie_reach})": "ЗОМБИ" if zombie else "",
            "Impressions-бейдж": rec.get("impressions_text") or "",
            "Плейсменты": pps,
            "Формат": fmt,
            "Вертикаль": vert,
            "CTA": rec.get("cta_text") or "",
            "Плательщик": pb.get("payer") or "",
            "Бенефициар": pb.get("beneficiary") or "",
        })

        a = agg_v.setdefault(vert or "—", dict(n=0, eu=0, uk=0, z=0,
                                               fmt=Counter(), payer=Counter()))
        a["n"] += 1
        a["eu"] += eu_reach or 0
        a["uk"] += uk_reach or 0
        a["z"] += 1 if zombie else 0
        a["fmt"][fmt] += 1
        a["payer"][pb.get("payer") or "—"] += 1

    n = len(rows)
    log_success(f"объявлений в расчёте: {n}, зомби: {zombies}")
    return {"rows": rows, "agg_v": agg_v, "gender_tot": gender_tot,
            "age_by_gender": age_by_gender, "country_agg": country_agg,
            "cover": cover, "n": n, "zombies": zombies,
            "zombie_rule": f"старт ≥{zombie_days} дн назад и EU+UK reach < {zombie_reach}"}


# ─── Шаг D: CSV-выходы ──────────────────────────────────────────────────────

def _wcsv(path: Path, rows: list):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerows(rows)
    log_success(f"записан {path}")


def step_d_outputs(save_as: str, calc: dict, listing_meta: dict):
    log_step("ШАГ D: CSV-выходы", emoji="💾")
    out_dir = Path("scans") / save_as
    out_dir.mkdir(parents=True, exist_ok=True)
    n = calc["n"]

    # 1. ads_v4.csv
    header = list(calc["rows"][0].keys()) if calc["rows"] else []
    _wcsv(out_dir / "ads_v4.csv",
          [header] + [[r[h] for h in header] for r in calc["rows"]])

    # 2. aggregates.csv — machine-readable блоки, все числа отдельными ячейками
    payer_names = sorted({p for a in calc["agg_v"].values() for p in a["payer"]} - {"—"})
    agg_rows = [["ВЕРТИКАЛИ"],
                ["Вертикаль", "Объявлений", "EU Reach", "UK Reach", "Зомби",
                 "IMAGE", "VIDEO", "no_data"] + payer_names + ["Без payer"]]
    for v, a in sorted(calc["agg_v"].items(), key=lambda x: -x[1]["eu"]):
        agg_rows.append([v, a["n"], a["eu"], a["uk"], a["z"],
                         a["fmt"].get("IMAGE", 0), a["fmt"].get("VIDEO", 0),
                         a["fmt"].get("no_data", 0)]
                        + [a["payer"].get(p, 0) for p in payer_names]
                        + [a["payer"].get("—", 0)])
    gt = calc["gender_tot"]; T = sum(gt.values()) or 1
    agg_rows += [[], ["ПОЛ (EU REACH)"], ["Пол", "Reach", "Процент"],
                 ["Мужчины", gt["male"], round(100 * gt["male"] / T, 1)],
                 ["Женщины", gt["female"], round(100 * gt["female"] / T, 1)],
                 ["Неизвестно", gt["unknown"], round(100 * gt["unknown"] / T, 1)],
                 [], ["ВОЗРАСТ × ПОЛ (EU REACH)"], ["Возраст", "Мужчины", "Женщины"]]
    for a in AGE_ORDER:
        agg_rows.append([a, calc["age_by_gender"]["male"].get(a, 0),
                         calc["age_by_gender"]["female"].get(a, 0)])
    cv = calc["cover"]
    agg_rows += [[], ["ПОКРЫТИЕ"], ["Метрика", "Доступно", "Всего", "Процент"],
                 ["Плейсменты", cv["placements"], n, round(100 * cv["placements"] / n, 1) if n else 0],
                 ["Формат", cv["format"], n, round(100 * cv["format"] / n, 1) if n else 0],
                 ["Дата старта", cv["start_date"], n, round(100 * cv["start_date"] / n, 1) if n else 0],
                 ["Impressions-бейдж", cv["impressions"], n, round(100 * cv["impressions"] / n, 1) if n else 0],
                 [], ["ЗОМБИ"], ["Метрика", "Значение"],
                 [f"Зомби всего ({calc['zombie_rule']})", calc["zombies"]],
                 [], ["ПРИМЕЧАНИЯ"],
                 ["Недоступность данных листинга: карточки-группы (collation) едут одной записью-представителем; члены групп в пагинации не появляются"],
                 [f"Футер System status при скролле {'достигнут' if listing_meta.get('reached_footer') else 'НЕ достигнут'}"],
                 [f"Срез: {date.today().isoformat()}; сортировка ленты Ads Library — по total_impressions по убыванию"]]
    _wcsv(out_dir / "aggregates.csv", agg_rows)

    # 3. demography_by_country.csv
    demo_rows = [["Страна", "Возраст", "Мужчины", "Женщины", "Неизвестно", "Всего"]]
    tot_by_c = Counter()
    for (c, a), v in calc["country_agg"].items():
        tot_by_c[c] += sum(v)
    order = {a: i for i, a in enumerate(AGE_ORDER)}
    for (c, a), v in sorted(calc["country_agg"].items(),
                            key=lambda x: (-tot_by_c[x[0][0]], x[0][0],
                                           order.get(x[0][1], 9))):
        demo_rows.append([c, a, v[0], v[1], v[2], sum(v)])
    _wcsv(out_dir / "demography_by_country.csv", demo_rows)


# ─── Main ───────────────────────────────────────────────────────────────────

def run(target: str, save_as: str | None = None, top_n: int = 0,
        har_path: str | None = None, skip_deep: bool = False,
        zombie_days: int = 7, zombie_reach: int = 100) -> dict:
    save_as = save_as or target
    log_header(f"FB AUDIENCE REPORT — {target} → scans/{save_as}/")

    listing = step_a_listing(target, save_as, har_path)
    records = listing.get("records") or {}
    if not records and not skip_deep:
        log_warn("листинг пуст (rate-limit или нет объявлений) — "
                 "работаю по имеющимся сайдкарам")

    if not skip_deep:
        ids = list(records.keys())
        step_b_deepscan(ids, save_as, top_n)
    else:
        log_info("deep-scan пропущен (--skip-deep)")

    calc = step_c_compute(save_as, records, zombie_days, zombie_reach)
    if not calc["rows"]:
        log_error("нет сайдкаров для расчёта — нечего писать")
        return calc
    step_d_outputs(save_as, calc, listing)
    log_header("ГОТОВО")
    log_info(f"ads_v4.csv: {calc['n']} строк | зомби: {calc['zombies']} | "
             f"плейсменты: {calc['cover']['placements']}/{calc['n']}")
    return calc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="domain или FB handle (например client-a.example | client-a)")
    ap.add_argument("--save-as", default=None,
                    help="папка в scans/ (дефолт = target; для handle укажи домен)")
    ap.add_argument("--top", type=int, default=0, help="deep-scan только первых N (0 = все)")
    ap.add_argument("--har", default=None, help="путь к HAR-файлу браузера (доп. источник)")
    ap.add_argument("--skip-deep", action="store_true",
                    help="без deep-scan — расчёты по имеющимся сайдкарам")
    ap.add_argument("--zombie-days", type=int, default=7)
    ap.add_argument("--zombie-reach", type=int, default=100)
    a = ap.parse_args()
    run(a.target, save_as=a.save_as, top_n=a.top, har_path=a.har,
        skip_deep=a.skip_deep, zombie_days=a.zombie_days, zombie_reach=a.zombie_reach)
