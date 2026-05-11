"""
TNC — Facebook Page ID Finder v2 (orchestrator)
================================================
Thin orchestrator. Discovery логика — в fb_discovery.py,
Ads Library scraping — в fb_ads_scraper.py.

Pipeline:
  fetch_homepage → find_all_fb_handles → prioritize_handles
  → для каждого handle: check_fb_page_alive_playwright → get_ads_data
  → сохраняем scans/{domain}/fb.json

Brand-keyword fallback: если на homepage 0 FB ссылок (или WAF block),
ищем по brand-name в Ads Library с fuzzy name filter.

Запуск:
    python fb_page_id.py bandago.com
    python fb_page_id.py hipcamp.com
    python fb_page_id.py BandagoHQ          # напрямую по handle
"""

import sys
import json
import time
import requests
from urllib.parse import urlparse

from utils import scan_path, HEADERS, setup_console
setup_console()

# Discovery: homepage → handles → page-alive
from fb_discovery import (
    detect_site_country,
    fetch_homepage,
    find_all_fb_handles,
    prioritize_handles,
    check_fb_page_alive_playwright,
)

# Ads Library scraping
from fb_ads_scraper import (
    build_ads_library_urls,
    get_ads_data,
)


def run(target: str) -> dict:
    print(f"\n{'═' * 60}")
    print(f"  Facebook Accounts Finder v2")
    print(f"  Target: {target}")
    print(f"{'═' * 60}")

    # Определяем что передали
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target

    if is_handle:
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}"}]
        brand_name = handle
        site_country = "ALL"
        site_country_source = "no domain — defaulting to ALL"
    else:
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        brand_name = urlparse(base_url).netloc.split(".")[0]

        # Fetch homepage: requests → Playwright fallback при 403/error.
        # Playwright обходит Cloudflare/WAF и отдаёт тот же HTML что пользователь
        # видит в браузере — вместе с FB-линкой в footer, даже если поверх висят
        # cookie/geo popups (они не скрывают static HTML).
        _html, _homepage_status, _fetch_method, _homepage_error = fetch_homepage(base_url)
        if _fetch_method == "playwright":
            print(f"    ✓ Homepage получен через Playwright (HTTP {_homepage_status})")

        # Для country detection нужны headers — они есть только в requests-ответе.
        # Если Playwright — оставляем пустыми, detect_site_country свалится на TLD.
        _headers = {}
        if _fetch_method == "requests":
            try:
                _r = requests.get(base_url, headers=HEADERS, timeout=10, allow_redirects=True)
                _headers = dict(_r.headers)
            except Exception:
                pass

        country_result = detect_site_country(urlparse(base_url).netloc, html=_html, response_headers=_headers)
        site_country = country_result["country"]
        site_country_source = country_result["source"]

        print(f"\n🌍 Страна сайта: {site_country}  (via {site_country_source})")
        print(f"\n🔍 Ищу Facebook аккаунты на {target}...")
        # Передаём уже скачанный HTML — избегаем повторного fetch
        handles = find_all_fb_handles(base_url, html=_html)

        # Homepage blocked flag — триггер brand-name fallback только если оба способа
        # (requests + Playwright) не смогли пробиться.
        homepage_blocked = _fetch_method == "blocked_by_waf"

        if not handles:
            full_domain = urlparse(base_url).netloc
            discovery_meta = {
                "homepage_status":        _homepage_status,
                "homepage_error":         _homepage_error,
                "homepage_fetch_method":  _fetch_method,
                "fallback_attempted":     False,
                "fallback_keyword":       None,
                "fallback_result":        None,
            }

            # Brand-keyword fallback запускаем ВСЕГДА когда handles пусто:
            #   - WAF block (homepage не достали)
            #   - Homepage 200 OK, но 0 FB ссылок в HTML (Carolinas franchise pattern,
            #     Car Keys To Go: homepage не линкует city-specific FB pages, а FB
            #     pages при этом существуют — Spartanburg / Rock Hill / Charlotte etc).
            # Confidence-note меняется в зависимости от причины — отчёт разделяет.
            if homepage_blocked:
                print(f"  ⚠️  Homepage заблокирован (status={_homepage_status}, err={_homepage_error})")
                confidence_note = ("Homepage was blocked. These ads were found by "
                                   "searching Ads Library with brand-name keyword and "
                                   "then fuzzy-matching advertiser names. "
                                   "Manual verification recommended.")
            else:
                print(f"  ⚠️  Facebook ссылки не найдены на homepage (status {_homepage_status})")
                confidence_note = ("Homepage returned 200 OK but contained no FB links. "
                                   "These ads were found by brand-keyword search in Ads "
                                   "Library and fuzzy-matched advertiser names. "
                                   "Manual verification recommended (especially for franchise "
                                   "brands with city-specific FB pages).")

            print(f"  🔄 Пробую brand-name fallback — поиск в Ads Library по '{brand_name}'...")
            discovery_meta["fallback_attempted"] = True
            discovery_meta["fallback_keyword"] = brand_name

            ads_urls = build_ads_library_urls(brand_name)
            # 3-pass scan по brand-keyword (без page_id — пост-фильтр по name)
            ads_data = get_ads_data(brand_name, country="ALL", domain=full_domain)
            fb_total = ads_data.get("total_ever") or 0

            if fb_total > 0:
                active_block = ads_data.get("active") or {}
                inactive_block = ads_data.get("inactive")
                active_count = active_block.get("count", 0) or 0
                print(f"  ✅ Brand-fallback нашёл {fb_total} объявлений (всего; активных {active_count})")
                discovery_meta["fallback_result"] = f"found_{fb_total}_ads"
                virtual_account = {
                    "handle":             None,
                    "display_name":       brand_name,
                    "url":                None,
                    "page_id":            None,
                    "alive":              True,
                    # 3-pass структура
                    "total_ever":         fb_total,
                    "active":             active_block,
                    "inactive":           inactive_block,
                    # back-compat плоские поля
                    "active_ads_count":   active_count,
                    "partnership_ads":    ads_data.get("partnership_ads", False),
                    "partnership_count":  ads_data.get("partnership_count", 0),
                    "ad_texts":           ads_data.get("ad_texts", []),
                    "saved_images":       ads_data.get("saved_images", []),
                    "structured_ads":     ads_data.get("structured_ads", []),
                    "ads_library_mode":   ads_data.get("ads_library_mode", "unknown"),
                    "raw_keyword_total":  ads_data.get("raw_keyword_total", 0),
                    "extraction_method":  ads_data.get("extraction_method", "unknown"),
                    "ads_search_term":    brand_name,
                    "ads_library":        ads_urls,
                    "discovery_method":   "brand_keyword_fallback",
                    "confidence_note":    confidence_note,
                }
                output = {
                    "target":              target,
                    "brand_name":          brand_name,
                    "site_country":        site_country,
                    "site_country_source": site_country_source,
                    "accounts":            [virtual_account],
                    "discovery_meta":      discovery_meta,
                }
                filename = scan_path(full_domain, "fb.json")
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
                print(f"\n💾 Сохранено (via brand-fallback): {filename}")
                return output
            else:
                print(f"  ❌ Brand-fallback: 0 ads по '{brand_name}'")
                discovery_meta["fallback_result"] = "no_ads_found"

            # Save empty fb.json with discovery_meta so report can explain what happened
            output = {
                "target":              target,
                "brand_name":          brand_name,
                "site_country":        site_country,
                "site_country_source": site_country_source,
                "accounts":            [],
                "discovery_meta":      discovery_meta,
            }
            filename = scan_path(full_domain, "fb.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            print(f"\n💾 Сохранено (empty): {filename}")
            return output

        print(f"  ✓ Найдено ссылок: {len(handles)}")
        handles = prioritize_handles(handles, brand_name)

    # Обрабатываем каждый аккаунт
    results = []
    print(f"\n{'─' * 60}")

    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        display = item.get("display_name", f"@{handle}")
        fb_page_url = item["url"]
        print(f"\n  {display} [{fmt}]")

        if fmt in ("people", "profile", "pages"):
            status = check_fb_page_alive_playwright(item["url"].replace("https://www.facebook.com/", ""))
        else:
            status = check_fb_page_alive_playwright(handle)

        if not status["alive"]:
            print(f"    ✗ DEAD LINK — {status['reason']}")
            results.append({
                "handle": handle,
                "url": fb_page_url,
                "published": False,
                "broken_reason": status["reason"],
                "page_id": None,
                "ads_library": None,
            })
            continue

        display_name = status.get("display_name") or display
        fb_page_id = status.get("page_id") or item.get("page_id")

        print(f"    ✓ Published | Display name: {display_name}"
              + (f" | Page ID: {fb_page_id}" if fb_page_id else ""))

        ads_urls = build_ads_library_urls(display_name, page_id=fb_page_id)
        print(f"    📢 Ads Library: {ads_urls['ALL']['active_only']}")

        print(f"    🔢 Проверяю рекламу по имени '{display_name}'...")
        full_domain = urlparse(base_url).netloc if not is_handle else brand_name
        ads_data = get_ads_data(display_name, page_id=fb_page_id, country="ALL",
                                 fb_page_url=fb_page_url, domain=full_domain)

        total_ever     = ads_data.get("total_ever")
        active_block   = ads_data.get("active")    # dict | None
        inactive_block = ads_data.get("inactive")  # dict | None
        # back-compat плоские поля (для step1_sitemap.py и старых консьюмеров)
        count          = ads_data.get("count", 0)
        ad_texts       = ads_data.get("ad_texts", [])
        partnership    = ads_data.get("partnership_ads", False)
        partnership_n  = ads_data.get("partnership_count", 0)
        saved_images   = ads_data.get("saved_images", [])

        # ── Печать итогов по 3-х проходному скану ──────────────────────────
        if total_ever is None:
            print(f"    ⚠️  Не удалось определить количество")
        elif total_ever == 0:
            print(f"    ❌ Объявлений нет (никогда не крутились)")
        else:
            active_n   = (active_block or {}).get("count", 0) or 0
            inactive_n = (inactive_block or {}).get("count", 0) or 0
            print(f"    📊 Всего: {total_ever}  |  ✅ активных: {active_n}  |  📦 архив: {inactive_n}")

        # Mode/raw transparency — показывает как фильтровались keyword результаты
        ads_lib_mode = ads_data.get("ads_library_mode")
        raw_total = ads_data.get("raw_keyword_total")
        if raw_total and raw_total != count:
            mode_label = {
                "page":                              "verified by page_id",
                "keyword_filtered_by_pid_or_name":   "keyword + page_id+name filter",
                "keyword_filtered_by_page_id":       "keyword + page_id filter",
                "keyword_filtered_by_name":          "keyword + fuzzy name filter",
                "keyword_raw":                       "keyword raw (unfiltered)",
                "unknown":                           "unknown",
            }.get(ads_lib_mode, ads_lib_mode)
            print(f"    🔍 Filter mode: {mode_label} ({count} matched / {raw_total} raw)")

        if partnership:
            print(f"    🤝 Partnership ads: да (~{partnership_n})")
        if ad_texts:
            print(f"    📝 Текстов объявлений (active+archive): {len(ad_texts)}")
        if saved_images:
            print(f"    🖼  Изображений скачано: {len(saved_images)}")

        results.append({
            "handle":             handle,
            "display_name":       display_name,
            "url":                fb_page_url,
            "page_id":            fb_page_id,
            "alive":              True,
            # 3-проходная структура
            "total_ever":         total_ever,
            "active":             active_block,
            "inactive":           inactive_block,
            # back-compat плоские поля + filter transparency
            "active_ads_count":   count,
            "partnership_ads":    partnership,
            "partnership_count":  partnership_n,
            "ad_texts":           ad_texts,
            "saved_images":       saved_images,
            "structured_ads":     ads_data.get("structured_ads", []),
            "ads_library_mode":   ads_data.get("ads_library_mode", "unknown"),
            "raw_keyword_total":  ads_data.get("raw_keyword_total", 0),
            "extraction_method":  ads_data.get("extraction_method", "unknown"),
            "ads_search_term":    display_name,
            "ads_library":        ads_urls,
            "discovery_method":   "homepage_link",
        })

        time.sleep(0.5)

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  ИТОГО")
    print(f"{'═' * 60}")
    alive = [r for r in results if r["alive"]]
    broken = [r for r in results if not r["alive"]]
    print(f"  Published аккаунтов:    {len(alive)}")
    if broken:
        print(f"\n  ⚠️  DEAD LINKS ON SITE ({len(broken)}):")
        for r in broken:
            print(f"    • facebook.com/{r['handle']}")

    # Сохраняем
    output = {
        "target": target,
        "brand_name": brand_name,
        "site_country": site_country,
        "site_country_source": site_country_source,
        "accounts": results,
    }

    filename = scan_path(urlparse(base_url).netloc if not is_handle else brand_name, "fb.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Сохранено: {filename}")

    return output


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python fb_page_id.py bandago.com")
        print("  python fb_page_id.py hipcamp.com")
        print("  python fb_page_id.py BandagoHQ")
        sys.exit(1)

    run(sys.argv[1])
