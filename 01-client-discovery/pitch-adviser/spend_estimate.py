# -*- coding: utf-8 -*-
"""Прикидка затрат клиента на Meta-рекламу по EU+UK охвату.

Вход — готовый репорт движка: scans/<домен>/<домен> — Ads Library Intelligence v2.html
(контракт входов, см. README). Ничего больше не читаем.

Формула (см. README «Формула прикидки затрат»):
    охват (EU + UK одним числом) x частота = показы
    показы / 1000 x CPM = затраты
Частота, порог и CPM берутся из живой Google-таблицы Родиона через
benchmarks_loader (при недоступности — из кэша, с предупреждением).

Запуск:
    python spend_estimate.py client-a.example
    python spend_estimate.py client-a.example --vertical "Онлайн-образование" --objective Traffic
"""
import argparse
import re
import sys
from pathlib import Path

from benchmarks_loader import load_benchmarks, pick_cpm, list_verticals

sys.stdout.reconfigure(encoding="utf-8")

ENGINE_SCANS = Path(__file__).resolve().parent.parent / "engine" / "scans"


def parse_ads(html: str) -> list[dict]:
    """Таблица «Приложение: все отсмотренные объявления» → список объявлений."""
    i = html.find("Приложение: все отсмотренные")
    if i < 0:
        raise SystemExit("В репорте нет таблицы всех объявлений — проверь версию репорта.")
    m = re.search(r"<table.*?</table>", html[i:], re.S)
    if not m:
        raise SystemExit("Таблица объявлений не распозналась.")
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S)[1:]

    def num(s: str) -> int:
        digits = re.sub(r"[^\d]", "", s or "")
        return int(digits) if digits else 0

    ads = []
    for r in rows:
        c = [re.sub(r"<[^>]+>", "", x).strip()
             for x in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        if len(c) < 15:
            continue
        ads.append({
            "lib": c[0], "start": c[2],
            "reach": num(c[3]) + num(c[4]),      # EU + UK одним числом
            "eu": num(c[3]), "uk": num(c[4]),
            "zombie": "ЗОМБИ" in c[9],
            "fmt": c[12], "vert": c[13],
        })
    return ads


def estimate(ads: list[dict], bench: dict, cpm_low: float, cpm_high: float) -> dict:
    threshold = bench["threshold"]
    f_small, f_big = bench["freq_small"], bench["freq_big"]
    small = [a["reach"] for a in ads if 0 < a["reach"] < threshold]
    big = [a["reach"] for a in ads if a["reach"] >= threshold]
    imp_low = sum(small) * f_small[0] + sum(big) * f_big[0]
    imp_high = sum(small) * f_small[1] + sum(big) * f_big[1]
    return {
        "n_zero": sum(1 for a in ads if a["reach"] == 0),
        "eu": sum(a["eu"] for a in ads), "uk": sum(a["uk"] for a in ads),
        "reach": sum(a["reach"] for a in ads),
        "n_small": len(small), "reach_small": sum(small),
        "n_big": len(big), "reach_big": sum(big),
        "imp_low": imp_low, "imp_high": imp_high,
        "cost_low": imp_low / 1000 * cpm_low,
        "cost_high": imp_high / 1000 * cpm_high,
    }


def sp(n) -> str:
    """число с пробелами-разделителями"""
    return f"{n:,.0f}".replace(",", " ")


def main() -> None:
    ap = argparse.ArgumentParser(description="Прикидка затрат по EU+UK охвату")
    ap.add_argument("domain", help="домен, напр. client-a.example")
    ap.add_argument("--vertical", default="", help="таргетинг из бенчмарк-таблицы (часть названия)")
    ap.add_argument("--objective", default="Sales",
                    help="Sales (есть конверсионное событие) или Traffic (событий нет)")
    ap.add_argument("--geo", default="WW-чистый", help="строка Geo из таблицы")
    ap.add_argument("--list-verticals", action="store_true", help="показать таргетинги таблицы и выйти")
    args = ap.parse_args()

    bench = load_benchmarks()
    if args.list_verticals:
        for v in list_verticals(bench):
            print(" ·", v)
        return

    profile = pick_cpm(bench, objective=args.objective,
                       vertical_hint=args.vertical, geo=args.geo)
    cpm_low, cpm_high = profile["cpm_from"], profile["cpm_to"]

    report = ENGINE_SCANS / args.domain / f"{args.domain} — Ads Library Intelligence v2.html"
    if not report.exists():
        raise SystemExit(f"Не найден репорт: {report}")
    print(f"Читаю репорт: {report.name}")
    ads = parse_ads(report.read_text(encoding="utf-8"))
    print(f"Разобрано объявлений: {len(ads)}")

    r = estimate(ads, bench, cpm_low, cpm_high)
    top = sorted((a["reach"] for a in ads), reverse=True)
    share10 = sum(top[:10]) / r["reach"] * 100 if r["reach"] else 0
    fmt_img = sum(1 for a in ads if a["fmt"] == "IMAGE")
    fmt_vid = sum(1 for a in ads if a["fmt"] == "VIDEO")
    zombies = sum(1 for a in ads if a["zombie"])
    thr, f_small, f_big = bench["threshold"], bench["freq_small"], bench["freq_big"]

    print(f"\n── {args.domain}: прикидка затрат по EU+UK ──")
    print(f"Охват: EU {sp(r['eu'])} + UK {sp(r['uk'])} = {sp(r['reach'])} человек")
    print(f"  небольшой охват (<{sp(thr)}): {r['n_small']} объявл., {sp(r['reach_small'])} — частота {f_small[0]}–{f_small[1]}")
    print(f"  большой охват (≥{sp(thr)}): {r['n_big']} объявл., {sp(r['reach_big'])} — частота {f_big[0]}–{f_big[1]}")
    if r["n_zero"]:
        print(f"  с нулевым охватом: {r['n_zero']} (в расчёт не входят)")
    print(f"Показы ≈ {sp(r['imp_low'])} – {sp(r['imp_high'])}")
    print(f"CPM ${cpm_low}–${cpm_high} → ЗАТРАТЫ ≈ ${sp(r['cost_low'])} – ${sp(r['cost_high'])}")

    print(f"\nСтрока бенчмарков: {profile['objective']} · {profile['targeting']} · "
          f"{profile['geo']} · {profile['gender']} · {profile['placements']} · "
          f"{profile['age_from']:.0f}–{profile['age_to']:.0f}")
    if profile["comment"]:
        print(f"  комментарий строки: {profile['comment'][:120]}")
    print(f"Контекст: топ-10 объявлений держат {share10:.0f}% охвата · "
          f"зомби {zombies} из {len(ads)} · IMAGE {fmt_img} / VIDEO {fmt_vid}")
    print("Только EU+UK (Meta раскрывает охват лишь по ним), worldwide не "
          "экстраполируем. Все цифры — оценка.")


# Tested: 2026-07-20 on client-a.example — 268 объявлений, охват EU+UK 3 336 555,
# показы 5.12–6.17 млн, затраты $20 491–49 368 при CPM 4–8 из строки
# «Sales · Онлайн-образование / курсы · WW-чистый». Совпало с ручным прогоном.
if __name__ == "__main__":
    main()
