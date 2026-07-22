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

# Ивенты «корзина» на НЕ-commerce странице → жёлтый флаг (не красный): событие
# работает и данные в пиксель идут, но классом commerce — алгоритм платформы
# учится на «положил в корзину» вместо «оставил заявку». Гейт Rodion'а 2026-07-22
# (plurio.ai): Book a Demo → Meta:AddToCart. Book a Demo = намерение, не Lead;
# статус страницы остаётся OK, флаг = «стоит рассмотреть событие класса Lead».
_CART_WORDS = ("addtocart", "add_to_cart")

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
    from scanners.base_scanner import (PIXEL_RULES, get_event_from_request, match_pixel_platform,
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
        # POST-тело дочитывается только когда query пуст (BUGS-2026-07-13, Проблема 1):
        # на artbouquet Meta:AddToCart летит POST'ом именно по клику
        event = get_event_from_request(request, platform)
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


def _is_cart_type(tag: str) -> bool:
    ev = tag.split(":", 1)[-1].lower()
    return any(w in ev for w in _CART_WORDS)


def _derive_yellow_flag(page_type: str, fired: list):
    """Жёлтый флаг: cart-класс конверсия на lead-gen странице. Статус OK не меняет.
    Tested: 2026-07-22 on plurio.ai — BOOK A DEMO шлёт Meta:AddToCart на lead_form/homepage."""
    if page_type not in NON_COMMERCE_TYPES:
        return False, None
    sus = [t for t in fired if _is_cart_type(t)]
    if sus:
        return True, (f"при клике шлёт {', '.join(sus)} — событие commerce-класса на странице "
                      f"типа «{page_type}»; стоит рассмотреть событие класса Lead")
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
    result = {"url": url, "page_type": page_type, "buttons": [], "any_red_flag": False,
              "any_yellow_flag": False, "errors": []}

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
                   "partial_events": [], "red_flag": False, "red_flag_reason": None,
                   "yellow_flag": False, "yellow_flag_reason": None, "error": None}
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
                else:
                    # Жёлтый только когда нет красного: purchase-слова серьёзнее cart-слов.
                    yf, y_reason = _derive_yellow_flag(page_type, fired)
                    row["yellow_flag"], row["yellow_flag_reason"] = yf, y_reason
                    if yf:
                        result["any_yellow_flag"] = True
                        log_debug(f"click_page: YELLOW FLAG '{cand['text'][:40]}' → {conv} на {page_type}")
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


# ─── Form-fill journey (политика тестовых сабмитов, Rodion 2026-07-22) ────────
#
# Этап 1 («стреляет ли событие по пустой форме») уже покрыт основным проходом
# кнопок — Ctrl+клики по submit-кнопкам идут без данных. Здесь этап 2/3:
# заполняем видимые поля тестовыми значениями и жмём submit ПО-НАСТОЯЩЕМУ
# (без Ctrl) — ловим события после отправки PII (класс Lead?) и события,
# привязанные к SPA-навигации (кейс plurio: page_view_demo стреляет только
# при переходе внутри сайта, прямой заход его не показывает).

import re as _re

TEST_FORM_VALUES = {"email": "test@test.com", "tel": "5145550100", "text": "test"}
# Пароли и платёжные поля не заполняем никогда (политика в CLAUDE.md)
_FIELD_BLOCKLIST_RE = _re.compile(r"card|cvc|cvv|expir|iban|passw|search|captcha|coupon|promo", _re.I)
# Слова lead-класса: по ним отвечаем на вопрос «сработал ли Lead после PII».
# Строго конверсионные имена — gtm.formSubmit/framer_form_submit это plumbing,
# не конверсия (плюрио-урок: они стреляли всегда и давали ложное «Lead есть»).
_LEAD_WORDS = ("lead", "contact", "completeregistration", "generate_lead",
               "submitapplication")
# Рекламные пиксели — вопрос Rodion'а «сработал ли Lead» именно про них
# («пиксель» = Meta/TikTok, сверяемо Pixel Helper'ом; Google — «теги»)
_AD_PIXEL_PLATFORMS = {"Meta", "TikTok", "LinkedIn", "Pinterest", "Snapchat",
                       "Google Ads", "Bing/Microsoft", "Twitter/X"}
# Хосты внешних планировщиков — живое network-подтверждение (vs html-подстрока)
_SCHEDULER_HOSTS = ("calendly.com", "cal.com", "acuityscheduling.com",
                    "meetings.hubspot.com", "savvycal.com", "tidycal.com")

_DISCOVER_FIELDS_JS = """
() => {
  const out = [];
  let i = 0;
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  for (const el of document.querySelectorAll('input, textarea')) {
    const t = (el.type || 'text').toLowerCase();
    if (el.tagName === 'INPUT' && !['text', 'email', 'tel'].includes(t)) continue;
    if (el.disabled || el.readOnly || !vis(el)) continue;
    if ((el.value || '').trim()) continue;               // уже заполнено — не трогаем
    el.setAttribute('data-tnc-fill', String(i));
    out.push({ index: i, kind: el.tagName === 'TEXTAREA' ? 'textarea' : t,
               name: el.name || '', placeholder: el.placeholder || '',
               autocomplete: el.getAttribute('autocomplete') || '' });
    i++;
  }
  return out;
}"""


def _value_for_field(f: dict) -> str:
    """email/tel по типу; по подсказкам name/placeholder/autocomplete — уточняем."""
    hints = " ".join([f.get("name") or "", f.get("placeholder") or "",
                      f.get("autocomplete") or ""]).lower()
    if f.get("kind") == "email" or "mail" in hints:
        return TEST_FORM_VALUES["email"]
    if f.get("kind") == "tel" or _re.search(r"phone|tel|мобильн|телефон", hints):
        return TEST_FORM_VALUES["tel"]
    return TEST_FORM_VALUES["text"]


def _lead_class_tags(fired: list) -> list:
    """Теги lead-класса из списка событий (Meta:Lead, GA:elly_lead и т.п.)."""
    out = []
    for tag in fired:
        ev = tag.split(":", 1)[-1].lower().replace("_", "").replace(" ", "")
        if any(w.replace("_", "") in ev for w in _LEAD_WORDS):
            out.append(tag)
    return out


def _has_lead_class(fired: list) -> bool:
    return bool(_lead_class_tags(fired))


def fill_and_submit_form(page, url: str, page_type: str, debug: bool = False,
                          max_rounds: int = 2) -> dict:
    """Заполняет видимые поля тестовыми данными и жмёт submit по-настоящему.

    До max_rounds раундов (многошаговые формы: контакты → календарь).
    Финальный сторонний виджет (Calendly-слот) не трогаем — фиксируем только
    его network-появление. Returns dict для click_result["form_fill"].
    Tested: 2026-07-22 on plurio.ai/demo-booking-2-steps"""
    result = {"attempted": False, "rounds": 0, "fields": [], "submit_buttons": [],
              "events_fired": [], "conversion_events": [], "partial_events": [],
              "lead_class_event_after_submit": False, "navigated_to": None,
              "scheduler_hosts_seen": [], "error": None}

    holder = {"buf": None}
    listener = make_pixel_listener(holder, debug)
    sched_seen = set()

    def _sched_listener(req):
        u = req.url.lower()
        for h in _SCHEDULER_HOSTS:
            if h in u:
                sched_seen.add(h)

    page.on("request", listener)
    page.on("request", _sched_listener)
    try:
        for round_n in range(1, max_rounds + 1):
            fields = page.evaluate(_DISCOVER_FIELDS_JS)
            fields = [f for f in fields if not _FIELD_BLOCKLIST_RE.search(
                (f.get("name") or "") + " " + (f.get("placeholder") or "") + " " +
                (f.get("autocomplete") or ""))]
            if not fields:
                log_debug(f"fill_and_submit_form: раунд {round_n} — пустых полей нет, стоп")
                break

            result["attempted"] = True
            result["rounds"] = round_n
            for f in fields:
                val = _value_for_field(f)
                try:
                    page.locator(f'[data-tnc-fill="{f["index"]}"]').fill(val, timeout=3000)
                    result["fields"].append({"kind": f["kind"], "name": f.get("name") or
                                             f.get("placeholder") or "", "value": val})
                    log_debug(f"fill_and_submit_form: поле {f.get('name') or f.get('placeholder')!r} ← {val}")
                except Exception as e:
                    log_debug(f"fill_and_submit_form: поле {f} не заполнилось: {str(e)[:60]}")

            # Submit: сперва настоящий submit-элемент, затем формо-сабмитная CTA
            btn = page.locator('form button[type="submit"]:visible, form input[type="submit"]:visible').first
            try:
                has_btn = btn.count() > 0
            except Exception:
                has_btn = False
            if not has_btn:
                cands = discover_buttons(page, debug)
                sub = next((c for c in cands if c.get("isFormSubmit")), None)
                if sub is None:
                    log_debug("fill_and_submit_form: submit-кнопка не найдена — стоп")
                    break
                btn = page.locator(f'[data-tnc-btn="{sub["index"]}"]').first

            buf = {}
            holder["buf"] = buf
            try:
                marks = _js_watermarks(page)
            except Exception:
                marks = [0, 0]
            btn_text = ""
            try:
                btn_text = (btn.inner_text(timeout=1000) or btn.get_attribute("value") or "")
                # inner_text вложенных спанов дублирует надпись через \n — схлопываем
                btn_text = " ".join(dict.fromkeys(btn_text.split()))[:40]
            except Exception:
                pass
            log_debug(f"fill_and_submit_form: раунд {round_n} — настоящий клик submit «{btn_text}»")
            btn.click(timeout=5000, no_wait_after=True)      # БЕЗ Ctrl: навигация разрешена
            result["submit_buttons"].append(btn_text or "(submit)")
            page.wait_for_timeout(8000)                       # окно на события + SPA-переход

            if page.url != url:
                result["navigated_to"] = page.url
            try:
                _read_js_events(page, buf, marks, debug)
            except Exception as e:
                log_debug(f"fill_and_submit_form: js read err: {str(e)[:60]}")
            fired, conv, partial, _ = _flatten(buf)
            for t in fired:
                if t not in result["events_fired"]:
                    result["events_fired"].append(t)
            for t in conv:
                if t not in result["conversion_events"]:
                    result["conversion_events"].append(t)
            for t in partial:
                if t not in result["partial_events"]:
                    result["partial_events"].append(t)
            holder["buf"] = None
    except Exception as e:
        result["error"] = str(e)[:120]
        log_debug(f"fill_and_submit_form: исключение: {str(e)[:100]}")
    finally:
        holder["buf"] = None
        for fn in (listener, _sched_listener):
            try:
                page.remove_listener("request", fn)
            except Exception:
                pass

    lead_tags = _lead_class_tags(result["events_fired"])
    result["lead_class_events"] = lead_tags
    # Раздельный вердикт: рекламный пиксель (Meta и т.п.) vs dataLayer-маркеры.
    # Плюрио-кейс: elly_lead в dataLayer есть, Meta:Lead — нет; смешивать нельзя.
    result["ad_pixel_lead_fired"] = any(
        t.split(":", 1)[0] in _AD_PIXEL_PLATFORMS for t in lead_tags)
    result["lead_class_event_after_submit"] = bool(lead_tags)
    result["scheduler_hosts_seen"] = sorted(sched_seen)
    log_debug(f"fill_and_submit_form: done rounds={result['rounds']} "
              f"events={result['events_fired']} lead={result['lead_class_event_after_submit']} "
              f"schedulers={result['scheduler_hosts_seen']}")
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
