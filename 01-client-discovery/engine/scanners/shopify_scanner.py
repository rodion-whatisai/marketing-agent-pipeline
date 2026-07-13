"""
Shopify Scanner
==============
Специфика: web-pixels sandbox, Meta Pixel через pixelId в HTML.
CTA детектор знает про Shopify drawers, cart, mobile menu.
"""

import re
from .base_scanner import (
    base_scan_page, make_listeners, detect_external_services,
    PIXEL_RULES, capture_pixel_hit
)
from log import log_debug

SHOPIFY_PIXEL_PLATFORMS = {
    "550306007":  "Meta",
    "2179629271": "Google Analytics",
    "96403671":   "TikTok",
    "136216791":  "Pinterest",
}

# Маркеры ВЫЗОВОВ per-платформа — НЕ голые слова. Голые подстроки ("snapchat",
# "bing") били в UA-регэкспы рантайма Shopify web-pixels-manager
# (/(chromium|instagram|snapchat)/i, /bingbot/i), который грузится на КАЖДОМ
# магазине → фейковые «Snapchat ✅»/«Bing ✅» в клиентском отчёте (баг A1;
# allbirds: 0 запросов к tr.snapchat.com/bat.bing из ~1200, а в отчёте обе).
# Все маркеры lowercase — тело скрипта приводится к lower() перед матчингом.
# Tested: 2026-07-08 on allbirds.com — Snapchat/Bing исчезли (0 network-запросов);
#         pipsnacks.com — Pinterest жив через маркеры (code-only → warning в отчёте).
SHOPIFY_PIXEL_MARKERS = {
    "Meta":             ("fbevents.js", "fbq(", "connect.facebook.net", "facebook.com/tr"),
    "Google Analytics": ("googletagmanager.com", "google-analytics.com", "gtag("),
    "TikTok":           ("ttq.load", "analytics.tiktok.com"),
    "Pinterest":        ("pintrk", "ct.pinterest.com", "s.pinimg.com"),
    "Bing/Microsoft":   ("bat.bing.com", "uetq"),
    "LinkedIn":         ("lintrk", "snap.licdn.com", "px.ads.linkedin.com"),
    "Snapchat":         ("snaptr(", "tr.snapchat.com", "sc-static.net"),
}

_SHOPIFY_CTA_JS = """
() => {
    const NOISE_SELECTORS = [
        'header', 'nav', 'footer',
        '[id*="header" i]', '[class*="header" i]',
        '[id*="navbar" i]', '[class*="navbar" i]',
        '[id*="footer" i]', '[class*="footer" i]',
        '[id*="site-nav" i]', '[class*="site-nav" i]',
        '#CartDrawer', '[id*="cart-drawer" i]', '[class*="cart-drawer" i]',
        '#cart-notification', '[class*="cart-notification" i]',
        '#MobileMenu', '[id*="mobile-menu" i]', '[class*="mobile-menu" i]',
        '[class*="menu-drawer" i]', '[id*="menu-drawer" i]',
        '.predictive-search', '#predictive-search',
        '[class*="search-modal" i]', '[id*="search-modal" i]',
        'modal-dialog', '[class*="modal" i]',
        '[class*="drawer" i]', '[class*="overlay" i]',
        '[class*="cookie" i]', '[id*="cookie" i]',
        '[class*="consent" i]', '[id*="consent" i]',
        '[class*="gdpr" i]', '[id*="gdpr" i]',
        '[class*="announcement" i]', '[id*="announcement" i]',
        '[class*="breadcrumb" i]', '[class*="pagination" i]',
        '[class*="skip" i]',
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

    const MAIN_SELECTORS = [
        'main[id]', 'main',
        '#MainContent', '#main-content', '#main', '#content',
        '[id="MainContent"]',
        '.main-content', '.page-content', '.content-for-layout',
        '[id^="shopify-section-main"]',
        '.shopify-section:not([id*="header"]):not([id*="footer"]):not([id*="announcement"])',
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

    const SKIP_TEXTS = new Set([
        'close', 'ok', 'okay', 'cancel', 'dismiss', 'skip', 'back',
        'accept', 'accept all', 'reject all', 'decline', 'allow', 'deny',
        'agree', 'i agree', 'got it', 'save preferences', 'necessary only',
        'accept cookies', 'reject cookies', 'manage cookies', 'cookie settings',
        'icon-x', 'icon-hamburger', 'icon-search', 'icon-filter',
        'icon-account', 'icon-cart', 'icon-close', 'icon-arrow',
        'close cart', 'close menu', 'open cart', 'open menu',
        'site navigation', 'check out',
        'search', 'cart', 'menu', 'home', 'help center',
        'privacy policy', 'terms of service',
        'view all', 'see all', 'load more', 'show more', 'more', 'next',
        'continue', 'no thanks', 'maybe later',
        'continue shopping', 'return to store',
        'share', 'follow', 'print',
        'zoom', 'zoom in', 'zoom out',
        'decrease quantity', 'increase quantity', 'reduce',
        'product details', 'description', 'details',
        'size guide', 'size chart',
        'write a review', 'reviews', 'questions',
        'view slide 1', 'view slide 2', 'view slide 3', 'view slide 4',
        'view slide 5', 'view slide 6', 'view slide 7', 'view slide 8',
        'previous slide', 'next slide', 'previous', 'pause', 'play',
        'filter', 'sort', 'grid view', 'list view',
        // Google Maps
        'toggle fullscreen view', 'map camera controls', 'keyboard shortcuts',
        'drag to pan', 'use ctrl + scroll to zoom',
    ]);

    function getButtonText(el) {
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
        if (s.display === 'none') return false;
        const isHoverCta = el.closest(
            '.card__content, .product-card, [class*="card"], ' +
            '[class*="product-item"], [class*="collection-item"], ' +
            '[class*="card-wrapper"], .card'
        ) !== null;
        if (s.visibility === 'hidden' && !isHoverCta) return false;
        const r = el.getBoundingClientRect();
        if (!isHoverCta && r.width === 0 && r.height === 0) return false;
        return true;
    }

    const BUTTON_SELECTORS = [
        '[name="add"]', '[data-add-to-cart]',
        '.product-form__submit', '.shopify-payment-button button',
        'button[type="submit"]', 'input[type="submit"]', 'input[type="button"]',
        'a.button', 'a.btn',
        '[class*="btn-primary"]', '[class*="btn-cta"]', '[class*="-cta"]',
        '[class*="cta-"]', '.book-now', '.buy-now', '.get-started',
        'form button',
    ].join(', ');

    const searchRoot = mainZone || document.body;
    let candidates = [];
    try {
        const specific = Array.from(searchRoot.querySelectorAll(BUTTON_SELECTORS));
        const allButtons = mainZone ? Array.from(mainZone.querySelectorAll('button')) : [];
        const seen = new Set(specific);
        allButtons.forEach(el => { if (!seen.has(el)) { seen.add(el); specific.push(el); } });
        candidates = specific;
    } catch(e) {}

    const results = [];
    const seenTexts = new Set();
    const rejectedLog = [];

    Array.from(candidates).forEach(el => {
        const text = getButtonText(el);
        const textLower = text.toLowerCase();

        if (isInNoise(el)) {
            let noiseAncestor = null;
            let node = el.parentElement;
            while (node && node !== document.body) {
                if (noiseNodes.has(node)) {
                    noiseAncestor = (node.id || node.className.toString().substring(0, 40));
                    break;
                }
                node = node.parentElement;
            }
            rejectedLog.push({text: text || '(empty)', reason: 'isInNoise', ancestor: noiseAncestor});
            return;
        }
        if (!isVisible(el)) {
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            rejectedLog.push({text: text || '(empty)', reason: 'notVisible',
                display: s.display, visibility: s.visibility,
                opacity: s.opacity, w: r.width, h: r.height});
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
        if (/^[$£€¥₹]/.test(text.trim())) {
            rejectedLog.push({text: text, reason: 'priceButton'});
            return;
        }
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
            totalCandidates: candidates.length,
            rejected: rejectedLog,
        }
    };
}
"""


def detect_shopify_pixel_platforms(web_pixel_urls: list, web_pixel_bodies: dict = None) -> list:
    log_debug(f"detect_shopify_pixel_platforms: start urls={len(web_pixel_urls)} bodies={len(web_pixel_bodies or {})}")
    found = set()
    bodies = web_pixel_bodies or {}
    for url in web_pixel_urls:
        for app_id, platform in SHOPIFY_PIXEL_PLATFORMS.items():
            if app_id in url:
                log_debug(f"detect_shopify_pixel_platforms: app_id {app_id} → {platform} (url={url[:80]})")
                found.add(platform)
        body = (bodies.get(url) or "").lower()
        if not body:
            continue
        for platform, markers in SHOPIFY_PIXEL_MARKERS.items():
            if platform in found:
                continue
            marker = next((m for m in markers if m in body), None)
            if marker:
                log_debug(f"detect_shopify_pixel_platforms: маркер '{marker}' → {platform} (url={url[:80]})")
                found.add(platform)
    log_debug(f"detect_shopify_pixel_platforms: result={sorted(found)}")
    return list(found)


def _detect_cta_elements(page) -> list:
    log_debug("_detect_cta_elements: start")
    try:
        result = page.evaluate(_SHOPIFY_CTA_JS)
        ctas = result.get("ctas", [])
        debug = result.get("debug", {})
        log_debug(f"_detect_cta_elements: evaluate вернул ctas={len(ctas)}")
        if not ctas:
            total = debug.get("totalCandidates", 0)
            rejected = debug.get("rejected", [])
            main_zone = debug.get("mainZone")
            if total > 0:
                log_debug(f"CTA: 0 после фильтрации (кандидатов: {total}, main: {main_zone})")
                for r in rejected[:5]:
                    reason = r.get("reason", "?")
                    text = r.get("text", "")
                    if reason == "isInNoise":
                        log_debug(f"✗ [{reason}] '{text}' → предок: {r.get('ancestor')}")
                    elif reason == "notVisible":
                        log_debug(f"✗ [{reason}] '{text}' → display:{r.get('display')} vis:{r.get('visibility')} op:{r.get('opacity')} w:{r.get('w')} h:{r.get('h')}")
                    else:
                        log_debug(f"✗ [{reason}] '{text}'")
        in_main = [c["text"] for c in ctas if c.get("inMain")]
        not_in_main = [c["text"] for c in ctas if not c.get("inMain")]
        log_debug(f"_detect_cta_elements: in_main={len(in_main)} not_in_main={len(not_in_main)}")
        return (in_main + not_in_main)[:8]
    except Exception as e:
        log_debug(f"_detect_cta_elements: evaluate failed: {e}")
        return []


def scan_page(page, url: str, page_type: str, expect_events: list,
              platform: str = "shopify") -> dict:

    log_debug(f"scan_page: start url={url} page_type={page_type} platform={platform}")

    pixel_events     = {}
    pixel_ids        = {}
    all_html_parts   = []
    web_pixel_urls   = []
    web_pixel_bodies = {}
    request_urls_all = []
    pixel_hits       = []   # {url, method, body_snippet} — улики стенда (POST-тела)

    on_request_base, on_response = make_listeners(
        pixel_events, web_pixel_urls, web_pixel_bodies, all_html_parts, pixel_ids
    )

    def on_request(request):
        request_urls_all.append(request.url)
        capture_pixel_hit(request, pixel_hits)
        on_request_base(request)

    page.on("request", on_request)
    page.on("response", on_response)

    errors = []
    try:
        log_debug(f"scan_page: goto (try1, 20s) url={url}")
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
    except Exception as e:
        log_debug(f"scan_page: goto try1 failed: {e} — retry 10s")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(2000)
        except Exception as e2:
            log_debug(f"scan_page: goto try2 failed: {e2}")
            errors.append(str(e2)[:100])

    try:
        main_html = page.content()
        all_html_parts.append(main_html)
        log_debug(f"scan_page: page.content() ok len={len(main_html)}")
    except Exception as e:
        log_debug(f"scan_page: page.content() failed: {e}")
        main_html = ""

    combined_html = "\n".join(all_html_parts)
    log_debug(f"scan_page: combined_html len={len(combined_html)} web_pixel_urls={len(web_pixel_urls)}")

    shopify_pixel_platforms = []
    if web_pixel_urls:
        shopify_pixel_platforms = detect_shopify_pixel_platforms(web_pixel_urls, web_pixel_bodies)
    else:
        log_debug("scan_page: web_pixel_urls пуст — пропускаю detect_shopify_pixel_platforms")
    if "Meta" not in shopify_pixel_platforms:
        if re.search(r'fbevents\.js|facebook\.net/en_US|"pixelId"\s*:\s*"\d{10,}"', combined_html):
            log_debug("scan_page: Meta найден по HTML regex (fbevents/facebook.net/pixelId)")
            shopify_pixel_platforms.append("Meta")
    # PageView НЕ синтезируем: pixel_events = только network-подтверждённые события (A1).
    # Платформа из web-pixels кода без запросов остаётся в shopify_pixel_platforms
    # и рендерится downstream как «обнаружен в коде, запросов не зафиксировано».
    # Tested: 2026-07-08 on gymshark.com — Bing (network, ID 134606084) цел;
    #         tinytronics.nl — регрессии нет (статусы страниц идентичны old-vs-new).
    code_only = [sp for sp in shopify_pixel_platforms if sp not in pixel_events]
    if code_only:
        log_debug(f"scan_page: в коде web-pixels, но без network-запросов: {code_only} — событий не приписываем")

    cta_elements = _detect_cta_elements(page)

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)

    result = base_scan_page(
        page, url, page_type, expect_events,
        platform=platform,
        pixel_events=pixel_events,
        web_pixel_urls=web_pixel_urls,
        web_pixel_bodies=web_pixel_bodies,
        all_html_parts=all_html_parts,
        pixel_ids=pixel_ids,
    )

    result["external_services"] = detect_external_services(combined_html, request_urls_all)
    result["network_requests"] = request_urls_all[:300]   # сырьё для постмортемов (см. generic_scanner)
    result["pixel_hits"] = pixel_hits                     # улики стенда: метод+тело
    result["cta_elements"] = list(set(cta_elements))[:8]
    result["has_cta"] = bool(cta_elements) or result["content_analysis"]["is_page_of_interest"]
    result["has_iframe_form"] = False
    result["iframe_forms"] = []
    result["forms_count"] = result["content_analysis"]["forms_count"]
    result["ctas_in_html"] = {k: v[:3] for k, v in result["content_analysis"]["ctas"].items() if v}
    result["shopify_pixel_platforms"] = shopify_pixel_platforms
    result["errors"] = errors

    has_cta = result["has_cta"]
    has_conv = bool(result["conversion_events_found"])
    log_debug(f"scan_page: status-decision has_cta={has_cta} has_conv={has_conv}")

    if has_conv:
        log_debug("scan_page: → ✅ OK (conversion event найден)")
        result["status"] = "✅ OK"
    elif has_cta:
        log_debug("scan_page: → 🚨 GAP (CTA есть, conversion нет)")
        result["status"] = "🚨 GAP"
    else:
        log_debug("scan_page: → ➖ NO CTA")
        result["status"] = "➖ NO CTA"

    # Shopify + Cal.com/Calendly = вероятен server-side CAPI — GAP не подтверждён
    if result["status"] == "🚨 GAP":
        external = result.get("external_services", {})
        has_serverside_scheduler = any(
            svc in external for svc in ("Cal.com", "Calendly", "Acuity", "HubSpot Meetings")
        )
        if has_serverside_scheduler:
            log_debug("scan_page: GAP пересмотрен → server-side scheduler найден (Cal.com/Calendly/Acuity/HubSpot)")
            result["status"] = "⚠️ возможен server-side tracking, GAP не подтверждён"
            result["unverified_reason"] = (
                "Cal.com/Calendly detected on Shopify — conversion likely tracked "
                "via server-side CAPI, not visible to browser scan"
            )

    log_debug(f"scan_page: done url={url} status={result['status']}")
    return result
