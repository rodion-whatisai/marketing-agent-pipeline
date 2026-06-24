"""
Base Scanner — общая логика для всех платформ.
Pixel detection, CTA JS, external services, статус страницы.
"""

import re
from urllib.parse import urlparse, parse_qs

from page_classifier import classify_page_content

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
        "domains": ["analytics.google.com/g/collect", "google-analytics.com/collect"],
        "event_param": "en",
        "id_param": "tid",                             # ?tid=G-XXXX
    },
    "Google Ads": {
        "domains": ["googleadservices.com/pagead/conversion",
                    "google.com/pagead/1p-conversion"],
        "event_param": None,
        "id_path_re": r"/conversion/(\d{6,})",         # /pagead/conversion/<id>/
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
        "domains": ["analytics.tiktok.com/api/v2/pixel"],
        "event_param": "event",
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
    "Microsoft Clarity": ["clarity.ms/collect", "clarity.ms/s/"],
}

# Payment services — only via network, never HTML
# (Jotform/other iframes reference Stripe in their CSS/JS → false positives)
NETWORK_ONLY_SERVICES = {"Stripe", "Paddle", "Gumroad"}

ANALYTICS_TOOLS = {"Microsoft Clarity", "Hotjar", "FullStory", "Lucky Orange"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def get_pixel_id_from_url(url: str, platform: str) -> str:
    """Достаёт ID пикселя/счётчика из tracking-URL. '' если нет.
    Path-regex (Meta SDK config, Google Ads) ловит ID даже когда beacon
    события подавлён — например Meta в headless."""
    rule = PIXEL_RULES.get(platform, {})
    try:
        parsed = urlparse(url)
        id_re = rule.get("id_path_re")
        if id_re:
            m = re.search(id_re, parsed.path)
            if m:
                return m.group(1)
        ip = rule.get("id_param")
        if ip:
            params = parse_qs(parsed.query)
            if ip in params and params[ip][0]:
                return params[ip][0]
    except Exception:
        pass
    return ""


def is_conversion_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER1.get(platform, []))


def is_partial_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER2.get(platform, []))


def is_noise_event(platform: str, event: str) -> bool:
    return event in NOISE_EVENTS.get(platform, [])


def detect_external_services(html: str, requests_urls: list = None) -> dict:
    found = {}
    html_lower = html.lower()
    for service, domains in EXTERNAL_SERVICES.items():
        if service in NETWORK_ONLY_SERVICES:
            continue
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


# ─── Network listeners ────────────────────────────────────────────────────────

def make_listeners(pixel_events: dict, web_pixel_urls: list, web_pixel_bodies: dict,
                   all_html_parts: list, pixel_ids: dict = None):
    """Returns (on_request, on_response) closures that populate shared dicts."""
    if pixel_ids is None:
        pixel_ids = {}

    def on_request(request):
        req_url = request.url
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
                    # Собираем ID пикселя — для presence (headless) и детекта дублей
                    pid = get_pixel_id_from_url(req_url, platform)
                    if pid:
                        pixel_ids.setdefault(platform, [])
                        if pid not in pixel_ids[platform]:
                            pixel_ids[platform].append(pid)
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
                    if "/web-pixels" in url:
                        web_pixel_bodies[url] = text
                except Exception:
                    pass
        except Exception:
            pass

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
    pixel_events    = pixel_events    or {}
    web_pixel_urls  = web_pixel_urls  or []
    web_pixel_bodies= web_pixel_bodies or {}
    all_html_parts  = all_html_parts  or []
    pixel_ids       = pixel_ids       or {}

    if extra_html:
        all_html_parts.append(extra_html)

    combined_html = "\n".join(all_html_parts)
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
                conversion_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif ev.get("is_partial"):
                partial_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif not ev["is_noise"]:
                noise_only = False

    missing_events = []
    for expected in expect_events:
        found = False
        for plat, events in pixel_events.items():
            if any(expected.lower() in e["event"].lower() for e in events):
                found = True
                break
        if not found:
            missing_events.append(expected)

    has_conv = len(conversion_events_found) > 0

    if has_conv:
        status = "✅ OK"
    else:
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
        "pixel_events": {
            plat: [e for e in events if not e["is_noise"]]
            for plat, events in pixel_events.items()
            if any(not e["is_noise"] for e in events)
        },
        "pixel_ids": {p: ids for p, ids in pixel_ids.items() if ids},
        "duplicate_pixels": [p for p, ids in pixel_ids.items() if len(ids) >= 2],
        "external_services": external_services,
        "content_analysis": content_analysis,
        "errors": [],
    }
