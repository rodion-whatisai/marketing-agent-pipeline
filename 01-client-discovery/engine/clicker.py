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
from urllib.parse import urlparse

from utils import HEADERS
from log import log_debug, log_step, log_header, log_info, log_warn, log_success


MAX_BUTTONS = 12

# Слова CTA — для приоритезации кнопок (выше приоритет = раньше кликаем)
CTA_WORDS = (
    "book", "buy", "quote", "contact", "submit", "send", "apply", "get started",
    "add to cart", "add to bag", "add to basket", "request", "sign up", "subscribe",
    "order", "checkout", "demo", "enquir", "call", "get a", "reserve", "register",
    # FR
    "devis", "soumission", "contactez", "reserver", "commander", "ajouter",
)

# Ивенты «покупка/чекаут» — если стрельнули на НЕ-commerce странице → красный флаг
_PURCHASE_WORDS = ("purchase", "checkout", "placeanorder", "completepayment", "addpaymentinfo")

# Типы страниц, где purchase-ивент подозрителен (нет товара — а Purchase стрельнул)
NON_COMMERCE_TYPES = {
    "lead_form", "contact", "homepage", "search_results",
    "location", "use_case", "about", "faq_support", "blog_content",
}


# ─── JS: дженерик-поиск кнопок (адаптировано из wordpress_scanner._WP_CTA_JS) ──
# Стэмпит каждому выжившему кандидату data-tnc-btn="<index>" — стабильный хэндл
# для повторного локейта. Фильтрует nav/footer/cookie-шум и служебные тексты.
_DISCOVER_BUTTONS_JS = """
() => {
    const NOISE_SELECTORS = [
        'header','nav','footer',
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

    const SKIP_TEXTS = new Set(['close','ok','okay','cancel','dismiss','skip','back','accept','accept all','reject all','decline','allow','deny','agree','i agree','got it','save preferences','necessary only','accept cookies','reject cookies','manage cookies','cookie settings','search','menu','home','privacy policy','terms of service','view all','see all','load more','show more','more','next','continue','no thanks','maybe later','share','follow','print','previous','pause','play']);

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

# JS: читает события из dataLayer (GA4) и fbq.queue (Meta) — стреляют туда после клика
_READ_JS_EVENTS_JS = """
() => {
    const events = [];
    const NOISE = new Set(['gtm.js','gtm.init','gtm.load','gtm.dom','gtm.init_consent','page_view','user_engagement','session_start','first_visit','OneTrustLoaded','OptanonLoaded','OneTrustGroupsUpdated','dl_intelligems_script_loaded','dl_user_data','scroll','click','form_start','form_close']);
    if (window.dataLayer){ const seen=new Set(); for(const item of window.dataLayer){ if(item && item.event && !NOISE.has(item.event) && !seen.has(item.event)){ seen.add(item.event); events.push({platform:'Google Analytics', event:item.event}); } } }
    if (window.fbq && window.fbq.queue){ const seen=new Set(); for(const item of window.fbq.queue){ if(Array.isArray(item) && item[0]==='track' && !seen.has(item[1])){ seen.add(item[1]); events.push({platform:'Meta', event:item[1]}); } } }
    return events;
}
"""

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
    from scanners.base_scanner import (PIXEL_RULES, get_event_from_url,
                                        is_conversion_event, is_partial_event, is_noise_event)

    def on_request(request):
        buf = holder.get("buf")
        if buf is None:
            return
        req_url = request.url
        for platform, rules in PIXEL_RULES.items():
            for domain in rules["domains"]:
                if domain in req_url:
                    event = get_event_from_url(req_url, platform)
                    buf.setdefault(platform, [])
                    if not any(e["event"] == event for e in buf[platform]):
                        buf[platform].append({
                            "event": event,
                            "is_conversion": is_conversion_event(platform, event),
                            "is_partial": is_partial_event(platform, event),
                            "is_noise": is_noise_event(platform, event),
                            "source": "click",
                        })
                    return
    return on_request


def _read_js_events(page, buf: dict, debug: bool = False):
    """Подмешивает в buf события из dataLayer/fbq (живут только если клик НЕ увёл страницу)."""
    from scanners.base_scanner import is_conversion_event, is_partial_event, is_noise_event
    js_events = page.evaluate(_READ_JS_EVENTS_JS)
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


# ─── Дженерик-поиск кнопок ────────────────────────────────────────────────────

def discover_buttons(page, debug: bool = False) -> list:
    """Все значимые кнопки страницы (с проставленным data-tnc-btn), отсортированы по
    приоритету и обрезаны до MAX_BUTTONS."""
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


# ─── Привязка событий + красный флаг ──────────────────────────────────────────

def _is_purchase_type(tag: str) -> bool:
    ev = tag.split(":", 1)[-1].lower()
    return any(w in ev for w in _PURCHASE_WORDS)


def _flatten(buf: dict):
    fired, conv, partial = [], [], []
    for plat, evs in buf.items():
        for e in evs:
            if e.get("is_noise"):
                continue
            tag = f"{plat}:{e['event']}"
            if tag not in fired:
                fired.append(tag)
            if e.get("is_conversion") and tag not in conv:
                conv.append(tag)
            if e.get("is_partial") and tag not in partial:
                partial.append(tag)
    return fired, conv, partial


def _derive_red_flag(page_type: str, fired: list):
    """Красный флаг: purchase-type конверсия на не-commerce странице."""
    if page_type not in NON_COMMERCE_TYPES:
        return False, None
    bad = [t for t in fired if _is_purchase_type(t)]
    if bad:
        return True, f"purchase-type конверсия {bad} на странице типа '{page_type}'"
    return False, None


# ─── Основная функция ─────────────────────────────────────────────────────────

def click_page(page, url: str, page_type: str, platform: str = "unknown",
               debug: bool = False) -> dict:
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

    cands = discover_buttons(page, debug)
    if not cands:
        log_debug("click_page: кнопок не найдено")
        return result
    log_debug(f"click_page: {len(cands)} кнопок к клику")

    reloads = 0
    MAX_RELOADS = 4

    holder = {"buf": None}
    listener = make_pixel_listener(holder, debug)
    page.on("request", listener)
    try:
        page.context.on("request", listener)
    except Exception:
        pass

    try:
        for cand in cands:
            row = {"button_text": cand["text"], "button_tag": cand["tag"],
                   "is_form_submit": cand["isFormSubmit"], "clicked": False,
                   "navigated_to": None, "events_fired": [], "conversion_events": [],
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
                loc = page.locator(f'[data-tnc-btn="{idx}"]').first
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000, no_wait_after=True)  # сырой клик, не виснем на навигации
                row["clicked"] = True
                page.wait_for_timeout(2500)                  # окно для network-пикселей

                if page.url != pre:
                    row["navigated_to"] = page.url
                    log_debug(f"click_page: '{cand['text'][:30]}' увёл на {page.url[:50]}")
                else:
                    try:
                        _read_js_events(page, buf, debug)
                    except Exception as e:
                        log_debug(f"click_page: js read err: {str(e)[:60]}")

                fired, conv, partial = _flatten(buf)
                row["events_fired"] = fired
                row["conversion_events"] = conv
                row["partial_events"] = partial
                rf, reason = _derive_red_flag(page_type, fired)
                row["red_flag"], row["red_flag_reason"] = rf, reason
                if rf:
                    result["any_red_flag"] = True
                    log_warn(f"🚩 RED FLAG: '{cand['text'][:40]}' → {conv} на {page_type}")
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
        context = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1440, "height": 900})
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
    parser = argparse.ArgumentParser(description="TNC Clicker v2 — debug mode")
    parser.add_argument("url", help="URL страницы")
    parser.add_argument("page_type", nargs="?", default="homepage", help="Тип страницы (для red-flag логики)")
    parser.add_argument("platform", nargs="?", default="unknown", help="Платформа")
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--headless", action="store_true", default=False)
    args = parser.parse_args()

    run_standalone(args.url, args.page_type, args.platform, debug=args.debug, headless=args.headless)
