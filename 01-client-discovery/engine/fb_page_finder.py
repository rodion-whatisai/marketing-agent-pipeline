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
from fb_ads_scraper import build_ads_library_urls, _ad_leads_to_domain
from utils import HEADERS, setup_console
from log import log_info, log_warn, log_error, log_success, log_step, log_header, log_debug
setup_console()


# ─── Мост к находкам step1 (fb.json) ────────────────────────────────────────

def _handles_from_fb_json(domain: str, verbose: bool = True) -> list:
    """Страница бренда из scans/<domain>/fb.json — когда homepage без FB-ссылок.

    Источники по убыванию: accounts[].url (доменный якорь step1-скаута) →
    page_profile_uri объявлений, ведущих на домен клиента (старые fb.json без
    поля url). Возвращает handle-список в формате find_all_fb_handles."""
    from pathlib import Path
    import re as _re
    fb_path = Path("scans") / domain / "fb.json"
    if not fb_path.exists():
        log_debug(f"_handles_from_fb_json: {fb_path} отсутствует")
        return []
    try:
        data = json.loads(fb_path.read_text(encoding="utf-8"))
    except Exception as e:
        log_debug(f"_handles_from_fb_json: {fb_path} не читается: {e}")
        return []

    page_urls = []
    for acc in data.get("accounts") or []:
        if acc.get("url"):
            page_urls.append((acc["url"], acc.get("display_name")))
        else:
            for ad in acc.get("structured_ads") or []:
                uri = ad.get("page_profile_uri")
                if uri and _ad_leads_to_domain(ad, domain) and \
                        not any(u == uri for u, _ in page_urls):
                    page_urls.append((uri, ad.get("page_name") or acc.get("display_name")))
    if not page_urls:
        log_debug("_handles_from_fb_json: в fb.json нет пригодных страниц")
        return []

    handles = []
    for url, name in page_urls:
        m = _re.search(r'facebook\.com/(\d+)', url)
        if m:
            # Числовой URI → page_id известен сразу, ветка A (без delegate-моста)
            handles.append({"handle": m.group(1), "url": url.rstrip("/"),
                            "format": "pages", "id_type": "page",
                            "page_id": m.group(1), "display_name": name or m.group(1)})
        else:
            tail = url.rstrip("/").split("facebook.com/")[-1].split("?")[0]
            handles.append({"handle": tail, "url": url.rstrip("/"),
                            "format": "vanity", "id_type": "unknown",
                            "display_name": name or tail})
    if verbose:
        log_success(f"  Мост fb.json: {len(handles)} страница(ы) из находок step1 — "
                    f"{[h['url'] for h in handles]}", emoji="📎")
    return handles


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
    log_debug(f"find_brand_pages(target={target!r}, verbose={verbose}, find_delegate={find_delegate})")
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target
    log_debug(f"классификация входа: is_domain={is_domain}, is_handle={is_handle}")

    # ── Вход = handle напрямую ─────────────────────────────────────────────
    if is_handle:
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        log_debug(f"ветка handle: распарсен handle={handle!r}")
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}",
                    "format": "vanity", "display_name": handle}]
        country = "ALL"
        country_source = "handle input — defaulting to ALL"
        if verbose: log_info(f"  Вход = handle '{handle}', country=ALL")

    # ── Вход = домен сайта ─────────────────────────────────────────────────
    else:
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        domain = urlparse(base_url).netloc
        brand_name = domain.split(".")[0].replace("www.", "")
        log_debug(f"ветка домен: base_url={base_url}, domain={domain}, brand_name={brand_name}")

        # Скачиваем homepage HTML для (a) детекта страны (b) поиска FB-ссылок
        html, headers = "", {}
        log_debug(f"GET homepage: {base_url} (timeout=10)")
        try:
            r = requests.get(base_url, headers=HEADERS, timeout=10)
            html = r.text
            headers = dict(r.headers)
            log_debug(f"homepage ответ: status={r.status_code}, {len(html):,} bytes")
            if verbose: log_success(f"  Homepage загружен ({len(html):,} bytes, status={r.status_code})")
        except Exception as e:
            log_debug(f"GET homepage failed: {e}")
            if verbose: log_warn(f"  Homepage недоступен: {str(e)[:80]}")

        # Страна
        log_debug(f"detect_site_country(domain={domain})")
        c = detect_site_country(domain, html=html, response_headers=headers)
        country, country_source = c["country"], c["source"]
        log_debug(f"страна детектирована: country={country}, source={country_source}")
        if verbose: log_info(f"  🌍 Страна: {country}  (via {country_source})")

        # Поиск FB-ссылок на странице
        log_debug(f"find_all_fb_handles(base_url={base_url})")
        handles = find_all_fb_handles(base_url)
        if not handles:
            # Мост к находкам step1: сайт без FB-ссылок ≠ бренда нет в FB.
            # fb.json (скаут step1) хранит страницу, найденную доменным якорем
            # в Ads Library (объявления ведут на сайт клиента).
            # Tested: 2026-07-22 on plurio.ai — homepage без ссылок, fb.json
            # даёт facebook.com/61563178984628/
            handles = _handles_from_fb_json(domain, verbose)
            if not handles:
                log_debug("find_all_fb_handles и fb.json пусты — ранний return []")
                if verbose: log_error(f"  FB-ссылки не найдены ни на homepage, ни в fb.json")
                return []
        log_debug(f"find_all_fb_handles вернул {len(handles)} handle(s)")
        if verbose: log_info(f"  📎 Найдено FB-ссылок: {len(handles)}")
        handles = prioritize_handles(handles, brand_name)
        log_debug(f"prioritize_handles завершён, порядок: {[h['handle'] for h in handles]}")

    # ── Для каждого handle: достать Page ID, alive check, построить Ad Library URLs ──
    # Симметрия с fb_page_id.run() — ветка A (id_type=page) vs ветка B (profile/unknown).
    results = []
    log_debug(f"начинаю обход {len(handles)} handle(s)")
    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        id_type = item.get("id_type", "unknown")
        fb_url = item["url"]
        log_debug(f"handle обработка: handle={handle!r}, format={fmt}, id_type={id_type}, url={fb_url}")

        if verbose: log_step(f"  → {handle}  [{fmt} / id_type={id_type}]  {fb_url}", emoji="🔍")

        # ── Получение Page ID ──────────────────────────────────────────────
        if id_type == "page":
            # Ветка A: Page ID готов из URL (/pages/Name/{ID}/)
            page_id = item.get("page_id")
            log_debug(f"ветка A (id_type=page): page_id из URL = {page_id}")
            if verbose: log_info(f"    📎 Page ID из URL: {page_id} — alive check")
            status = check_fb_page_alive_playwright(page_id) if page_id \
                else check_fb_page_alive_playwright(handle)
            log_debug(f"alive check (ветка A) вернул: {status}")
        else:
            # Ветка B: id_type ∈ {profile, unknown}
            log_debug(f"ветка B (id_type={id_type}): find_delegate={find_delegate}")
            if find_delegate:
                # Мост через delegate_page.id (медленно — ~15 сек Playwright)
                if verbose: log_step(f"    id_type={id_type} — ищу delegate_page.id на {fb_url}", emoji="🔍")
                log_debug(f"find_delegate_page_id(fb_url={fb_url})")
                page_id = find_delegate_page_id(fb_url)
                log_debug(f"find_delegate_page_id вернул: {page_id}")
                if not page_id:
                    log_debug("delegate_page.id не найден — добавляю dead-запись, continue")
                    if verbose: log_warn(f"    delegate_page.id не найден — Page для этого handle нет")
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
                if verbose: log_info(f"    📎 Delegate Page ID: {page_id} — alive check")
                status = check_fb_page_alive_playwright(page_id)
                log_debug(f"alive check (ветка B, delegate) вернул: {status}")
            else:
                # find_delegate=False — пропускаем мост, alive check по handle напрямую
                page_id = None
                log_debug(f"ветка B без delegate: alive check по {fmt} напрямую")
                if fmt in ("people", "profile"):
                    status = check_fb_page_alive_playwright(
                        fb_url.replace("https://www.facebook.com/", ""))
                else:
                    status = check_fb_page_alive_playwright(handle)
                log_debug(f"alive check (ветка B, no delegate) вернул: {status}")

        alive = status.get("alive", False)
        display_name = status.get("display_name") or item.get("display_name") or handle
        log_debug(f"alive={alive}, display_name={display_name!r}")

        if not alive:
            reason = status.get("reason", "unknown")
            log_debug(f"страница DEAD (reason={reason}) — добавляю dead-запись, continue")
            if verbose: log_warn(f"    DEAD — {reason}")
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

        if verbose: log_success(f"    ALIVE | display_name='{display_name}' | Page ID: {page_id}")

        # Строим Ad Library URLs — view_all_page_id для classic Page IDs,
        # keyword search для new-style / без page_id (см. fb_ads_scraper).
        log_debug(f"build_ads_library_urls(display_name={display_name!r}, page_id={page_id})")
        ads_urls = build_ads_library_urls(display_name, page_id=page_id)["ALL"]
        log_debug(f"ads_urls построены: active={ads_urls['active_only']}")
        if verbose:
            log_info(f"    📢 Ad Library (active): {ads_urls['active_only']}")

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
        log_debug(f"alive-запись добавлена для handle={handle!r}")

    log_debug(f"find_brand_pages завершён: {len(results)} запись(ей)")
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
    log_header(f"FB PAGE FINDER — target: {target}")

    pages = find_brand_pages(target)

    log_header(f"RESULT — {len(pages)} brand page(s)")
    print(json.dumps(pages, indent=2, ensure_ascii=False))
