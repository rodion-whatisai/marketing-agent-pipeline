# -*- coding: utf-8 -*-
"""Чтение CPM-бенчмарков из базы фабрики (tnc-factory).

Источник истины — таблица `benchmarks` в Postgres фабрики
(решение D12 в FACTORY.md). Правится руками через Adminer:
http://localhost:8080 → сервер `db`, база `factory`, таблица `benchmarks`.

Если база не поднята — работаем с последней выгрузки
benchmarks/cpm-cache.csv и предупреждаем, какого она числа.

Кто пользуется: spend_estimate.py и (позже) остальные расчёты адвайзера.
"""
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "benchmarks" / "cpm-cache.csv"
FACTORY_ENV = Path(r"C:\Users\user\tnc-factory\.env")

# служебные строки таблицы (не профили CPM) — узнаём по колонке param
UTIL_SMALL_FREQ = "Частота при небольшом охвате"
UTIL_BIG_FREQ = "Частота при большом охвате"
UTIL_THRESHOLD = "Порог небольшой/большой охват"


def _db_url() -> str:
    """Строка подключения: из переменной окружения или из .env фабрики."""
    if os.environ.get("FACTORY_DB_URL"):
        return os.environ["FACTORY_DB_URL"]
    if not FACTORY_ENV.exists():
        raise FileNotFoundError(f"нет файла настроек фабрики: {FACTORY_ENV}")
    env = {}
    for line in FACTORY_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
            f"@localhost:5432/{env['POSTGRES_DB']}")


def _num(s):
    """'2.5' / '50 000' / '' / None → float | None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_db() -> list[dict]:
    """Тянет строки из таблицы benchmarks. Бросает исключение, если не вышло."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_db_url(), row_factory=dict_row, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT param, objective, targeting, geo, gender, age_from, age_to,
                       placements, formats, value_from, value_to, currency,
                       comment, provenance, status
                FROM benchmarks ORDER BY id
            """)
            return cur.fetchall()


def _save_cache(rows: list[dict]) -> None:
    """Дублирует прочитанное из базы в CSV — страховка на случай, что база не поднята."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    head = ["Параметр", "Campaign Objective", "Ориентировочный таргетинг", "Geo",
            "Gender", "Age from", "Age to", "Placements", "Formats",
            "CPM from", "CPM to", "Currency", "Комментарий", "Обновлено"]
    keys = ["param", "objective", "targeting", "geo", "gender", "age_from", "age_to",
            "placements", "formats", "value_from", "value_to", "currency", "comment"]
    with CACHE_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([f"# выгружено из базы фабрики {stamp}"])
        w.writerow(head)
        for r in rows:
            w.writerow([r.get(k) if r.get(k) is not None else "" for k in keys]
                       + [stamp[:10]])


def _load_cache() -> tuple[list[dict], str]:
    if not CACHE_FILE.exists():
        raise FileNotFoundError(f"нет и кэша бенчмарков: {CACHE_FILE}")
    with CACHE_FILE.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    stamp = ""
    if rows and rows[0] and rows[0][0].startswith("#"):
        stamp = (rows[0][0].lstrip("# ")
                 .replace("выгружено из живой таблицы ", "")
                 .replace("выгружено из базы фабрики ", ""))
        rows = rows[1:]
    head = [h.strip() for h in rows[0]]
    csv_to_db = {"Параметр": "param", "Campaign Objective": "objective",
                 "Ориентировочный таргетинг": "targeting", "Geo": "geo",
                 "Gender": "gender", "Age from": "age_from", "Age to": "age_to",
                 "Placements": "placements", "Formats": "formats",
                 "CPM from": "value_from", "CPM to": "value_to",
                 "Currency": "currency", "Комментарий": "comment"}
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        raw = {head[i]: (r[i].strip() if i < len(r) else "") for i in range(len(head))}
        out.append({db: raw.get(csv_col, "") for csv_col, db in csv_to_db.items()})
    return out, stamp


def load_benchmarks(quiet: bool = False) -> dict:
    """Читает бенчмарки: база фабрики → при сбое кэш.

    Возвращает {"profiles": [...], "freq_small": (lo, hi), "freq_big": (lo, hi),
                "threshold": float, "source": str}
    """
    try:
        rows = _fetch_db()
        _save_cache(rows)
        source = "база фабрики (таблица benchmarks)"
    except Exception as e:                       # база не поднята / нет доступа
        rows, stamp = _load_cache()
        source = (f"КЭШ от {stamp or 'неизвестной даты'} — базу фабрики прочитать "
                  f"не вышло: {e}")

    if not quiet:
        print(f"Бенчмарки: {source}")

    profiles, freq_small, freq_big, threshold = [], None, None, None
    for r in rows:
        param = (r.get("param") or "").strip()
        lo, hi = _num(r.get("value_from")), _num(r.get("value_to"))
        if param == "CPM":
            age_from, age_to = _num(r.get("age_from")), _num(r.get("age_to"))
            profiles.append({
                "objective": (r.get("objective") or "").strip(),
                "targeting": (r.get("targeting") or "").strip(),
                "geo": (r.get("geo") or "").strip(),
                "gender": (r.get("gender") or "").strip(),
                "age_from": age_from, "age_to": age_to,
                "placements": (r.get("placements") or "").strip(),
                "cpm_from": lo, "cpm_to": hi,
                "comment": (r.get("comment") or "").strip(),
                "status": (r.get("status") or "").strip(),
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
        raise SystemExit("В бенчмарках не нашлись служебные строки: " + ", ".join(missing))

    return {"profiles": profiles, "freq_small": freq_small,
            "freq_big": freq_big, "threshold": threshold, "source": source}


def pick_cpm(bench: dict, objective: str, vertical_hint: str = "",
             geo: str = "WW-чистый", gender: str = "All",
             placements: str = "All", age: tuple = (18, 65)) -> dict:
    """Выбирает строку бенчмарков под профиль клиента.

    Порядок: точное совпадение по вертикали → иначе строка «БЕЗ таргетинга»
    того же objective. Возвращает саму строку (в ней comment/status — провенанс).
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
        f"В бенчмарках нет подходящей строки: objective={objective}, "
        f"вертикаль={vertical_hint!r}, geo={geo}, gender={gender}, "
        f"placements={placements}, age={age}"
    )


def list_verticals(bench: dict) -> list[str]:
    seen = []
    for p in bench["profiles"]:
        if p["targeting"] not in seen:
            seen.append(p["targeting"])
    return seen


# Tested: 2026-07-20 on tnc-factory — читает 112 CPM-профилей + 3 служебные
# строки из базы; при остановленном контейнере переходит на кэш и предупреждает.
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    b = load_benchmarks()
    print(f"Профилей: {len(b['profiles'])} | частота малая {b['freq_small']}, "
          f"большая {b['freq_big']}, порог {b['threshold']:.0f}")
    print("Таргетинги:")
    for v in list_verticals(b):
        print("  ·", v)
