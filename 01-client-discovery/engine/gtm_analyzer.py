"""
TNC Pipeline — GTM Analyzer
============================
Находит GTM container ID на сайте, скачивает контейнер,
парсит теги, триггеры, переменные.

Запуск:
    python gtm_analyzer.py bandago.com
    python gtm_analyzer.py hipcamp.com
    python gtm_analyzer.py GTM-TWNL2R          # напрямую по ID
"""

import sys
import re
import json
import requests
from urllib.parse import urlparse

from utils import get_scan_dir, scan_path



from utils import HEADERS
from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header


# ─── Поиск GTM ID ─────────────────────────────────────────────────────────────

def find_gtm_ids(base_url: str) -> list:
    """
    Ищет GTM/GA4/Ads ID в исходном HTML сайта.
    Возвращает список всех найденных tag IDs.
    GTM-/GT- = GTM контейнеры (можно скачать и проанализировать)
    G-         = GA4 Measurement ID (прямой тег, без GTM)
    AW-        = Google Ads (прямой тег)
    """
    log_debug(f"find_gtm_ids: start base_url={base_url}")
    ids = set()
    try:
        log_debug(f"find_gtm_ids: GET {base_url} (timeout=15)")
        r = requests.get(base_url, headers=HEADERS, timeout=15)
        html = r.text
        log_debug(f"find_gtm_ids: status={r.status_code} html_len={len(html)}")

        # GTM-XXXXX и GT-XXXXX — контейнеры
        for f in re.findall(r'(?:GTM|GT)-[A-Z0-9]+', html):
            if re.match(r'^(?:GTM|GT)-[A-Z0-9]{4,}$', f):
                log_debug(f"find_gtm_ids: matched GTM/GT container id={f}")
                ids.add(f)

        # G-XXXXXXX — GA4 Measurement ID (прямой gtag.js)
        # Ищем только в контексте gtag/config вызовов или как строковый литерал
        # Реальный ID: G- + буквы/цифры, 6-12 символов, в кавычках или после gtag(
        for f in re.findall(r'(?<=["\' ])(G-[A-Z0-9]{6,12})(?=["\' ])', html):
            # Фильтруем CSS/JS переменные — реальный ID не содержит только буквы
            if re.search(r'[0-9]', f):  # должна быть хотя бы одна цифра
                log_debug(f"find_gtm_ids: matched GA4 id={f}")
                ids.add(f)
            else:
                log_debug(f"find_gtm_ids: skipped G- candidate (no digit) id={f}")

        # AW-XXXXXXXXX — Google Ads Conversion ID (прямой gtag.js)
        for f in re.findall(r'AW-[0-9]{7,}', html):
            log_debug(f"find_gtm_ids: matched Google Ads id={f}")
            ids.add(f)

    except Exception as e:
        log_warn(f"Ошибка при загрузке сайта: {e}")
    log_debug(f"find_gtm_ids: done, found {len(ids)} ids")
    return list(ids)


def find_tag_ids_in_page(page) -> list:
    """
    Ищет GTM/GA4/Ads IDs в уже загруженной Playwright странице.
    Смотрит в window.dataLayer, document.scripts и window объект.
    Используется в Step 2 вместо requests-based find_gtm_ids().
    """
    log_debug("find_tag_ids_in_page: start (Playwright page)")
    import re as _re
    ids = set()
    try:
        # Достаём всё что есть в DOM — скрипты + dataLayer
        log_debug("find_tag_ids_in_page: evaluating DOM scripts + dataLayer")
        js_result = page.evaluate("""
        () => {
            const texts = [];
            // Все script теги
            document.querySelectorAll('script').forEach(s => {
                if (s.src) texts.push(s.src);
                if (s.textContent) texts.push(s.textContent.substring(0, 5000));
            });
            // dataLayer
            if (window.dataLayer) {
                texts.push(JSON.stringify(window.dataLayer).substring(0, 3000));
            }
            // google_tag_manager object
            if (window.google_tag_manager) {
                texts.push(Object.keys(window.google_tag_manager).join(' '));
            }
            return texts.join(' ');
        }
        """)

        # GTM-XXXXX и GT-XXXXX
        for f in _re.findall(r'(?:GTM|GT)-[A-Z0-9]{4,}', js_result):
            log_debug(f"find_tag_ids_in_page: matched GTM/GT container id={f}")
            ids.add(f)

        # G-XXXXXXX (GA4) — требуем цифру
        for f in _re.findall(r'G-[A-Z0-9]{6,12}', js_result):
            if _re.search(r'[0-9]', f):
                log_debug(f"find_tag_ids_in_page: matched GA4 id={f}")
                ids.add(f)
            else:
                log_debug(f"find_tag_ids_in_page: skipped G- candidate (no digit) id={f}")

        # AW-XXXXXXXXX (Google Ads)
        for f in _re.findall(r'AW-[0-9]{7,}', js_result):
            log_debug(f"find_tag_ids_in_page: matched Google Ads id={f}")
            ids.add(f)

    except Exception as e:
        log_debug(f"find_tag_ids_in_page: page.evaluate failed: {e}")

    log_debug(f"find_tag_ids_in_page: done, found {len(ids)} ids")
    return list(ids)


# ─── Загрузка GTM контейнера ──────────────────────────────────────────────────

def download_gtm_container(gtm_id: str) -> dict | None:
    """
    Скачивает GTM контейнер и извлекает JSON конфигурацию.
    GTM публично отдаёт контейнер по стандартному URL.
    """
    log_debug(f"download_gtm_container: start gtm_id={gtm_id}")
    url = f"https://www.googletagmanager.com/gtm.js?id={gtm_id}"
    try:
        log_debug(f"download_gtm_container: GET {url} (timeout=15)")
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            log_error(f"GTM вернул статус {r.status_code}")
            return None

        js = r.text
        log_debug(f"download_gtm_container: status=200 js_len={len(js)}")

        # GTM прячет конфигурацию в переменную data внутри JS
        # Ищем JSON объект с ключами "resource" или "version"
        patterns = [
            r'var data\s*=\s*(\{.+?\})\s*;?\s*\n',
            r'"resource"\s*:\s*(\{.+?"entities":.+?\})\s*[,}]',
            r'data\.push\((\{.+?\})\)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, js, re.DOTALL)
            log_debug(f"download_gtm_container: pattern matched {len(matches)} candidates")
            for match in matches:
                try:
                    # Пробуем распарсить как JSON
                    obj = json.loads(match)
                    if "resource" in obj or "entities" in obj or "macros" in obj:
                        log_debug("download_gtm_container: parsed config JSON via primary patterns")
                        return obj
                except Exception as e:
                    log_debug(f"download_gtm_container: candidate JSON parse failed: {e}")
                    continue

        # Более агрессивный поиск — ищем большой JSON объект
        # GTM обычно содержит macros, rules, tags
        log_debug("download_gtm_container: primary patterns missed, trying aggressive big-JSON search")
        big_json = re.findall(
            r'\{["\']?(?:resource|macros|rules|tags|version)["\']?\s*:.*?\}(?=\s*[;,\n])',
            js, re.DOTALL
        )
        log_debug(f"download_gtm_container: big-JSON search found {len(big_json)} candidates")
        for candidate in big_json:
            try:
                obj = json.loads(candidate)
                if any(k in obj for k in ["macros", "rules", "tags", "resource"]):
                    log_debug("download_gtm_container: parsed config JSON via big-JSON search")
                    return obj
            except Exception as e:
                log_debug(f"download_gtm_container: big-JSON candidate parse failed: {e}")
                continue

        # Fallback — возвращаем сырой JS для анализа паттернами
        log_debug("download_gtm_container: no structured JSON parsed, returning raw_js fallback")
        return {"raw_js": js, "gtm_id": gtm_id}

    except Exception as e:
        log_error(f"Ошибка загрузки контейнера: {e}")
        return None


# ─── Паттерны для анализа сырого JS ──────────────────────────────────────────

# Паттерны для извлечения данных из минифицированного GTM JS
EXTRACTION_PATTERNS = {
    # Google Analytics / GA4
    "ga4_ids": r'"G-[A-Z0-9]+"',
    "ua_ids": r'"UA-\d+-\d+"',

    # Google Ads
    "gads_ids": r'"AW-\d+"',
    "gads_labels": r'"[a-zA-Z0-9_\-]{10,}"(?=.*conversion_label)',

    # Meta / Facebook
    "meta_pixel_ids": r'fbq\s*\(\s*"init"\s*,\s*"(\d{10,})"',
    "meta_pixel_ids2": r'"(\d{14,})"(?=.*facebook)',

    # GTM container IDs (вложенные)
    "gtm_ids": r'"(GTM-[A-Z0-9]+)"',

    # Hotjar
    "hotjar_ids": r'hjid\s*[:=]\s*(\d+)',
    "hotjar_ids2": r'"(\d{6,8})"(?=.*hotjar)',

    # LinkedIn
    "linkedin_ids": r'"(\d{6,})"(?=.*linkedin)',

    # TikTok
    "tiktok_ids": r'"([A-Z0-9]{15,})"(?=.*tiktok)',

    # Conversion triggers — страницы где стреляют конверсии
    "conversion_pages": r'"(/[a-z0-9/_\-]+)"(?=.*(?:conversion|thank|success|confirm|reserve|complete))',

    # Value передача
    "value_params": r'(?:value|revenue|price)\s*[:=]\s*([0-9.]+|["\'][^"\']+["\'])',

    # DataLayer events
    "datalayer_events": r'dataLayer\.push\s*\(\s*\{[^}]*"event"\s*:\s*"([^"]+)"',

    # Custom HTML tags hints
    "custom_scripts": r'<script[^>]*>(.*?)</script>',
}

# Известные платформы и их признаки в JS
PLATFORM_SIGNATURES = {
    "Meta Pixel": [
        r'fbq\s*\(', r'connect\.facebook\.net', r'fbevents\.js',
        r'facebook\.com/tr', r'Meta Pixel',
    ],
    "Google Analytics GA4": [
        r'G-[A-Z0-9]{6,}', r'gtag\s*\(', r'analytics\.google\.com',
        r'google-analytics\.com/g/collect',
    ],
    "Google Ads": [
        r'AW-\d{6,}', r'googleadservices\.com', r'conversion_id',
        r'google\.com/pagead',
    ],
    "LinkedIn Insight": [
        r'snap\.licdn\.com', r'linkedin\.com/li', r'_linkedin_partner_id',
        r'px\.ads\.linkedin\.com',
    ],
    "TikTok Pixel": [
        r'analytics\.tiktok\.com', r'ttq\.', r'TiktokAnalyticsObject',
    ],
    "Hotjar": [
        r'hotjar\.com', r'hjid\s*[:=]', r'hj\s*\(',
    ],
    "Microsoft/Bing": [
        r'bat\.bing\.com', r'uetq\s*=', r'bing\.com/action',
    ],
    "Intercom": [r'intercom\.com', r'intercomSettings'],
    "HubSpot": [r'hubspot\.com', r'hs-scripts', r'hbspt\.'],
    "Drift": [r'drift\.com', r'driftt\.com'],
    "Zendesk": [r'zendesk\.com', r'zopim'],
    "Hotjar": [r'hotjar\.com', r'hjSetting'],
    "Clarity": [r'clarity\.ms', r'microsoft\.com/clarity'],
    "Segment": [r'segment\.com', r'analytics\.js'],
    "Mixpanel": [r'mixpanel\.com', r'mixpanel\.init'],
    "Amplitude": [r'amplitude\.com', r'amplitude\.init'],
    "Klaviyo": [r'klaviyo\.com'],
    "Mailchimp": [r'mailchimp\.com', r'chimpstatic\.com'],
    "Optimizely": [r'optimizely\.com'],
    "VWO": [r'vwo\.com', r'visualwebsiteoptimizer'],
    "Stripe": [r'stripe\.com', r'stripe\.js'],
    "Crisp": [r'crisp\.chat'],
    "Freshchat": [r'freshchat\.com', r'freshworks\.com'],
}

# Признаки конверсионных событий в JS
CONVERSION_EVENT_PATTERNS = {
    "Meta": {
        "Purchase":          [r'fbq\s*\(\s*["\']track["\'],\s*["\']Purchase["\']'],
        "Lead":              [r'fbq\s*\(\s*["\']track["\'],\s*["\']Lead["\']'],
        "InitiateCheckout":  [r'fbq\s*\(\s*["\']track["\'],\s*["\']InitiateCheckout["\']'],
        "ViewContent":       [r'fbq\s*\(\s*["\']track["\'],\s*["\']ViewContent["\']'],
        "AddToCart":         [r'fbq\s*\(\s*["\']track["\'],\s*["\']AddToCart["\']'],
        "CompleteRegistration": [r'fbq\s*\(\s*["\']track["\'],\s*["\']CompleteRegistration["\']'],
        "Search":            [r'fbq\s*\(\s*["\']track["\'],\s*["\']Search["\']'],
        "Contact":           [r'fbq\s*\(\s*["\']track["\'],\s*["\']Contact["\']'],
    },
    "Google Ads": {
        "Conversion":        [r'gtag\s*\(\s*["\']event["\'],\s*["\']conversion["\']',
                              r'google_conversion_id'],
        "Value passed":      [r'["\']value["\']:\s*(?!0\b)([1-9][0-9.]*)',
                              r'send_to.*AW-'],
    },
    "GA4": {
        "purchase":          [r'gtag.*["\']purchase["\']', r'event.*purchase'],
        "begin_checkout":    [r'gtag.*["\']begin_checkout["\']'],
        "generate_lead":     [r'gtag.*["\']generate_lead["\']'],
        "view_item":         [r'gtag.*["\']view_item["\']'],
        "form_submit":       [r'gtag.*["\']form_submit["\']'],
    },
}

# Признаки проблем
PROBLEM_PATTERNS = {
    "Google Ads value=0": r'["\']value["\']:\s*0\b',
    "Hardcoded value":    r'["\']value["\']:\s*[1-9][0-9]*(?:\.[0-9]+)?\b',
    "CMP blocking":       r'OneTrust|cookiebot|usercentrics|axeptio',
    "CAPI hints":         r'conversions_api|server_events|fbcapi|capi',
    "Server-side GTM":    r'sgtm\.|server-side|collect\?v=2.*sst',
}


def analyze_js(js: str) -> dict:
    """Анализирует GTM JS на наличие платформ, событий, проблем."""
    log_debug(f"analyze_js: start js_len={len(js)}")
    results = {
        "platforms_found": {},
        "ids_found": {},
        "conversion_events": {},
        "problems": {},
        "dataLayer_events": [],
        "conversion_pages": [],
    }

    # ── Платформы ────────────────────────────────────────────────
    for platform, patterns in PLATFORM_SIGNATURES.items():
        hits = []
        for pattern in patterns:
            if re.search(pattern, js, re.IGNORECASE):
                hits.append(pattern)
        if hits:
            log_debug(f"analyze_js: platform detected {platform} ({len(hits)} signature hits)")
            results["platforms_found"][platform] = True

    # ── IDs ──────────────────────────────────────────────────────
    id_patterns = {
        "GA4":        r'G-[A-Z0-9]{6,}',
        "UA":         r'UA-\d{6,}-\d+',
        "Google Ads": r'AW-\d{6,}',
        "GTM":        r'GTM-[A-Z0-9]{4,}',
        "Hotjar":     r'(?:hjid|_hjid)\s*[:=,]\s*(\d{6,8})',
        "Meta Pixel": r'fbq\s*\(\s*["\']init["\'],\s*["\'](\d{10,})["\']',
    }

    for name, pattern in id_patterns.items():
        found = re.findall(pattern, js)
        if found:
            unique = list(set(found))
            log_debug(f"analyze_js: ids_found[{name}] = {unique}")
            results["ids_found"][name] = unique

    # ── Конверсионные события ────────────────────────────────────
    for platform, events in CONVERSION_EVENT_PATTERNS.items():
        found_events = []
        for event_name, patterns in events.items():
            for pattern in patterns:
                if re.search(pattern, js, re.IGNORECASE):
                    found_events.append(event_name)
                    break
        if found_events:
            log_debug(f"analyze_js: conversion_events[{platform}] = {found_events}")
            results["conversion_events"][platform] = found_events

    # ── Проблемы ─────────────────────────────────────────────────
    for problem, pattern in PROBLEM_PATTERNS.items():
        if re.search(pattern, js, re.IGNORECASE):
            log_debug(f"analyze_js: problem detected {problem}")
            results["problems"][problem] = True

    # ── DataLayer events ─────────────────────────────────────────
    dl_events = re.findall(
        r'dataLayer\.push\s*\(\s*\{[^}]*["\']event["\'][^}]*["\']([^"\']+)["\']',
        js
    )
    results["dataLayer_events"] = list(set(dl_events))
    log_debug(f"analyze_js: dataLayer_events count={len(results['dataLayer_events'])}")

    # ── Страницы конверсий ───────────────────────────────────────
    conv_pages = re.findall(
        r'["\']([/][a-z0-9/_\-]*(?:thank|success|confirm|reserve|complete|checkout|purchase)[a-z0-9/_\-]*)["\']',
        js, re.IGNORECASE
    )
    results["conversion_pages"] = list(set(conv_pages))[:10]
    log_debug(f"analyze_js: conversion_pages count={len(results['conversion_pages'])}")

    log_debug("analyze_js: done")
    return results


def print_analysis(gtm_id: str, analysis: dict, base_url: str = ""):
    log_debug(f"print_analysis: rendering report for gtm_id={gtm_id} base_url={base_url}")
    print(f"\n{'═' * 65}")
    print(f"  GTM CONTAINER ANALYSIS")
    if base_url:
        print(f"  Site:      {base_url}")
    print(f"  Container: {gtm_id}")
    print(f"{'═' * 65}")

    # ── IDs ──────────────────────────────────────────────────────
    ids = analysis.get("ids_found", {})
    if ids:
        print(f"\n🔑 НАЙДЕННЫЕ ID")
        for platform, id_list in ids.items():
            for id_val in id_list:
                print(f"  {platform:<20} {id_val}")

    # ── Платформы ────────────────────────────────────────────────
    platforms = analysis.get("platforms_found", {})
    if platforms:
        print(f"\n📡 ПЛАТФОРМЫ В КОНТЕЙНЕРЕ ({len(platforms)})")
        for p in sorted(platforms.keys()):
            print(f"  ✓ {p}")

    # ── Конверсионные события ────────────────────────────────────
    conv = analysis.get("conversion_events", {})
    print(f"\n🎯 КОНВЕРСИОННЫЕ СОБЫТИЯ")
    if conv:
        for platform, events in conv.items():
            print(f"  {platform}:")
            for ev in events:
                print(f"    ✓ {ev}")
    else:
        print(f"  ❌ Не найдено ни одного конверсионного события в коде контейнера")

    # ── Проблемы ─────────────────────────────────────────────────
    problems = analysis.get("problems", {})
    if problems:
        print(f"\n⚠️  ОБНАРУЖЕННЫЕ ПРОБЛЕМЫ")
        for problem in problems:
            if problem == "Google Ads value=0":
                print(f"  🚨 {problem} — конверсия передаётся с нулевой ценностью")
            elif problem == "CMP blocking":
                print(f"  ⚠️  {problem} — CMP может блокировать теги до согласия")
            elif problem == "CAPI hints":
                print(f"  ℹ️  {problem} — есть признаки server-side tracking")
            elif problem == "Server-side GTM":
                print(f"  ℹ️  {problem} — используется server-side GTM")
            else:
                print(f"  ℹ️  {problem}")

    # ── DataLayer events ─────────────────────────────────────────
    dl = analysis.get("dataLayer_events", [])
    if dl:
        print(f"\n📊 DATALAYER EVENTS (в коде контейнера)")
        for ev in dl[:15]:
            print(f"  • {ev}")

    # ── Страницы конверсий ───────────────────────────────────────
    pages = analysis.get("conversion_pages", [])
    if pages:
        print(f"\n📍 CONVERSION TRIGGER PAGES")
        for p in pages:
            print(f"  • {p}")

    print(f"\n{'═' * 65}\n")


def run(target: str) -> dict:
    log_debug(f"run: start target={target}")
    # Определяем — это домен или GTM ID
    if target.startswith("GTM-"):
        log_debug("run: target looks like a GTM container ID, skipping site search")
        gtm_ids = [target]
        base_url = ""
        domain = target
    else:
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        domain = urlparse(base_url).netloc
        log_debug(f"run: target is a domain, base_url={base_url} domain={domain}")

        print(f"\n{'═' * 65}")
        print(f"  TNC Pipeline — GTM Analyzer")
        print(f"  Target: {base_url}")
        print(f"{'═' * 65}")

        log_step("Ищу GTM ID на сайте...", emoji="🔍")
        gtm_ids = find_gtm_ids(base_url)

        if not gtm_ids:
            log_error("GTM ID не найден на сайте")
            print(f"  Попробуй передать ID напрямую: python gtm_analyzer.py GTM-XXXXXX")
            return {}

        log_success(f"Найдено: {', '.join(gtm_ids)}")

    all_results = {}

    for gtm_id in gtm_ids:
        log_step(f"Загружаю контейнер {gtm_id}...", emoji="📦")
        container = download_gtm_container(gtm_id)

        if not container:
            log_error(f"Не удалось загрузить контейнер {gtm_id}")
            continue

        # Получаем JS для анализа
        js_text = container.get("raw_js", json.dumps(container))
        log_debug(f"run: analyzing js for {gtm_id} (js_len={len(js_text)})")
        analysis = analyze_js(js_text)
        all_results[gtm_id] = analysis

        print_analysis(gtm_id, analysis, base_url)

    # Сохраняем
    if all_results:
        filename = scan_path(domain, "gtm.json")
        log_debug(f"run: writing {len(all_results)} container result(s) to {filename}")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        log_success(f"Сохранено: {filename}", emoji="💾")

    log_debug(f"run: done, {len(all_results)} container(s) analyzed")
    return all_results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python gtm_analyzer.py bandago.com")
        print("  python gtm_analyzer.py hipcamp.com")
        print("  python gtm_analyzer.py GTM-TWNL2R")
        sys.exit(1)

    run(sys.argv[1])
