# -*- coding: utf-8 -*-
"""Чтение CPM-бенчмарков из Google-таблицы Родиона.

Единственная точка доступа к бенчмаркам: живая таблица в Drive
(сервисный аккаунт, ключ .secrets/gsheets.json — настройка в
benchmarks/SETUP-drive-access.md). Если доступа нет — работаем с
последней удачной выгрузки benchmarks/cpm-cache.csv и предупреждаем.

Кто пользуется: spend_estimate.py и (позже) остальные расчёты адвайзера.
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_FILE = ROOT / ".secrets" / "gsheets.json"
CACHE_FILE = ROOT / "benchmarks" / "cpm-cache.csv"
SHEET_ID = "16k73ARvb1zHV-4vMTm5iulz0EsBdXJJo1Ruo-bQMuHM"  # «TNC Pitch-Adviser — CPM бенчмарки v2»
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# служебные строки таблицы (не профили CPM) — узнаём по колонке «Параметр»
UTIL_SMALL_FREQ = "Частота при небольшом охвате"
UTIL_BIG_FREQ = "Частота при большом охвате"
UTIL_THRESHOLD = "Порог небольшой/большой охват"


def _num(s):
    """'2.5' / '50 000' / '' → float | None"""
    if s is None:
        return None
    s = str(s).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_live() -> list[list[str]]:
    """Тянет строки из живой таблицы. Бросает исключение, если не вышло."""
    import gspread
    from google.oauth2.service_account import Credentials

    if not KEY_FILE.exists():
        raise FileNotFoundError(
            f"нет ключа сервисного аккаунта: {KEY_FILE} "
            f"(настройка — benchmarks/SETUP-drive-access.md)"
        )
    creds = Credentials.from_service_account_file(str(KEY_FILE), scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1.get_all_values()


def _save_cache(rows: list[list[str]]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    with CACHE_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([f"# выгружено из живой таблицы {stamp}"])
        w.writerows(rows)


def _load_cache() -> tuple[list[list[str]], str]:
    if not CACHE_FILE.exists():
        raise FileNotFoundError(f"нет и кэша бенчмарков: {CACHE_FILE}")
    with CACHE_FILE.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    stamp = ""
    if rows and rows[0] and rows[0][0].startswith("#"):
        stamp = rows[0][0].lstrip("# ").replace("выгружено из живой таблицы ", "")
        rows = rows[1:]
    return rows, stamp


def load_benchmarks(quiet: bool = False) -> dict:
    """Читает бенчмарки: живая таблица → при сбое кэш.

    Возвращает {"profiles": [...], "freq_small": (lo, hi), "freq_big": (lo, hi),
                "threshold": float, "source": str}
    """
    try:
        rows = _fetch_live()
        _save_cache(rows)
        source = "живая таблица (Drive)"
    except Exception as e:  # нет ключа / нет сети / нет доступа
        rows, stamp = _load_cache()
        source = f"КЭШ от {stamp or 'неизвестной даты'} — живую таблицу прочитать не вышло: {e}"

    if not quiet:
        print(f"Бенчмарки: {source}")

    head = [h.strip() for h in rows[0]]
    idx = {name: i for i, name in enumerate(head)}

    def cell(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    profiles, freq_small, freq_big, threshold = [], None, None, None
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        param = cell(row, "Параметр")
        lo, hi = _num(cell(row, "CPM from")), _num(cell(row, "CPM to"))
        if param == "CPM":
            profiles.append({
                "objective": cell(row, "Campaign Objective"),
                "targeting": cell(row, "Ориентировочный таргетинг"),
                "geo": cell(row, "Geo"),
                "gender": cell(row, "Gender"),
                "age_from": _num(cell(row, "Age from")),
                "age_to": _num(cell(row, "Age to")),
                "placements": cell(row, "Placements"),
                "cpm_from": lo, "cpm_to": hi,
                "comment": cell(row, "Комментарий"),
            })
        elif param.startswith(UTIL_SMALL_FREQ):
            freq_small = (lo, hi)
        elif param.startswith(UTIL_BIG_FREQ):
            freq_big = (lo, hi)
        elif param.startswith(UTIL_THRESHOLD):
            threshold = lo

    missing = [n for n, v in [("частота малого охвата", freq_small),
                              ("частота большого охвата", freq_big),
                              ("порог охвата", threshold)] if not v]
    if missing:
        raise SystemExit("В таблице не нашлись служебные строки: " + ", ".join(missing))

    return {"profiles": profiles, "freq_small": freq_small,
            "freq_big": freq_big, "threshold": threshold, "source": source}


def pick_cpm(bench: dict, objective: str, vertical_hint: str = "",
             geo: str = "WW-чистый", gender: str = "All",
             placements: str = "All", age: tuple = (18, 65)) -> dict:
    """Выбирает строку таблицы под профиль клиента.

    Порядок: точное совпадение по всем признакам → та же вертикаль с
    дефолтным соцдемом → строка «БЕЗ таргетинга» того же objective.
    Возвращает саму строку-профиль (в ней есть comment — провенанс).
    """
    def matches(p, targeting_filter):
        return (p["objective"].startswith(objective)
                and targeting_filter(p["targeting"])
                and p["geo"] == geo
                and p["gender"] == gender
                and p["placements"] == placements
                and (p["age_from"], p["age_to"]) == age)

    hint = (vertical_hint or "").lower()
    if hint:
        exact = [p for p in bench["profiles"]
                 if matches(p, lambda t: hint in t.lower())]
        if exact:
            return exact[0]

    broad = [p for p in bench["profiles"]
             if matches(p, lambda t: "БЕЗ таргетинга" in t)]
    if broad:
        return broad[0]

    raise SystemExit(
        f"В таблице нет подходящей строки: objective={objective}, "
        f"вертикаль={vertical_hint!r}, geo={geo}, gender={gender}, "
        f"placements={placements}, age={age}"
    )


def list_verticals(bench: dict) -> list[str]:
    seen = []
    for p in bench["profiles"]:
        if p["targeting"] not in seen:
            seen.append(p["targeting"])
    return seen


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    b = load_benchmarks()
    print(f"Профилей: {len(b['profiles'])} | частота малая {b['freq_small']}, "
          f"большая {b['freq_big']}, порог {b['threshold']:.0f}")
    print("Таргетинги в таблице:")
    for v in list_verticals(b):
        print("  ·", v)
