"""
Module 1: FB Page Finder
=========================
ВХОД:  domain (например "aerosus.fr") или handle (например "Aerosus")
ВЫХОД: list[dict] — найденные брендовые FB-страницы с meta + Ad Library URLs

Каждый элемент списка:
    {
        "handle":           "aerosusfr",
        "display_name":     "Aerosus",
        "fb_url":           "https://www.facebook.com/pages/Aerosusfr/790551394328177/",
        "alive":            True,
        "page_id":          "790551394328177",          # may be None
        "country":          "FR",                        # детектирован от домена
        "country_source":   "tld (.fr)",
        "ads_library_urls": {
            "all":      "https://...active_status=all...",
            "active":   "https://...active_status=active...",
            "inactive": "https://...active_status=inactive...",
        },
    }

Standalone:
    python fb_page_finder.py aerosus.fr
    python fb_page_finder.py mannes.fr
"""
import sys
import json
import requests
from urllib.parse import urlparse

# Переиспользуем discovery-функции из старого fb_page_id (Step 1 = thin facade)
from fb_page_id import (
    detect_site_country,
    find_all_fb_handles,
    prioritize_handles,
    check_fb_page_alive_playwright,
    build_ads_library_urls,
)
from utils import HEADERS, setup_console
setup_console()


# ─── Главная функция ────────────────────────────────────────────────────────

def find_brand_pages(target: str, verbose: bool = True) -> list:
    """
    Находит брендовые FB-страницы для домена/handle.
    Возвращает list[dict] (см. модуль docstring).
    """
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target

    # ── Вход = handle напрямую ─────────────────────────────────────────────
    if is_handle:
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}",
                    "format": "vanity", "display_name": handle}]
        country = "ALL"
        country_source = "handle input — defaulting to ALL"
        if verbose: print(f"  ℹ Вход = handle '{handle}', country=ALL")

    # ── Вход = домен сайта ─────────────────────────────────────────────────
    else:
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        domain = urlparse(base_url).netloc
        brand_name = domain.split(".")[0].replace("www.", "")

        # Скачиваем homepage HTML для (a) детекта страны (b) поиска FB-ссылок
        html, headers = "", {}
        try:
            r = requests.get(base_url, headers=HEADERS, timeout=10)
            html = r.text
            headers = dict(r.headers)
            if verbose: print(f"  ✓ Homepage загружен ({len(html):,} bytes, status={r.status_code})")
        except Exception as e:
            if verbose: print(f"  ⚠ Homepage недоступен: {str(e)[:80]}")

        # Страна
        c = detect_site_country(domain, html=html, response_headers=headers)
        country, country_source = c["country"], c["source"]
        if verbose: print(f"  🌍 Страна: {country}  (via {country_source})")

        # Поиск FB-ссылок на странице
        handles = find_all_fb_handles(base_url)
        if not handles:
            if verbose: print(f"  ❌ FB-ссылки не найдены на homepage")
            return []
        if verbose: print(f"  📎 Найдено FB-ссылок: {len(handles)}")
        handles = prioritize_handles(handles, brand_name)

    # ── Для каждого handle: проверяем alive + строим Ad Library URLs ───────
    results = []
    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        fb_url = item["url"]

        if verbose: print(f"\n  → {handle}  [{fmt}]  {fb_url}")

        # Проверка живости через Playwright
        if fmt in ("people", "profile", "pages"):
            status = check_fb_page_alive_playwright(fb_url.replace("https://www.facebook.com/", ""))
        else:
            status = check_fb_page_alive_playwright(handle)

        alive = status.get("alive", False)
        display_name = status.get("display_name") or item.get("display_name") or handle
        page_id = status.get("page_id")  # может отсутствовать

        if not alive:
            reason = status.get("reason", "unknown")
            if verbose: print(f"    ✗ DEAD — {reason}")
            results.append({
                "handle": handle, "display_name": display_name, "fb_url": fb_url,
                "alive": False, "broken_reason": reason, "page_id": None,
                "country": country, "country_source": country_source,
                "ads_library_urls": None,
            })
            continue

        if verbose: print(f"    ✓ ALIVE | display_name='{display_name}'")

        # Строим Ad Library URLs (все 3 status'а)
        ads_urls = build_ads_library_urls(display_name)["ALL"]
        if verbose:
            print(f"    📢 Ad Library (active): {ads_urls['active_only']}")

        results.append({
            "handle":           handle,
            "display_name":     display_name,
            "fb_url":           fb_url,
            "alive":            True,
            "page_id":          page_id,
            "country":          country,
            "country_source":   country_source,
            "ads_library_urls": {
                "all":      ads_urls["all"],
                "active":   ads_urls["active_only"],
                "inactive": ads_urls["inactive_only"],
            },
        })

    return results


# ─── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fb_page_finder.py <domain or handle>")
        print("Examples:")
        print("  python fb_page_finder.py aerosus.fr")
        print("  python fb_page_finder.py mannes.fr")
        print("  python fb_page_finder.py Aerosus")
        sys.exit(1)

    target = sys.argv[1]
    print(f"\n{'═' * 70}")
    print(f"  FB PAGE FINDER — target: {target}")
    print(f"{'═' * 70}\n")

    pages = find_brand_pages(target)

    print(f"\n{'═' * 70}")
    print(f"  RESULT — {len(pages)} brand page(s)")
    print(f"{'═' * 70}")
    print(json.dumps(pages, indent=2, ensure_ascii=False))
