"""
meta_ads_insights_demo.py
=========================================================================
ДЕМО: как код "коннектится" к Meta и тянет отчёт по ad set'ам.

Данные подобраны под 5 кейсов A-E из стадии 03 (mock-adsets.csv / engine/meta_api.py):
    A - winner | B - bleeding | C - learning | D - attribution_trap | E - creative_fatigue
То есть цифры матчатся с витриной, а не рандом.

-------------------------------------------------------------------------
ЧТО ТАКОЕ "КОННЕКТ" (цепочка из 4 звеньев):

  Приложение (App)  ->  System User (робот)  ->  Токен (ключ)  ->  запрос с токеном
       паспорт              владелец ключа          пароль          GET к Ads Insights API
                                                                          |
                                                                          v
                                                    Meta проверяет токен -> отдаёт JSON

  "Коннект" = разовый HTTPS-запрос, внутри которого лежит токен.
-------------------------------------------------------------------------

Два режима запуска:
  python meta_ads_insights_demo.py --demo   # ОФЛАЙН: берёт sample_response.json,
                                            # разворачивает в CSV. Токен не нужен.
  python meta_ads_insights_demo.py          # БОЕВОЙ: реально дёргает Meta.
                                            # Нужны META_ACCESS_TOKEN + AD_ACCOUNT_ID.
=========================================================================
"""

import os
import sys
import csv
import json

# На Windows консоль cp1252 падает на кириллице/эмодзи -> переключаем на utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests  # pip install requests

# =========================================================================
# КОНФИГ
# =========================================================================
API_VERSION = "v25.0"                       # актуальна v25.0 (Meta, 18.02.2026)
AD_ACCOUNT_ID = "act_<AD_ACCOUNT_ID>"        # свой, формат act_1234567890 (не секрет)
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "PASTE_YOUR_SYSTEM_USER_TOKEN_HERE")

# Поля запроса. На уровне ad set. purchases/cpa/roas Meta отдаёт ВЛОЖЕННО,
# массивами actions / cost_per_action_type / purchase_roas -> разворачиваем ниже.
FIELDS = [
    "date_start", "date_stop", "adset_name",
    "impressions", "clicks", "spend", "ctr", "cpm",
    "actions",                # -> покупки (purchase)
    "cost_per_action_type",   # -> CPA (стоимость покупки)
    "purchase_roas",          # -> ROAS
]

PARAMS_BASE = {
    "level": "adset",          # уровень: account / campaign / adset / ad
    "date_preset": "last_7d",  # период (можно time_range={'since':..,'until':..})
    "time_increment": 1,       # 1 = строка на каждый день
    "limit": 100,
}

# Плоские колонки итогового CSV (то, что удобно SQLить).
CSV_COLUMNS = ["date_start", "adset", "case", "spend", "impressions",
               "clicks", "purchases", "ctr", "cpm", "cpa", "roas"]

OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "meta_ads_insights_demo.csv")
SAMPLE_JSON = os.path.join(os.path.dirname(__file__), "sample_response.json")


# =========================================================================
# Разворачивание вложенных полей Meta -> плоская строка CSV
# =========================================================================
def _action_value(items, action_types):
    """Из [{'action_type':'purchase','value':'30'}, ...] достать value по нужному типу."""
    for it in items or []:
        if it.get("action_type") in action_types:
            return it.get("value")
    return None


def flatten_row(row):
    """Сырая insights-строка Meta -> плоский dict под CSV_COLUMNS."""
    name = row.get("adset_name", "")
    letter, _, case = name.partition(" · ")     # "A · winner" -> "A", "winner"
    return {
        "date_start": row.get("date_start"),
        "adset": letter or name,
        "case": case,
        "spend": row.get("spend"),
        "impressions": row.get("impressions"),
        "clicks": row.get("clicks"),
        "purchases": _action_value(row.get("actions"),
                                   {"purchase", "omni_purchase",
                                    "offsite_conversion.fb_pixel_purchase"}),
        "ctr": row.get("ctr"),
        "cpm": row.get("cpm"),
        "cpa": _action_value(row.get("cost_per_action_type"),
                             {"purchase", "omni_purchase"}),
        "roas": _action_value(row.get("purchase_roas"),
                              {"omni_purchase", "purchase"}),
    }


def save_csv(raw_rows):
    """Развернуть и записать в CSV. Дальше можно SQLить (DuckDB/pandas)."""
    if not raw_rows:
        print("[csv] строк нет — файл не пишу.")
        return
    flat = [flatten_row(r) for r in raw_rows]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    print(f"[csv] записал {len(flat)} строк -> {OUTPUT_CSV}")


# =========================================================================
# БОЕВОЙ режим: реальные запросы к Meta
# =========================================================================
def test_connection():
    """Крошечный запрос 'как зовут аккаунт?' — жив ли токен (это и есть коннект)."""
    print(f"[коннект-тест] спрашиваю имя аккаунта {AD_ACCOUNT_ID} ...")
    url = f"https://graph.facebook.com/{API_VERSION}/{AD_ACCOUNT_ID}"
    params = {"fields": "name,account_status,currency", "access_token": ACCESS_TOKEN}
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"[коннект-тест] ОШИБКА {resp.status_code}: {resp.text}")
        return False
    d = resp.json()
    print(f"[коннект-тест] OK -> {d.get('name')} "
          f"(валюта {d.get('currency')}, статус {d.get('account_status')})")
    return True


def fetch_insights():
    """Тянем insights по ad set'ам, по дням, с пагинацией."""
    print(f"[insights] тяну отчёт по ad set'ам за {PARAMS_BASE['date_preset']} ...")
    url = f"https://graph.facebook.com/{API_VERSION}/{AD_ACCOUNT_ID}/insights"
    params = dict(PARAMS_BASE)
    params["fields"] = ",".join(FIELDS)
    params["access_token"] = ACCESS_TOKEN     # токен внутри запроса = коннект

    all_rows, page = [], 1
    while url:
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            print(f"[insights] ОШИБКА {resp.status_code}: {resp.text}")
            break
        payload = resp.json()
        rows = payload.get("data", [])
        all_rows.extend(rows)
        print(f"[insights] страница {page}: +{len(rows)} (всего {len(all_rows)})")
        url = payload.get("paging", {}).get("next")   # готовый URL след. страницы
        params = None
        page += 1
    print(f"[insights] готово. Всего строк: {len(all_rows)}")
    return all_rows


# =========================================================================
# MAIN
# =========================================================================
def run_demo():
    """ОФЛАЙН: показать весь путь ответ->CSV на готовом примере, без токена."""
    print(f"[demo] читаю {SAMPLE_JSON} (без обращения к Meta) ...")
    payload = json.load(open(SAMPLE_JSON, encoding="utf-8"))
    rows = payload.get("data", [])
    print(f"[demo] в примере строк: {len(rows)} (5 кейсов A-E x 5 дней)")
    save_csv(rows)


def run_live():
    if ACCESS_TOKEN == "PASTE_YOUR_SYSTEM_USER_TOKEN_HERE":
        print("ВНИМАНИЕ: реальный токен не подставлен. Поставь META_ACCESS_TOKEN +")
        print("впиши AD_ACCOUNT_ID. Или гоняй офлайн-демо: python ... --demo\n")
    if not test_connection():
        print("Коннект не прошёл — отчёт не тяну.")
        return
    rows = fetch_insights()
    for r in rows[:5]:
        print("  ", flatten_row(r))
    save_csv(rows)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        run_demo()
    else:
        run_live()
