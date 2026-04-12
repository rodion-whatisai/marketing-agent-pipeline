"""
TNC Pipeline — Step 2: Pages of Interest Scanner
=================================================
Берёт результат Step 1, открывает каждую страницу браузером,
определяет CTA элементы и перехватывает pixel events.

Запуск:
    python step2_scan.py scans/bandago.com/bandago.com_step1.json
    python step2_scan.py scans/bandago.com/bandago.com_step1.json --priority 1
    python step2_scan.py scans/bandago.com/bandago.com_step1.json --priority 2
"""

import sys
import json
import time
import argparse
import re
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

from page_classifier import classify_page_content, get_page_priority_label
from popup_handler import handle_popups
from utils import get_scan_dir, scan_path, setup_logging, HEADERS


# ─── Pixel rules ─────────────────────────────────────────────────────────────

PIXEL_RULES = {
    "Meta": {
        "domains": ["facebook.com/tr", "connect.facebook.net/en_US/fbevents"],
        "event_param": "ev",
    },
    "Google Analytics": {
        "domains": ["analytics.google.com/g/collect", "google-analytics.com/collect"],
        "event_param": "en",
    },
    "Google Ads": {
        "domains": ["googleadservices.com/pagead/conversion",
                    "google.com/pagead/1p-conversion"],
        "event_param": None,
    },
    "Bing/Microsoft": {
        "domains": ["bat.bing.com/action", "bat.bing.com/p/action"],
        "event_param": "ea",
    },
    "LinkedIn": {
        "domains": ["px.ads.linkedin.com", "snap.licdn.com"],
        "event_param": "conversionId",
    },
    "TikTok": {
        "domains": ["analytics.tiktok.com/api/v2/pixel"],
        "event_param": "event",
    },
}

# ─── Shopify web-pixels detector ────────────────────────────────────────────

SHOPIFY_PIXEL_PLATFORMS = {
    # app ID → платформа
    "550306007":  "Meta",       # Meta Pixel app
    "2179629271": "Google Analytics",
    "96403671":   "TikTok",
    "136216791":  "Pinterest",
}

SHOPIFY_PIXEL_NAMES = {
    # keyword в URL → платформа
    "facebook":   "Meta",
    "meta":       "Meta",
    "google":     "Google Analytics",
    "tiktok":     "TikTok",
    "pinterest":  "Pinterest",
    "bing":       "Bing/Microsoft",
    "microsoft":  "Bing/Microsoft",
    "linkedin":   "LinkedIn",
    "snapchat":   "Snapchat",
}


def detect_shopify_pixel_platforms(web_pixel_urls: list, web_pixel_bodies: dict = None) -> list:
    """Определяет платформы из Shopify web-pixels URLs и JS контента."""
    found = set()
    bodies = web_pixel_bodies or {}

    for url in web_pixel_urls:
        # По app ID в URL
        for app_id, platform in SHOPIFY_PIXEL_PLATFORMS.items():
            if app_id in url:
                found.add(platform)

        # По ключевым словам в URL
        url_lower = url.lower()
        for keyword, platform in SHOPIFY_PIXEL_NAMES.items():
            if keyword in url_lower:
                found.add(platform)

        # По содержимому JS файла
        body = bodies.get(url, "")
        if body:
            if "fbevents.js" in body or "facebook.net" in body:
                found.add("Meta")
            if "googletagmanager" in body or "google-analytics" in body or "gtag" in body:
                found.add("Google Analytics")
            if "tiktok" in body.lower() or "ttq.load" in body:
                found.add("TikTok")
            if "pinterest" in body.lower() or "pintrk" in body:
                found.add("Pinterest")
            if "bing" in body.lower() or "bat.bing" in body:
                found.add("Bing/Microsoft")
            if "snap.com" in body or "snapchat" in body.lower():
                found.add("Snapchat")

    return list(found)


# Tier 1 — критичные конверсии
CONVERSION_EVENTS_TIER1 = {
    "Meta": ["Purchase", "Lead", "InitiateCheckout", "AddToCart",
             "CompleteRegistration", "Schedule", "Contact", "AddPaymentInfo"],
    "Google Analytics": ["purchase", "begin_checkout", "add_to_cart",
                         "generate_lead", "form_submit", "conversion"],
    "Google Ads": ["conversion"],
    "Bing/Microsoft": ["purchase", "lead", "conversion"],
    "TikTok": ["Purchase", "AddToCart", "InitiateCheckout", "PlaceAnOrder"],
}

# Tier 2 — частичный трекинг
CONVERSION_EVENTS_TIER2 = {
    "Meta": ["ViewContent", "Search", "Subscribe"],
    "Google Analytics": ["view_item", "view_item_list", "search",
                         "select_item", "view_promotion"],
    "Google Ads": [],
    "Bing/Microsoft": [],
    "TikTok": ["ViewContent"],
}

CONVERSION_EVENTS = CONVERSION_EVENTS_TIER1

NOISE_EVENTS = {
    "Meta": ["PageView", "fired"],
    "Google Analytics": [
        "gtm.init", "gtm.init_consent", "gtm.js", "fired",
        "page_view", "user_engagement", "session_start", "first_visit",
        "scroll", "click", "view_item_list",
        "form_start", "form_close",
    ],
    "Google Ads": [],
    "Bing/Microsoft": ["fired"],
    "TikTok": ["fired"],
    "LinkedIn": ["fired"],
}


def get_event_from_url(url: str, platform: str) -> str:
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        rule = PIXEL_RULES.get(platform, {})
        ep = rule.get("event_param")
        if ep and ep in params:
            return params[ep][0]
    except Exception:
        pass
    return "fired"


def is_conversion_event(platform: str, event: str) -> bool:
    conv = CONVERSION_EVENTS_TIER1.get(platform, [])
    return any(c.lower() in event.lower() for c in conv)


def is_partial_event(platform: str, event: str) -> bool:
    conv = CONVERSION_EVENTS_TIER2.get(platform, [])
    return any(c.lower() in event.lower() for c in conv)


def is_noise_event(platform: str, event: str) -> bool:
    noise = NOISE_EVENTS.get(platform, [])
    return event in noise


# ─── Внешние сервисы ─────────────────────────────────────────────────────────

EXTERNAL_SERVICES = {
    "Calendly":          ["calendly.com"],
    "Acuity":            ["acuityscheduling.com", "squarespacescheduling.com"],
    "HubSpot Meetings":  ["meetings.hubspot.com", "meetings.hs.com"],
    "Cal.com":           ["cal.com/"],
    "Tidycal":           ["tidycal.com"],
    "SimplyBook":        ["simplybook.me", "simplybook.it"],
    "Doodle":            ["doodle.com"],
    "Setmore":           ["setmore.com"],
    "Typeform":          ["typeform.com"],
    "Jotform":           ["jotform.com"],
    "Google Forms":      ["docs.google.com/forms", "forms.gle"],
    "Tally":             ["tally.so"],
    "Paperform":         ["paperform.co"],
    "HubSpot Forms":     ["hsforms.com", "hsforms.net"],
    "ActiveCampaign":    ["activehosted.com"],
    "Intercom":          ["intercom.io", "widget.intercom.io"],
    "Drift":             ["drift.com", "js.driftt.com"],
    "Crisp":             ["crisp.chat"],
    "Tidio":             ["tidio.co"],
    "Zendesk":           ["zendesk.com/embeddable"],
    "Freshchat":         ["freshchat.com", "wchat.freshchat.com"],
    "Stripe":            ["js.stripe.com", "checkout.stripe.com"],
    "Paddle":            ["paddle.com"],
    "Gumroad":           ["gumroad.com"],
    "Pipedrive":         ["pipedrivewebforms.com"],
    "Microsoft Clarity":  ["clarity.ms/collect", "clarity.ms/s/"],
}


# Сервисы поведенческой аналитики — не конверсионные, отдельная категория
ANALYTICS_TOOLS = {
    "Microsoft Clarity",
    "Hotjar",
    "FullStory",
    "Lucky Orange",
}


def detect_external_services(html: str, requests_urls: list = None) -> dict:
    found = {}
    html_lower = html.lower()
    for service, domains in EXTERNAL_SERVICES.items():
        for domain in domains:
            if domain in html_lower:
                found[service] = {"detected_via": "html", "domain": domain}
                break
    if requests_urls:
        for req_url in requests_urls:
            req_lower = req_url.lower()
            for service, domains in EXTERNAL_SERVICES.items():
                if service not in found:
                    for domain in domains:
                        if domain in req_lower:
                            found[service] = {"detected_via": "network", "domain": domain}
                            break
    return found


# ─── CTA Detection (JS-based, platform-aware) ────────────────────────────────

_CTA_JS = """
() => {
    // ── 1. Шумовые контейнеры — header, nav, footer, Shopify drawers/modals ──
    const NOISE_SELECTORS = [
        'header', 'nav', 'footer',
        '[id*="header" i]', '[class*="header" i]',
        '[id*="navbar" i]', '[class*="navbar" i]',
        '[id*="footer" i]', '[class*="footer" i]',
        '[id*="site-nav" i]', '[class*="site-nav" i]',
        // Shopify: cart drawer, mobile menu, search modal, overlays
        '#CartDrawer', '[id*="cart-drawer" i]', '[class*="cart-drawer" i]',
        '#cart-notification', '[class*="cart-notification" i]',
        '#MobileMenu', '[id*="mobile-menu" i]', '[class*="mobile-menu" i]',
        '[class*="menu-drawer" i]', '[id*="menu-drawer" i]',
        '.predictive-search', '#predictive-search',
        '[class*="search-modal" i]', '[id*="search-modal" i]',
        'modal-dialog', '[class*="modal" i]',
        '[class*="drawer" i]', '[class*="overlay" i]',
        // Cookie/consent banners
        '[class*="cookie" i]', '[id*="cookie" i]',
        '[class*="consent" i]', '[id*="consent" i]',
        '[class*="gdpr" i]', '[id*="gdpr" i]',
        // Announcement bars
        '[class*="announcement" i]', '[id*="announcement" i]',
        // Breadcrumbs, pagination
        '[class*="breadcrumb" i]', '[class*="pagination" i]',
        // Accessibility skip-links
        '[class*="skip" i]',
        // Google Maps — все кнопки внутри карты (language/version независимо)
        '[class*="gm-style"]',
    ];

    const noiseNodes = new Set();
    NOISE_SELECTORS.forEach(sel => {
        try { document.querySelectorAll(sel).forEach(el => noiseNodes.add(el)); }
        catch(e) {}
    });

    function isInNoise(el) {
        let node = el.parentElement;
        while (node && node !== document.body) {
            if (noiseNodes.has(node)) return true;
            node = node.parentElement;
        }
        return false;
    }

    // ── 2. Основная контентная зона — ищем main, или fallback ──
    const MAIN_SELECTORS = [
        'main[id]', 'main',
        '#MainContent', '#main-content', '#main', '#content',
        '[id="MainContent"]',
        '.main-content', '.page-content', '.content-for-layout',
        // Shopify-специфичные
        '[id^="shopify-section-main"]',
        '.shopify-section:not([id*="header"]):not([id*="footer"]):not([id*="announcement"])',
        // Fallback по контенту
        'article', '.product__info-container', '.product-form',
        '.collection__grid', '.product-grid', '.page-width',
    ];

    let mainZone = null;
    for (const sel of MAIN_SELECTORS) {
        try {
            const el = document.querySelector(sel);
            if (el && el.offsetHeight > 50) { mainZone = el; break; }
        } catch(e) {}
    }

    // ── 3. Тексты-шум — даже если кнопка вне nav ──
    const SKIP_TEXTS = new Set([
        // UI/modal controls
        'close', 'ok', 'okay', 'cancel', 'dismiss', 'skip', 'back',
        // Cookie
        'accept', 'accept all', 'reject all', 'decline', 'allow', 'deny',
        'agree', 'i agree', 'got it', 'save preferences', 'necessary only',
        'accept cookies', 'reject cookies', 'manage cookies', 'cookie settings',
        // Shopify UI artefacts
        'icon-x', 'icon-hamburger', 'icon-search', 'icon-filter',
        'icon-account', 'icon-cart', 'icon-close', 'icon-arrow',
        'close cart', 'close menu', 'open cart', 'open menu',
        'site navigation', 'check out',
        // Nav/footer links
        'search', 'cart', 'menu', 'home', 'help center',
        'privacy policy', 'terms of service',
        // Generic browsing
        'view all', 'see all', 'load more', 'show more', 'more', 'next',
        'continue', 'no thanks', 'maybe later',
        'continue shopping', 'return to store',
        // Social/sharing
        'share', 'follow', 'print',
        // Shopify product page UI — не CTA
        'zoom', 'zoom in', 'zoom out',
        'decrease quantity', 'increase quantity', 'reduce',
        'product details', 'description', 'details',
        'size guide', 'size chart',
        'write a review', 'reviews', 'questions',
        // Slider/carousel controls
        'view slide 1', 'view slide 2', 'view slide 3', 'view slide 4',
        'view slide 5', 'view slide 6', 'view slide 7', 'view slide 8',
        'previous slide', 'next slide', 'previous', 'pause', 'play',
        // Shopify collection navigation links — не CTA
        'filter', 'sort', 'grid view', 'list view',
        // Price buttons (Shopify price variant selector)
    ]);

    function getButtonText(el) {
        // aria-label > value > visible text (первая непустая строка)
        const aria = (el.getAttribute('aria-label') || '').trim();
        if (aria.length > 1 && aria.length < 80) return aria;

        const val = (el.getAttribute('value') || '').trim();
        if (val.length > 1 && val.length < 80) return val;

        const raw = (el.innerText || el.textContent || '').trim();
        const lines = raw.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
        return lines[0] || '';
    }

    function isVisible(el) {
        const s = window.getComputedStyle(el);
        // display:none — дропаем всегда
        if (s.display === 'none') return false;
        // Hover-кнопки на карточках коллекций (Quick view, Add to cart) живут с opacity:0
        // до mouseover — они валидный CTA, не дропаем по opacity/visibility
        const isHoverCta = el.closest(
            '.card__content, .product-card, [class*="card"], ' +
            '[class*="product-item"], [class*="collection-item"], ' +
            '[class*="card-wrapper"], .card'
        ) !== null;
        if (s.visibility === 'hidden' && !isHoverCta) return false;
        // opacity не фильтруем — hover-кнопки имеют opacity:0 до наведения
        const r = el.getBoundingClientRect();
        // Для hover-кнопок допускаем нулевой rect (position:absolute вне viewport до hover)
        if (!isHoverCta && r.width === 0 && r.height === 0) return false;
        return true;
    }

    // ── 4. Селекторы кнопок — от специфичных к общим ──
    const BUTTON_SELECTORS = [
        // Shopify product form
        '[name="add"]', '[data-add-to-cart]',
        '.product-form__submit', '.shopify-payment-button button',
        // Form submits
        'button[type="submit"]', 'input[type="submit"]', 'input[type="button"]',
        // CTA классы
        'a.button', 'a.btn',
        '[class*="btn-primary"]', '[class*="btn-cta"]', '[class*="-cta"]',
        '[class*="cta-"]', '.book-now', '.buy-now', '.get-started',
        // Любые кнопки внутри form (contact, lead, newsletter)
        'form button',
    ].join(', ');

    // Дополнительно — все голые <button> внутри main zone
    // (Quick view, Add to cart в collection grid — часто без классов и type)
    const MAIN_BUTTON_SELECTOR = 'button';

    const searchRoot = mainZone || document.body;
    let candidates;
    try {
        const specific = Array.from(searchRoot.querySelectorAll(BUTTON_SELECTORS));
        // Голые кнопки — только если есть main zone, иначе слишком много шума
        const allButtons = mainZone
            ? Array.from(mainZone.querySelectorAll(MAIN_BUTTON_SELECTOR))
            : [];
        // Объединяем, дедуплицируем по ссылке
        const seen = new Set(specific);
        allButtons.forEach(el => { if (!seen.has(el)) { seen.add(el); specific.push(el); } });
        candidates = specific;
    } catch(e) { candidates = []; }

    const results = [];
    const seenTexts = new Set();
    const rejectedLog = [];

    Array.from(candidates).forEach(el => {
        const text = getButtonText(el);
        const textLower = text.toLowerCase();

        if (isInNoise(el)) {
            // Найдём какой именно шумовой предок
            let noiseAncestor = null;
            let node = el.parentElement;
            while (node && node !== document.body) {
                if (noiseNodes.has(node)) { noiseAncestor = (node.id || node.className.toString().substring(0,40)); break; }
                node = node.parentElement;
            }
            rejectedLog.push({text: text || '(empty)', reason: 'isInNoise', ancestor: noiseAncestor});
            return;
        }
        if (!isVisible(el)) {
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            rejectedLog.push({text: text || '(empty)', reason: 'notVisible', display: s.display, visibility: s.visibility, opacity: s.opacity, w: r.width, h: r.height});
            return;
        }
        if (!text || text.length < 2 || text.length > 80) {
            rejectedLog.push({text: text || '(empty)', reason: 'badText'});
            return;
        }
        if (SKIP_TEXTS.has(textLower)) {
            rejectedLog.push({text: text, reason: 'skipText'});
            return;
        }
        // Цена как кнопка ($24.00, £50) — Shopify variant/price selector
        if (/^[$£€¥₹]/.test(text.trim())) {
            rejectedLog.push({text: text, reason: 'priceButton'});
            return;
        }
        // Чисто цифровые кнопки (количество, пагинация)
        if (/^[0-9]+$/.test(text.trim())) {
            rejectedLog.push({text: text, reason: 'numericButton'});
            return;
        }
        if (seenTexts.has(textLower)) {
            rejectedLog.push({text: text, reason: 'duplicate'});
            return;
        }
        seenTexts.add(textLower);

        results.push({
            text: text,
            tag: el.tagName.toLowerCase(),
            isFormSubmit: el.closest('form') !== null,
            inMain: mainZone ? mainZone.contains(el) : null,
        });
    });

    return {
        ctas: results,
        debug: {
            mainZone: mainZone ? (mainZone.id || mainZone.className.toString().substring(0, 60)) : null,
            totalCandidates: Array.from(candidates).length,
            rejected: rejectedLog,
        }
    };
}
"""


def _detect_cta_elements(page, platform: str = "unknown") -> list:
    """
    JS-based CTA detection.
    Исключает header/nav/footer/drawers/modals через DOM-обход.
    Ищет кнопки только в main-контентной зоне.
    Возвращает список строк (тексты CTA).
    """
    try:
        result = page.evaluate(_CTA_JS)
        ctas = result.get("ctas", [])
        debug = result.get("debug", {})

        # Логируем debug если ничего не нашли — помогает диагностировать
        if not ctas:
            main_zone = debug.get("mainZone")
            total = debug.get("totalCandidates", 0)
            rejected = debug.get("rejected", [])
            if total > 0:
                print(f"       ℹ️  CTA: 0 после фильтрации (кандидатов: {total}, main: {main_zone})")
                for r in rejected[:5]:
                    reason = r.get("reason", "?")
                    text = r.get("text", "")
                    if reason == "isInNoise":
                        print(f"         ✗ [{reason}] '{text}' → предок: {r.get('ancestor')}")
                    elif reason == "notVisible":
                        print(f"         ✗ [{reason}] '{text}' → display:{r.get('display')} vis:{r.get('visibility')} op:{r.get('opacity')} w:{r.get('w')} h:{r.get('h')}")
                    else:
                        print(f"         ✗ [{reason}] '{text}'")

        # Для Shopify — приоритизируем кнопки в main-зоне
        if platform == "shopify":
            in_main = [c["text"] for c in ctas if c.get("inMain")]
            not_in_main = [c["text"] for c in ctas if not c.get("inMain")]
            ordered = in_main + not_in_main
        else:
            ordered = [c["text"] for c in ctas]

        return ordered[:8]

    except Exception as e:
        # Fallback: ничего не сломалось, просто не нашли
        print(f"       ⚠️  CTA evaluate error: {e}")
        return []


def scan_page(page, url: str, page_type: str, expect_events: list, platform: str = "unknown") -> dict:
    """Сканирует одну страницу — события + CTA элементы."""

    pixel_events = {}
    all_html_parts = []
    web_pixel_urls = []   # Shopify web-pixels URLs для детектора
    web_pixel_bodies = {} # JS контент web-pixels файлов

    def on_request(request):
        req_url = request.url
        # Shopify web-pixels — собираем для детектора платформ
        if "/web-pixels" in req_url:
            web_pixel_urls.append(req_url)
        for platform, rules in PIXEL_RULES.items():
            for domain in rules["domains"]:
                if domain in req_url:
                    event = get_event_from_url(req_url, platform)
                    pixel_events.setdefault(platform, [])
                    entry = {
                        "event": event,
                        "is_conversion": is_conversion_event(platform, event),
                        "is_partial": is_partial_event(platform, event),
                        "is_noise": is_noise_event(platform, event),
                    }
                    if not any(e["event"] == event for e in pixel_events[platform]):
                        pixel_events[platform].append(entry)
                    break

    def on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            url = response.url
            if "javascript" in ct or "html" in ct:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="ignore")
                    all_html_parts.append(text)
                    # Сохраняем JS контент web-pixels файлов
                    if "/web-pixels" in url:
                        web_pixel_bodies[url] = text
                except Exception:
                    pass
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    errors = []
    # Shopify грузит пиксели через web-pixels-manager — нужно больше времени
    _wait_ms = 3000 if platform == "shopify" else 1500
    _wait_ms_fallback = 2000 if platform == "shopify" else 1000
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(_wait_ms)
    except Exception as e:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(_wait_ms_fallback)
        except Exception as e2:
            errors.append(str(e2)[:100])

    try:
        main_html = page.content()
        all_html_parts.append(main_html)
    except Exception:
        main_html = ""

    combined_html = "\n".join(all_html_parts)
    content_analysis = classify_page_content(combined_html, page)
    external_services = detect_external_services(combined_html)

    # Shopify web-pixels — определяем реальные платформы из URLs + JS + HTML
    shopify_pixel_platforms = []
    if platform == "shopify":
        if web_pixel_urls:
            shopify_pixel_platforms = detect_shopify_pixel_platforms(web_pixel_urls, web_pixel_bodies)
        # Fallback: ищем pixel ID прямо в HTML
        if "Meta" not in shopify_pixel_platforms:
            import re as _re
            if _re.search(r'fbevents\.js|facebook\.net/en_US|"pixelId"\s*:\s*"\d{10,}"', combined_html):
                shopify_pixel_platforms.append("Meta")
        # Добавляем как PageView в pixel_events если нашли через web-pixels
        for sp in shopify_pixel_platforms:
            if sp not in pixel_events:
                pixel_events[sp] = [{"event": "PageView", "is_conversion": False, "is_partial": False, "is_noise": True}]

    cta_elements = _detect_cta_elements(page, platform=platform)

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)

    conversion_events_found = []
    partial_events_found = []
    noise_only = True
    for platform, events in pixel_events.items():
        for ev in events:
            if ev["is_conversion"]:
                conversion_events_found.append(f"{platform}:{ev['event']}")
                noise_only = False
            elif ev.get("is_partial"):
                partial_events_found.append(f"{platform}:{ev['event']}")
                noise_only = False
            elif not ev["is_noise"]:
                noise_only = False

    missing_events = []
    for expected in expect_events:
        found = False
        for platform, events in pixel_events.items():
            if any(expected.lower() in e["event"].lower() for e in events):
                found = True
                break
        if not found:
            missing_events.append(expected)

    # has_cta: реальные кнопки из JS-детектора ИЛИ classify_page_content считает страницу значимой
    has_cta = bool(cta_elements) or content_analysis["is_page_of_interest"]
    has_conv = len(conversion_events_found) > 0

    if has_cta and has_conv:
        status = "✅ OK"
    elif has_cta and not has_conv:
        status = "🚨 GAP"
    elif not has_cta:
        status = "➖ NO_CTA"
    else:
        status = "❓ UNKNOWN"

    return {
        "url": url,
        "path": urlparse(url).path or "/",
        "page_type": page_type,
        "status": status,
        "has_cta": has_cta,
        "cta_elements": list(set(cta_elements))[:8],
        "ctas_in_html": {
            k: v[:3] for k, v in content_analysis["ctas"].items() if v
        },
        "forms_count": content_analysis["forms_count"],
        "pixel_events": {
            platform: [e for e in events if not e["is_noise"]]
            for platform, events in pixel_events.items()
            if any(not e["is_noise"] for e in events)
        },
        "conversion_events_found": conversion_events_found,
        "partial_events_found": partial_events_found,
        "missing_events": missing_events,
        "only_noise_events": noise_only,
        "external_services": external_services,
        "shopify_pixel_platforms": shopify_pixel_platforms,
        "errors": errors,
    }


def run(step1_file: str, max_priority: int = 2, only_url: str = None, debug_mode: bool = False, click_mode: bool = False):
    try:
        with open(step1_file, "r", encoding="utf-8") as f:
            step1 = json.load(f)
    except Exception as e:
        print(f"❌ Не могу открыть {step1_file}: {e}")
        sys.exit(1)

    base_url = step1["base_url"]
    domain = urlparse(base_url).netloc
    platform = step1.get("platform", {}).get("platform", "unknown")

    to_scan_raw = step1.get("to_scan", step1.get("classified", []))
    to_scan = [p for p in to_scan_raw if p.get("priority", 5) <= max_priority]
    if only_url:
        # Exact match по path, или частичный по URL если не нашли точного
        exact = [p for p in to_scan if p.get("path", "") == only_url]
        to_scan = exact if exact else [p for p in to_scan if only_url in p.get("url", "")]
        if not to_scan:
            print(f"❌ Страница '{only_url}' не найдена в step1.json")
            return

    print(f"\n{'═' * 65}")
    print(f"  TNC Pipeline — Step 2: Page Scanner")
    print(f"  Target:   {base_url}")
    print(f"  Priority: ≤ {max_priority} ({get_page_priority_label(max_priority)})")
    print(f"  Pages:    {len(to_scan)}")
    print(f"{'═' * 65}\n")

    gtm_insights = {}
    gtm_platforms = set()
    all_tag_ids = []

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        # ── Шаг 1: открываем homepage, закрываем баннеры ────────────
        print(f"🌐 Открываем {base_url}...")
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(3500)  # ждём рендера баннеров
        except Exception as e:
            print(f"  ⚠️  Не удалось открыть: {e}")

        # ── Шаг 2: баннеры и куки ────────────────────────────────────
        print(f"🍪 Проверяем consent и гео-баннеры...")
        try:
            popup_result = handle_popups(page, verbose=True)
            if popup_result["cookie_consent"] != "not_found":
                print(f"  ✓ Consent принят — ждём загрузки тегов ({popup_result['wait_after_ms']}ms)...")
            elif popup_result["geo_modal"] != "not_found":
                print(f"  ✓ Гео-модал закрыт")
            else:
                print(f"  ℹ️  Баннеров не обнаружено")
        except Exception as e:
            print(f"  ⚠️  Popup handler: {e}")

        # ── Шаг 3: ищем теги ПОСЛЕ consent ──────────────────────────
        print(f"🔍 Поиск тегов (GTM / GA4 / Google Ads)...")
        try:
            from gtm_analyzer import find_gtm_ids, find_tag_ids_in_page, download_gtm_container, analyze_js

            # Сначала через Playwright — видим динамически загруженные теги
            all_tag_ids = find_tag_ids_in_page(page)

            # Fallback — статичный HTML через requests
            if not all_tag_ids:
                all_tag_ids = find_gtm_ids(base_url)

            gtm_container_ids = [i for i in all_tag_ids if i.startswith(("GTM-", "GT-"))]
            direct_ga4_ids    = [i for i in all_tag_ids if i.startswith("G-")]
            direct_ads_ids    = [i for i in all_tag_ids if i.startswith("AW-")]

            if gtm_container_ids:
                print(f"  ✓ Google Tag Manager: {', '.join(gtm_container_ids)}")
                for gtm_id in gtm_container_ids:
                    container = download_gtm_container(gtm_id)
                    if container:
                        js_text = container.get("raw_js", json.dumps(container))
                        analysis = analyze_js(js_text)
                        gtm_insights[gtm_id] = analysis
                        for plat in analysis.get("platforms_found", {}):
                            gtm_platforms.add(plat)
                if gtm_platforms:
                    print(f"  ✓ Платформы в GTM: {', '.join(sorted(gtm_platforms))}")
                gtm_file = scan_path(domain, "gtm.json")
                with open(gtm_file, "w", encoding="utf-8") as f:
                    json.dump(gtm_insights, f, indent=2, ensure_ascii=False)

            if direct_ga4_ids:
                print(f"  ✓ GA4 (прямой тег): {', '.join(direct_ga4_ids)}")
                gtm_platforms.add("Google Analytics GA4")

            if direct_ads_ids:
                print(f"  ✓ Google Ads (прямой тег): {', '.join(direct_ads_ids)}")
                gtm_platforms.add("Google Ads")

            if not all_tag_ids:
                print(f"  ℹ️  Теги Google не найдены — смотрим network напрямую")

        except Exception as e:
            print(f"  ⚠️  Поиск тегов не удался: {e}")

        expected_platforms = {GTM_TO_SCAN.get(p, p) for p in gtm_platforms}

        for i, item in enumerate(to_scan, 1):
            url = item["url"]
            path = item["path"]
            ptype = item["type"]
            expect = item.get("expect_events", [])

            result = scan_page(page, url, ptype, expect, platform=platform)
            result["gtm_expected_platforms"] = list(expected_platforms)

            # ── Clicker — если включён флаг --click ──────────────────
            if click_mode:
                try:
                    from clicker import click_page, CLICKABLE_TYPES
                    if ptype in CLICKABLE_TYPES:
                        import datetime as _dtc
                        _tsc = _dtc.datetime.now().strftime("%H:%M:%S")
                        print(f"  [{_tsc}] 🖱  Clicker: {ptype}...")
                        click_result = click_page(page, url, ptype, platform=platform, debug=debug_mode)
                        result["click_result"] = click_result
                        for ev in click_result.get("conversion_events", []):
                            if ev not in result["conversion_events_found"]:
                                result["conversion_events_found"].append(ev)
                        if click_result["conversion_events"]:
                            print(f"       🎯 После клика: {', '.join(click_result['conversion_events'])}")
                        elif click_result["clicked"]:
                            print(f"       ➖ Кликнули, событий нет")
                        if click_result.get("error"):
                            print(f"       ⚠️  {click_result['error']}")
                except Exception as e:
                    print(f"  ⚠️  Clicker error: {e}")

            results.append(result)

            has_any_pixel   = bool(result["pixel_events"])
            has_shopify_px  = bool(result.get("shopify_pixel_platforms"))
            has_tracking    = has_any_pixel or has_shopify_px
            has_conv        = bool(result["conversion_events_found"])
            has_cta         = bool(result["cta_elements"])
            cta_elements    = result["cta_elements"]
            pixel_events_r  = result["pixel_events"]
            shopify_plats   = result.get("shopify_pixel_platforms", [])

            # ── Статус ───────────────────────────────────────────────
            if not has_tracking and not expected_platforms:
                result["status"] = "❌ NO TRACKING"
                no_tracking_pages.append(result)
            elif not has_tracking and expected_platforms:
                result["status"] = "🚨 GAP"
                gaps.append(result)
            elif has_tracking and not has_conv and has_cta:
                result["status"] = "🚨 GAP"
                gaps.append(result)
            elif has_conv:
                result["status"] = "✅ OK"
                oks.append(result)
            else:
                result["status"] = "➖ NO CTA"
                no_ctas.append(result)

            # ── Детальный вывод ──────────────────────────────────────
            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            print(f"\n  [{i:>2}/{len(to_scan)}] {path}  [{_ts}]")
            print(f"  {'─' * 55}")

            # Собираем активные платформы
            active_platforms = {}
            for plat, evts in pixel_events_r.items():
                non_noise = [e["event"] for e in evts if not e["is_noise"]]
                noise     = [e["event"] for e in evts if e["is_noise"]]
                active_platforms[plat] = {
                    "source": "direct",
                    "events": non_noise,
                    "noise": noise,
                }
            for plat in shopify_plats:
                if plat not in active_platforms:
                    active_platforms[plat] = {
                        "source": "shopify-worker",
                        "events": [],
                        "noise": ["PageView"],
                    }

            # Определяем источник для Google tools
            def src_tag(plat):
                info = active_platforms.get(plat)
                if not info: return ""
                return " (Shopify worker)" if info["source"] == "shopify-worker" else " (прямой)"

            # ── CTA ──
            if cta_elements:
                print(f"  CTA кнопки:    {', '.join(cta_elements[:5])}")
            else:
                print(f"  CTA кнопки:    не найдены")

            print()

            # ── Google tools ──
            has_gtm = bool([i for i in all_tag_ids if i.startswith(("GTM-","GT-"))]) if 'all_tag_ids' in dir() else False
            has_ga4 = "Google Analytics" in active_platforms or bool([i for i in (all_tag_ids if 'all_tag_ids' in dir() else []) if i.startswith("G-")])
            has_ads = "Google Ads" in active_platforms or bool([i for i in (all_tag_ids if 'all_tag_ids' in dir() else []) if i.startswith("AW-")])

            gtm_str = f"GTM {'✅' if has_gtm else '❌'}"
            ga4_str = f"GA4 {'✅' if has_ga4 else '❌'}"
            if has_ga4: ga4_str += src_tag("Google Analytics")
            ads_str = f"Google Ads {'✅' if has_ads else '❌'}"
            if has_ads: ads_str += src_tag("Google Ads")
            print(f"  Google tools:  {gtm_str}   {ga4_str}   {ads_str}")

            # ── Платформы ──
            OTHER_PLATFORMS = ["Meta", "TikTok", "Bing/Microsoft", "LinkedIn", "Snapchat", "Pinterest"]
            plat_parts = []
            for plat in OTHER_PLATFORMS:
                if plat in active_platforms:
                    tag = src_tag(plat)
                    plat_parts.append(f"{plat} ✅{tag}")
                else:
                    plat_parts.append(f"{plat} ❌")
            print(f"  Платформы:     {('   '.join(plat_parts[:3]))}")
            if len(plat_parts) > 3:
                print(f"                 {('   '.join(plat_parts[3:]))}")

            print()

            # ── События ──
            fired_events = []
            for plat, info in active_platforms.items():
                for ev in info["events"] + info["noise"]:
                    fired_events.append((ev, plat))

            if fired_events:
                for ev, plat in fired_events:
                    print(f"  События:       {ev} → {plat}")
            else:
                print(f"  События:       не зафиксированы")

            # Ожидаемые но не найденные
            missing_ev = result.get("missing_events", [])
            if missing_ev:
                [print(f"  {ev}: не зафиксирован при загрузке") for ev in missing_ev]

            # ── Доп. аналитика ──
            ext = result.get("external_services", {})
            conv_svcs = [s for s in ext if s not in ANALYTICS_TOOLS]
            anal_svcs = [s for s in ext if s in ANALYTICS_TOOLS]
            if conv_svcs or anal_svcs:
                print()
            if conv_svcs:
                print(f"  Внешние:       {', '.join(conv_svcs)}")
            if anal_svcs:
                print(f"  Доп. аналитика: {', '.join(anal_svcs)}")

            print(f"  → {result['status']}")

            time.sleep(0.3)

        browser.close()

    print(f"\n{'═' * 65}")
    print(f"  РЕЗУЛЬТАТ")
    print(f"{'═' * 65}")
    print(f"  ✅ OK  (CTA + Events):           {len(oks)}")
    print(f"  🚨 GAP (пиксель, нет конверсий): {len(gaps)}")
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

    conv_external = {s: p for s, p in all_external.items() if s not in ANALYTICS_TOOLS}
    anal_external = {s: p for s, p in all_external.items() if s in ANALYTICS_TOOLS}

    if conv_external:
        print(f"\n🔗 ВНЕШНИЕ СЕРВИСЫ — конверсии вне сайта (attribution потерян):")
        for svc, pages in conv_external.items():
            pages_str = ", ".join(pages[:3])
            print(f"   {svc:<25} на: {pages_str}")

    if anal_external:
        print(f"\n📊 ПОВЕДЕНЧЕСКАЯ АНАЛИТИКА — установлена:")
        for svc, pages in anal_external.items():
            pages_str = ", ".join(pages[:2])
            print(f"   {svc:<25} на: {pages_str}")
        print(f"   ℹ️  Есть heatmap/session recording — можно использовать для аргументации аудита")

    if gaps:
        print(f"\n🚨 GAPS — страницы где есть CTA но нет событий:")
        for r in gaps:
            label = get_page_priority_label(
                next((p["priority"] for p in to_scan if p["url"] == r["url"]), 5)
            )
            print(f"\n  {label} {r['path']}")
            if r["cta_elements"]:
                print(f"    CTA кнопки:   {', '.join(r['cta_elements'][:4])}")

            # При загрузке — что реально зафиксировали
            fired = []
            for plat, evts in r.get("pixel_events", {}).items():
                names = [e["event"] for e in evts]
                if names:
                    fired.append(f"{', '.join(names)} → {plat}")
            for plat in r.get("shopify_pixel_platforms", []):
                if plat not in r.get("pixel_events", {}):
                    fired.append(f"PageView → {plat}")
            if fired:
                print(f"    При загрузке: {' | '.join(fired)}")
            else:
                print(f"    При загрузке: ничего не зафиксировано")

            # Не зафиксированные события
            if r["missing_events"]:
                for ev in r["missing_events"]:
                    print(f"    {ev}: не зафиксирован при загрузке")

    if oks:
        print(f"\n✅ OK — страницы с корректным tracking:")
        for r in oks:
            print(f"  {r['path']}")
            for ev in r["conversion_events_found"]:
                print(f"    → {ev}")

    all_external = {}
    for r in results:
        for svc, info in r.get("external_services", {}).items():
            all_external.setdefault(svc, []).append(r["path"])

    output = {
        "base_url": base_url,
        "scanned": len(results),
        # Данные из step1 — сколько страниц найдено и отфильтровано
        "sitemap_total": len(step1.get("classified", [])),
        "sitemap_poi": len(step1.get("to_scan", step1.get("classified", []))),
        "sitemap_deduped": len(results),
        "gaps": len(gaps),
        "oks": len(oks),
        "no_ctas": len(no_ctas),
        "no_tracking": len(no_tracking_pages),
        "gtm_platforms": list(expected_platforms),
        "external_services": all_external,
        "gap_pages": gaps,
        "ok_pages": oks,
        "no_tracking_pages": no_tracking_pages,
        "all_pages": results,
    }

    filename = scan_path(domain, f"{domain}_step2.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Сохранено: {filename}")

    step2_file = scan_path(domain, f"{domain}_step2.json")
    print(f"\n{'═' * 65}")
    print(f"  💡 Следующий шаг:")
    print(f"     python report.py {step2_file}")
    print(f"{'═' * 65}\n")

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TNC Step 2 — Page Scanner")
    parser.add_argument("step1_file", help="JSON файл из Step 1")
    parser.add_argument("--priority", type=int, default=2,
                        help="Максимальный приоритет (1=CRITICAL, 2=HIGH, 3=MEDIUM)")
    parser.add_argument("--url", type=str, default=None,
                        help="Сканировать только одну страницу по URL или пути (напр. /collections/beauty-services)")
    parser.add_argument("--debug", action="store_true", default=False,
                        help="Verbose: все pixel requests + полный CTA rejected log")
    parser.add_argument("--click", action="store_true", default=False,
                        help="Кликать кнопки Add to Cart и продукты для поимки событий")
    args = parser.parse_args()

    _log_path = setup_logging(
        json.load(open(args.step1_file)).get("base_url", "unknown"), step="step2"
    )

    run(args.step1_file, max_priority=args.priority, only_url=args.url,
        debug_mode=args.debug, click_mode=args.click)

    print(f"\n📝 Лог: {_log_path}")
