"""
TNC Pipeline — Step 2: Pages of Interest Scanner
=================================================
Оркестратор: читает step1.json, определяет платформу,
вызывает нужный scanner из scanners/.

Запуск:
    python step2_scan.py scans/bandago.com/bandago.com_step1.json
    python step2_scan.py scans/bandago.com/bandago.com_step1.json --priority 1
    python step2_scan.py scans/bandago.com/bandago.com_step1.json --url /contact
"""

import sys
import json
import time
import argparse
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

from page_classifier import get_page_priority_label
from popup_handler import handle_popups
from utils import get_scan_dir, scan_path, setup_logging, HEADERS
from scanners import get_scanner
from scanners.base_scanner import ANALYTICS_TOOLS
from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header


def run(step1_file: str, max_priority: int = 2, only_url: str = None,
        debug_mode: bool = False, click_mode: bool = False, headed: bool = False):
    log_debug(f"run: start step1_file={step1_file} max_priority={max_priority} only_url={only_url} click_mode={click_mode} headed={headed}")
    try:
        with open(step1_file, "r", encoding="utf-8") as f:
            step1 = json.load(f)
    except Exception as e:
        log_error(f"Не могу открыть {step1_file}: {e}")
        sys.exit(1)

    base_url = step1["base_url"]
    domain = urlparse(base_url).netloc
    platform = step1.get("platform", {}).get("platform", "unknown")
    log_debug(f"run: base_url={base_url} domain={domain} platform={platform}")

    to_scan_raw = step1.get("to_scan", step1.get("classified", []))
    to_scan = [p for p in to_scan_raw if p.get("priority", 5) <= max_priority]
    log_debug(f"run: to_scan_raw={len(to_scan_raw)} → filtered by priority≤{max_priority} → {len(to_scan)} pages")
    if only_url:
        exact = [p for p in to_scan if p.get("path", "") == only_url]
        to_scan = exact if exact else [p for p in to_scan if only_url in p.get("url", "")]
        log_debug(f"run: only_url={only_url} → {'exact match' if exact else 'substring match'} → {len(to_scan)} pages")
        if not to_scan:
            log_error(f"Страница '{only_url}' не найдена в step1.json")
            return

    log_header("TNC Pipeline — Step 2: Page Scanner")
    print(f"  Target:   {base_url}")
    print(f"  Platform: {platform.upper()}")
    print(f"  Priority: ≤ {max_priority} ({get_page_priority_label(max_priority)})")
    print(f"  Pages:    {len(to_scan)}")
    print()

    scanner = get_scanner(platform)
    log_debug(f"run: scanner resolved for platform={platform}")

    GTM_TO_SCAN = {
        "Meta Pixel": "Meta",
        "Google Analytics GA4": "Google Analytics",
        "Google Ads": "Google Ads",
        "TikTok Pixel": "TikTok",
        "LinkedIn Insight": "LinkedIn",
        "Microsoft/Bing": "Bing/Microsoft",
    }

    results = []
    gaps = []
    oks = []
    no_ctas = []
    no_tracking_pages = []
    unverified_pages = []
    gtm_insights = {}
    gtm_platforms = set()
    all_tag_ids = []

    if headed:
        log_info("Режим: headed (видимый браузер) — GTM-теги и Meta-beacon стреляют как в реальном браузере", emoji="🪟")
    with sync_playwright() as p:
        log_debug(f"run: launching chromium headless={not headed}")
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        log_step(f"Открываем {base_url}...", emoji="🌐")
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(3500)
        except Exception as e:
            log_debug(f"run: goto/wait для {base_url} не удался (продолжаем): {e}")

        log_step("Проверяем consent и гео-баннеры...", emoji="🍪")
        try:
            popup_result = handle_popups(page)
            log_debug(f"run: handle_popups result={popup_result}")
            if popup_result.get("cookie_consent") != "not_found":
                print(f"  ✓ Consent: {popup_result.get('cookie_consent')}")
            else:
                print("  ℹ️  Баннеров не обнаружено")
        except Exception as e:
            log_debug(f"run: handle_popups не удался: {e}")
            print("  ℹ️  Баннеров не обнаружено")

        log_step("Поиск тегов (GTM / GA4 / Google Ads)...", emoji="🔍")
        try:
            from gtm_analyzer import find_tag_ids_in_page, \
                download_gtm_container, analyze_js
            all_found_ids = find_tag_ids_in_page(page)
            gtm_container_ids = [x for x in all_found_ids if x.startswith(("GTM-", "GT-"))]
            direct_ga4_ids = [x for x in all_found_ids if x.startswith("G-")]
            direct_ads_ids = [x for x in all_found_ids if x.startswith("AW-")]
            all_tag_ids = all_found_ids
            log_debug(f"run: find_tag_ids → all={all_found_ids} gtm={gtm_container_ids} ga4={direct_ga4_ids} ads={direct_ads_ids}")

            if gtm_container_ids:
                log_success(f"Google Tag Manager: {', '.join(gtm_container_ids)}", emoji="✓")
                for gtm_id in gtm_container_ids:
                    log_debug(f"run: download_gtm_container {gtm_id}")
                    container = download_gtm_container(gtm_id)
                    if container:
                        js_text = container.get("raw_js", json.dumps(container))
                        analysis = analyze_js(js_text)
                        gtm_insights[gtm_id] = analysis
                        log_debug(f"run: GTM {gtm_id} analyze_js → platforms={list(analysis.get('platforms_found', {}).keys())}")
                        for plat in analysis.get("platforms_found", {}):
                            gtm_platforms.add(plat)
                    else:
                        log_debug(f"run: GTM {gtm_id} контейнер не скачался")
                if gtm_platforms:
                    log_success(f"Платформы в GTM: {', '.join(sorted(gtm_platforms))}", emoji="✓")
                gtm_file = scan_path(domain, "gtm.json")
                with open(gtm_file, "w", encoding="utf-8") as f:
                    json.dump(gtm_insights, f, indent=2, ensure_ascii=False)
                log_debug(f"run: gtm insights сохранены в {gtm_file}")
            if direct_ga4_ids:
                log_success(f"GA4 (прямой тег): {', '.join(direct_ga4_ids)}", emoji="✓")
                gtm_platforms.add("Google Analytics GA4")
            if direct_ads_ids:
                log_success(f"Google Ads (прямой тег): {', '.join(direct_ads_ids)}", emoji="✓")
                gtm_platforms.add("Google Ads")
            if not all_tag_ids:
                log_info("Теги Google не найдены — смотрим network напрямую")
        except Exception as e:
            log_warn(f"Поиск тегов не удался: {e}")

        expected_platforms = {GTM_TO_SCAN.get(p_name, p_name) for p_name in gtm_platforms}
        log_debug(f"run: expected_platforms={expected_platforms}")

        for i, item in enumerate(to_scan, 1):
            url = item["url"]
            path = item["path"]
            ptype = item["type"]
            expect = item.get("expect_events", [])
            log_debug(f"run: [{i}/{len(to_scan)}] scan url={url} type={ptype} expect={expect}")

            result = scanner(page, url, ptype, expect, platform=platform)
            result["gtm_expected_platforms"] = list(expected_platforms)
            log_debug(f"run: [{i}] scanner вернул pixel_events={list(result.get('pixel_events', {}).keys())} conv={result.get('conversion_events_found')} cta={len(result.get('cta_elements', []))}")

            if click_mode:
                try:
                    from clicker import click_page, CLICKABLE_TYPES
                    if ptype in CLICKABLE_TYPES:
                        import datetime as _dtc
                        _tsc = _dtc.datetime.now().strftime("%H:%M:%S")
                        log_info(f"[{_tsc}] Clicker: {ptype}...", emoji="🖱")
                        click_result = click_page(page, url, ptype, platform=platform, debug=debug_mode)
                        result["click_result"] = click_result
                        log_debug(f"run: [{i}] click_result clicked={click_result.get('clicked')} conv={click_result.get('conversion_events')} error={click_result.get('error')}")
                        for ev in click_result.get("conversion_events", []):
                            if ev not in result["conversion_events_found"]:
                                result["conversion_events_found"].append(ev)
                        if click_result["conversion_events"]:
                            print(f"       🎯 После клика: {', '.join(click_result['conversion_events'])}")
                        elif click_result["clicked"]:
                            print(f"       ➖ Кликнули, событий нет")
                        if click_result.get("error"):
                            print(f"       ⚠️  {click_result['error']}")
                    else:
                        log_debug(f"run: [{i}] click_mode пропущен — type={ptype} не в CLICKABLE_TYPES")
                except Exception as e:
                    log_warn(f"Clicker error: {e}")

            results.append(result)

            has_any_pixel  = bool(result["pixel_events"])
            has_shopify_px = bool(result.get("shopify_pixel_platforms"))
            has_pixel_id   = bool(result.get("pixel_ids"))          # пиксель пойман по ID (presence — работает в headless)
            has_tracking   = has_any_pixel or has_shopify_px or has_pixel_id
            has_conv       = bool(result["conversion_events_found"])
            has_cta        = bool(result.get("cta_elements")) or result.get("has_iframe_form", False)
            cta_elements   = result.get("cta_elements", [])
            pixel_events_r = result["pixel_events"]
            shopify_plats  = result.get("shopify_pixel_platforms", [])

            log_debug(f"run: [{i}] classify has_tracking={has_tracking} has_conv={has_conv} has_cta={has_cta} pixel_id={has_pixel_id} expected={bool(expected_platforms)}")
            if not has_tracking and not expected_platforms:
                result["status"] = "❌ NO TRACKING"
                no_tracking_pages.append(result)
                log_debug(f"run: [{i}] → NO TRACKING (нет пикселей и нет ожидаемых платформ)")
            elif not has_tracking and expected_platforms:
                result["status"] = "🚨 GAP"
                gaps.append(result)
                log_debug(f"run: [{i}] → GAP (ожидались платформы из GTM, но трекинг не пойман)")
            elif has_tracking and not has_conv:
                external = result.get("external_services", {})
                has_serverside_scheduler = any(
                    svc in external for svc in ("Cal.com", "Calendly", "Acuity", "HubSpot Meetings")
                )
                # Пиксель пойман по ID, но НИ одного события — типично для Meta в headless
                # (beacon подавлён). Не врём «сломано»: пиксель есть, срабатывание не проверяемо.
                pixel_present_no_events = has_pixel_id and not has_any_pixel and not has_shopify_px
                if pixel_present_no_events:
                    ids_str = "; ".join(f"{p}: {', '.join(ids)}" for p, ids in result.get("pixel_ids", {}).items())
                    result["status"] = "⚠️ пиксель установлен, событие в headless не проверено"
                    result["unverified_reason"] = (
                        f"Пиксель(и) обнаружен(ы) по ID ({ids_str}), но конверсионных событий "
                        f"в headless-скане не зафиксировано (Meta-beacon в headless подавляется — "
                        f"типичный случай). Требуется headed-проверка срабатывания."
                    )
                    unverified_pages.append(result)
                    log_debug(f"run: [{i}] → unverified (пиксель по ID, событий нет — headless beacon подавлён)")
                elif platform == "shopify" and has_serverside_scheduler:
                    svc_names = [s for s in external if s in ("Cal.com", "Calendly", "Acuity", "HubSpot Meetings")]
                    result["status"] = f"⚠️ форма бронирования обнаружена ({', '.join(svc_names)}). Конверсионное событие при загрузке страницы не зафиксировано."
                    unverified_pages.append(result)
                    log_debug(f"run: [{i}] → unverified (shopify + scheduler {svc_names})")
                elif has_cta:
                    result["status"] = "🚨 GAP"
                    gaps.append(result)
                    log_debug(f"run: [{i}] → GAP (трекинг есть, CTA есть, конверсий нет)")
                else:
                    result["status"] = "➖ NO CTA"
                    no_ctas.append(result)
                    log_debug(f"run: [{i}] → NO CTA (трекинг есть, но ни CTA ни конверсий)")
            elif has_conv:
                result["status"] = "✅ OK"
                oks.append(result)
                log_debug(f"run: [{i}] → OK (конверсионные события зафиксированы)")
            else:
                result["status"] = "➖ NO CTA"
                no_ctas.append(result)
                log_debug(f"run: [{i}] → NO CTA (fallthrough)")

            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            print(f"\n  [{i:>2}/{len(to_scan)}] {path}  [{_ts}]")
            print(f"  {'─' * 55}")

            active_platforms = {}
            for plat, evts in pixel_events_r.items():
                non_noise = [e["event"] for e in evts if not e["is_noise"]]
                noise     = [e["event"] for e in evts if e["is_noise"]]
                active_platforms[plat] = {"source": "direct", "events": non_noise, "noise": noise}
            for plat in shopify_plats:
                if plat not in active_platforms:
                    active_platforms[plat] = {"source": "shopify-worker", "events": [], "noise": ["PageView"]}

            def src_tag(plat):
                info = active_platforms.get(plat)
                if not info: return ""
                return " (Shopify worker)" if info["source"] == "shopify-worker" else ""

            if cta_elements:
                print(f"  CTA кнопки:    {', '.join(cta_elements[:5])}")
            elif result.get("has_iframe_form"):
                iframe_forms = result.get("iframe_forms", [])
                print(f"  CTA кнопки:    [iframe форма: {', '.join(iframe_forms[:2])}]")
            else:
                print(f"  CTA кнопки:    не найдены")

            print()

            has_gtm = bool([x for x in all_tag_ids if x.startswith(("GTM-", "GT-"))])
            has_ga4 = "Google Analytics" in active_platforms or bool([x for x in all_tag_ids if x.startswith("G-")])
            has_ads = "Google Ads" in active_platforms or bool([x for x in all_tag_ids if x.startswith("AW-")])
            print(f"  Google tools:  GTM {'✅' if has_gtm else '❌'}   GA4 {'✅' if has_ga4 else '❌'}   Google Ads {'✅' if has_ads else '❌'}")

            OTHER_PLATFORMS = ["Meta", "TikTok", "Bing/Microsoft", "LinkedIn", "Snapchat", "Pinterest"]
            plat_parts = [f"{pl} {'✅' + src_tag(pl) if pl in active_platforms else '❌'}" for pl in OTHER_PLATFORMS]
            print(f"  Платформы:     {'   '.join(plat_parts[:3])}")
            if len(plat_parts) > 3:
                print(f"                 {'   '.join(plat_parts[3:])}")
            print()

            # Пиксели, пойманные по ID (presence) + предупреждение о дублях (жёлтая зона)
            if result.get("pixel_ids"):
                ids_str = " | ".join(f"{p}: {', '.join(ids)}" for p, ids in result["pixel_ids"].items())
                print(f"  🔵 Пиксели по ID: {ids_str}")
            if result.get("duplicate_pixels"):
                print(f"  ⚠️  ДУБЛЬ ПИКСЕЛЯ: {', '.join(result['duplicate_pixels'])} — найдено >1 ID одной платформы. Возможна дублирующая установка → двойной счёт событий. Проверить вручную.")

            fired_events = [(ev, plat) for plat, info in active_platforms.items()
                            for ev in info["events"] + info["noise"]]
            if fired_events:
                for ev, plat in fired_events:
                    print(f"  События:       {ev} → {plat}")
            else:
                print(f"  События:       не зафиксированы")

            missing_ev = result.get("missing_events", [])
            if missing_ev and result.get("status") != "❌ NO TRACKING":
                for ev in missing_ev:
                    print(f"  {ev}: не зафиксирован при загрузке")

            ext = result.get("external_services", {})
            conv_svcs = [s for s in ext if s not in ANALYTICS_TOOLS]
            anal_svcs = [s for s in ext if s in ANALYTICS_TOOLS]
            if conv_svcs:
                print(f"\n  Внешние:       {', '.join(conv_svcs)}")
            if anal_svcs:
                print(f"  Доп. аналитика: {', '.join(anal_svcs)}")

            print(f"  → {result['status']}")
            time.sleep(0.3)

        browser.close()

    n_unverified = len(unverified_pages)
    n_real_gaps  = len(gaps)
    log_debug(f"run: итоги — ok={len(oks)} gaps={n_real_gaps} unverified={n_unverified} no_tracking={len(no_tracking_pages)} no_cta={len(no_ctas)}")

    print()
    log_header("РЕЗУЛЬТАТ")
    print(f"  ✅ OK  (CTA + Events):           {len(oks)}")
    print(f"  🚨 GAP (пиксель, нет конверсий): {n_real_gaps}")
    if n_unverified:
        print(f"  ⚠️  Форма найдена, событие не зафиксировано: {n_unverified}")
    print(f"  ❌ NO TRACKING (пикселей нет):   {len(no_tracking_pages)}")
    print(f"  ➖ No CTA:                        {len(no_ctas)}")

    if no_tracking_pages:
        print(f"\n❌ NO TRACKING — пиксели не установлены на сайте")
        print(f"   Ни Meta Pixel, ни GA4, ни GTM не обнаружены.")
        print(f"   Весь рекламный трафик идёт вслепую.")

    all_external = {}
    for r in results:
        for svc, info in r.get("external_services", {}).items():
            all_external.setdefault(svc, []).append(r["path"])

    conv_external = {s: pg for s, pg in all_external.items() if s not in ANALYTICS_TOOLS}
    anal_external = {s: pg for s, pg in all_external.items() if s in ANALYTICS_TOOLS}

    unverified_paths = {r["path"] for r in unverified_pages}

    if conv_external:
        # Не показываем Cal.com/Calendly для unverified страниц — там это ожидаемо
        filtered_conv = {}
        for svc, pages in conv_external.items():
            filtered_pages = [p for p in pages if p not in unverified_paths]
            if filtered_pages:
                filtered_conv[svc] = filtered_pages
        if filtered_conv:
            print(f"\n🔗 ВНЕШНИЕ СЕРВИСЫ — конверсии вне сайта (attribution потерян):")
            for svc, pages in filtered_conv.items():
                print(f"   {svc:<25} на: {', '.join(pages[:3])}")

    if anal_external:
        print(f"\n📊 ПОВЕДЕНЧЕСКАЯ АНАЛИТИКА:")
        for svc, pages in anal_external.items():
            print(f"   {svc:<25} на: {', '.join(pages[:2])}")

    if gaps:
        print(f"\n🚨 GAPS — страницы где есть CTA но нет событий:")
        for r in gaps:
            label = get_page_priority_label(
                next((p["priority"] for p in to_scan if p["url"] == r["url"]), 5)
            )
            print(f"\n  {label} {r['path']}")
            if r.get("cta_elements"):
                print(f"    CTA кнопки:   {', '.join(r['cta_elements'][:4])}")
            fired = []
            for plat, evts in r.get("pixel_events", {}).items():
                names = [e["event"] for e in evts]
                if names:
                    fired.append(f"{', '.join(names)} → {plat}")
            for plat in r.get("shopify_pixel_platforms", []):
                if plat not in r.get("pixel_events", {}):
                    fired.append(f"PageView → {plat}")
            print(f"    При загрузке: {' | '.join(fired) if fired else 'ничего не зафиксировано'}")
            for ev in r.get("missing_events", []):
                print(f"    {ev}: не зафиксирован при загрузке")

    if unverified_pages:
        print(f"\n⚠️  ФОРМЫ БРОНИРОВАНИЯ — событие не зафиксировано:")
        for r in unverified_pages:
            ext = list(r.get("external_services", {}).keys())
            svc = [s for s in ext if s in ("Cal.com", "Calendly", "Acuity", "HubSpot Meetings")]
            print(f"\n  {r['path']}")
            print(f"    {r['status']}")

    if oks:
        print(f"\n✅ OK — страницы с корректным tracking:")
        for r in oks:
            print(f"  {r['path']}")
            for ev in r["conversion_events_found"]:
                print(f"    → {ev}")

    output = {
        "base_url": base_url,
        "scanned": len(results),
        "sitemap_total": len(step1.get("classified", [])),
        "sitemap_poi": len(step1.get("to_scan", step1.get("classified", []))),
        "sitemap_deduped": len(results),
        "lang_removed": step1.get("lang_removed", 0),
        "lang_prefixes": step1.get("lang_prefixes", []),
        "sitemap_categories": {
            p["type"]: sum(1 for x in step1.get("classified", []) if x.get("type") == p["type"])
            for p in step1.get("classified", [])
        } if step1.get("classified") else {},
        "gaps": len(gaps),
        "oks": len(oks),
        "no_ctas": len(no_ctas),
        "no_tracking": len(no_tracking_pages),
        "unverified": len(unverified_pages),
        "gtm_platforms": list(expected_platforms),
        "external_services": all_external,
        "gap_pages": gaps,
        "ok_pages": oks,
        "no_tracking_pages": no_tracking_pages,
        "unverified_pages": unverified_pages,
        "all_pages": results,
    }

    filename = scan_path(domain, f"{domain}_step2.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print()
    log_success(f"Сохранено: {filename}", emoji="💾")

    step2_file = scan_path(domain, f"{domain}_step2.json")
    print()
    log_header("💡 Следующий шаг:")
    print(f"     python report.py {step2_file}")
    print()

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TNC Step 2 — Page Scanner")
    parser.add_argument("step1_file", help="JSON файл из Step 1")
    parser.add_argument("--priority", type=int, default=2)
    parser.add_argument("--url", type=str, default=None)
    parser.add_argument("--debug", action="store_true", default=False)
    parser.add_argument("--click", action="store_true", default=False)
    parser.add_argument("--headed", action="store_true", default=False,
                        help="Видимый браузер — ловит Meta/GA4-события, которые headless подавляет")
    parser.add_argument("--quiet", action="store_true", default=False,
                        help="Приглушить: показывать только INFO+ (скрыть DEBUG)")
    args = parser.parse_args()

    import log
    if args.debug:
        log.set_level("DEBUG")  # --debug = и клик-дебаг clicker'а, и полный лог-поток
    if args.quiet:
        log.set_level("INFO")

    _log_path = setup_logging(
        json.load(open(args.step1_file, encoding="utf-8")).get("base_url", "unknown"), step="step2"
    )
    run(args.step1_file, max_priority=args.priority, only_url=args.url,
        debug_mode=args.debug, click_mode=args.click, headed=args.headed)
    print()
    log_info(f"Лог: {_log_path}", emoji="📝")
