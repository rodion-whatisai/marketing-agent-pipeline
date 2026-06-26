"""
Module 3: FB Ad Modal Opener
=============================
ВХОД:  library_id (например "1843412783120928") + опционально готовый Playwright page
ВЫХОД: success: bool, page остаётся с открытой модалкой (для следующего шага)

Логика:
    1. Навигация: https://www.facebook.com/ads/library/?id={library_id}
    2. Дисмисс cookie banner если есть
    3. Click 'See ad details'
    4. Ждём heading 'Transparency by location' (надёжный сигнал что модалка отрисована)

Standalone:
    python fb_ad_modal_open.py 1843412783120928           # открыть, сохранить HTML
    python fb_ad_modal_open.py aerosus.fr                  # запустит Steps 1+2, возьмёт первый library_id
"""
import sys
import argparse
from pathlib import Path

from utils import setup_console
from log import log_info, log_warn, log_error, log_success, log_step, log_header, log_debug
setup_console()


COOKIE_DISMISS_SELECTORS = [
    'button:has-text("Allow all cookies")',
    'button:has-text("Accept all")',
    '[data-cookiebanner="accept_button"]',
]


def _dismiss_cookie_banner(page, verbose: bool = False) -> bool:
    """Пытается дисмиссить cookie banner. Возвращает True если что-то нажал."""
    log_debug(f"_dismiss_cookie_banner: проверяю {len(COOKIE_DISMISS_SELECTORS)} селекторов")
    for sel in COOKIE_DISMISS_SELECTORS:
        log_debug(f"_dismiss_cookie_banner: пробую селектор {sel}")
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=600):
                el.click(timeout=2000)
                page.wait_for_timeout(700)
                if verbose: log_step(f"dismissed cookies via {sel}", emoji="🍪")
                return True
        except Exception as e:
            log_debug(f"_dismiss_cookie_banner: селектор {sel} не сработал: {e}")
    log_debug("_dismiss_cookie_banner: ни один селектор не сработал")
    return False


def open_ad_modal(library_id: str, page=None,
                   verbose: bool = True) -> dict:
    """
    Открывает модалку для одного объявления.
    Если page=None — создаёт свой browser/context; иначе работает на переданной странице.
    Возвращает {success, library_id, error?}.
    Page остаётся с открытой модалкой (для следующих шагов).
    """
    log_debug(f"open_ad_modal: вход library_id={library_id}, own_browser={page is None}")
    own_browser = page is None
    browser = ctx = None

    if own_browser:
        log_debug("open_ad_modal: own_browser=True — создаю свой playwright/browser/context")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log_error("playwright not installed")
            return {"success": False, "library_id": library_id,
                    "error": "playwright not installed"}
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

    url = f"https://www.facebook.com/ads/library/?id={library_id}"
    if verbose: log_step(f"[{library_id}] open {url}", emoji="📂")

    try:
        log_debug(f"open_ad_modal: page.goto {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3000)
        log_debug("open_ad_modal: страница загружена, дисмиссю cookie banner")

        _dismiss_cookie_banner(page, verbose=verbose)

        # Click 'See ad details' — это <div role="button">, не <button>
        # Используем role-based locator (ловит и <button>, и [role="button"])
        log_debug("open_ad_modal: жду селектор 'See ad details'")
        page.wait_for_selector('text=See ad details', timeout=12000)
        btn = page.get_by_role("button", name="See ad details").first
        btn.scroll_into_view_if_needed(timeout=5000)
        btn.click(force=True, timeout=8000)
        if verbose: log_step(f"clicked 'See ad details'", emoji="🖱")

        # Ждём heading 'Transparency by location' — модалка отрисована
        log_debug("open_ad_modal: жду heading 'Transparency by location'")
        try:
            page.wait_for_selector("text=Transparency by location", timeout=10000)
            if verbose: log_success(f"модалка открыта (Transparency heading present)")
        except Exception as e:
            log_debug(f"open_ad_modal: heading 'Transparency by location' не появился: {e}")
            if verbose: log_warn(f"heading 'Transparency by location' не появился")

        page.wait_for_timeout(800)

        result = {"success": True, "library_id": library_id, "url": url}

        if own_browser:
            # Возвращаем HTML и закрываем браузер для standalone-режима
            log_debug("open_ad_modal: own_browser — собираю HTML и закрываю браузер")
            result["html"] = page.content()
            browser.close()
            pw.stop()

        return result

    except Exception as e:
        if verbose: log_error(f"ошибка: {str(e)[:120]}")
        if own_browser:
            try:
                browser.close()
                pw.stop()
            except Exception as e2:
                log_debug(f"open_ad_modal: cleanup браузера упал: {e2}")
        return {"success": False, "library_id": library_id, "error": str(e)[:200]}


# ─── Standalone ─────────────────────────────────────────────────────────────

def _resolve_target_to_library_ids(target: str, top_n: int = 1) -> list:
    """target может быть либо library_id (только цифры), либо domain.
    Если domain — запускает Steps 1+2 чтобы получить library_ids."""
    log_debug(f"_resolve_target_to_library_ids: target={target}, top_n={top_n}")
    if target.isdigit():
        log_debug("_resolve_target_to_library_ids: target — цифры, это library_id")
        return [target]
    # это domain → Steps 1+2
    log_info(f"'{target}' — не library_id, запускаю Steps 1+2...")
    from fb_page_finder import find_brand_pages
    from fb_ads_listing import scrape_ads_listing

    # find_delegate=False — листингу page_id не нужен, экономим Playwright
    log_debug(f"_resolve_target_to_library_ids: find_brand_pages({target})")
    pages = find_brand_pages(target, verbose=False, find_delegate=False)
    alive = [p for p in pages if p.get("alive")]
    if not alive:
        log_error(f"Step 1: нет живых FB страниц")
        return []

    page0 = alive[0]
    log_success(f"Step 1: @{page0['handle']}, display='{page0['display_name']}'")

    log_debug(f"_resolve_target_to_library_ids: scrape_ads_listing top_n={top_n}")
    listing = scrape_ads_listing(page0["ads_library_urls"],
                                  display_name=page0["display_name"],
                                  top_n=top_n, verbose=False)
    active = listing.get("active") or {}
    inactive = listing.get("inactive") or {}
    ids = list(active.get("library_ids") or []) or list(inactive.get("library_ids") or [])
    log_success(f"Step 2: {len(ids)} library_ids ({'active' if active else 'inactive'})")
    return ids[:top_n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="library_id (цифры) или domain")
    ap.add_argument("--save-html", action="store_true",
                    help="сохранить HTML модалки в scans/_explore/modal_<id>.html")
    args = ap.parse_args()

    log_header(f"FB AD MODAL OPENER — target: {args.target}")

    library_ids = _resolve_target_to_library_ids(args.target, top_n=1)
    if not library_ids:
        sys.exit(1)

    lib_id = library_ids[0]
    log_info(f"→ Открываю модалку для library_id={lib_id}")

    result = open_ad_modal(lib_id)

    log_header("RESULT")
    print(f"success:    {result.get('success')}")
    print(f"library_id: {result.get('library_id')}")
    print(f"url:        {result.get('url')}")

    if result.get("success") and result.get("html"):
        html = result["html"]
        print(f"html size:  {len(html):,} bytes")
        # Какие секции присутствуют
        print(f"\n  Секции в DOM:")
        from fb_ad_modal_parse import detect_sections
        for s in detect_sections(html):
            print(f"    ✓ {s}")
        # missing = [lbl for lbl in ALL_SECTION_LABELS if lbl not in html]
        # for s in missing:
        #     print(f"    ✗ {s}")

        if args.save_html:
            out = Path(f"scans/_explore/modal_{lib_id}.html").resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            log_success(f"Сохранено: {out}", emoji="💾")

    if not result.get("success"):
        print(f"error:      {result.get('error')}")
        sys.exit(1)
