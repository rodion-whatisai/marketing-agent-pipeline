# -*- coding: utf-8 -*-
"""Прикидка затрат клиента на Meta-рекламу по EU+UK охвату.

Вход — готовый репорт движка: scans/<домен>/<домен> — Ads Library Intelligence v2.html
(контракт входов, см. README). Ничего больше не читаем.

Формула (см. README «Формула прикидки затрат»):
    охват (EU + UK одним числом) x частота = показы
    показы / 1000 x CPM = затраты
Частота и CPM — из бенчмарк-таблицы в Drive (пока константами ниже,
чтение таблицы через Drive добавим позже).

Запуск:  python spend_estimate.py client-a.example
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ENGINE_SCANS = Path(__file__).resolve().parent.parent / "engine" / "scans"

# ── бенчмарки (источник: «TNC Pitch-Adviser — CPM бенчмарки v2», Drive) ──
FREQ_THRESHOLD = 50_000          # порог «небольшой / большой охват», людей
FREQ_SMALL = (2.0, 2.5)          # частота при небольшом охвате
FREQ_BIG = (1.5, 1.8)            # чем больше охват, тем ниже частота
BENCH_SRC = "CPM бенчмарки v2 (Drive), строки: частота + профиль вертикали"


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


def estimate(ads: list[dict], cpm_low: float, cpm_high: float) -> dict:
    small = [a["reach"] for a in ads if 0 < a["reach"] < FREQ_THRESHOLD]
    big = [a["reach"] for a in ads if a["reach"] >= FREQ_THRESHOLD]
    imp_low = sum(small) * FREQ_SMALL[0] + sum(big) * FREQ_BIG[0]
    imp_high = sum(small) * FREQ_SMALL[1] + sum(big) * FREQ_BIG[1]
    return {
        "n_ads": len(ads),
        "n_zero": sum(1 for a in ads if a["reach"] == 0),
        "eu": sum(a["eu"] for a in ads),
        "uk": sum(a["uk"] for a in ads),
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
    if len(sys.argv) < 2:
        raise SystemExit("Использование: python spend_estimate.py <домен> [cpm_from] [cpm_to]")
    domain = sys.argv[1]
    cpm_low = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    cpm_high = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

    report = ENGINE_SCANS / domain / f"{domain} — Ads Library Intelligence v2.html"
    print(f"Читаю репорт: {report.name}")
    if not report.exists():
        raise SystemExit(f"Не найден репорт: {report}")

    ads = parse_ads(report.read_text(encoding="utf-8"))
    print(f"Разобрано объявлений: {len(ads)}")
    r = estimate(ads, cpm_low, cpm_high)

    top = sorted((a["reach"] for a in ads), reverse=True)
    share10 = sum(top[:10]) / r["reach"] * 100 if r["reach"] else 0
    fmt_img = sum(1 for a in ads if a["fmt"] == "IMAGE")
    fmt_vid = sum(1 for a in ads if a["fmt"] == "VIDEO")
    zombies = sum(1 for a in ads if a["zombie"])

    print(f"\n── {domain}: прикидка затрат по EU+UK ──")
    print(f"Охват: EU {sp(r['eu'])} + UK {sp(r['uk'])} = {sp(r['reach'])} человек")
    print(f"  небольшой охват (<{sp(FREQ_THRESHOLD)}): {r['n_small']} объявл., {sp(r['reach_small'])} — частота {FREQ_SMALL[0]}–{FREQ_SMALL[1]}")
    print(f"  большой охват (≥{sp(FREQ_THRESHOLD)}): {r['n_big']} объявл., {sp(r['reach_big'])} — частота {FREQ_BIG[0]}–{FREQ_BIG[1]}")
    if r["n_zero"]:
        print(f"  с нулевым охватом: {r['n_zero']} (в расчёт не входят)")
    print(f"Показы ≈ {sp(r['imp_low'])} – {sp(r['imp_high'])}")
    print(f"CPM ${cpm_low}–${cpm_high} → ЗАТРАТЫ ≈ ${sp(r['cost_low'])} – ${sp(r['cost_high'])}")
    print(f"\nКонтекст: топ-10 объявлений держат {share10:.0f}% охвата · "
          f"зомби {zombies} из {len(ads)} · IMAGE {fmt_img} / VIDEO {fmt_vid}")
    print(f"Допущения: частота и CPM — {BENCH_SRC}. Только EU+UK (Meta раскрывает "
          f"охват лишь по ним), worldwide не экстраполируем. Все цифры — оценка.")


if __name__ == "__main__":
    main()
