"""
Generic Scanner
==============
Для всех платформ кроме Shopify и WordPress:
Webflow, Squarespace, Wix, кастомные сайты, и т.д.
"""

from .base_scanner import (
    base_scan_page, make_listeners, detect_external_services
)
from .wordpress_scanner import _detect_cta_elements as _fallback_cta_detector


def scan_page(page, url: str, page_type: str, expect_events: list,
              cta_detector_fn=None, platform: str = "unknown") -> dict:

    pixel_events     = {}
    pixel_ids        = {}
    all_html_parts   = []
    web_pixel_urls   = []
    web_pixel_bodies = {}
    request_urls_all = []

    on_request, on_response = make_listeners(
        pixel_events, web_pixel_urls, web_pixel_bodies, all_html_parts, pixel_ids
    )

    def on_request_extended(request):
        request_urls_all.append(request.url)
        on_request(request)

    page.on("request", on_request_extended)
    page.on("response", on_response)

    errors = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
    except Exception as e:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(1000)
        except Exception as e2:
            errors.append(str(e2)[:100])

    try:
        main_html = page.content()
        all_html_parts.append(main_html)
    except Exception:
        main_html = ""

    if cta_detector_fn is not None:
        cta_elements = cta_detector_fn(page, platform=platform)
    else:
        cta_elements = _fallback_cta_detector(page)

    page.remove_listener("request", on_request_extended)
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
    result["cta_elements"] = list(set(cta_elements))[:8]
    result["has_cta"] = bool(cta_elements) or result["content_analysis"]["is_page_of_interest"]
    result["forms_count"] = result["content_analysis"]["forms_count"]
    result["ctas_in_html"] = {k: v[:3] for k, v in result["content_analysis"]["ctas"].items() if v}
    result["shopify_pixel_platforms"] = []
    result["errors"] = errors

    has_cta = result["has_cta"]
    has_conv = bool(result["conversion_events_found"])

    if has_conv:
        result["status"] = "✅ OK"
    elif has_cta:
        result["status"] = "🚨 GAP"
    else:
        result["status"] = "➖ NO CTA"

    return result
