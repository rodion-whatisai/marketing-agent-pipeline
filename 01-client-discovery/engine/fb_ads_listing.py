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
from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header
setup_console()

DEFAULT_TOP_N = 10


# ─── Парсинг listing HTML ───────────────────────────────────────────────────

def _parse_count(html: str) -> int:
    """Извлекает количество объявлений на странице — несколько эвристик."""
    log_debug(f"_parse_count: html len={len(html)}")
    # 1. JSON в HTML: "search_results_connection":{"count":42}
    m = re.search(r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', html)
    if m:
        log_debug(f"_parse_count: matched search_results_connection count={m.group(1)}")
        return int(m.group(1))
    # 2. Заголовок "~42 results"
    m = re.search(r"~?(\d[\d,\s]*)\s+results?", html, re.IGNORECASE)
    if m:
        try:
            n = int(re.sub(r"[,\s]", "", m.group(1)))
            if 0 < n < 1_000_000:
                log_debug(f"_parse_count: matched '~N results' n={n}")
                return n
        except ValueError as e:
            log_debug(f"_parse_count: ValueError parsing results count: {e}")
            pass
    # 3. Пустой результат
    if any(s in html.lower() for s in
           ['no ads match', 'no results', '"edges":[]', '"count":0']):
        log_debug("_parse_count: empty-result marker found → 0")
        return 0
    log_debug("_parse_count: no heuristic matched → None")
    return None  # не смогли определить


def _parse_library_ids(html: str, max_n: int) -> list:
    """Library IDs из листинга в порядке появления (= impressions DESC).
    FB рендерит карточки сверху-вниз, поэтому первые N = top-N по impressions."""
    log_debug(f"_parse_library_ids: max_n={max_n}, html len={len(html)}")
    ids = re.findall(r"Library ID:\s*(\d{10,20})", html)
    log_debug(f"_parse_library_ids: raw matches={len(ids)}")
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
        if len(out) >= max_n:
            break
    log_debug(f"_parse_library_ids: unique returned={len(out)}")
    return out


def _parse_ad_texts(html: str) -> list:
    """Уникальные тексты объявлений с listing."""
    log_debug(f"_parse_ad_texts: html len={len(html)}")
    texts = re.findall(r'white-space:\s*pre-wrap[^>]*><span>([^<]{10,600})', html)
    log_debug(f"_parse_ad_texts: raw matches={len(texts)}")
    seen = set()
    out = []
    for t in texts:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    log_debug(f"_parse_ad_texts: unique returned={len(out)}")
    return out


def _parse_partnership(html: str) -> tuple:
    """Возвращает (is_partnership: bool, count: int)."""
    n = len(re.findall(r"branded_content", html, re.IGNORECASE))
    estimated = max(0, n // 3)  # ~3 упоминания на каждое branded_content объявление
    log_debug(f"_parse_partnership: branded_content mentions={n}, estimated={estimated}")
    return (estimated > 0, estimated)


# ─── Скрейп одного status'а ─────────────────────────────────────────────────

def _scroll_to_load_more(page, target_count: int, max_scrolls: int = 6):
    """Скроллит вниз чтобы FB подгрузил больше карточек."""
    log_debug(f"_scroll_to_load_more: target_count={target_count}, max_scrolls={max_scrolls}")
    for _ in range(max_scrolls):
        cnt = len(re.findall(r"Library ID:\s*\d{10,}", page.content()))
        log_debug(f"_scroll_to_load_more: current cards={cnt}")
        if cnt >= target_count:
            log_debug("_scroll_to_load_more: target reached → stop")
            return
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1200)


def _scrape_status(page, url: str, status_label: str, top_n: int,
                    verbose: bool = True) -> dict:
    """Скрейпит один URL listing'а."""
    log_debug(f"_scrape_status: status_label={status_label}, top_n={top_n}, url={url}")
    if verbose: log_step(f"[{status_label}] open URL...", emoji="🔎")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3500)
    except Exception as e:
        if verbose: log_warn(f"goto failed: {str(e)[:80]}")
        return {"count": None, "library_ids": [], "ad_texts": [],
                "partnership_ads": False, "partnership_count": 0,
                "error": str(e)[:100]}

    # Если нужны top-N — скроллим чтобы подгрузить
    if top_n > 12:
        log_debug(f"_scrape_status: top_n={top_n} > 12 → scrolling to load more")
        _scroll_to_load_more(page, target_count=top_n)

    log_debug("_scrape_status: capturing page.content() for parsing")
    html = page.content()
    count = _parse_count(html)
    library_ids = _parse_library_ids(html, max_n=top_n)
    ad_texts = _parse_ad_texts(html)
    is_partnership, partnership_n = _parse_partnership(html)
    log_debug(f"_scrape_status: parsed count={count}, library_ids={len(library_ids)}, "
              f"ad_texts={len(ad_texts)}, partnership={is_partnership}")

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
    log_debug(f"scrape_ads_listing: display_name='{display_name}', top_n={top_n}, verbose={verbose}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        log_error(f"playwright not installed: {e}")
        return {"error": "playwright not installed"}

    result = {
        "display_name": display_name,
        "total_ever":   None,
        "active":       None,
        "inactive":     None,
    }

    with sync_playwright() as p:
        log_debug("scrape_ads_listing: launching headless Chromium")
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

        # ── Pass 1: total ever ─────────────────────────────────────────
        log_debug("scrape_ads_listing: Pass 1 (active_status=all) — total ever")
        pass1 = _scrape_status(page, ads_library_urls["all"], "all",
                                top_n=1, verbose=verbose)
        total = pass1.get("count")
        result["total_ever"] = total
        if verbose: log_info(f"total_ever = {total}", emoji="📊")

        if not total or total == 0:
            if verbose: log_error("Объявлений нет — стоп")
            browser.close()
            return result

        # ── Pass 2: active ─────────────────────────────────────────────
        log_debug("scrape_ads_listing: Pass 2 (active_status=active)")
        pass2 = _scrape_status(page, ads_library_urls["active"], "active",
                                top_n=top_n, verbose=verbose)
        active_count = pass2.get("count", 0) or 0
        if active_count > 0:
            result["active"] = pass2
            if verbose:
                log_success(f"active: count={active_count}, "
                      f"library_ids={len(pass2['library_ids'])}, "
                      f"texts={len(pass2['ad_texts'])}")
        else:
            log_debug("scrape_ads_listing: active_count == 0")
            if verbose: log_info("active: 0", emoji="➖")

        # ── Pass 3: inactive (только если есть смысл) ─────────────────
        if total > active_count:
            log_debug("scrape_ads_listing: Pass 3 (active_status=inactive) — total > active")
            pass3 = _scrape_status(page, ads_library_urls["inactive"], "inactive",
                                    top_n=top_n, verbose=verbose)
            inactive_count = pass3.get("count", 0) or 0
            if inactive_count > 0:
                result["inactive"] = pass3
                if verbose:
                    log_info(f"inactive: count={inactive_count}, "
                          f"library_ids={len(pass3['library_ids'])}, "
                          f"texts={len(pass3['ad_texts'])}", emoji="📦")
        else:
            log_debug("scrape_ads_listing: skipping Pass 3 (active == total)")
            if verbose: log_info("inactive: пропуск (active == total)", emoji="➖")

        browser.close()
    return result


# ─── Полные ad-records листинга (GraphQL-перехват, для fb_audience_report) ──

def normalize_listing_record(obj: dict) -> dict:
    """Плоские поля из ad-record листинга (ad_archive_id + snapshot).
    Источник: network_request (GraphQL пагинации листинга или HAR браузера)."""
    snap = obj.get("snapshot") or {}
    body = (snap.get("body") or {})
    body_text = body.get("text") if isinstance(body, dict) else (body or None)
    cards = snap.get("cards") or []
    # DCO: top-level body бывает шаблоном {{product.brand}} — fallback на cards[0]
    if (not body_text or "{{" in str(body_text)) and cards:
        b0 = (cards[0] or {}).get("body")
        if b0:
            body_text = b0 if isinstance(b0, str) else b0.get("text")
    imp = obj.get("impressions_with_index") or {}
    return {
        "publisher_platform": obj.get("publisher_platform") or [],
        "display_format":     snap.get("display_format"),
        "body_text":          (body_text or "")[:300],
        "link_url":           snap.get("link_url"),
        "cta_text":           snap.get("cta_text"),
        "cta_type":           snap.get("cta_type"),
        "title":              snap.get("title"),
        "start_date_unix":    obj.get("start_date"),
        "end_date_unix":      obj.get("end_date"),
        "is_active":          obj.get("is_active"),
        "collation_count":    obj.get("collation_count"),
        "total_active_time":  obj.get("total_active_time"),
        "impressions_text":   imp.get("impressions_text"),
        "n_videos":           len(snap.get("videos") or []),
        "n_images":           len(snap.get("images") or []),
        "n_cards":            len(cards),
        # превью креатива: resized (~50KB), не original (1-2MB) — иначе репорт раздувается
        "image_url":          ((snap.get("images") or [{}])[0] or {}).get("resized_image_url"),
        "video_preview_url":  ((snap.get("videos") or [{}])[0] or {}).get("video_preview_image_url"),
    }


def _walk_ad_records(obj, out: dict):
    """Рекурсивно собирает все ad-records (ad_archive_id + snapshot) из JSON."""
    if isinstance(obj, dict):
        aid = obj.get("ad_archive_id")
        if aid and isinstance(obj.get("snapshot"), dict):
            out[str(aid)] = normalize_listing_record(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_ad_records(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_ad_records(x, out)


def parse_records_from_json_text(text: str, out: dict):
    """Извлекает ad-records из тела GraphQL-ответа (может быть несколько JSON-строк)."""
    for line in text.splitlines():
        if "ad_archive_id" not in line:
            continue
        i = line.find("{")
        if i < 0:
            continue
        try:
            _walk_ad_records(json.loads(line[i:]), out)
        except json.JSONDecodeError:
            continue


def collect_active_ad_records(listing_url: str, max_stale: int = 8,
                               max_rounds: int = 120, verbose: bool = True) -> dict:
    """Скроллит листинг, перехватывая GraphQL-пагинацию → полные ad-records.

    Конец скролла: футер «System status» в DOM (наблюдение Rodion'а: появляется
    только когда список кончился) ЛИБО max_stale раундов без новых записей.
    Возвращает {"records": {lib_id: rec}, "reached_footer": bool, "rounds": int}.
    Известное ограничение: карточки-группы (collation) едут ОДНОЙ записью-представителем,
    члены группы отдельно в пагинации не появляются — покрытие < 100%, репортим честно.
    # Tested: 2026-07-18 on client-a --top 10 — 118 records, футер достигнут."""
    from playwright.sync_api import sync_playwright
    records: dict = {}
    log_step(f"Листинг ad-records: {listing_url[:90]}...", emoji="📜")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.on("response", lambda r: _on_listing_response(r, records))
        page.goto(listing_url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(6000)
        stale, prev, rounds, reached_footer = 0, 0, 0, False
        while stale < max_stale and rounds < max_rounds:
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1800)
            rounds += 1
            now = len(records)
            stale = 0 if now > prev else stale + 1
            prev = now
            try:
                # футер «System status» рендерится только в конце списка
                reached_footer = page.evaluate(
                    "() => document.body.innerText.includes('System status')")
            except Exception as e:
                log_debug(f"collect_active_ad_records: footer-check упал: {e}")
            if reached_footer and stale >= 2:
                log_debug(f"collect_active_ad_records: футер System status + stale — конец")
                break
        browser.close()
    if verbose:
        log_success(f"листинг: {len(records)} ad-records, раундов {rounds}, "
                    f"футер {'достигнут' if reached_footer else 'НЕ достигнут'}")
    return {"records": records, "reached_footer": reached_footer, "rounds": rounds}


def _on_listing_response(r, records: dict):
    if "/api/graphql" not in r.url:
        return
    try:
        body = r.text()
    except Exception:
        return
    if "ad_archive_id" in body:
        parse_records_from_json_text(body, records)


def parse_har_ad_records(har_path: str) -> dict:
    """Ad-records из HAR-файла браузера (канал Rodion'а: залогиненный вид даёт
    вдобавок impressions_text). Возвращает {lib_id: rec}."""
    from pathlib import Path
    records: dict = {}
    har = json.loads(Path(har_path).read_text(encoding="utf-8"))
    for e in (har.get("log") or {}).get("entries") or []:
        text = (((e.get("response") or {}).get("content") or {}).get("text")) or ""
        if "ad_archive_id" in text:
            parse_records_from_json_text(text, records)
    log_success(f"HAR: {len(records)} ad-records из {har_path}")
    return records


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

    log_header(f"FB ADS LISTING — target: {args.target}, top={args.top}")

    # Если задан --display-name → строим URL'ы напрямую (без Step 1)
    if args.display_name:
        log_debug("__main__: --display-name provided → build URLs directly (skip Step 1)")
        from fb_ads_scraper import build_ads_library_urls
        urls = build_ads_library_urls(args.display_name)["ALL"]
        ads_urls = {"all": urls["all"], "active": urls["active_only"],
                    "inactive": urls["inactive_only"]}
        display_name = args.display_name
    else:
        # Сначала Step 1
        log_debug("__main__: no --display-name → running Step 1 find_brand_pages")
        from fb_page_finder import find_brand_pages
        # find_delegate=False — listing работает через display_name, экономим Playwright
        pages = find_brand_pages(args.target, verbose=False, find_delegate=False)
        alive = [p for p in pages if p.get("alive")]
        if not alive:
            log_error(f"Нет живых FB страниц для {args.target}")
            sys.exit(1)
        page0 = alive[0]
        display_name = page0["display_name"]
        ads_urls = page0["ads_library_urls"]
        log_success(f"Step 1: handle=@{page0['handle']}, display_name='{display_name}'")
        if len(alive) > 1:
            log_info(f"обнаружено {len(alive)} аккаунтов, беру первый")

    # Step 2
    log_step("Запускаю 3-проходный скрейп listing'а...", emoji="→")
    result = scrape_ads_listing(ads_urls, display_name=display_name, top_n=args.top)

    log_header("RESULT")
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
