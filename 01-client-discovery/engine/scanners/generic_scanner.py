"""
Generic Scanner
==============
Для всех платформ кроме Shopify и WordPress:
Webflow, Squarespace, Wix, кастомные сайты, и т.д.
"""

from .base_scanner import (
    base_scan_page, make_listeners, detect_external_services, discover_buttons,
    capture_pixel_hit, navigate_and_gate, gated_result,
)
from log import log_debug, log_warn, log_info


def scan_page(page, url: str, page_type: str, expect_events: list,
              cta_detector_fn=None, platform: str = "unknown") -> dict:

    log_debug(f"scan_page: start url={url} page_type={page_type} platform={platform}")

    pixel_events     = {}
    pixel_ids        = {}
    all_html_parts   = []
    web_pixel_urls   = []
    web_pixel_bodies = {}
    request_urls_all = []
    pixel_hits       = []   # {url, method, body_snippet} — улики стенда (POST-тела)

    on_request, on_response = make_listeners(
        pixel_events, web_pixel_urls, web_pixel_bodies, all_html_parts, pixel_ids
    )

    def on_request_extended(request):
        request_urls_all.append(request.url)
        capture_pixel_hit(request, pixel_hits)
        on_request(request)

    page.on("request", on_request_extended)
    page.on("response", on_response)
    log_debug(f"scan_page: listeners attached, navigating to {url}")

    # Шлюз (день 7): goto с ретраем + вердикт «жива ли и та ли страница»
    gate = navigate_and_gate(page, url, settle_ms=1500, retry_settle_ms=1000)
    errors = list(gate.get("errors", []))
    if gate.get("http_error") or gate.get("redirected"):
        page.remove_listener("request", on_request_extended)
        page.remove_listener("response", on_response)
        return gated_result(url, page_type, gate)

    try:
        log_debug(f"scan_page: capturing page content for {url}")
        main_html = page.content()
        all_html_parts.append(main_html)
    except Exception as e:
        log_debug(f"scan_page: page.content() failed for {url}: {e}")
        main_html = ""

    # Один детектор кнопок (общий с кликером): находит, помечает data-tnc-btn, отдаёт список.
    # Кликер потом возьмёт ровно cta_buttons — отсюда «CTA: N» и «кликнули N/N» совпадают.
    cta_buttons = discover_buttons(page)
    cta_elements = [c["text"] for c in cta_buttons]
    log_info(f"       CTA: {len(cta_buttons)} найдено")
    log_debug(f"scan_page: discover_buttons → {len(cta_buttons)} CTA for {url}")

    page.remove_listener("request", on_request_extended)
    page.remove_listener("response", on_response)
    log_debug(f"scan_page: listeners removed, {len(request_urls_all)} requests captured for {url}")

    # Дайджест «кого слушал / что поймал» — сырьё (per-request) ушло в FIRE
    _digest = []
    for _plat, _evs in pixel_events.items():
        _names = sorted({e["event"] for e in _evs})
        _digest.append(f"{_plat}[{', '.join(_names)}]" if _names else _plat)
    log_debug(f"👂 Слушал {len(request_urls_all)} запросов → "
              + (", ".join(_digest) if _digest else "пиксели не пойманы"))

    combined_html = "\n".join(all_html_parts)

    log_debug(f"scan_page: running base_scan_page for {url}")
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
    # Сырьё для постмортемов: без него дебаг «почему пиксели не пойманы» требует
    # живого перескана (2026-07-07: 76 URL tinytronics были потеряны — пришлось переезжать)
    result["network_requests"] = request_urls_all[:300]
    result["pixel_hits"] = pixel_hits                          # улики стенда: метод+тело
    result["gate"] = gate                                      # шлюз: финальный URL/статус
    result["cta_buttons"] = cta_buttons                       # полный список (помечен в DOM) — для кликера
    result["cta_elements"] = cta_elements[:8]                 # порядок приоритета сохранён (JS уже дедупит)
    result["has_cta"] = bool(cta_buttons) or result["content_analysis"]["is_page_of_interest"]
    result["forms_count"] = result["content_analysis"]["forms_count"]
    result["ctas_in_html"] = {k: v[:3] for k, v in result["content_analysis"]["ctas"].items() if v}
    result["shopify_pixel_platforms"] = []
    result["errors"] = errors

    has_cta = result["has_cta"]
    has_conv = bool(result["conversion_events_found"])

    if has_conv:
        log_debug(f"scan_page: {url} → status OK (conversion event found)")
        result["status"] = "✅ OK"
    elif has_cta:
        log_debug(f"scan_page: {url} → status GAP (CTA present, no conversion event)")
        result["status"] = "🚨 GAP"
    else:
        log_debug(f"scan_page: {url} → status NO CTA")
        result["status"] = "➖ NO CTA"

    log_debug(f"scan_page: done url={url} status={result['status']}")
    return result
