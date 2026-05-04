"""
Module 2: FB Ads Library Listing Scraper
=========================================
ВХОД:  ads_library_urls (dict с 3 status'ами) или (display_name, country)
ВЫХОД: dict с разбивкой по active/inactive — для каждого статуса:
        count, library_ids[] (top-N в порядке impressions DESC), ad_texts[]

3-проходная логика:
    Pass 1  active_status=all      → есть ли что-то вообще; если 0 → стоп
    Pass 2  active_status=active   → активные (count + library_ids + texts)
    Pass 3  active_status=inactive → архив (только если total > active)

Standalone:
    python fb_ads_listing.py aerosus.fr           # запускает сначала Step 1
    python fb_ads_listing.py aerosus.fr --top 5   # ограничить top-N
"""
import sys
import re
import json
import argparse

from utils import setup_console
setup_console()

DEFAULT_TOP_N = 10


# ─── Парсинг listing HTML ───────────────────────────────────────────────────

def _parse_count(html: str) -> int:
    """Извлекает количество объявлений на странице — несколько эвристик."""
    # 1. JSON в HTML: "search_results_connection":{"count":42}
    m = re.search(r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', html)
    if m:
        return int(m.group(1))
    # 2. Заголовок "~42 results"
    m = re.search(r"~?(\d[\d,\s]*)\s+results?", html, re.IGNORECASE)
    if m:
        try:
            n = int(re.sub(r"[,\s]", "", m.group(1)))
            if 0 < n < 1_000_000:
                return n
        except ValueError:
            pass
    # 3. Пустой результат
    if any(s in html.lower() for s in
           ['no ads match', 'no results', '"edges":[]', '"count":0']):
        return 0
    return None  # не смогли определить


def _parse_library_ids(html: str, max_n: int) -> list:
    """Library IDs из листинга в порядке появления (= impressions DESC).
    FB рендерит карточки сверху-вниз, поэтому первые N = top-N по impressions."""
    ids = re.findall(r"Library ID:\s*(\d{10,20})", html)
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
        if len(out) >= max_n:
            break
    return out


def _parse_ad_texts(html: str) -> list:
    """Уникальные тексты объявлений с listing."""
    texts = re.findall(r'white-space:\s*pre-wrap[^>]*><span>([^<]{10,600})', html)
    seen = set()
    out = []
    for t in texts:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_partnership(html: str) -> tuple:
    """Возвращает (is_partnership: bool, count: int)."""
    n = len(re.findall(r"branded_content", html, re.IGNORECASE))
    estimated = max(0, n // 3)  # ~3 упоминания на каждое branded_content объявление
    return (estimated > 0, estimated)


# ─── Скрейп одного status'а ─────────────────────────────────────────────────

def _scroll_to_load_more(page, target_count: int, max_scrolls: int = 6):
    """Скроллит вниз чтобы FB подгрузил больше карточек."""
    for _ in range(max_scrolls):
        cnt = len(re.findall(r"Library ID:\s*\d{10,}", page.content()))
        if cnt >= target_count:
            return
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1200)


def _scrape_status(page, url: str, status_label: str, top_n: int,
                    verbose: bool = True) -> dict:
    """Скрейпит один URL listing'а."""
    if verbose: print(f"      🔎 [{status_label}] open URL...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3500)
    except Exception as e:
        if verbose: print(f"      ⚠ goto failed: {str(e)[:80]}")
        return {"count": None, "library_ids": [], "ad_texts": [],
                "partnership_ads": False, "partnership_count": 0,
                "error": str(e)[:100]}

    # Если нужны top-N — скроллим чтобы подгрузить
    if top_n > 12:
        _scroll_to_load_more(page, target_count=top_n)

    html = page.content()
    count = _parse_count(html)
    library_ids = _parse_library_ids(html, max_n=top_n)
    ad_texts = _parse_ad_texts(html)
    is_partnership, partnership_n = _parse_partnership(html)

    return {
        "count":             count,
        "library_ids":       library_ids,
        "ad_texts":          ad_texts,
        "partnership_ads":   is_partnership,
        "partnership_count": partnership_n,
    }


# ─── Главная функция ────────────────────────────────────────────────────────

def scrape_ads_listing(ads_library_urls: dict, display_name: str = "",
                        top_n: int = DEFAULT_TOP_N,
                        verbose: bool = True) -> dict:
    """3-проходный скрейп listing'а Ad Library.
    ads_library_urls: dict с ключами 'all', 'active', 'inactive' (из Step 1).
    Возвращает {total_ever, active, inactive}."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "playwright not installed"}

    result = {
        "display_name": display_name,
        "total_ever":   None,
        "active":       None,
        "inactive":     None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

        # ── Pass 1: total ever ─────────────────────────────────────────
        pass1 = _scrape_status(page, ads_library_urls["all"], "all",
                                top_n=1, verbose=verbose)
        total = pass1.get("count")
        result["total_ever"] = total
        if verbose: print(f"      📊 total_ever = {total}")

        if not total or total == 0:
            if verbose: print(f"      ❌ Объявлений нет — стоп")
            browser.close()
            return result

        # ── Pass 2: active ─────────────────────────────────────────────
        pass2 = _scrape_status(page, ads_library_urls["active"], "active",
                                top_n=top_n, verbose=verbose)
        active_count = pass2.get("count", 0) or 0
        if active_count > 0:
            result["active"] = pass2
            if verbose:
                print(f"      ✅ active: count={active_count}, "
                      f"library_ids={len(pass2['library_ids'])}, "
                      f"texts={len(pass2['ad_texts'])}")
        else:
            if verbose: print(f"      ➖ active: 0")

        # ── Pass 3: inactive (только если есть смысл) ─────────────────
        if total > active_count:
            pass3 = _scrape_status(page, ads_library_urls["inactive"], "inactive",
                                    top_n=top_n, verbose=verbose)
            inactive_count = pass3.get("count", 0) or 0
            if inactive_count > 0:
                result["inactive"] = pass3
                if verbose:
                    print(f"      📦 inactive: count={inactive_count}, "
                          f"library_ids={len(pass3['library_ids'])}, "
                          f"texts={len(pass3['ad_texts'])}")
        else:
            if verbose: print(f"      ➖ inactive: пропуск (active == total)")

        browser.close()
    return result


# ─── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="domain (запустит Step 1) или display_name")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                    help=f"top-N library_ids на статус (default {DEFAULT_TOP_N})")
    ap.add_argument("--country", default="ALL",
                    help="country filter для Ad Library (default ALL)")
    ap.add_argument("--display-name",
                    help="прямо задать display_name (пропустить Step 1)")
    args = ap.parse_args()

    print(f"\n{'═' * 70}")
    print(f"  FB ADS LISTING — target: {args.target}, top={args.top}")
    print(f"{'═' * 70}\n")

    # Если задан --display-name → строим URL'ы напрямую (без Step 1)
    if args.display_name:
        from fb_page_id import build_ads_library_urls
        urls = build_ads_library_urls(args.display_name)["ALL"]
        ads_urls = {"all": urls["all"], "active": urls["active_only"],
                    "inactive": urls["inactive_only"]}
        display_name = args.display_name
    else:
        # Сначала Step 1
        from fb_page_finder import find_brand_pages
        pages = find_brand_pages(args.target, verbose=False)
        alive = [p for p in pages if p.get("alive")]
        if not alive:
            print(f"❌ Нет живых FB страниц для {args.target}")
            sys.exit(1)
        page0 = alive[0]
        display_name = page0["display_name"]
        ads_urls = page0["ads_library_urls"]
        print(f"  ✓ Step 1: handle=@{page0['handle']}, display_name='{display_name}'")
        if len(alive) > 1:
            print(f"  ℹ обнаружено {len(alive)} аккаунтов, беру первый")

    # Step 2
    print(f"\n  → Запускаю 3-проходный скрейп listing'а...")
    result = scrape_ads_listing(ads_urls, display_name=display_name, top_n=args.top)

    print(f"\n{'═' * 70}")
    print(f"  RESULT")
    print(f"{'═' * 70}")
    print(f"display_name: {result.get('display_name')}")
    print(f"total_ever:   {result.get('total_ever')}")
    for s in ("active", "inactive"):
        block = result.get(s)
        if not block:
            print(f"\n[{s}]: <none>")
            continue
        print(f"\n[{s}]")
        print(f"  count:           {block.get('count')}")
        print(f"  library_ids ({len(block.get('library_ids', []))}):")
        for i, lid in enumerate(block.get("library_ids", []), 1):
            print(f"    {i}. {lid}")
        print(f"  ad_texts:        {len(block.get('ad_texts', []))} unique")
        print(f"  partnership:     {block.get('partnership_ads')} (~{block.get('partnership_count')})")
