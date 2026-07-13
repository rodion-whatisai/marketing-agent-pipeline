"""
TNC Pipeline — Clicker v2.0
============================
Кликает по ВСЕМ значимым кнопкам страницы и фиксирует, КАКОЙ трекинг-ивент
стрельнул по каждой. Цель — ловить misconfiguration: например, кнопка лид-формы,
которая стреляет `Purchase` → красный флаг (конверсия не того типа).

По умолчанию включён в step2 (выключить: `step2 --no-click`). Кликает на странице
любого типа. Формы НЕ заполняет, но submit-кнопки жмёт (сырой клик — пустые формы
обычно отбиваются клиентской валидацией). Клик, который уводит страницу, не ломает
прогон: перед следующей кнопкой состояние восстанавливается reload'ом.

Запуск standalone (debug):
    python clicker.py https://example.com/contact lead_form
"""

import argparse
from urllib.parse import urlparse, parse_qs

from utils import HEADERS
from log import log_debug, log_step, log_header, log_info, log_warn, log_success
from scanners.base_scanner import discover_buttons


# Ивенты «покупка/чекаут» — если стрельнули на НЕ-commerce странице → красный флаг
_PURCHASE_WORDS = ("purchase", "checkout", "placeanorder", "completepayment", "addpaymentinfo")

# Типы страниц, где purchase-ивент подозрителен (нет товара — а Purchase стрельнул)
NON_COMMERCE_TYPES = {
    "lead_form", "contact", "homepage", "search_results",
    "location", "use_case", "about", "faq_support", "blog_content",
}


# JS: читает события из dataLayer (GA4) и fbq.queue (Meta) — стреляют туда после клика.
# Принимает watermarks [dlOff, fbqOff] — длины ДО клика: читаем только новые элементы,
# иначе кумулятивный dataLayer приписывает событие первой кнопки всем последующим
# (Ctrl+клик не навигирует → один dataLayer живёт на все кнопки страницы).
# Clamp-to-0 — на случай сайтов, переприсваивающих window.dataLayer на лету.
# Tested: 2026-07-07 on tinytronics.nl homepage — до фикса Verlanglijst и Next slide
#         наследовали add_to_cart от Toevoegen (3 byte-identical записи в step2.json)

# Шаг B (2026-07-13): платформенная часть шума — из единого реестра platforms.py
# (была четвёртой локальной копией). dataLayer-специфика (GTM-этапы, записи CMP
# и приложений — не платформенные события) остаётся явной надбавкой здесь.
import json as _json
import platforms as _platforms

_DATALAYER_EXTRA_NOISE = [
    "gtm.load", "gtm.dom",
    "OneTrustLoaded", "OptanonLoaded", "OneTrustGroupsUpdated",
    "dl_intelligems_script_loaded", "dl_user_data",
]
_JS_NOISE_JSON = _json.dumps(sorted(
    set(_platforms.as_noise_events()["Google Analytics"]) | set(_DATALAYER_EXTRA_NOISE)))

_READ_JS_EVENTS_JS = """
([dlOff, fbqOff]) => {
    const events = [];
    const NOISE = new Set(__NOISE_JSON__);
    if (window.dataLayer){ const arr = window.dataLayer; const start = (dlOff <= arr.length) ? dlOff : 0; const seen=new Set(); for(const item of arr.slice(start)){ if(item && item.event && !NOISE.has(item.event) && !seen.has(item.event)){ seen.add(item.event); events.push({platform:'Google Analytics', event:item.event}); } } }
    if (window.fbq && window.fbq.queue){ const q = window.fbq.queue; const qs = (fbqOff <= q.length) ? fbqOff : 0; const seen=new Set(); for(const item of q.slice(qs)){ if(Array.isArray(item) && item[0]==='track' && !seen.has(item[1])){ seen.add(item[1]); events.push({platform:'Meta', event:item[1]}); } } }
    return events;
}
""".replace("__NOISE_JSON__", _JS_NOISE_JSON)

def _js_watermarks(page) -> list:
    """Длины dataLayer/fbq.queue ПЕРЕД кликом — _read_js_events прочтёт только новое."""
    return page.evaluate(
        "() => [window.dataLayer ? window.dataLayer.length : 0,"
        " (window.fbq && window.fbq.queue) ? window.fbq.queue.length : 0]")

# Shopify dl_-события → стандартные GA4
DL_CONVERSION_MAP = {
    "dl_add_to_cart":    ("Google Analytics", "add_to_cart"),
    "dl_view_item":      ("Google Analytics", "view_item"),
    "dl_begin_checkout": ("Google Analytics", "begin_checkout"),
    "dl_purchase":       ("Google Analytics", "purchase"),
    "dl_generate_lead":  ("Google Analytics", "generate_lead"),
    "dl_search":         ("Google Analytics", "search"),
    "dl_view_item_list": ("Google Analytics", "view_item_list"),
}


# ─── Перехват pixel events (network) ──────────────────────────────────────────

def make_pixel_listener(holder: dict, debug: bool = False):
    """on_request handler, пишущий в holder['buf']. Буфер свопается на каждую кнопку
    (без detach/attach) — так события привязываются к конкретному клику."""
    from scanners.base_scanner import (PIXEL_RULES, get_event_from_url, match_pixel_platform,
                                        is_conversion_event, is_partial_event, is_noise_event)

    def on_request(request):
        buf = holder.get("buf")
        if buf is None:
            return
        req_url = request.url
        # единый матчер с load-фазой: границы слов + lowercase (ревью дня 6 —
        # голая подстрока по оригинальному URL расходилась с base_scanner в обе
        # стороны и приписывала кнопке чужие конверсионные события, класс A3)
        platform = match_pixel_platform(req_url)
        if platform is None:
            return
        rules = PIXEL_RULES[platform]
        event = get_event_from_url(req_url, platform)
        # Какой именно пиксель стрельнул: id из ?id=<pixel> (для дубль-пикселей критично)
        pid = None
        id_param = rules.get("id_param")
        if id_param:
            _vals = parse_qs(urlparse(req_url).query).get(id_param)
            if _vals:
                pid = _vals[0]
        buf.setdefault(platform, [])
        if not any(e["event"] == event for e in buf[platform]):
            buf[platform].append({
                "event": event,
                "is_conversion": is_conversion_event(platform, event),
                "is_partial": is_partial_event(platform, event),
                "is_noise": is_noise_event(platform, event),
                "source": "click",
                "pixel_id": pid,
            })
    return on_request


def _read_js_events(page, buf: dict, marks: list = None, debug: bool = False):
    """Подмешивает в buf события из dataLayer/fbq (живут только если клик НЕ увёл страницу).
    marks = watermarks от _js_watermarks (снятые до клика); None → читать всё (легаси)."""
    from scanners.base_scanner import is_conversion_event, is_partial_event, is_noise_event
    js_events = page.evaluate(_READ_JS_EVENTS_JS, marks or [0, 0])
    for ev in js_events:
        plat, name = ev["platform"], ev["event"]
        if name in DL_CONVERSION_MAP:
            mplat, mname = DL_CONVERSION_MAP[name]
            for en in (name, mname):
                buf.setdefault(mplat, [])
                if not any(e["event"] == en for e in buf[mplat]):
                    buf[mplat].append({"event": en, "is_conversion": is_conversion_event(mplat, en),
                                       "is_partial": is_partial_event(mplat, en), "is_noise": False, "source": "js"})
        else:
            buf.setdefault(plat, [])
            if not any(e["event"] == name for e in buf[plat]):
                buf[plat].append({"event": name, "is_conversion": is_conversion_event(plat, name),
                                  "is_partial": is_partial_event(plat, name), "is_noise": is_noise_event(plat, name), "source": "js"})


# ─── Привязка событий + красный флаг ──────────────────────────────────────────

def _is_purchase_type(tag: str) -> bool:
    ev = tag.split(":", 1)[-1].lower()
    return any(w in ev for w in _PURCHASE_WORDS)


def _flatten(buf: dict):
    fired, conv, partial = [], [], []
    pixel_by_tag = {}   # "Meta:Purchase" → "1063725220370121" (какой пиксель стрельнул событие)
    for plat, evs in buf.items():
        for e in evs:
            if e.get("is_noise"):
                continue
            tag = f"{plat}:{e['event']}"
            if tag not in fired:
                fired.append(tag)
            if e.get("pixel_id") and tag not in pixel_by_tag:
                pixel_by_tag[tag] = e["pixel_id"]
            if e.get("is_conversion") and tag not in conv:
                conv.append(tag)
            if e.get("is_partial") and tag not in partial:
                partial.append(tag)
    return fired, conv, partial, pixel_by_tag


def _derive_red_flag(page_type: str, fired: list):
    """Красный флаг: purchase-type конверсия на не-commerce странице."""
    if page_type not in NON_COMMERCE_TYPES:
        return False, None
    bad = [t for t in fired if _is_purchase_type(t)]
    if bad:
        return True, f"при клике шлёт {', '.join(bad)} — на странице типа «{page_type}» purchase-событие быть не должно"
    return False, None


# ─── Основная функция ─────────────────────────────────────────────────────────

def click_page(page, url: str, page_type: str, platform: str = "unknown",
               debug: bool = False, cands: list = None) -> dict:
    """Кликает по всем значимым кнопкам страницы, фиксирует ивенты на каждую.

    Returns: {url, page_type, buttons:[{button_text, button_tag, is_form_submit,
              clicked, navigated_to, events_fired, conversion_events, partial_events,
              red_flag, red_flag_reason, error}], any_red_flag, errors}
    """
    log_debug(f"click_page: start url={url} page_type={page_type} platform={platform}")
    result = {"url": url, "page_type": page_type, "buttons": [], "any_red_flag": False, "errors": []}

    from popup_handler import handle_popups
    # Баннеры уже закрыл сканер на этой же странице/сессии — не дублируем сюда.
    page.wait_for_timeout(300)
    landing = page.url  # реальный текущий адрес (после возможного редиректа) — базлайн recovery

    # Сканер уже нашёл и пометил кнопки (data-tnc-btn) на этой же странице — берём их.
    # Если не передали (shopify/wordpress пути, standalone) — ищем сами тем же детектором.
    if cands is None:
        cands = discover_buttons(page, debug)
    if not cands:
        log_debug("click_page: кнопок не найдено")
        return result
    log_debug(f"click_page: {len(cands)} кнопок к клику")

    reloads = 0
    # По одному релоду на кнопку: каждый клик может увести → нужен возврат на страницу.
    # Потолок = число кнопок (а не высосанное 4) — легитимные клики не режем.
    MAX_RELOADS = len(cands)

    holder = {"buf": None}
    listener = make_pixel_listener(holder, debug)
    page.on("request", listener)
    try:
        page.context.on("request", listener)
    except Exception:
        pass

    # Ctrl+клик открывает ссылку в ФОНОВОЙ вкладке: основная страница не уходит,
    # а событие клика (напр. Meta SubscribedButtonClick) стреляет на ней. Фоновую
    # вкладку сразу закрываем, чтобы её load-события (PageView и т.п.) не попали в буфер.
    def _close_popup(p):
        try:
            p.close()
        except Exception:
            pass
    try:
        page.context.on("page", _close_popup)
    except Exception:
        pass

    try:
        for cand in cands:
            row = {"button_text": cand["text"], "button_tag": cand["tag"],
                   "is_form_submit": cand["isFormSubmit"], "clicked": False,
                   "navigated_to": None, "events_fired": [], "events_pixel": {}, "conversion_events": [],
                   "partial_events": [], "red_flag": False, "red_flag_reason": None, "error": None}
            try:
                idx = cand["index"]
                # Восстановление: предыдущий клик увёл страницу → reload landing + re-locate по тексту.
                # Базлайн — landing (реальный адрес), а НЕ номинальный url: страница могла
                # 302-редиректнуть (contact-us-confirmation → contact-us) и тогда page.url != url
                # всегда → reload-петля. Сравниваем с landing + потолок reload'ов.
                if page.url != landing:
                    if reloads >= MAX_RELOADS:
                        log_warn(f"click_page: потолок reload'ов ({MAX_RELOADS}) — прекращаю клики на странице")
                        break
                    reloads += 1
                    log_debug(f"click_page: url увело ({page.url[:50]}) — reload {landing[:50]} ({reloads}/{MAX_RELOADS})")
                    page.goto(landing, wait_until="domcontentloaded", timeout=15000)
                    try:
                        handle_popups(page)
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    new_cands = discover_buttons(page, debug)
                    match = next((c for c in new_cands if (c["text"] or "").lower() == (cand["text"] or "").lower()), None)
                    if match is None:
                        row["error"] = "кнопка пропала после reload"
                        result["buttons"].append(row)
                        continue
                    idx = match["index"]

                buf = {}
                holder["buf"] = buf
                pre = page.url
                try:
                    marks = _js_watermarks(page)   # длины dataLayer/fbq ДО клика
                except Exception:
                    marks = [0, 0]
                loc = page.locator(f'[data-tnc-btn="{idx}"]').first
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000, no_wait_after=True, modifiers=["Control"])  # Ctrl+клик: ссылка в фон, страница не уходит
                row["clicked"] = True
                page.wait_for_timeout(2500)                  # окно для network-пикселей

                if page.url != pre:
                    row["navigated_to"] = page.url
                    log_debug(f"click_page: '{cand['text'][:30]}' увёл на {page.url[:50]}")
                else:
                    try:
                        _read_js_events(page, buf, marks, debug)
                    except Exception as e:
                        log_debug(f"click_page: js read err: {str(e)[:60]}")

                fired, conv, partial, pixel_by_tag = _flatten(buf)
                row["events_fired"] = fired
                row["events_pixel"] = pixel_by_tag
                row["conversion_events"] = conv
                row["partial_events"] = partial
                rf, reason = _derive_red_flag(page_type, fired)
                row["red_flag"], row["red_flag_reason"] = rf, reason
                if rf:
                    result["any_red_flag"] = True
                    log_debug(f"click_page: RED FLAG '{cand['text'][:40]}' → {conv} на {page_type} (рендерится в step2 «События»)")
            except Exception as e:
                row["error"] = str(e)[:100]
                log_debug(f"click_page: button '{cand['text'][:30]}' error: {str(e)[:80]}")
            finally:
                holder["buf"] = None
            result["buttons"].append(row)
    finally:
        try:
            page.remove_listener("request", listener)
        except Exception:
            pass
        try:
            page.context.remove_listener("request", listener)
        except Exception:
            pass
        try:
            page.context.remove_listener("page", _close_popup)
        except Exception:
            pass

    log_debug(f"click_page: done {len(result['buttons'])} кнопок, any_red_flag={result['any_red_flag']}")
    return result


# ─── CLI (standalone debug) ───────────────────────────────────────────────────

def run_standalone(url: str, page_type: str, platform: str = "unknown",
                   debug: bool = True, headless: bool = False):
    """Запуск напрямую для отладки одной страницы."""
    from playwright.sync_api import sync_playwright
    from popup_handler import handle_popups

    log_header("TNC Clicker — Debug Mode")
    print(f"  URL:   {url}")
    print(f"  Type:  {page_type}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1440, "height": 900},
                                      ignore_https_errors=True)  # клиентский сайт: битый сертификат — не повод падать
        page = context.new_page()

        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        log_step(f"Открываем homepage {base_url}...", emoji="🌐")
        page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        handle_popups(page, verbose=True)
        page.wait_for_timeout(1000)

        if url != base_url:
            log_step(f"Переходим на {url}...", emoji="🌐")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)

        log_step("Кликаем по кнопкам...", emoji="🖱")
        result = click_page(page, url, page_type, platform=platform, debug=debug)

        log_header("РЕЗУЛЬТАТ — кнопки")
        for b in result["buttons"]:
            ev = ", ".join(b["events_fired"]) or "—"
            flag = " 🚩" if b["red_flag"] else ""
            nav = f"  → {b['navigated_to'][:40]}" if b["navigated_to"] else ""
            mark = "✓" if b["clicked"] else "×"
            print(f"  [{mark}] {b['button_text'][:32]:32} | {ev}{flag}{nav}")
            if b["red_flag"]:
                print(f"        🚩 {b['red_flag_reason']}")
            if b["error"]:
                print(f"        ⚠️  {b['error']}")
        if result["any_red_flag"]:
            print("\n  🚩 НАЙДЕНЫ КРАСНЫЕ ФЛАГИ — см. выше")
        print()

        if not headless:
            input("Enter чтобы закрыть браузер...")
        browser.close()


if __name__ == "__main__":
    from utils import setup_console
    setup_console()  # UTF-8 до первого вывода: русский argparse-help и log_header("═"×65) падали на cp1252
    # Tested: 2026-07-09 --help под PYTHONIOENCODING=cp1252 — help печатается, exit 0
    parser = argparse.ArgumentParser(description="TNC Clicker v2 — debug mode")
    parser.add_argument("url", help="URL страницы")
    parser.add_argument("page_type", nargs="?", default="homepage", help="Тип страницы (для red-flag логики)")
    parser.add_argument("platform", nargs="?", default="unknown", help="Платформа")
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--headless", action="store_true", default=False)
    args = parser.parse_args()

    run_standalone(args.url, args.page_type, args.platform, debug=args.debug, headless=args.headless)
