"""
TNC Testbed — make_expected: куратор эталонов золотого корпуса
===============================================================
Читает свежий scans/<domain>/<domain>_step2.json, показывает по-русски
по одной странице, ждёт апрув (как learn.py для паттернов) и пишет
golden/expected_<domain>.json с провенансом (кто/когда/на каком коммите).

Запуск:
    python make_expected.py tinytronics.nl              # интерактив, y/n по страницам
    python make_expected.py tinytronics.nl --draft      # черновик без вопросов (verified_by=draft)
    python make_expected.py tinytronics.nl --update     # перезаверка существующего эталона
    python make_expected.py tinytronics.nl --step2 path # взять step2.json не из scans/

Правила формата — golden/README.md. Главное: эталон = ПРАВДА (что сканер
ОБЯЗАН видеть), а не текущий вывод. В интерактиве отвечай n там, где сканер
сейчас ошибается, и правь поле руками — это станет known fail в стенде.
"""

import sys
import json
import argparse
import datetime
import subprocess

from utils import setup_console
setup_console()  # UTF-8 до первого вывода — иначе cp1252-крэш на эмодзи статусов

from log import log_error, log_success, log_info, log_step, log_warn
import eval_lib as ev


# ─── Кандидат из скана ────────────────────────────────────────────────────────

def build_candidate(step2: dict, step1: dict) -> dict:
    """Собрать заготовку эталона из текущего вывода сканера (только стабильные поля)."""
    platform = None
    if step1:
        p = step1.get("platform")
        platform = p.get("platform") if isinstance(p, dict) else p

    site = {
        "gtm_platforms": sorted(ev.canonical_set(step2.get("gtm_platforms"))),
        "counters": {k: step2.get(k, 0)
                     for k in ("gaps", "oks", "no_ctas", "no_tracking", "unverified")},
    }
    # platform=None НЕ пишем: отсутствующее поле = не проверяется; null стал бы
    # вечным false FAIL после появления step1 (ревью 2026-07-13)
    if platform:
        site["platform"] = platform

    pages = {}
    for pg in step2.get("all_pages") or []:
        path = pg.get("path") or "/"
        pages[path] = {
            "status": ev.normalize_status(pg.get("status")),
            "page_type": pg.get("page_type"),
            "has_cta": pg.get("has_cta"),
            "platforms_detected": sorted(ev.page_platforms(pg)),
            "external_services": sorted(ev.page_external_services(pg)),
            "missing_events": sorted(pg.get("missing_events") or []),
        }
    return {"site": site, "pages": pages}


# поля-пины добавляются руками в JSON (build_candidate их не генерирует) —
# при --update они ОБЯЗАНЫ переноситься из прежнего эталона, иначе молча
# теряются защиты от известных false positives (ревью 2026-07-13)
PIN_FIELDS = ("platforms_forbidden", "external_services_forbidden", "conversion_events_min")


def merge_pins(existing: dict, candidate: dict) -> int:
    """Перенести ручные пины из прежнего эталона в кандидата. Возвращает счётчик."""
    carried = 0
    for path, old_pg in (existing.get("pages") or {}).items():
        new_pg = candidate["pages"].get(path)
        if not new_pg:
            continue
        for pin in PIN_FIELDS:
            if pin in old_pg:
                new_pg[pin] = old_pg[pin]
                carried += 1
    return carried


def scanner_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, cwd=ev.ENGINE_DIR)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ─── Интерактивная куратура ──────────────────────────────────────────────────

_STATUS_ALIASES = {
    "ok": "OK", "gap": "GAP", "nt": "NO_TRACKING", "no_tracking": "NO_TRACKING",
    "nc": "NO_CTA", "no_cta": "NO_CTA", "unv": "UNVERIFIED", "unverified": "UNVERIFIED",
    "r": "REDIRECTED", "redirected": "REDIRECTED", "404": "HTTP_ERROR", "http": "HTTP_ERROR",
}


def show_page(path: str, page: dict):
    print(f"\n  {path}")
    print(f"    тип: {page['page_type']}  |  статус: {page['status']}  |  CTA: {'есть' if page['has_cta'] else 'нет'}")
    print(f"    платформы: {', '.join(page['platforms_detected']) or '—'}")
    if page["external_services"]:
        print(f"    сервисы: {', '.join(page['external_services'])}")
    if page["missing_events"]:
        print(f"    отчёт называет отсутствующими: {', '.join(page['missing_events'])}")


def _ask(prompt: str, valid_simple: tuple, allow_status: bool = False):
    """input() с переспросом: нераспознанный ввод НЕ трактуется как апрув
    (ревью 2026-07-13 — опечатка молча выкидывала страницу из эталона).
    Возвращает ('y'|'n'|'q', None) или ('s', STATUS) при allow_status."""
    while True:
        choice = input(prompt).strip().lower()
        if choice in ("q", "quit", "exit"):
            return "q", None
        if choice in ("y", "yes", ""):
            return "y", None
        if choice in ("n", "no"):
            return "n", None
        if allow_status and choice.replace(" ", "").startswith("s="):
            raw = choice.replace(" ", "")[2:]
            status = _STATUS_ALIASES.get(raw, raw.upper())
            if status in ev.KNOWN_STATUSES:
                return "s", status
            log_warn(f"Неизвестный статус {raw!r}. Варианты: ok/gap/nt/nc/unv/r/404")
            continue
        hint = "y/n/s=СТАТУС/q" if allow_status else "y/n/q"
        print(f"    не понял {choice!r} — жду {hint}")


def curate_interactive(candidate: dict, domain: str, existing: dict = None) -> dict:
    """y/n/правка по каждой странице. Возвращает финальные pages
    (страницы с ответом n выкидываются из эталона = не проверяются).
    existing — прежний эталон при --update: показываем старый статус, если разошёлся."""
    old_pages = (existing or {}).get("pages") or {}
    print(f"\n{'═' * 65}")
    print(f"  ЭТАЛОН — {domain}: подтверди что сканер видит ПРАВДУ")
    print(f"  y (или enter) = верно как есть   n = не проверять эту страницу")
    print(f"  s=СТАТУС = статус на самом деле другой (ok/gap/nt/nc/unv/r/404)")
    print(f"  q = выйти без сохранения")
    print(f"{'═' * 65}")

    print("\n  САЙТ ЦЕЛИКОМ:")
    print(f"    платформа: {candidate['site'].get('platform', '— (step1 не заморожен)')}")
    print(f"    GTM-платформы: {', '.join(candidate['site']['gtm_platforms']) or '—'}")
    print(f"    счётчики: {candidate['site']['counters']}")
    action, _ = _ask("\n  Сайт-блок верен? [y/n/q]: ", ("y", "n", "q"))
    if action == "q":
        return None
    if action == "n":
        log_warn("Сайт-блок выкинут из эталона — счётчики и GTM проверяться не будут")
        candidate["site"] = ({"platform": candidate["site"]["platform"]}
                             if "platform" in candidate["site"] else {})

    final_pages = {}
    pages = candidate["pages"]
    for i, (path, page) in enumerate(pages.items(), 1):
        show_page(path, page)
        old = old_pages.get(path) or {}
        if old.get("status") and old["status"] != page["status"]:
            print(f"    ⚠  в прежнем эталоне статус был: {old['status']} (скан сейчас говорит {page['status']})")
        action, status = _ask(f"  [{i}/{len(pages)}] Верно? [y/n/s=СТАТУС/q]: ",
                              ("y", "n", "q"), allow_status=True)
        if action == "q":
            return None
        if action == "n":
            print("    ⏭  страница не проверяется")
            continue
        if action == "s":
            page["status"] = status
            print(f"    ✏  статус исправлен на {status} — в стенде это будет known fail до фикса")
        final_pages[path] = page

    candidate["pages"] = final_pages
    return candidate


# ─── Main ────────────────────────────────────────────────────────────────────

def run(domain: str, draft: bool = False, update: bool = False, step2_path: str = None):
    step2_file = step2_path or (ev.ENGINE_DIR / "scans" / domain / f"{domain}_step2.json")
    log_step(f"Куратор эталона: {domain}", emoji="📐")

    try:
        step2 = ev.load_json(step2_file)
    except Exception as e:
        log_error(f"Не могу открыть step2: {step2_file} — {e}")
        sys.exit(1)

    step1 = None
    s1_path = ev.frozen_step1_path(domain)
    if s1_path.exists():
        step1 = ev.load_json(s1_path)
    else:
        log_warn(f"Замороженного step1 нет ({s1_path}) — site.platform не заполнится")

    out_path = ev.expected_path(domain)
    existing = None
    if out_path.exists():
        try:
            existing = ev.load_expected(domain)
        except Exception as e:
            log_warn(f"Существующий эталон не читается ({e}) — работаем как с нуля")
        if not (update or draft):
            log_error(f"Эталон уже есть: {out_path}. Перезаверка — флаг --update.")
            sys.exit(1)
        if draft and existing and existing.get("verified_by") != "draft":
            # черновик НЕ имеет права затирать человеко-заверенный эталон (ревью 2026-07-13)
            log_error(f"Эталон заверен человеком (verified_by={existing.get('verified_by')!r}) — "
                      f"--draft его не перезапишет. Перезаверка: --update (интерактив).")
            sys.exit(1)

    candidate = build_candidate(step2, step1)

    if existing and update:
        carried = merge_pins(existing, candidate)
        if carried:
            log_info(f"Перенесено {carried} ручных пинов из прежнего эталона (forbidden/conversion_min)")

    if draft:
        provenance = {
            "verified_by": "draft",
            "verified_against": "текущий вывод сканера — НЕ проверено человеком",
            "notes": "ЧЕРНОВИК: сгенерирован автоматически, ждёт апрува Rodion'ом "
                     "(make_expected.py --update).",
        }
    else:
        candidate = curate_interactive(candidate, domain, existing=existing)
        if candidate is None:
            log_warn("Выход без сохранения")
            sys.exit(0)
        old_notes = (existing or {}).get("notes", "")
        prompt = (f"\n  Заметки к эталону (enter = оставить прежние: {old_notes[:60]!r}): "
                  if old_notes else "\n  Заметки к эталону (enter = пусто): ")
        notes = input(prompt).strip() or old_notes
        provenance = {
            "verified_by": "rodion",
            "verified_against": "интерактивная куратура make_expected.py",
            "notes": notes,
        }

    expected = {
        "schema_version": 1,
        "domain": domain,
        **provenance,
        "verified_date": datetime.date.today().isoformat(),
        "scanner_commit": scanner_commit(),
        "site": candidate["site"],
        "pages": candidate["pages"],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(expected, f, ensure_ascii=False, indent=2)
    log_success(f"Эталон сохранён: {out_path} "
                f"({len(candidate['pages'])} страниц, verified_by={provenance['verified_by']})", emoji="💾")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Куратор эталонов золотого корпуса (см. golden/README.md)")
    parser.add_argument("domain", help="домен из корпуса, например tinytronics.nl")
    parser.add_argument("--draft", action="store_true",
                        help="черновик без вопросов (verified_by=draft, ждёт апрува)")
    parser.add_argument("--update", action="store_true",
                        help="перезаверка существующего эталона (после дрейфа/фикса)")
    parser.add_argument("--step2", default=None, help="путь к step2.json (дефолт: scans/<domain>/)")
    args = parser.parse_args()
    try:
        run(args.domain, draft=args.draft, update=args.update, step2_path=args.step2)
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C / закрытый stdin — выходим тихо, эталон не сохранён (ревью 2026-07-13)
        print()
        log_warn("Прервано — эталон не сохранён")
        sys.exit(1)
