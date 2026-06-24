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

# Discovery (homepage → handle → page-alive → delegate) и Ads Library URL builder
from fb_discovery import (
    detect_site_country,
    find_all_fb_handles,
    prioritize_handles,
    check_fb_page_alive_playwright,
    find_delegate_page_id,
)
from fb_ads_scraper import build_ads_library_urls
from utils import HEADERS, setup_console
setup_console()


# ─── Главная функция ────────────────────────────────────────────────────────

def find_brand_pages(target: str, verbose: bool = True,
                      find_delegate: bool = True) -> list:
    """
    Находит брендовые FB-страницы для домена/handle.
    Возвращает list[dict] (см. модуль docstring).

    find_delegate=True (дефолт) — для profile/unknown handles вызывает
    find_delegate_page_id (~10-15 сек/handle через Playwright). Симметрия с
    fb_page_id.run, page_id попадает в результат.

    find_delegate=False — пропускает delegate lookup, page_id=None для всех
    profile/unknown handles. Использовать когда caller'у page_id не нужен
    (fb_scan / fb_ads_listing / fb_ad_modal_open работают через display_name).
    Экономит ~15 сек на каждый profile/unknown handle.
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

    # ── Для каждого handle: достать Page ID, alive check, построить Ad Library URLs ──
    # Симметрия с fb_page_id.run() — ветка A (id_type=page) vs ветка B (profile/unknown).
    results = []
    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        id_type = item.get("id_type", "unknown")
        fb_url = item["url"]

        if verbose: print(f"\n  → {handle}  [{fmt} / id_type={id_type}]  {fb_url}")

        # ── Получение Page ID ──────────────────────────────────────────────
        if id_type == "page":
            # Ветка A: Page ID готов из URL (/pages/Name/{ID}/)
            page_id = item.get("page_id")
            if verbose: print(f"    📎 Page ID из URL: {page_id} — alive check")
            status = check_fb_page_alive_playwright(page_id) if page_id \
                else check_fb_page_alive_playwright(handle)
        else:
            # Ветка B: id_type ∈ {profile, unknown}
            if find_delegate:
                # Мост через delegate_page.id (медленно — ~15 сек Playwright)
                if verbose: print(f"    🔍 id_type={id_type} — ищу delegate_page.id на {fb_url}")
                page_id = find_delegate_page_id(fb_url)
                if not page_id:
                    if verbose: print(f"    ⚠ delegate_page.id не найден — Page для этого handle нет")
                    results.append({
                        "handle":           handle,
                        "display_name":     item.get("display_name") or handle,
                        "fb_url":           fb_url,
                        "alive":            False,
                        "broken_reason":    "no_delegate_page_id",
                        "page_id":          None,
                        "country":          country,
                        "country_source":   country_source,
                        "ads_library_urls": None,
                    })
                    continue
                if verbose: print(f"    📎 Delegate Page ID: {page_id} — alive check")
                status = check_fb_page_alive_playwright(page_id)
            else:
                # find_delegate=False — пропускаем мост, alive check по handle напрямую
                page_id = None
                if fmt in ("people", "profile"):
                    status = check_fb_page_alive_playwright(
                        fb_url.replace("https://www.facebook.com/", ""))
                else:
                    status = check_fb_page_alive_playwright(handle)

        alive = status.get("alive", False)
        display_name = status.get("display_name") or item.get("display_name") or handle

        if not alive:
            reason = status.get("reason", "unknown")
            if verbose: print(f"    ✗ DEAD — {reason}")
            results.append({
                "handle":           handle,
                "display_name":     display_name,
                "fb_url":           fb_url,
                "alive":            False,
                "broken_reason":    reason,
                "page_id":          page_id,
                "country":          country,
                "country_source":   country_source,
                "ads_library_urls": None,
            })
            continue

        if verbose: print(f"    ✓ ALIVE | display_name='{display_name}' | Page ID: {page_id}")

        # Строим Ad Library URLs — view_all_page_id для classic Page IDs,
        # keyword search для new-style / без page_id (см. fb_ads_scraper).
        ads_urls = build_ads_library_urls(display_name, page_id=page_id)["ALL"]
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
