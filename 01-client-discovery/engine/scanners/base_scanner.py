"""
Base Scanner — общая логика для всех платформ.
Pixel detection, CTA JS, external services, статус страницы.
"""

import re
from urllib.parse import urlparse, parse_qs

from page_classifier import classify_page_content
from log import log_debug, log_fire


# ─── Общий детектор кнопок/CTA (один источник истины для сканера и кликера) ────
# Сканер находит кнопки ОДИН раз, помечает их data-tnc-btn, отдаёт список; кликер
# берёт этот же список. Раньше детекторов было два (узкий в сканере + широкий в
# кликере) → «CTA: 0, но кликнули 5». Теперь оба зовут это.

MAX_BUTTONS = 12

# Слова CTA — для приоритезации кнопок (выше приоритет = раньше кликаем)
CTA_WORDS = (
    "book", "buy", "quote", "contact", "submit", "send", "apply", "get started",
    "add to cart", "add to bag", "add to basket", "request", "sign up", "subscribe",
    "order", "checkout", "demo", "enquir", "call", "get a", "reserve", "register",
    # FR
    "devis", "soumission", "contactez", "reserver", "commander", "ajouter",
)

# JS: дженерик-поиск кнопок. Стэмпит каждому выжившему кандидату data-tnc-btn="<i>"
# — стабильный хэндл для повторного локейта. Фильтрует nav/footer/cookie-шум и
# служебные тексты.
# Tested: 2026-07-08 nav-фильтр (role="navigation"/menubar/menu + голые названия
#         разделов + стрелки каруселей): tinytronics → только Toevoegen/Verlanglijst
#         (события целы); nissan.ie → VIEW OFFERS/DISCOVER MORE/EXPLORE THE RANGE;
#         thebodyshop → VIEW PRODUCT. Пункты меню в CTA не попадают.
_DISCOVER_BUTTONS_JS = """
() => {
    const NOISE_SELECTORS = [
        'header','nav','footer',
        '[role="navigation"]','[role="menubar"]','[role="menu"]',
        '[id*="header" i]','[class*="header" i]',
        '[id*="navbar" i]','[class*="navbar" i]',
        '[id*="footer" i]','[class*="footer" i]',
        '[id*="site-nav" i]','[class*="site-nav" i]',
        '[class*="menu-toggle" i]','[class*="mobile-menu" i]',
        '[class*="cookie" i]','[id*="cookie" i]',
        '[class*="consent" i]','[id*="gdpr" i]',
        '[class*="breadcrumb" i]','[class*="pagination" i]',
        '[class*="skip" i]','[class*="gm-style"]',
    ];
    const noiseNodes = new Set();
    NOISE_SELECTORS.forEach(sel => { try { document.querySelectorAll(sel).forEach(el => noiseNodes.add(el)); } catch(e){} });
    function isInNoise(el){ let n=el.parentElement; while(n && n!==document.body){ if(noiseNodes.has(n)) return true; n=n.parentElement; } return false; }

    const MAIN_SELECTORS = ['main','#main','#main-content','#content','#primary','.site-content','.site-main','#page','.page-content','article','.post','.page','.entry-content'];
    let mainZone=null;
    for(const sel of MAIN_SELECTORS){ try{ const el=document.querySelector(sel); if(el && el.offsetHeight>50){ mainZone=el; break; } }catch(e){} }

    const SKIP_TEXTS = new Set(['close','ok','okay','cancel','dismiss','skip','back','accept','accept all','reject all','decline','allow','deny','agree','i agree','got it','save preferences','necessary only','accept cookies','reject cookies','manage cookies','cookie settings','search','menu','home','privacy policy','terms of service','view all','see all','load more','show more','more','next','continue','no thanks','maybe later','share','follow','print','previous','pause','play','use my current location','use my location',
        // голые названия разделов = навигация, не CTA (страховка для меню вне nav-контейнеров)
        'products','orders','returns','account','my account','delivery','about','about us','news','blog',
        // стрелки каруселей — не CTA (tinytronics: 'Previous slide' давал 5s-таймаут на каждой странице)
        'previous slide','next slide','prev slide']);

    function getButtonText(el){
        const aria=(el.getAttribute('aria-label')||'').trim(); if(aria.length>1 && aria.length<80) return aria;
        const val=(el.getAttribute('value')||'').trim(); if(val.length>1 && val.length<80) return val;
        const raw=(el.innerText||el.textContent||'').trim();
        const lines=raw.split('\\n').map(l=>l.trim()).filter(l=>l.length>0);
        return lines[0]||'';
    }
    function isVisible(el){ const s=window.getComputedStyle(el); if(s.display==='none')return false; if(s.visibility==='hidden')return false; const r=el.getBoundingClientRect(); if(r.width===0 && r.height===0) return false; return true; }

    const BUTTON_SELECTORS = [
        'button','[role="button"]','input[type="submit"]','input[type="button"]',
        'a.button','a.btn','[class*="btn"]','[class*="cta"]',
        '.wpforms-submit','.wpcf7-submit','.elementor-button','.wp-block-button__link',
        'form button',
    ].join(', ');

    let candidates=[];
    try{ candidates = Array.from(document.querySelectorAll(BUTTON_SELECTORS)); }catch(e){ candidates=[]; }

    const results=[]; const seenTexts=new Set();
    candidates.forEach(el => {
        const text=getButtonText(el); const tl=text.toLowerCase();
        if(isInNoise(el)) return;
        if(!isVisible(el)) return;
        if(!text || text.length<2 || text.length>80) return;
        if(SKIP_TEXTS.has(tl)) return;
        if(/^[$£€¥₹]/.test(text.trim())) return;
        if(/^[0-9]+$/.test(text.trim())) return;
        if(seenTexts.has(tl)) return;
        seenTexts.add(tl);
        const idx = results.length;
        try { el.setAttribute('data-tnc-btn', String(idx)); } catch(e){}
        results.push({
            index: idx,
            text: text,
            tag: el.tagName.toLowerCase(),
            isFormSubmit: el.closest('form') !== null,
            inMain: mainZone ? mainZone.contains(el) : false,
        });
    });
    return results;
}
"""


def discover_buttons(page, debug: bool = False) -> list:
    """Все значимые кнопки страницы (с проставленным data-tnc-btn), отсортированы по
    приоритету и обрезаны до MAX_BUTTONS. Один детектор для сканера и кликера."""
    try:
        raw = page.evaluate(_DISCOVER_BUTTONS_JS)
    except Exception as e:
        log_debug(f"discover_buttons: evaluate error: {str(e)[:80]}")
        return []

    def prio(c):
        t = (c.get("text") or "").lower()
        if c.get("isFormSubmit"):
            return 0
        if any(w in t for w in CTA_WORDS):
            return 1
        if c.get("inMain"):
            return 2
        return 3

    raw.sort(key=prio)
    out = raw[:MAX_BUTTONS]
    log_debug(f"discover_buttons: {len(raw)} найдено → {len(out)} после cap {MAX_BUTTONS}")
    return out


# ─── Pixel rules ──────────────────────────────────────────────────────────────

PIXEL_RULES = {
    "Meta": {
        "domains": ["facebook.com/tr", "connect.facebook.net/en_US/fbevents",
                    "connect.facebook.net/signals/config"],
        "event_param": "ev",
        "id_param": "id",                              # facebook.com/tr?id=<id>
        "id_path_re": r"/signals/config/(\d{6,})",     # SDK config — летит даже в headless
    },
    "Google Analytics": {
        # 'google-analytics.com/g/collect' substring-ловит и www., и region1. хосты —
        # реальные GA4 endpoints; старые записи оставлены для legacy UA / analytics.google.com
        # Tested: 2026-07-07 on tinytronics.nl — GA4 бил в www.google-analytics.com/g/collect,
        #         старые паттерны его не матчили (ломался на '/g/')
        "domains": ["analytics.google.com/g/collect", "google-analytics.com/collect",
                    "google-analytics.com/g/collect"],
        "event_param": "en",
        "id_param": "tid",                             # ?tid=G-XXXX
    },
    "Google Ads": {
        # ccm/collect и viewthroughconversion — presence-пинги современного gtag:
        # регистрируют платформу/ID, но page_view и т.п. глушатся NOISE_EVENTS ниже
        "domains": ["googleadservices.com/pagead/conversion",
                    "google.com/pagead/1p-conversion",
                    "google.com/ccm/collect",
                    "doubleclick.net/ccm/s/collect",
                    "doubleclick.net/pagead/viewthroughconversion",
                    "pagead/1p-user-list"],
        "event_param": "en",                           # ccm/collect несёт en=page_view
        "id_path_re": r"/(?:conversion|viewthroughconversion)/(\d{6,})",
    },
    "Bing/Microsoft": {
        "domains": ["bat.bing.com/action", "bat.bing.com/p/action"],
        "event_param": "ea",
        "id_param": "ti",                              # ?ti=<id>
    },
    "LinkedIn": {
        "domains": ["px.ads.linkedin.com", "snap.licdn.com"],
        "event_param": "conversionId",
        "id_param": "pid",                             # ?pid=<id>
    },
    "TikTok": {
        # i18n/pixel = загрузка SDK (events.js/config) — presence-сигнал, как fbevents у Meta.
        # Подстроки без хоста кроют региональные хосты (analytics-sg.tiktok.com и т.п.).
        # Кейс: bobbies.com — TikTok через GTM грузил i18n/pixel/events.js, старое правило
        # (только analytics.tiktok.com/api/v2/pixel) его не видело → ложный «TikTok ❌».
        "domains": ["tiktok.com/api/v2/pixel", "tiktok.com/i18n/pixel/"],
        "event_param": "event",
        "id_param": "sdkid",                           # events.js?sdkid=<PIXEL_ID>&lib=ttq
    },
    "Snapchat": {
        # До 2026-07-08 правила НЕ БЫЛО вообще — «Snapchat ❌» не мог стать ✅ в принципе
        "domains": ["tr.snapchat.com", "sc-static.net/scevent"],
        "event_param": None,
    },
}

CONVERSION_EVENTS_TIER1 = {
    "Meta": ["Purchase", "Lead", "InitiateCheckout", "AddToCart",
             "CompleteRegistration", "Schedule", "Contact", "AddPaymentInfo"],
    "Google Analytics": ["purchase", "begin_checkout", "add_to_cart",
                         "generate_lead", "form_submit", "conversion"],
    "Google Ads": ["conversion"],
    "Bing/Microsoft": ["purchase", "lead", "conversion"],
    "TikTok": ["Purchase", "AddToCart", "InitiateCheckout", "PlaceAnOrder"],
    "Snapchat": ["PURCHASE", "START_CHECKOUT", "ADD_CART", "SIGN_UP", "LEAD"],
}

CONVERSION_EVENTS_TIER2 = {
    "Meta": ["ViewContent", "Search", "Subscribe"],
    "Google Analytics": ["view_item", "view_item_list", "search",
                         "select_item", "view_promotion"],
    "Google Ads": [],
    "Bing/Microsoft": [],
    "TikTok": ["ViewContent"],
}

NOISE_EVENTS = {
    "Meta": ["fired"],   # PageView НЕ noise: показываем «Meta: PageView» (пиксель активен, шлёт baseline)
    "Google Analytics": [
        "gtm.init", "gtm.init_consent", "gtm.js", "fired",
        "page_view", "user_engagement", "session_start", "first_visit",
        "scroll", "click", "view_item_list",
        "form_start", "form_close",
    ],
    # page_view/gtag.config с ccm/collect — presence-пинг, НЕ конверсия: без этого
    # ccm-хиты ложно «озеленяли» бы GAP-страницы
    "Google Ads": ["page_view", "gtag.config"],
    "Bing/Microsoft": ["fired"],
    "TikTok": ["fired"],
    "LinkedIn": ["fired"],
    "Snapchat": ["fired"],
}

# ─── External services ────────────────────────────────────────────────────────

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
    "Jotform":           ["jotform.com", "jotfor.ms"],
    "Google Forms":      ["docs.google.com/forms", "forms.gle"],
    "Tally":             ["tally.so"],
    "Paperform":         ["paperform.co"],
    "HubSpot Forms":     ["hsforms.com", "hsforms.net"],
    "ActiveCampaign":    ["activehosted.com"],
    # Саппорт-чаты скрыты — не конверсионный канал, шум для tracking-аудита (Zendesk тоже убран):
    # "Intercom":          ["intercom.io", "widget.intercom.io"],
    # "Drift":             ["drift.com", "js.driftt.com"],
    # "Crisp":             ["crisp.chat"],
    # "Tidio":             ["tidio.co"],
    # "Freshchat":         ["freshchat.com", "wchat.freshchat.com"],
    "Stripe":            ["js.stripe.com", "checkout.stripe.com"],
    "Paddle":            ["paddle.com"],
    "Gumroad":           ["gumroad.com"],
    "Pipedrive":         ["pipedrivewebforms.com"],
    "Microsoft Clarity": ["clarity.ms/collect", "clarity.ms/s/"],
}

# Payment services — only via network, never HTML
# (Jotform/other iframes reference Stripe in their CSS/JS → false positives)
NETWORK_ONLY_SERVICES = {"Stripe", "Paddle", "Gumroad"}

ANALYTICS_TOOLS = {"Microsoft Clarity", "Hotjar", "FullStory", "Lucky Orange"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_event_from_url(url: str, platform: str) -> str:
    log_fire(f"get_event_from_url: start platform={platform} url={url}")
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        rule = PIXEL_RULES.get(platform, {})
        ep = rule.get("event_param")
        if ep and ep in params:
            log_fire(f"get_event_from_url: {platform} event_param '{ep}' -> '{params[ep][0]}'")
            return params[ep][0]
        log_fire(f"get_event_from_url: {platform} no event_param match (ep={ep}) -> 'fired'")
    except Exception as e:
        log_debug(f"get_event_from_url: parse failed for {url}: {e}")
    return "fired"


def get_pixel_id_from_url(url: str, platform: str) -> str:
    """Достаёт ID пикселя/счётчика из tracking-URL. '' если нет.
    Path-regex (Meta SDK config, Google Ads) ловит ID даже когда конверсионное
    событие не стрельнуло — например при пассивной загрузке без действия пользователя."""
    log_fire(f"get_pixel_id_from_url: start platform={platform} url={url}")
    rule = PIXEL_RULES.get(platform, {})
    try:
        parsed = urlparse(url)
        id_re = rule.get("id_path_re")
        if id_re:
            m = re.search(id_re, parsed.path)
            if m:
                log_fire(f"get_pixel_id_from_url: {platform} id via path-regex -> '{m.group(1)}'")
                return m.group(1)
        ip = rule.get("id_param")
        if ip:
            params = parse_qs(parsed.query)
            if ip in params and params[ip][0]:
                log_fire(f"get_pixel_id_from_url: {platform} id via query-param '{ip}' -> '{params[ip][0]}'")
                return params[ip][0]
        log_fire(f"get_pixel_id_from_url: {platform} no id found")
    except Exception as e:
        log_debug(f"get_pixel_id_from_url: parse failed for {url}: {e}")
    return ""


def is_conversion_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER1.get(platform, []))


def is_partial_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER2.get(platform, []))


def is_noise_event(platform: str, event: str) -> bool:
    return event in NOISE_EVENTS.get(platform, [])


def detect_external_services(html: str, requests_urls: list = None) -> dict:
    log_debug(f"detect_external_services: start html_len={len(html)} n_requests={len(requests_urls) if requests_urls else 0}")
    found = {}
    html_lower = html.lower()
    for service, domains in EXTERNAL_SERVICES.items():
        if service in NETWORK_ONLY_SERVICES:
            log_debug(f"detect_external_services: skip {service} (network-only) in HTML pass")
            continue
        for domain in domains:
            if domain in html_lower:
                log_debug(f"detect_external_services: {service} matched in HTML via '{domain}'")
                found[service] = {"detected_via": "html", "domain": domain}
                break
    if requests_urls:
        for req_url in requests_urls:
            req_lower = req_url.lower()
            for service, domains in EXTERNAL_SERVICES.items():
                if service not in found:
                    for domain in domains:
                        if domain in req_lower:
                            log_debug(f"detect_external_services: {service} matched in network via '{domain}'")
                            found[service] = {"detected_via": "network", "domain": domain}
                            break
    log_debug(f"detect_external_services: done, found {len(found)} service(s): {sorted(found)}")
    return found


# ─── Pixel hits — сырьё улик для испытательного стенда ───────────────────────
# Meta шлёт содержательные события multipart-POST'ом, TikTok всё в JSON-телах
# (BUGS-2026-07-13) — network_requests (только URL) для улик стенда недостаточно.
# capture_pixel_hit пишет {url, method, body_snippet} для запросов к известным
# пиксель-хостам. Это НЕ детекция (не влияет на вердикты сканера) — только сырьё
# для eval_lib.find_event_evidence / гейта Rodion'а. См. TESTBED-PLAN.md.

PIXEL_HIT_HOSTS = (
    "facebook.com/tr", "connect.facebook.net",
    "analytics.tiktok.com",
    "ct.pinterest.com",
    "tr.snapchat.com", "sc-static.net",
    "bat.bing.com",
    "google-analytics.com", "analytics.google.com",
    "googleadservices.com", "googleads.g.doubleclick.net",
    "google.com/ccm/collect", "google.com/pagead", "google.com/rmkt",
    "google.com/measurement", "googletagmanager.com/gtag",
    "px.ads.linkedin.com", "snap.licdn.com",
)

PIXEL_HIT_CAP = 200          # запросов на страницу
PIXEL_HIT_URL_CAP = 1000     # символов URL — event-параметр (en=/ev=) у GET-пикселей
                             # живёт глубоко в query, 300 символов его отрезало (ревью 2026-07-13)
PIXEL_HIT_BODY_CAP = 3000    # символов тела (Meta multipart ~2KB — влезает целиком)


def capture_pixel_hit(request, out: list):
    """Записать запрос к пиксель-хосту с методом и телом. Тихо молчит на прочем.

    Известное ограничение: слушатели сканеров снимаются ДО клик-фазы (кликер
    держит собственный listener), поэтому клик-события сюда не попадают —
    их улики собирает witness.py --journey (день 3 плана, TESTBED-PLAN.md)."""
    url = request.url
    if len(out) >= PIXEL_HIT_CAP or not any(h in url for h in PIXEL_HIT_HOSTS):
        return
    body = None
    try:
        body = request.post_data
    except Exception:
        pass  # бинарное/недоступное тело — фиксируем хотя бы url+method
    out.append({
        "url": url[:PIXEL_HIT_URL_CAP],
        "method": request.method,
        "body_snippet": (body or "")[:PIXEL_HIT_BODY_CAP] or None,
    })


# ─── Network listeners ────────────────────────────────────────────────────────

def make_listeners(pixel_events: dict, web_pixel_urls: list, web_pixel_bodies: dict,
                   all_html_parts: list, pixel_ids: dict = None):
    """Returns (on_request, on_response) closures that populate shared dicts."""
    if pixel_ids is None:
        pixel_ids = {}

    def on_request(request):
        req_url = request.url
        if "/web-pixels" in req_url:
            log_fire(f"on_request: web-pixels asset captured url={req_url}")
            web_pixel_urls.append(req_url)
        for platform, rules in PIXEL_RULES.items():
            for domain in rules["domains"]:
                if domain in req_url:
                    log_fire(f"on_request: {platform} pixel request matched domain '{domain}' url={req_url}")
                    event = get_event_from_url(req_url, platform)
                    pixel_events.setdefault(platform, [])
                    entry = {
                        "event": event,
                        "is_conversion": is_conversion_event(platform, event),
                        "is_partial": is_partial_event(platform, event),
                        "is_noise": is_noise_event(platform, event),
                    }
                    log_fire(f"on_request: {platform} event='{event}' conversion={entry['is_conversion']} partial={entry['is_partial']} noise={entry['is_noise']}")
                    if not any(e["event"] == event for e in pixel_events[platform]):
                        log_fire(f"on_request: {platform} new event '{event}' recorded")
                        pixel_events[platform].append(entry)
                    # Собираем ID пикселя — для presence (headless) и детекта дублей
                    pid = get_pixel_id_from_url(req_url, platform)
                    if pid:
                        pixel_ids.setdefault(platform, [])
                        if pid not in pixel_ids[platform]:
                            log_fire(f"on_request: {platform} new pixel id '{pid}' recorded")
                            pixel_ids[platform].append(pid)
                    break

    def on_response(response):
        try:
            # Редиректы (3xx) — тела нет, читать незачем (избегаем холостых попыток + шума).
            if response.status >= 300:
                return
            ct = response.headers.get("content-type", "")
            url = response.url
            if "javascript" in ct or "html" in ct:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="ignore")
                    all_html_parts.append(text)
                    if "/web-pixels" in url:
                        log_fire(f"on_response: web-pixels body captured url={url} len={len(text)}")
                        web_pixel_bodies[url] = text
                except Exception as e:
                    log_debug(f"on_response: body read/decode failed url={url}: {e}")
        except Exception as e:
            log_debug(f"on_response: header access failed: {e}")

    return on_request, on_response


# ─── Base page scan ───────────────────────────────────────────────────────────

def base_scan_page(page, url: str, page_type: str, expect_events: list,
                   platform: str = "unknown",
                   pixel_events: dict = None,
                   web_pixel_urls: list = None,
                   web_pixel_bodies: dict = None,
                   all_html_parts: list = None,
                   pixel_ids: dict = None,
                   extra_html: str = "") -> dict:
    """
    Core scan logic shared by all platform scanners.
    Callers attach listeners before calling this, or pass pre-populated dicts.
    """
    log_debug(f"base_scan_page: start url={url} page_type={page_type} platform={platform} expect_events={expect_events}")
    pixel_events    = pixel_events    or {}
    web_pixel_urls  = web_pixel_urls  or []
    web_pixel_bodies= web_pixel_bodies or {}
    all_html_parts  = all_html_parts  or []
    pixel_ids       = pixel_ids       or {}

    if extra_html:
        log_debug(f"base_scan_page: appending extra_html len={len(extra_html)}")
        all_html_parts.append(extra_html)

    combined_html = "\n".join(all_html_parts)
    log_debug(f"base_scan_page: combined_html len={len(combined_html)} from {len(all_html_parts)} part(s); classifying content")
    content_analysis = classify_page_content(combined_html, page)

    # Build request URL list for network-only service detection
    request_urls = list(web_pixel_urls)  # subclasses can extend this

    external_services = detect_external_services(combined_html, request_urls)

    # Aggregate events
    conversion_events_found = []
    partial_events_found = []
    noise_only = True

    for plat, events in pixel_events.items():
        for ev in events:
            if ev["is_conversion"]:
                log_debug(f"base_scan_page: conversion event {plat}:{ev['event']}")
                conversion_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif ev.get("is_partial"):
                log_debug(f"base_scan_page: partial event {plat}:{ev['event']}")
                partial_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif not ev["is_noise"]:
                log_debug(f"base_scan_page: non-noise event {plat}:{ev['event']} (clears noise_only)")
                noise_only = False

    missing_events = []
    for expected in expect_events:
        found = False
        for plat, events in pixel_events.items():
            if any(expected.lower() in e["event"].lower() for e in events):
                found = True
                break
        if not found:
            log_debug(f"base_scan_page: expected event '{expected}' not found")
            missing_events.append(expected)

    has_conv = len(conversion_events_found) > 0

    if has_conv:
        log_debug(f"base_scan_page: status OK — {len(conversion_events_found)} conversion event(s): {conversion_events_found}")
        status = "✅ OK"
    else:
        log_debug(f"base_scan_page: status GAP — no conversion events (partial={partial_events_found}, noise_only={noise_only})")
        status = "🚨 GAP"

    return {
        "url": url,
        "path": urlparse(url).path or "/",
        "page_type": page_type,
        "status": status,
        "conversion_events_found": conversion_events_found,
        "partial_events_found": partial_events_found,
        "missing_events": missing_events,
        "only_noise_events": noise_only,
        # noise-события СОХРАНЯЕМ (SDK-load 'fired', page_view и т.п.): по ним step2/report
        # показывают ПРИСУТСТВИЕ пикселя («Платформы: TikTok ✅»), а conversion-логика
        # смотрит на is_noise-флаги и с событиями их не путает. Старый фильтр выкидывал
        # noise-only платформы целиком → bobbies: TikTok[fired] пойман, но «TikTok ❌».
        # Tested: 2026-07-08 on bobbies.com/en/contact
        "pixel_events": {plat: evs for plat, evs in pixel_events.items() if evs},
        "pixel_ids": {p: ids for p, ids in pixel_ids.items() if ids},
        "duplicate_pixels": [p for p, ids in pixel_ids.items() if len(ids) >= 2],
        "external_services": external_services,
        "content_analysis": content_analysis,
        "errors": [],
    }
