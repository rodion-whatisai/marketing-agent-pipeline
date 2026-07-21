"""
TNC — Facebook Page ID Finder v2 (orchestrator)
================================================
Thin orchestrator. Discovery логика — в fb_discovery.py,
Ads Library scraping — в fb_ads_scraper.py.

Pipeline:
  fetch_homepage
  → find_all_fb_handles (классификация id_type: page / profile / unknown)
  → prioritize_handles (id_type=page приоритетнее всего)
  → для каждого handle:
       Ветка A (id_type=page):
         alive check + display_name → get_ads_data
       Ветка B (id_type=profile/unknown):
         find_delegate_page_id (мост profile→Page через GraphQL)
         → alive check delegate Page + display_name
         → get_ads_data
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

from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header

# Discovery: homepage → handles → page-alive → delegate
from fb_discovery import (
    detect_site_country,
    fetch_homepage,
    find_all_fb_handles,
    prioritize_handles,
    check_fb_page_alive_playwright,
    find_delegate_page_id,
)

# Ads Library scraping
from fb_ads_scraper import (
    build_ads_library_urls,
    get_ads_data,
)


def run(target: str, html: str = None, headers: dict = None, status: int = None) -> dict:
    log_debug(f"run() вход — target={target}")
    log_header(f"Facebook Accounts Finder v2 — Target: {target}")

    # Определяем что передали
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target
    log_debug(f"Классификация target: is_domain={is_domain} is_handle={is_handle}")

    if is_handle:
        log_debug("Ветка handle — target распознан как FB handle напрямую")
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}"}]
        brand_name = handle
        site_country = "ALL"
        site_country_source = "no domain — defaulting to ALL"
        log_debug(f"Handle разобран: handle={handle} brand_name={brand_name}")
    else:
        log_debug("Ветка domain — target распознан как домен")
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        brand_name = urlparse(base_url).netloc.split(".")[0]
        log_debug(f"base_url={base_url} brand_name={brand_name}")

        # Fetch homepage: requests → Playwright fallback при 403/error.
        # Playwright обходит Cloudflare/WAF и отдаёт тот же HTML что пользователь
        # видит в браузере — вместе с FB-линкой в footer, даже если поверх висят
        # cookie/geo popups (они не скрывают static HTML).
        if html and status == 200:
            # HTML и headers уже скачаны вызывателем (step1) — fetch не нужен.
            log_debug("homepage/headers переданы из step1 — fetch_homepage пропущен")
            _html, _homepage_status, _fetch_method, _homepage_error = html, status, "requests", None
            _headers = headers or {}
        else:
            log_debug(f"fetch_homepage({base_url}) — старт")
            _html, _homepage_status, _fetch_method, _homepage_error = fetch_homepage(base_url)
            log_debug(f"fetch_homepage результат: status={_homepage_status} method={_fetch_method} error={_homepage_error}")
            if _fetch_method == "playwright":
                log_success(f"Homepage получен через Playwright (HTTP {_homepage_status})", emoji="✓")

            # Для country detection нужны headers — они есть только в requests-ответе.
            # Если Playwright — оставляем пустыми, detect_site_country свалится на TLD.
            _headers = {}
            if _fetch_method == "requests":
                log_debug("fetch_method=requests — повторный GET для headers (country detection)")
                try:
                    _r = requests.get(base_url, headers=HEADERS, timeout=10, allow_redirects=True)
                    _headers = dict(_r.headers)
                except Exception as e:
                    log_debug(f"повторный GET для headers упал: {e}")

        log_debug("detect_site_country() — старт")
        country_result = detect_site_country(urlparse(base_url).netloc, html=_html, response_headers=_headers)
        site_country = country_result["country"]
        site_country_source = country_result["source"]

        log_info(f"🌍 Страна сайта: {site_country}  (via {site_country_source})")
        log_step(f"Ищу Facebook аккаунты на {target}...", emoji="🔍")
        # Передаём уже скачанный HTML — избегаем повторного fetch
        handles = find_all_fb_handles(base_url, html=_html)
        log_debug(f"find_all_fb_handles вернул {len(handles)} handle(s)")

        # Главную не достали ни requests, ни браузером — тогда включаем brand-name fallback.
        # Оба написания: "not_fetched" — текущее, "blocked_by_waf" — в сканах до 2026-07-21.
        homepage_not_fetched = _fetch_method in ("not_fetched", "blocked_by_waf")

        if not handles:
            log_debug("handles пусто — вход в brand-keyword fallback")
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
            #   - главную не достали
            #   - Homepage 200 OK, но 0 FB ссылок в HTML (Carolinas franchise pattern,
            #     Car Keys To Go: homepage не линкует city-specific FB pages, а FB
            #     pages при этом существуют — Spartanburg / Rock Hill / Charlotte etc).
            # Confidence-note меняется в зависимости от причины — отчёт разделяет.
            if homepage_not_fetched:
                log_warn(f"Главную не достали (status={_homepage_status}, err={_homepage_error})")
                confidence_note = ("We could not read the site's homepage, so the Facebook "
                                   "link could not be confirmed from the site itself. These "
                                   "ads were found by searching Ads Library with brand-name "
                                   "keyword and then fuzzy-matching advertiser names. "
                                   "Manual verification recommended.")
            else:
                log_warn(f"Facebook ссылки не найдены на homepage (status {_homepage_status})")
                confidence_note = ("Homepage returned 200 OK but contained no FB links. "
                                   "These ads were found by brand-keyword search in Ads "
                                   "Library and fuzzy-matched advertiser names. "
                                   "Manual verification recommended (especially for franchise "
                                   "brands with city-specific FB pages).")

            log_step(f"Пробую brand-name fallback — поиск в Ads Library по '{brand_name}'...", emoji="🔄")
            discovery_meta["fallback_attempted"] = True
            discovery_meta["fallback_keyword"] = brand_name

            ads_urls = build_ads_library_urls(brand_name)
            # 3-pass scan по brand-keyword (без page_id — пост-фильтр по name)
            log_debug(f"get_ads_data(brand-keyword='{brand_name}', domain={full_domain}) — старт")
            ads_data = get_ads_data(brand_name, country="ALL", domain=full_domain)
            fb_total = ads_data.get("total_ever") or 0
            log_debug(f"get_ads_data вернул total_ever={fb_total}")

            if fb_total > 0:
                active_block = ads_data.get("active") or {}
                inactive_block = ads_data.get("inactive")
                active_count = active_block.get("count", 0) or 0
                log_success(f"Brand-fallback нашёл {fb_total} объявлений (всего; активных {active_count})")
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
                log_success(f"Сохранено (via brand-fallback): {filename}", emoji="💾")
                return output
            else:
                log_error(f"Brand-fallback: 0 ads по '{brand_name}'")
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
            log_success(f"Сохранено (empty): {filename}", emoji="💾")
            return output

        log_success(f"Найдено ссылок: {len(handles)}", emoji="✓")
        handles = prioritize_handles(handles, brand_name)
        log_debug(f"prioritize_handles → {len(handles)} handle(s) после сортировки")

    # Обрабатываем каждый аккаунт
    results = []
    log_header("Обработка аккаунтов")

    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        id_type = item.get("id_type", "unknown")
        display = item.get("display_name", f"@{handle}")
        fb_page_url = item["url"]
        log_step(f"{display} [format={fmt} / id_type={id_type}]", emoji="🌐")

        # ─── Получение Page ID (Шаг 5 pipeline) ──────────────────────────
        if id_type == "page":
            # Ветка A: Page ID готов из URL (/pages/Name/{ID}/)
            log_debug(f"Ветка A (id_type=page) — Page ID готов из URL")
            fb_page_id = item["page_id"]
            log_info(f"📎 Page ID из URL: {fb_page_id} — открываю Page для alive check")
            status = check_fb_page_alive_playwright(fb_page_id)
            log_debug(f"check_fb_page_alive_playwright вернул alive={status.get('alive')}")
        else:
            # Ветка B: id_type ∈ {profile, unknown} — ищем delegate_page.id
            log_debug(f"Ветка B (id_type={id_type}) — поиск delegate_page.id")
            log_step(f"id_type={id_type} — ищу delegate_page.id на {fb_page_url}", emoji="🔍")
            fb_page_id = find_delegate_page_id(fb_page_url)
            log_debug(f"find_delegate_page_id вернул {fb_page_id}")
            if not fb_page_id:
                log_warn(f"delegate_page.id не найден — Page для этого handle нет")
                results.append({
                    "handle": handle,
                    "url": fb_page_url,
                    "alive": False,
                    "published": False,
                    "broken_reason": "no_delegate_page_id",
                    "page_id": None,
                    "ads_library": None,
                })
                continue
            # Открываем delegate Page для alive check + display_name
            log_info(f"📎 Delegate Page ID: {fb_page_id} — открываю Page для alive check")
            status = check_fb_page_alive_playwright(fb_page_id)
            log_debug(f"check_fb_page_alive_playwright (delegate) вернул alive={status.get('alive')}")

        if not status["alive"]:
            log_warn(f"Page DEAD — {status['reason']}", emoji="✗")
            results.append({
                "handle": handle,
                "url": fb_page_url,
                "alive": False,
                "published": False,
                "broken_reason": status["reason"],
                "page_id": fb_page_id,
                "ads_library": None,
            })
            continue

        display_name = status.get("display_name") or display
        log_success(f"Page alive | Display name: {display_name} | Page ID: {fb_page_id}", emoji="✓")

        ads_urls = build_ads_library_urls(display_name, page_id=fb_page_id)
        log_info(f"📢 Ads Library: {ads_urls['ALL']['active_only']}")

        log_step(f"Проверяю рекламу по имени '{display_name}'...", emoji="🔢")
        full_domain = urlparse(base_url).netloc if not is_handle else brand_name
        log_debug(f"get_ads_data(display_name='{display_name}', page_id={fb_page_id}, domain={full_domain}) — старт")
        ads_data = get_ads_data(display_name, page_id=fb_page_id, country="ALL",
                                 fb_page_url=fb_page_url, domain=full_domain)
        log_debug(f"get_ads_data вернул total_ever={ads_data.get('total_ever')} count={ads_data.get('count')}")

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
            log_warn(f"Не удалось определить количество")
        elif total_ever == 0:
            log_error(f"Объявлений нет (никогда не крутились)")
        else:
            active_n   = (active_block or {}).get("count", 0) or 0
            inactive_n = (inactive_block or {}).get("count", 0) or 0
            log_info(f"📊 Всего: {total_ever}  |  ✅ активных: {active_n}  |  📦 архив: {inactive_n}")

        # Mode/raw transparency — показывает как фильтровались keyword результаты
        ads_lib_mode = ads_data.get("ads_library_mode")
        raw_total = ads_data.get("raw_keyword_total")
        if ads_lib_mode != "page" and raw_total and raw_total != count:
            log_debug(f"Filter transparency: raw_total={raw_total} != count={count}, mode={ads_lib_mode}")
            mode_label = {
                "keyword_filtered_by_pid_or_name":   "keyword + page_id+name filter",
                "keyword_filtered_by_page_id":       "keyword + page_id filter",
                "keyword_filtered_by_name":          "keyword + fuzzy name filter",
                "keyword_raw":                       "keyword raw (unfiltered)",
                "unknown":                           "unknown",
            }.get(ads_lib_mode, ads_lib_mode)
            log_info(f"🔍 Filter mode: {mode_label} ({count} matched / {raw_total} raw)")

        if partnership:
            log_info(f"🤝 Partnership ads: да (~{partnership_n})")
        if ad_texts:
            log_info(f"📝 Текстов объявлений (active+archive): {len(ad_texts)}")
        if saved_images:
            log_info(f"🖼  Изображений скачано: {len(saved_images)}")

        results.append({
            "handle":             handle,
            "display_name":       display_name,
            "url":                fb_page_url,
            "page_id":            fb_page_id,
            "alive":              True,
            # Самоописание страницы из того же HTML (источник для business_type):
            # category есть у каждой страницы; "wall" = логин-стена, не отсутствие
            "page_category":      status.get("page_category"),
            "page_bio":           status.get("page_bio"),
            "about_access":       status.get("about_access", "wall"),
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

        log_debug(f"account обработан: handle={handle} page_id={fb_page_id}")
        time.sleep(0.5)

    # Summary
    log_header("ИТОГО")
    alive = [r for r in results if r["alive"]]
    broken = [r for r in results if not r["alive"]]
    log_info(f"Published аккаунтов:    {len(alive)}")
    if broken:
        log_warn(f"DEAD LINKS ON SITE ({len(broken)}):")
        for r in broken:
            log_debug(f"dead link: facebook.com/{r['handle']}")
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
    log_success(f"Сохранено: {filename}", emoji="💾")
    log_debug(f"run() выход — {len(results)} account(s) в результате")

    return output


if __name__ == "__main__":
    if len(sys.argv) < 2:
        log_info("Usage:")
        print("  python fb_page_id.py bandago.com")
        print("  python fb_page_id.py hipcamp.com")
        print("  python fb_page_id.py BandagoHQ")
        sys.exit(1)

    run(sys.argv[1])
