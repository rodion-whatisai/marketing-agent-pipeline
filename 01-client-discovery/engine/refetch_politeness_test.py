"""
Тест правила WAF / вежливой дочитки (утв. Родионом 2026-07-20)
=============================================================
Правило: если главная сайта открывается (200), считаем что WAF на сайте НЕТ.
403/429 на внутренних страницах — это НЕ WAF сайта, а наш способ захода:
  429 = мы частим  →  пауза + один повтор
  403 = бот-детект на requests  →  повтор через Playwright (ходит как Chrome)

Тест берёт домены, у которых главная = 200, находит внутреннюю страницу
(About/Company/Contacts из карты сайта step1) и дочитывает её ВЕЖЛИВО:
та же сессия, тот же UA, пауза между запросами. Ждём, что внутренняя тоже 200 —
т.е. параллельные агенты без пауз (allbirds 429) не воспроизводятся при темпе.

Запуск:  python refetch_politeness_test.py
"""
import json
import re
import sys
import time

import requests

from utils import setup_console, HEADERS, scan_path
from log import log_info, log_success, log_warn, log_error, log_header, log_step

PAUSE_SEC = 2.5          # пауза между запросами одного домена
RETRY_429_WAIT = 6       # пауза перед повтором на 429
INNER_PATH_HINTS = ("about", "company", "our-story", "our-team", "mission",
                    "who-we-are", "what-we-do", "contact")

DOMAINS = ["allbirds.com", "miro.com", "acronis.com", "gymshark.com",
           "bombas.com", "client-a.example"]


def pick_inner_path(domain: str) -> str | None:
    """Внутренняя страница-самоописание из карты сайта step1 (About/Contacts...)."""
    p = scan_path(domain, f"{domain}_step1.json")
    if not p.exists():
        return None
    step1 = json.loads(p.read_text(encoding="utf-8"))
    candidates = []
    for rec in step1.get("classified", []):
        path = (rec.get("path") or "").lower()
        rtype = (rec.get("type") or "").lower()
        if rtype in ("about", "careers") or any(h in path for h in INNER_PATH_HINTS):
            # приоритет: about/company/our-story выше, contact ниже
            rank = 0 if any(h in path for h in INNER_PATH_HINTS[:-1]) else 1
            candidates.append((rank, rec.get("url") or path))
    candidates.sort()
    return candidates[0][1] if candidates else None


def fetch_requests(session: requests.Session, url: str) -> int:
    try:
        r = session.get(url, headers=HEADERS, timeout=12)
        return r.status_code
    except Exception as e:
        log_warn(f"requests fail: {str(e)[:60]}")
        return -1


def fetch_playwright(url: str) -> int:
    """Fallback как в движке — headless Chrome, тот же UA."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
            pg = ctx.new_page()
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=20000)
            code = resp.status if resp else -1
            b.close()
            return code
    except Exception as e:
        log_warn(f"playwright fail: {str(e)[:60]}")
        return -1


def check_domain(domain: str) -> dict:
    log_step(f"{domain}", emoji="🌐")
    session = requests.Session()
    base = f"https://{domain}"

    home = fetch_requests(session, base)
    log_info(f"главная: HTTP {home}")
    if home != 200:
        log_warn("главная не 200 — по правилу сайт нельзя протестировать (пропуск)")
        return {"domain": domain, "home": home, "verdict": "skip_home_not_200"}

    inner = pick_inner_path(domain)
    if not inner:
        log_warn("внутренней страницы-самоописания в карте нет")
        return {"domain": domain, "home": home, "inner": None, "verdict": "no_inner_page"}

    log_info(f"дочитка (пауза {PAUSE_SEC}с): {inner}")
    time.sleep(PAUSE_SEC)
    code = fetch_requests(session, inner)
    method = "requests"

    if code == 429:
        log_warn(f"429 — мы частим; жду {RETRY_429_WAIT}с и один повтор")
        time.sleep(RETRY_429_WAIT)
        code = fetch_requests(session, inner)
    if code == 403 or code == -1:
        log_warn(f"{code} на requests — повтор через Playwright (как Chrome)")
        time.sleep(PAUSE_SEC)
        code = fetch_playwright(inner)
        method = "playwright"

    ok = code == 200
    (log_success if ok else log_error)(
        f"внутренняя: HTTP {code} ({method}) — {'OK, правило подтверждено' if ok else 'НЕ открылась'}")
    return {"domain": domain, "home": home, "inner": inner,
            "inner_status": code, "method": method,
            "verdict": "polite_ok" if ok else "polite_failed"}


def main():
    log_header("Тест правила WAF / вежливой дочитки")
    rows = [check_domain(d) for d in DOMAINS]
    log_header("ИТОГ")
    ok = sum(1 for r in rows if r.get("verdict") == "polite_ok")
    testable = [r for r in rows if r.get("verdict") not in ("skip_home_not_200",)]
    for r in rows:
        print(f"  {r['domain']:<20} главная={r.get('home')} "
              f"внутр={r.get('inner_status','—')} ({r.get('method','—')}) → {r['verdict']}")
    log_info(f"Вежливо открылось: {ok} из {len(testable)} тестируемых "
             f"(правило: главная 200 ⇒ внутренняя тоже 200 при паузах)")


if __name__ == "__main__":
    setup_console()
    main()
