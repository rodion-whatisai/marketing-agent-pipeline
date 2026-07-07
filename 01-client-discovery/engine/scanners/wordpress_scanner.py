"""
WordPress Scanner
================
Специфика:
- Elementor lazy-load: scroll перед снапшотом DOM
- WPForms: ждём инициализацию виджета через Elementor
- Jotform/Typeform/HubSpot: cross-origin iframe — детектим по src, не лезем внутрь
- Jotform postMessage: ловим submission-completed из iframe
- Jetpack pixel.wp.com: фильтруем — не рекламный
- MAIN_SELECTORS: знает про Elementor и WordPress структуру
"""

from .base_scanner import (
    base_scan_page, make_listeners, detect_external_services
)
from log import log_info, log_debug

# Internal WordPress analytics — не рекламные пиксели
WORDPRESS_NOISE_DOMAINS = ["pixel.wp.com"]

# Iframe форм-провайдеры — детектим по src iframe
IFRAME_FORM_PROVIDERS = [
    "jotform.com", "jotfor.ms",
    "typeform.com",
    "hsforms.com", "hsforms.net",
    "paperform.co",
    "tally.so",
    "cognito",
    "wufoo.com",
]

# JS для CTA detection — WordPress версия
# Знает про Elementor, WPForms, WordPress структуру
_WP_CTA_JS = """
() => {
    const NOISE_SELECTORS = [
        'header', 'nav', 'footer',
        '[id*="header" i]', '[class*="header" i]',
        '[id*="navbar" i]', '[class*="navbar" i]',
        '[id*="footer" i]', '[class*="footer" i]',
        '[id*="site-nav" i]', '[class*="site-nav" i]',
        '[class*="menu-toggle" i]', '[class*="mobile-menu" i]',
        '[class*="cookie" i]', '[id*="cookie" i]',
        '[class*="consent" i]', '[id*="gdpr" i]',
        '[class*="breadcrumb" i]', '[class*="pagination" i]',
        '[class*="skip" i]',
        '[class*="gm-style"]',
        '.wd-side-hidden', '.wd-mobile-nav',
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

    // WordPress/Elementor main zone selectors
    const MAIN_SELECTORS = [
        // WordPress стандартные
        'main', '#main', '#main-content', '#content', '#primary',
        '.site-content', '.site-main',
        '#page', '.page-content',
        // Elementor специфичные
        '[data-elementor-type="wp-page"]',
        '[data-elementor-type="wp-post"]',
        '.elementor-section-wrap',
        '.elementor-inner',
        // WPForms контейнер
        '.wpforms-container',
        // WordPress блок-редактор
        '.wp-block-group', '.entry-content',
        // Woodmart тема (studioaplus)
        '.wd-page-wrapper', '.site-wrapper',
        // Общий fallback
        'article', '.post', '.page',
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
        'search', 'menu', 'home', 'privacy policy', 'terms of service',
        'view all', 'see all', 'load more', 'show more', 'more', 'next',
        'continue', 'no thanks', 'maybe later',
        'share', 'follow', 'print',
        'previous', 'pause', 'play',
        'remove item', 'wpforms-submit',
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
        if (s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        return true;
    }

    const BUTTON_SELECTORS = [
        // WPForms
        '.wpforms-submit', '.wpforms-submit-container button[type=submit]',
        // Contact Form 7
        '.wpcf7-submit',
        // Gravity Forms
        '.gform_button', '.gf_btn',
        // Generic form submits
        'button[type="submit"]', 'input[type="submit"]', 'input[type="button"]',
        // CTA классы WordPress/Elementor
        '.elementor-button', '.e-btn',
        'a.button', 'a.btn', '.wp-block-button__link',
        '[class*="btn-primary"]', '[class*="btn-cta"]',
        '.book-now', '.buy-now', '.get-started',
        'form button',
    ].join(', ');

    const searchRoot = mainZone || document.body;
    let candidates = [];
    try {
        const specific = Array.from(searchRoot.querySelectorAll(BUTTON_SELECTORS));
        const allButtons = mainZone
            ? Array.from(mainZone.querySelectorAll('button'))
            : [];
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


def _detect_iframe_forms(page) -> tuple[bool, list]:
    """
    Ищет cross-origin iframe формы на странице.
    Возвращает (has_iframe_form, [список провайдеров]).
    """
    log_debug("_detect_iframe_forms: start — ищем cross-origin iframe формы")
    try:
        iframes = page.query_selector_all("iframe")
        log_debug(f"_detect_iframe_forms: найдено iframe на странице: {len(iframes)}")
        found = []
        for iframe in iframes:
            src = iframe.get_attribute("src") or ""
            for provider in IFRAME_FORM_PROVIDERS:
                if provider in src:
                    label = provider.split(".")[0]  # "jotform", "typeform" etc.
                    log_debug(f"_detect_iframe_forms: матч провайдера '{provider}' в src → {label}")
                    found.append(label)
                    break
        log_debug(f"_detect_iframe_forms: итог has_iframe_form={bool(found)}, провайдеры={found}")
        return bool(found), found
    except Exception as e:
        log_debug(f"_detect_iframe_forms: исключение при опросе iframe: {e}")
        return False, []


def _detect_cta_elements(page) -> list:
    """JS-based CTA detection для WordPress."""
    log_debug("_detect_cta_elements: start — запускаем _WP_CTA_JS через page.evaluate")
    try:
        result = page.evaluate(_WP_CTA_JS)
        ctas = result.get("ctas", [])
        debug = result.get("debug", {})
        log_debug(f"_detect_cta_elements: получено ctas={len(ctas)}, mainZone={debug.get('mainZone')}, totalCandidates={debug.get('totalCandidates', 0)}")

        if not ctas:
            main_zone = debug.get("mainZone")
            total = debug.get("totalCandidates", 0)
            rejected = debug.get("rejected", [])
            log_debug(f"_detect_cta_elements: ctas пуст — ветка диагностики отклонённых (total={total}, rejected={len(rejected)})")
            if total > 0:
                log_info(f"       CTA: 0 после фильтрации (кандидатов: {total}, main: {main_zone})")
                for r in rejected[:5]:
                    reason = r.get("reason", "?")
                    text = r.get("text", "")
                    if reason == "isInNoise":
                        log_debug(f"         ✗ [{reason}] '{text}' → предок: {r.get('ancestor')}")
                    elif reason == "notVisible":
                        log_debug(f"         ✗ [{reason}] '{text}' → display:{r.get('display')} vis:{r.get('visibility')} op:{r.get('opacity')} w:{r.get('w')} h:{r.get('h')}")
                    else:
                        log_debug(f"         ✗ [{reason}] '{text}'")

        log_debug(f"_detect_cta_elements: возвращаем top-8 текстов CTA (всего {len(ctas)})")
        return [c["text"] for c in ctas][:8]
    except Exception as e:
        log_debug(f"_detect_cta_elements: исключение при evaluate/parse CTA: {e}")
        return []


def scan_page(page, url: str, page_type: str, expect_events: list,
              platform: str = "wordpress") -> dict:

    log_debug(f"scan_page: start url={url} page_type={page_type} platform={platform} expect_events={expect_events}")

    pixel_events     = {}
    pixel_ids        = {}
    all_html_parts   = []
    web_pixel_urls   = []
    web_pixel_bodies = {}
    request_urls_all = []

    from .base_scanner import make_listeners, PIXEL_RULES

    on_request_base, on_response = make_listeners(
        pixel_events, web_pixel_urls, web_pixel_bodies, all_html_parts, pixel_ids
    )

    def on_request(request):
        req_url = request.url
        # Фильтруем WordPress internal pixels
        if any(d in req_url for d in WORDPRESS_NOISE_DOMAINS):
            log_debug(f"on_request: пропускаем WordPress internal pixel — {req_url}")
            return
        request_urls_all.append(req_url)
        on_request_base(request)

    page.on("request", on_request)
    page.on("response", on_response)

    errors = []

    # Jotform postMessage listener — вешаем ДО загрузки страницы
    log_debug("scan_page: вешаем Jotform/Typeform postMessage listener (до goto)")
    try:
        page.evaluate("""
            window.__tnc_iframe_submitted = false;
            window.__tnc_iframe_provider = null;
            window.addEventListener('message', function(e) {
                var data = e.data;
                if (!data) return;
                // Jotform submission-completed
                if (typeof data === 'object' && data.action === 'submission-completed') {
                    window.__tnc_iframe_submitted = true;
                    window.__tnc_iframe_provider = 'jotform';
                }
                // Jotform legacy string format
                if (typeof data === 'string' && data.indexOf('JF_') !== -1) {
                    window.__tnc_iframe_submitted = true;
                    window.__tnc_iframe_provider = 'jotform';
                }
                // Typeform
                if (typeof data === 'object' && data.type === 'form-submit') {
                    window.__tnc_iframe_submitted = true;
                    window.__tnc_iframe_provider = 'typeform';
                }
            });
        """)
    except Exception as e:
        log_debug(f"scan_page: не удалось повесить postMessage listener: {e}")

    log_debug(f"scan_page: goto {url} (попытка 1, timeout=20000)")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
    except Exception as e:
        log_debug(f"scan_page: goto попытка 1 не удалась ({e}) — повтор с timeout=10000")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(1500)
        except Exception as e2:
            log_debug(f"scan_page: goto попытка 2 тоже не удалась: {e2}")
            errors.append(str(e2)[:100])

    # Scroll — запускаем lazy-load Elementor виджетов и WPForms
    log_debug("scan_page: scroll-проход (lazy-load Elementor/WPForms)")
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.7)")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(600)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception as e:
        log_debug(f"scan_page: scroll-проход прерван исключением: {e}")

    # Ждём WPForms submit кнопку если есть WPForms на странице
    log_debug("scan_page: ждём .wpforms-submit (timeout=2000)")
    try:
        page.wait_for_selector(".wpforms-submit", timeout=2000)
        log_debug("scan_page: .wpforms-submit найден — WPForms присутствует")
    except Exception as e:
        log_debug(f"scan_page: .wpforms-submit не появился (нет WPForms или timeout): {e}")

    try:
        main_html = page.content()
        all_html_parts.append(main_html)
        log_debug(f"scan_page: page.content() получен, длина HTML={len(main_html)}")
    except Exception as e:
        log_debug(f"scan_page: page.content() упал — main_html='' : {e}")
        main_html = ""

    # Detect iframe forms
    has_iframe_form, iframe_providers = _detect_iframe_forms(page)

    # CTA detection
    cta_elements = _detect_cta_elements(page)
    log_debug(f"scan_page: CTA detection → {len(cta_elements)} текстов; iframe_forms={iframe_providers}")

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)

    combined_html = "\n".join(all_html_parts)

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
    result["cta_elements"] = list(set(cta_elements))[:8]
    result["has_iframe_form"] = has_iframe_form
    result["iframe_forms"] = iframe_providers

    # has_cta: реальные кнопки ИЛИ iframe форма ИЛИ classify говорит что страница значимая
    result["has_cta"] = (
        bool(cta_elements) or
        has_iframe_form or
        result["content_analysis"]["is_page_of_interest"]
    )
    result["forms_count"] = result["content_analysis"]["forms_count"]
    result["ctas_in_html"] = {k: v[:3] for k, v in result["content_analysis"]["ctas"].items() if v}
    result["shopify_pixel_platforms"] = []
    result["errors"] = errors

    has_cta = result["has_cta"]
    has_conv = bool(result["conversion_events_found"])
    log_debug(f"scan_page: статус-решение — has_cta={has_cta}, has_conv={has_conv}")

    if has_conv:
        log_debug("scan_page: ветка has_conv → статус ✅ OK")
        result["status"] = "✅ OK"
    elif has_cta:
        log_debug("scan_page: ветка has_cta (без conv) → статус 🚨 GAP")
        result["status"] = "🚨 GAP"
    else:
        log_debug("scan_page: нет CTA и нет conv → статус ➖ NO CTA")
        result["status"] = "➖ NO CTA"

    log_debug(f"scan_page: done url={url} → status={result['status']}")
    return result
