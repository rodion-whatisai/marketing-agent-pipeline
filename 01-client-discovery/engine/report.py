
"""
TNC Pipeline — Report Generator v2
====================================
Генерирует человекочитаемый отчёт из результатов Step 2.
Рекомендации строятся динамически из реальных данных сканирования.

Запуск:
    python report.py step2_bandago.com.json
    python report.py step2_hipcamp.com.json
"""

import sys
import json
import os
from urllib.parse import urlparse
from collections import defaultdict
from pathlib import Path

from utils import get_scan_dir, scan_path
from log import log_warn, log_error, log_debug




# ─── Стандартные события по платформам ───────────────────────────────────────
# Шаг B (2026-07-13): локальные копии убраны — списки читаются из единого
# реестра platforms.py (ревью дня 5: копии разъехались — form_start был
# одновременно шумом для сканера и «конверсией» для отчёта, Google Ads
# page_view-пинги печатались как события). Презентационные отличия отчёта
# (PageView не перечисляем и т.п.) — явной надбавкой в реестре.
import platforms as _platforms

STANDARD_CONVERSION_EVENTS = _platforms.as_report_standard_events()
NOISE_EVENTS = _platforms.as_report_noise_events()


def load(path: str) -> dict:
    log_debug(f"load: start path={path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    log_debug(f"load: loaded {len(data)} top-level keys from {path}")
    return data


def is_standard_conversion(platform: str, event: str) -> bool:
    return any(
        s.lower() == event.lower()
        for s in STANDARD_CONVERSION_EVENTS.get(platform, [])
    )


def is_noise(platform: str, event: str) -> bool:
    return event in NOISE_EVENTS.get(platform, [])


def is_custom_event(platform: str, event: str) -> bool:
    """Событие есть, не шум, не стандартное — значит кастомное."""
    return (
        not is_noise(platform, event) and
        not is_standard_conversion(platform, event) and
        event not in ("fired", "track", "unknown", "request_fired")
    )


def analyze_platform_data(all_pages: list) -> dict:
    """
    Анализирует данные по каждой платформе:
    - покрытие страниц
    - стандартные конверсионные события
    - кастомные события
    - шум
    """
    log_debug(f"analyze_platform_data: start pages={len(all_pages)}")
    platforms = {}

    for page in all_pages:
        # Только network-подтверждённые события. Платформы из shopify_pixel_platforms
        # без запросов НЕ подмешиваем (A1: дубль синтеза PageView) — они рендерятся
        # отдельной строкой «обнаружены в коде, запросов не зафиксировано».
        combined_events = dict(page.get("pixel_events", {}))

        for platform, events in combined_events.items():
            if platform not in platforms:
                platforms[platform] = {
                    "pages_fired": 0,
                    "total_pages": len(all_pages),
                    "standard_conversions": set(),
                    "custom_events": set(),
                    "noise_only_pages": 0,
                }

            platforms[platform]["pages_fired"] += 1
            has_non_noise = False

            for ev in events:
                name = ev.get("event", "")
                if is_standard_conversion(platform, name):
                    platforms[platform]["standard_conversions"].add(name)
                    has_non_noise = True
                elif is_custom_event(platform, name):
                    platforms[platform]["custom_events"].add(name)
                    has_non_noise = True

            if not has_non_noise:
                platforms[platform]["noise_only_pages"] += 1

    # Конвертируем set → list
    for p in platforms:
        platforms[p]["standard_conversions"] = sorted(platforms[p]["standard_conversions"])
        platforms[p]["custom_events"] = sorted(platforms[p]["custom_events"])

    log_debug(f"analyze_platform_data: done platforms={list(platforms.keys())}")
    return platforms


def build_recommendations(platforms: dict, gap_pages: list, base_url: str, gtm: dict = None) -> list:
    """
    Строит рекомендации динамически на основе реальных данных.
    """
    log_debug(f"build_recommendations: start base_url={base_url} platforms={list(platforms.keys())} gaps={len(gap_pages)}")
    recs = []
    domain = urlparse(base_url).netloc

    # Группируем gap страницы по типу
    gap_by_type = defaultdict(list)
    for p in gap_pages:
        gap_by_type[p["page_type"]].append(p)

    # ── Meta Pixel ───────────────────────────────────────────────
    meta = platforms.get("Meta", {})
    if meta:
        std_conv = meta["standard_conversions"]
        custom = meta["custom_events"]
        coverage = meta["pages_fired"]
        total = meta["total_pages"]
        log_debug(f"build_recommendations: Meta std_conv={std_conv} custom={custom} coverage={coverage}/{total}")

        if not std_conv and not custom:
            log_debug("build_recommendations: Meta — пиксель грузится, событий нет (priority 1)")
            recs.append({
                "priority": 1,
                "platform": "Meta Pixel",
                "issue": "Пиксель загружается но ни одного события нет",
                "detail": f"Покрытие {coverage}/{total} страниц — только загрузка файла пикселя. "
                          f"Алгоритм Meta работает вслепую.",
                "action": _suggest_meta_events(gap_by_type),
            })
        elif not std_conv and custom:
            log_debug("build_recommendations: Meta — только кастомные события (priority 2)")
            recs.append({
                "priority": 2,
                "platform": "Meta Pixel",
                "issue": f"Только кастомные события: {', '.join(custom)}",
                "detail": "Стандартных конверсионных событий нет — Meta не может оптимизировать "
                          "по Purchase/Lead/ViewContent.",
                "action": _suggest_meta_events(gap_by_type),
            })
        elif std_conv:
            # Есть конверсии — смотрим чего не хватает
            missing = [e for e in ["Purchase", "Lead", "InitiateCheckout", "ViewContent"]
                      if not any(e.lower() in s.lower() for s in std_conv)]
            log_debug(f"build_recommendations: Meta — есть конверсии, missing={missing}")
            if missing:
                recs.append({
                    "priority": 3,
                    "platform": "Meta Pixel",
                    "issue": f"Есть события: {', '.join(std_conv)}. Отсутствуют: {', '.join(missing)}",
                    "detail": "Воронка частично настроена но есть пробелы.",
                    "action": f"Добавить: {', '.join(missing)}",
                })

    # ── Google Analytics ─────────────────────────────────────────
    ga = platforms.get("Google Analytics", {})
    gtm_ga_events = (gtm or {}).get("gtm_conversion_events", {}).get("GA4", [])
    gtm_has_ga = "Google Analytics GA4" in (gtm or {}).get("all_platforms", [])
    gtm_has_sgtm = (gtm or {}).get("has_sgtm", False)
    log_debug(f"build_recommendations: GA present={bool(ga)} gtm_has_ga={gtm_has_ga} gtm_ga_events={gtm_ga_events} sgtm={gtm_has_sgtm}")

    if ga:
        std_conv = ga["standard_conversions"]
        custom = ga["custom_events"]
        if not std_conv:
            if gtm_ga_events:
                recs.append({
                    "priority": 1,
                    "platform": "Google Analytics / GA4",
                    "issue": f"События настроены в GTM ({', '.join(gtm_ga_events)}) но не стреляют",
                    "detail": "Конфигурация есть в контейнере но dataLayer не передаёт данные. "
                              "Скорее всего триггер неправильный или ecommerce dataLayer не настроен.",
                    "action": "Проверить dataLayer.push на страницах конверсии, "
                              "убедиться что триггер совпадает с реальным событием",
                })
            else:
                missing_ga = ["purchase", "begin_checkout", "generate_lead", "view_item"]
                recs.append({
                    "priority": 2,
                    "platform": "Google Analytics / GA4",
                    "issue": "Нет конверсионных событий в GA4",
                    "detail": f"Есть только технические события"
                              f"{f' и кастомные: {chr(44).join(custom)}' if custom else ''}. "
                              f"Воронка не видна в отчётах.",
                    "action": f"Настроить через GTM: {', '.join(missing_ga)}",
                })
    elif gtm_has_ga:
        # GA4 есть в GTM но не видно при сканировании (server-side)
        if gtm_ga_events:
            detail = (f"GA4 работает через server-side GTM — события не видны при клиентском сканировании. "
                      f"В контейнере настроено: {', '.join(gtm_ga_events)}. "
                      f"Требуется проверка через GA4 DebugView или GTM Preview.")
            recs.append({
                "priority": 1,
                "platform": "Google Analytics / GA4",
                "issue": f"События в GTM ({', '.join(gtm_ga_events)}) — работают ли корректно неизвестно",
                "detail": detail,
                "action": "Проверить через GA4 DebugView: открыть сайт → Events → убедиться что purchase стреляет с корректным value",
            })
        else:
            recs.append({
                "priority": 2,
                "platform": "Google Analytics / GA4",
                "issue": "GA4 в GTM но конверсионных событий не настроено",
                "detail": "Тег установлен через GTM но только базовый page_view. "
                          "Воронка не видна в отчётах.",
                "action": "Настроить через GTM: purchase, begin_checkout, generate_lead, view_item",
            })

    # ── Google Ads ───────────────────────────────────────────────
    gads = platforms.get("Google Ads", {})
    gtm_has_gads = "Google Ads" in (gtm or {}).get("all_platforms", [])
    gtm_gads_id = (gtm or {}).get("all_ids", {}).get("Google Ads", [])
    log_debug(f"build_recommendations: Google Ads present={bool(gads)} gtm_has_gads={gtm_has_gads} gads_id={gtm_gads_id}")

    if gads:
        if not gads["standard_conversions"]:
            recs.append({
                "priority": 1,
                "platform": "Google Ads",
                "issue": "Тег есть, конверсии не настроены",
                "detail": "Реклама крутится без данных о конверсиях — невозможна оптимизация по результату.",
                "action": "Настроить конверсионные действия в Google Ads и передавать value",
            })
    elif gtm_has_gads:
        # Google Ads есть в GTM но не виден при сканировании
        id_str = f" (ID: {', '.join(gtm_gads_id)})" if gtm_gads_id else ""
        if gtm_has_sgtm:
            recs.append({
                "priority": 1,
                "platform": f"Google Ads{id_str}",
                "issue": "Google Ads работает через server-side GTM — конверсии не проверить клиентски",
                "detail": "Тег в GTM есть, но из-за server-side setup невозможно проверить "
                          "передаётся ли value в конверсию. Риск: value=0 или конверсии не фиксируются.",
                "action": "Проверить в Google Ads → Конверсии: убедиться что значение конверсии "
                          "динамическое а не 0. Сверить с реальными бронированиями.",
            })
        else:
            recs.append({
                "priority": 2,
                "platform": f"Google Ads{id_str}",
                "issue": "Google Ads в GTM но не зафиксирован при сканировании",
                "detail": "Возможно тег заблокирован CMP или неправильно настроен триггер.",
                "action": "Проверить через GTM Preview: убедиться что тег стреляет на нужных страницах",
            })

    # ── Bing ─────────────────────────────────────────────────────
    bing = platforms.get("Bing/Microsoft", {})
    if bing and not bing["standard_conversions"]:
        noise_pages = bing["noise_only_pages"]
        fired_pages = bing["pages_fired"]
        if fired_pages > 0:
            recs.append({
                "priority": 3,
                "platform": "Bing/Microsoft Ads",
                "issue": f"Тег грузится на {fired_pages} страницах, конверсий ноль",
                "detail": "Бюджет на Bing тратится без attribution.",
                "action": "Настроить UET конверсии в Microsoft Ads",
            })

    # ── TikTok ───────────────────────────────────────────────────
    tiktok = platforms.get("TikTok", {})
    if tiktok and not tiktok["standard_conversions"]:
        recs.append({
            "priority": 3,
            "platform": "TikTok Pixel",
            "issue": "Пиксель есть, конверсий нет",
            "detail": "TikTok Ads работает без данных о конверсиях.",
            "action": "Добавить ViewContent, InitiateCheckout, Purchase через Events API",
        })

    # ── Gap страницы с высоким приоритетом ───────────────────────
    critical_gaps = [p for p in gap_pages
                     if p["page_type"] in ("booking_confirm", "checkout", "quote", "lead_form")]
    log_debug(f"build_recommendations: critical_gaps={len(critical_gaps)}")
    if critical_gaps:
        paths = [p["path"] for p in critical_gaps[:3]]
        recs.append({
            "priority": 1,
            "platform": "Все платформы",
            "issue": f"Конверсионные страницы без tracking: {', '.join(paths)}",
            "detail": "Страницы где пользователь совершает ключевое действие — без единого события.",
            "action": "Приоритет №1: добавить события на эти страницы",
        })

    log_debug(f"build_recommendations: done recs={len(recs)}")
    return sorted(recs, key=lambda x: x["priority"])


def _suggest_meta_events(gap_by_type: dict) -> str:
    suggestions = []
    if gap_by_type.get("product") or gap_by_type.get("use_case"):
        suggestions.append("ViewContent на страницах продуктов/листингов")
    if gap_by_type.get("search_results"):
        suggestions.append("Search на страницах поиска")
    if gap_by_type.get("lead_form"):
        suggestions.append("Lead на контактных страницах")
    if gap_by_type.get("checkout"):
        suggestions.append("InitiateCheckout + Purchase на checkout")
    if gap_by_type.get("booking_confirm"):
        suggestions.append("Purchase/Lead на странице подтверждения")
    if not suggestions:
        suggestions.append("ViewContent, Lead, InitiateCheckout, Purchase")
    return "Добавить: " + " | ".join(suggestions)


TYPE_LABELS = {
    "lead_form":       "🔴 Lead Forms",
    "booking_confirm": "🔴 Booking / Confirm",
    "quote":           "🔴 Quote",
    "checkout":        "🔴 Checkout",
    "homepage":        "🟠 Homepage",
    "location":        "🟠 Location Pages",
    "product":         "🟠 Product / Listing Pages",
    "use_case":        "🟠 Use Case Pages",
    "search_results":  "🟠 Search / Browse Pages",
    "pricing":         "🟠 Pricing",
    "faq_support":     "🟡 FAQ / Guides",
    "about":           "🟡 About",
    "general":         "⚪ General",
}

PLATFORM_ICONS = {
    "Meta":             "📘 Meta Pixel",
    "Google Analytics": "📊 Google Analytics",
    "Google Ads":       "🟡 Google Ads",
    "Bing/Microsoft":   "🔷 Bing/Microsoft",
    "LinkedIn":         "💼 LinkedIn",
    "TikTok":           "🎵 TikTok",
}


def load_gtm_data(domain: str) -> dict:
    """Пробует загрузить GTM данные для домена.

    Пайплайн пишет их в scans/<domain>/gtm.json (gtm_analyzer из step2);
    legacy-имя gtm_<domain>.json рядом со скриптом — fallback для старых прогонов.
    # Tested: 2026-07-07 on tinytronics.nl — до фикса искался только legacy-путь,
    #         GTM-данные не грузились → build_recommendations выдавал 0 рекомендаций
    """
    log_debug(f"load_gtm_data: start domain={domain}")
    gtm_file = scan_path(domain, "gtm.json")
    if gtm_file.exists():
        log_debug(f"load_gtm_data: найден {gtm_file}")
        with open(gtm_file, "r", encoding="utf-8") as f:
            return json.load(f)
    legacy = f"gtm_{domain}.json"
    if os.path.exists(legacy):
        log_debug(f"load_gtm_data: найден legacy {legacy}")
        with open(legacy, "r", encoding="utf-8") as f:
            return json.load(f)
    log_debug(f"load_gtm_data: {gtm_file} нет — пусто")
    return {}


def merge_gtm_insights(gtm_data: dict) -> dict:
    """Извлекает ключевые инсайты из GTM данных."""
    log_debug(f"merge_gtm_insights: start containers={list(gtm_data.keys())}")
    insights = {
        "containers": [],
        "all_platforms": set(),
        "all_ids": {},
        "gtm_conversion_events": {},
        "problems": {},
        "has_sgtm": False,
        "has_capi": False,
    }
    for container_id, data in gtm_data.items():
        insights["containers"].append(container_id)
        insights["all_platforms"].update(data.get("platforms_found", {}).keys())
        for platform, ids in data.get("ids_found", {}).items():
            insights["all_ids"].setdefault(platform, []).extend(ids)
        for platform, events in data.get("conversion_events", {}).items():
            insights["gtm_conversion_events"].setdefault(platform, []).extend(events)
        problems = data.get("problems", {})
        insights["problems"].update(problems)
        if "Server-side GTM" in problems:
            insights["has_sgtm"] = True
        if "CAPI hints" in problems:
            insights["has_capi"] = True
    insights["all_platforms"] = list(insights["all_platforms"])
    log_debug(f"merge_gtm_insights: done platforms={insights['all_platforms']} sgtm={insights['has_sgtm']} capi={insights['has_capi']}")
    return insights


def print_report(data: dict, gtm_data: dict = None):
    log_debug(f"print_report: start base_url={data.get('base_url', '')} gtm_data_passed={gtm_data is not None}")
    all_pages = data.get("all_pages", data.get("gap_pages", []))
    gap_pages  = data.get("gap_pages", [])
    ok_pages   = data.get("ok_pages", [])
    unverified_pages_data = data.get("unverified_pages", [])
    base_url   = data.get("base_url", "")

    total   = data.get("scanned", len(all_pages))
    n_gaps  = data.get("gaps",    len(gap_pages))
    n_oks   = data.get("oks",     len(ok_pages))
    n_nocta = data.get("no_ctas", total - n_gaps - n_oks)

    # День 7: страницы, не прошедшие шлюз — рендерим отдельной строкой,
    # НЕ смешиваем с аудированными (принцип «статусы не смешиваются»)
    n_redirected = data.get("redirected", 0)
    n_http_err = data.get("http_errors", 0)
    if n_redirected or n_http_err:
        print(f"\nℹ️  Не аудировано: "
              + (f"{n_redirected} стр. — редирект на другой домен/в корень" if n_redirected else "")
              + ("; " if n_redirected and n_http_err else "")
              + (f"{n_http_err} стр. — не дочитали" if n_http_err else ""))
        for r in data.get("redirected_pages", []):
            print(f"    ↪ {r.get('path')} → {(r.get('gate') or {}).get('final_url', '')[:70]}")
        for r in data.get("http_error_pages", []):
            print(f"    ⛔ {r.get('path')} — HTTP {(r.get('gate') or {}).get('http_status')}")

    platforms = analyze_platform_data(all_pages)
    domain = urlparse(base_url).netloc

    # Загружаем GTM данные если не переданы явно (или переданы пустыми)
    if not gtm_data:
        gtm_data = load_gtm_data(domain)
    gtm = merge_gtm_insights(gtm_data) if gtm_data else {}

    # NO TRACKING — особый случай
    no_tracking = data.get("no_tracking", 0)
    is_no_tracking = no_tracking > 0 and not platforms and not gtm
    log_debug(f"print_report: total={total} gaps={n_gaps} oks={n_oks} nocta={n_nocta} is_no_tracking={is_no_tracking}")

    print(f"\n{'═' * 65}")
    print(f"  TRACKING AUDIT REPORT")
    print(f"  {base_url}")
    print(f"{'═' * 65}")

    if is_no_tracking:
        print(f"\n🚨 КРИТИЧНО: НА САЙТЕ НЕТ TRACKING")
        print(f"{'─' * 65}")
        print(f"  Проверено страниц: {total}")
        print(f"  GTM:        не найден")
        print(f"  Meta Pixel: не найден")
        print(f"  GA4:        не найден")
        print(f"  Google Ads: не найден")
        print(f"\n  Весь рекламный трафик идёт вслепую.")
        print(f"  Невозможно запустить ни одну платную кампанию с attribution.")
        print(f"\n💡 РЕКОМЕНДАЦИИ")
        print(f"{'─' * 65}")
        print(f"\n  1. Установить GTM — единая точка управления всем tracking")
        print(f"  2. Через GTM подключить Meta Pixel + GA4 как минимум")
        print(f"  3. Настроить конверсионные события: ViewContent, AddToCart,")
        print(f"     InitiateCheckout, Purchase")
        print(f"\n{'═' * 65}\n")
        return

    # ── 1. Общая статистика ──────────────────────────────────────
    print(f"\n📋 ОБЩАЯ СТАТИСТИКА")
    print(f"  Страниц просканировано:    {total}")
    print(f"  ✅ OK  (CTA + события):    {n_oks}")
    print(f"  🚨 GAP (CTA, нет событий): {n_gaps}")
    print(f"  ➖ Без CTA:                {n_nocta}")
    if n_gaps == total:
        print(f"\n  ⚠️  НИ ОДНА страница не имеет конверсионного tracking!")

    # ── GTM Container ────────────────────────────────────────────
    if gtm:
        print(f"\n{'─' * 65}")
        print(f"🏷  GTM CONTAINER")
        print(f"{'─' * 65}")
        print(f"  Контейнер:  {', '.join(gtm['containers'])}")

        if gtm["all_ids"]:
            print(f"\n  ID платформ:")
            for plat, ids in gtm["all_ids"].items():
                for id_val in ids:
                    print(f"    {plat:<20} {id_val}")

        if gtm["all_platforms"]:
            print(f"\n  Платформы в GTM: {', '.join(sorted(gtm['all_platforms']))}")

        if gtm["gtm_conversion_events"]:
            print(f"\n  События настроены в GTM (в коде):")
            for plat, evts in gtm["gtm_conversion_events"].items():
                print(f"    {plat}: {', '.join(evts)}")
            # Критический инсайт — в GTM настроено но не стреляет
            gtm_has_purchase = any(
                "purchase" in e.lower()
                for evts in gtm["gtm_conversion_events"].values()
                for e in evts
            )
            scan_has_purchase = any(
                any("purchase" in ev.get("event", "").lower()
                    for ev in p.get("pixel_events", {}).get("Google Analytics", []))
                for p in all_pages
            )
            if gtm_has_purchase and not scan_has_purchase:
                print(f"\n  ⚠️  purchase настроен в GTM но не зафиксирован при сканировании")
                print(f"     Вероятно: dataLayer не передаёт данные или триггер неправильный")

        flags = []
        if gtm["has_sgtm"]:
            flags.append("Server-side GTM")
        if gtm["has_capi"]:
            flags.append("CAPI / server events")
        if flags:
            print(f"\n  ℹ️  Дополнительно: {', '.join(flags)}")

        # Платформы в GTM но не видные при сканировании.
        # Маппинг — из реестра (ревью дня 6: локальная копия не знала Snapchat
        # Pixel → ложное «в GTM есть, но не видно» при живом Snapchat network)
        scanned_platforms = set(platforms.keys())
        gtm_platform_map = _platforms.as_gtm_to_scan()
        hidden = []
        for gtm_plat in gtm["all_platforms"]:
            scan_name = gtm_platform_map.get(gtm_plat, gtm_plat)
            if scan_name not in scanned_platforms:
                hidden.append(gtm_plat)
        if hidden:
            print(f"\n  ⚠️  В GTM есть но не видно при сканировании: {', '.join(hidden)}")
            if gtm["has_sgtm"]:
                print(f"     Причина: используется server-side GTM")

    # ── 2. По платформам ─────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"📡 ПОКРЫТИЕ ПО ПЛАТФОРМАМ")
    print(f"{'─' * 65}")

    if not platforms:
        print(f"  Ни одна платформа не зафиксирована")
    else:
        for platform, stats in sorted(platforms.items()):
            icon  = PLATFORM_ICONS.get(platform, platform)
            fired = stats["pages_fired"]
            total_p = stats["total_pages"]
            pct   = round(fired / total_p * 100) if total_p else 0
            std   = stats["standard_conversions"]
            custom = stats["custom_events"]

            print(f"\n  {icon}")
            print(f"    Страниц с активностью: {fired}/{total_p} ({pct}%)")

            if std:
                print(f"    ✅ Стандартные конверсии: {', '.join(std)}")
            else:
                print(f"    ❌ Стандартных конверсий: НЕТ")

            if custom:
                print(f"    🔧 Кастомные события:    {', '.join(custom)}")

    # Shopify web-pixels: найдены в коде воркеров, ни одного network-запроса.
    # Не «активность» и не событие — отдельная строка (consent-gated пиксель молчит легитимно).
    code_only = sorted({
        plat for p in all_pages for plat in p.get("shopify_pixel_platforms", [])
    } - set(platforms.keys()))
    if code_only:
        print(f"\n  ⚠️  Обнаружены в коде (Shopify web-pixels), запросов не зафиксировано: {', '.join(code_only)}")

    # ── 3. Что отсутствует глобально ─────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"🚫 ОТСУТСТВУЮЩИЕ СТАНДАРТНЫЕ СОБЫТИЯ")
    print(f"{'─' * 65}")

    key_events = {
        "Meta":             ["Purchase", "Lead", "InitiateCheckout", "ViewContent", "Search"],
        "Google Analytics": ["purchase", "begin_checkout", "generate_lead", "view_item"],
        "Google Ads":       ["conversion"],
        "Bing/Microsoft":   ["conversion"],
        "TikTok":           ["Purchase", "ViewContent", "InitiateCheckout"],
    }

    for platform, expected in key_events.items():
        if platform not in platforms:
            continue
        seen = set(platforms[platform]["standard_conversions"])
        missing = [e for e in expected
                   if not any(e.lower() in s.lower() for s in seen)]
        if missing:
            icon = PLATFORM_ICONS.get(platform, platform)
            print(f"\n  {icon}")
            for m in missing:
                print(f"    ✗ {m}")

    # ── Дубли пикселей (жёлтая зона) ─────────────────────────────
    dup_pages = [p for p in all_pages if p.get("duplicate_pixels")]
    if dup_pages:
        print(f"\n{'─' * 65}")
        print(f"⚠️  ДУБЛИРУЮЩИЕ ПИКСЕЛИ — возможен двойной счёт событий")
        print(f"{'─' * 65}")
        print(f"   Найдено >1 ID одной платформы. Частая причина искажённого ROAS.")
        print(f"   Проверить вручную (легитимный случай: пиксель агентства + пиксель клиента).")
        for p in dup_pages:
            for plat in p["duplicate_pixels"]:
                ids = p.get("pixel_ids", {}).get(plat, [])
                print(f"    {p['path']}  →  {plat}: {', '.join(ids)}")

    # ── 4. GAP страницы ──────────────────────────────────────────
    # gap_pages из step2 — уже только реальные GAP; unverified лежат отдельной корзиной
    real_gap_pages = gap_pages
    unverified_pages = unverified_pages_data + [
        p for p in gap_pages if "server-side tracking" in p.get("status", "")
    ]

    if real_gap_pages:
        print(f"\n{'─' * 65}")
        print(f"🚨 GAP СТРАНИЦЫ — CTA есть, событий нет")
        print(f"{'─' * 65}")

        gap_by_type = defaultdict(list)
        for p in real_gap_pages:
            gap_by_type[p["page_type"]].append(p)

        type_order = ["lead_form", "booking_confirm", "quote", "checkout",
                      "homepage", "product", "location", "use_case",
                      "search_results", "pricing", "about", "general"]

        for ptype in type_order:
            pages = gap_by_type.get(ptype, [])
            if not pages:
                continue
            label = TYPE_LABELS.get(ptype, ptype)
            print(f"\n  {label} ({len(pages)} стр.)")
            for p in pages:
                ctas = p.get("cta_elements", [])
                missing = p.get("missing_events", [])
                px = p.get("pixel_events", {})

                print(f"    {p['path']}")
                if ctas:
                    print(f"      CTA: {', '.join(ctas[:3])}")
                # При загрузке — что реально зафиксировали (только network-события, A1)
                fired = []
                for plat, evts in px.items():
                    names = [e["event"] for e in evts
                             if not is_noise(plat, e["event"])
                             and e["event"] not in ("fired", "track")]
                    if names:
                        fired.append(f"{', '.join(names)} → {plat}")
                code_only = [plat for plat in p.get("shopify_pixel_platforms", [])
                             if plat not in px]
                if fired:
                    print(f"      При загрузке: {' | '.join(fired)}")
                else:
                    print(f"      При загрузке: ничего не зафиксировано")
                if code_only:
                    print(f"      В коде (web-pixels), запросов нет: {', '.join(code_only)}")

                # Не зафиксированные события
                if missing:
                    for ev in missing:
                        print(f"      {ev}: не зафиксирован при загрузке")

    if unverified_pages:
        print(f"\n{'─' * 65}")
        print(f"⚠️  НЕ ПОДТВЕРЖДЕНО — пиксель есть, событие не зафиксировано")
        print(f"{'─' * 65}")
        print(f"   Браузерный скан не может подтвердить ИЛИ опровергнуть срабатывание.")
        for p in unverified_pages:
            print(f"\n    {p['path']}")
            print(f"      {p.get('status', '')}")
            pids = p.get("pixel_ids", {})
            if pids:
                ids_str = " | ".join(f"{plat}: {', '.join(ids)}" for plat, ids in pids.items())
                print(f"      Пиксели по ID: {ids_str}")
            if p.get("duplicate_pixels"):
                print(f"      ⚠️  ДУБЛЬ: {', '.join(p['duplicate_pixels'])} — >1 ID, возможен двойной счёт")
            reason = p.get("unverified_reason", "")
            if reason:
                print(f"      Причина: {reason}")
            ext = list(p.get("external_services", {}).keys())
            scheduler = [s for s in ext if s in ("Cal.com", "Calendly", "Acuity", "HubSpot Meetings")]
            if scheduler:
                print(f"      Сервис: {', '.join(scheduler)}")

    # ── 5. OK страницы ───────────────────────────────────────────
    if ok_pages:
        print(f"\n{'─' * 65}")
        print(f"✅ OK — tracking настроен")
        print(f"{'─' * 65}")
        for p in ok_pages:
            print(f"  {p['path']}")
            for ev in p.get("conversion_events_found", []):
                print(f"    → {ev}")

    # ── 6. Рекомендации ──────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"💡 РЕКОМЕНДАЦИИ (по приоритету)")
    print(f"{'─' * 65}")

    recs = build_recommendations(platforms, gap_pages, base_url, gtm)

    if not recs:
        # Нет автоматических рекомендаций ≠ «всё корректно»: формулировка строго
        # по наблюдённым фактам, зеркалит вердикт-ветвление generate_report_html
        if n_gaps > 0 or no_tracking > 0:
            print(f"\n  Автоматические рекомендации не сформированы.")
            print(f"  Зафиксировано: {n_gaps} страниц с CTA без конверсионных событий — см. секцию GAP.")
        elif n_oks > 0:
            print(f"\n  Конверсионные события зафиксированы на всех страницах с CTA — рекомендаций нет")
        else:
            print(f"\n  Данных недостаточно для автоматических рекомендаций — нужна ручная проверка")
    else:
        for i, rec in enumerate(recs, 1):
            print(f"\n  {i}. {rec['platform']}")
            print(f"     Проблема: {rec['issue']}")
            print(f"     Детали:   {rec['detail']}")
            print(f"     Действие: {rec['action']}")

    # ── 7. Внешние сервисы ───────────────────────────────────────
    external = data.get("external_services", {})
    if external:
        print(f"\n{'─' * 65}")
        print(f"🔗 ВНЕШНИЕ СЕРВИСЫ — конверсии происходят вне сайта")
        print(f"{'─' * 65}")
        print(f"  Пиксели НЕ видят эти конверсии. Attribution потерян.\n")
        for svc, pages in external.items():
            pages_str = ", ".join(pages[:3])
            print(f"  ⚠️  {svc:<25} найден на: {pages_str}")
        print(f"\n  Действие: настроить server-side tracking или GTM триггеры")
        print(f"  для передачи конверсий из внешних сервисов в пиксели.")

    print(f"\n{'═' * 65}\n")


if __name__ == "__main__":
    # UTF-8 сразу: load() ниже зовёт log_debug с эмодзи ДО setup_logging
    # (домен для имени лога известен только после загрузки JSON) — на cp1252 падало.
    # Tested: 2026-07-09 on kogerstaete.nl_step2.json под PYTHONIOENCODING=cp1252 —
    #         до фикса UnicodeEncodeError на 🐛 (report.py:59 → log.py:97), после — чистый прогон.
    from utils import setup_console
    setup_console()
    if "--debug" in sys.argv or "--quiet" in sys.argv:
        import log
        log.set_level("INFO" if "--quiet" in sys.argv else "DEBUG")  # --quiet приглушает до INFO+
        sys.argv = [a for a in sys.argv if a not in ("--debug", "--quiet")]  # убрать из позиционных
    if len(sys.argv) < 2:
        log_error("Usage: python report.py scans/<domain>/<domain>_step2.json [путь-к-gtm.json] [--debug|--quiet]")
        sys.exit(1)

    data = load(sys.argv[1])

    # GTM данные — второй аргумент или автоматически
    gtm_data = None
    if len(sys.argv) > 2:
        gtm_data = load(sys.argv[2])
    else:
        domain = urlparse(data.get("base_url", "")).netloc
        gtm_data = load_gtm_data(domain)

    # Logging
    from utils import setup_logging as _setup_logging
    _d2 = urlparse(data.get("base_url","")).netloc
    _setup_logging(_d2, step="report")

    print_report(data, gtm_data)

    # Сообщение после report


    # Генерируем HTML репорт
    try:
        import os
        from pathlib import Path
        from generate_report_html import run as generate_html_report
        generate_html_report(sys.argv[1])
    except Exception as e:
        log_warn(f"HTML репорт не сгенерирован: {e}")

    # Хинт — общий лог
    _d3 = urlparse(data.get("base_url", "")).netloc
    print(f"\n" + "─" * 65)
    print(f"  💾 Получить общий лог:")
    print(f"     python merge_logs.py {_d3}")
    print("─" * 65 + "\n")
