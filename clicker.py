"""
TNC Pipeline — Clicker v1.0
============================
Симулирует пользовательское взаимодействие со страницей
и перехватывает pixel events которые стреляют после кликов.

Поддерживаемые типы страниц:
    product       — кликает Add to Cart / Add to Bag
    search_results — кликает на первый продукт в листинге
    homepage      — кликает на первый продукт

НЕ кликает:
    lead_form     — формы (риск отправки реальных данных)
    checkout      — платёжные страницы

Запуск standalone (debug):
    python clicker.py https://www.thebodyshop.com/products/100-shea-butter product shopify
    python clicker.py https://www.thebodyshop.com/collections/body-butters search_results shopify --debug
"""

import re
import sys
import time
import argparse
import datetime
from urllib.parse import urlparse, parse_qs

from utils import HEADERS


# ─── Типы страниц которые кликаем ────────────────────────────────────────────

# Типы страниц которые кликаем в step2 --click режиме
# Только product — навигация (homepage/search_results) ломает контекст scan_page
CLICKABLE_TYPES = {"product"}

# Типы для standalone дебага (clicker.py напрямую) — включают навигацию
CLICKABLE_TYPES_STANDALONE = {"product", "search_results", "homepage"}

# Типы которые НЕ кликаем — документируем явно
SKIP_TYPES = {
    "lead_form":       "риск отправки реальных данных",
    "checkout":        "платёжная страница",
    "booking_confirm": "страница подтверждения",
    "quote":           "форма запроса",
}


# ─── Add to Cart селекторы (Shopify) ─────────────────────────────────────────

# Порядок важен — более специфичные первыми
ADD_TO_CART_SELECTORS = [
    # Shopify стандарт
    "[name='add']",
    "[data-add-to-cart]",
    ".product-form__submit",
    ".shopify-payment-button button",
    # Кнопки по тексту (aria-label или текст)
    "button[type='submit']",
    # Общие паттерны
    "[class*='add-to-cart']",
    "[class*='add_to_cart']",
    "[class*='addtocart']",
    "[id*='add-to-cart']",
    "[class*='btn-cart']",
]

# Тексты Add to Cart кнопок (case-insensitive)
ADD_TO_CART_TEXTS = [
    "add to cart",
    "add to bag",
    "add to basket",
    "add to trolley",
    "buy now",
    "buy it now",
    "добавить в корзину",
    "ajouter au panier",
    "in den warenkorb",
]

# Селекторы карточек продуктов в листинге
PRODUCT_CARD_SELECTORS = [
    # Shopify стандарт
    ".product-card a",
    ".card__content a",
    "[class*='product-item'] a",
    "[class*='product-card'] a",
    ".collection__grid a[href*='/products/']",
    ".product-grid a[href*='/products/']",
    # Общие
    "a[href*='/products/']",
]


# ─── Pixel event listener ─────────────────────────────────────────────────────

def make_pixel_listener(pixel_events: dict, debug: bool = False):
    """Создаёт on_request handler для перехвата pixel events после кликов."""
    from step2_scan import PIXEL_RULES, get_event_from_url, is_conversion_event, is_partial_event, is_noise_event

    def on_request(request):
        req_url = request.url
        # Shopify web-pixels — логируем в debug
        if "/web-pixels" in req_url and debug:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"       [{ts}] 🔷 [web-pixel] {req_url[:80]}")

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
                        "source": "click",
                    }
                    if not any(e["event"] == event for e in pixel_events[platform]):
                        pixel_events[platform].append(entry)
                    if debug:
                        flag = "🎯" if entry["is_conversion"] else ("🔸" if entry["is_partial"] else "·")
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"       [{ts}] {flag} [click→{platform}] {event}")
                    break

    return on_request


# ─── Поиск Add to Cart ────────────────────────────────────────────────────────

def find_add_to_cart(page, debug: bool = False) -> str | None:
    """
    Ищет кнопку Add to Cart на product page.
    Возвращает selector или None если не нашёл.
    """
    # 1. По CSS селекторам
    for sel in ADD_TO_CART_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                if debug:
                    print(f"       🛒 Add to Cart найден: [{sel}]")
                return sel
        except Exception:
            pass

    # 2. По тексту — ищем все кнопки с нужным текстом, берём первую visible
    for text in ADD_TO_CART_TEXTS:
        try:
            buttons = page.get_by_role("button", name=text, exact=False).all()
            for btn in buttons:
                try:
                    if btn.is_visible(timeout=200):
                        if debug:
                            print(f"       🛒 Add to Cart по тексту: '{text}'")
                        # Возвращаем через evaluate чтобы получить стабильный selector
                        return f"button-text:{text}"
                except Exception:
                    pass
        except Exception:
            pass

    if debug:
        print(f"       ⚠️  Add to Cart не найден")
    return None


def find_first_product_link(page, debug: bool = False) -> str | None:
    """
    Ищет ссылку на первый продукт в листинге.
    Возвращает href или None.
    """
    for sel in PRODUCT_CARD_SELECTORS:
        try:
            links = page.locator(sel).all()
            for link in links[:5]:
                href = link.get_attribute("href")
                if href and "/products/" in href:
                    # Строим полный URL
                    if href.startswith("/"):
                        base = page.url.split("/")[0] + "//" + page.url.split("/")[2]
                        href = base + href
                    if debug:
                        print(f"       🔗 Первый продукт: {href[:80]}")
                    return href
        except Exception:
            pass

    if debug:
        print(f"       ⚠️  Ссылка на продукт не найдена")
    return None


# ─── Основная функция ─────────────────────────────────────────────────────────

def click_page(page, url: str, page_type: str, platform: str = "unknown",
               debug: bool = False) -> dict:
    """
    Выполняет клики на странице и собирает pixel events.

    Args:
        page:       Playwright page (уже загружена)
        url:        URL страницы
        page_type:  тип из step1 классификации
        platform:   shopify / wordpress / etc
        debug:      verbose output

    Returns:
        {
            "clicked": bool,
            "action": str,           # что кликнули
            "new_events": dict,      # события после клика {platform: [events]}
            "conversion_events": [],  # конверсионные события
            "navigated_to": str,     # если перешли на другую страницу
            "error": str | None,
        }
    """
    result = {
        "clicked": False,
        "action": None,
        "new_events": {},
        "conversion_events": [],
        "navigated_to": None,
        "error": None,
    }

    # Проверяем что тип поддерживается
    if page_type not in CLICKABLE_TYPES:
        reason = SKIP_TYPES.get(page_type, "не поддерживается")
        result["error"] = f"skip: {reason}"
        if debug:
            print(f"       ⏭  Клик пропущен ({reason})")
        return result

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    if debug:
        print(f"       [{ts}] 🖱  Начинаем клики для {page_type}...")

    # Подключаем listener на page И на context (web-pixels идут через context)
    pixel_events = {}
    listener = make_pixel_listener(pixel_events, debug=debug)
    page.on("request", listener)
    try:
        page.context.on("request", listener)
    except Exception:
        pass

    try:
        if page_type == "product":
            result = _click_product_page(page, url, pixel_events, result, debug)

        elif page_type in ("search_results", "homepage"):
            result = _click_listing_page(page, url, pixel_events, result, debug)

    except Exception as e:
        result["error"] = str(e)[:100]
        if debug:
            print(f"       ❌ Ошибка клика: {e}")
    finally:
        page.remove_listener("request", listener)
        try:
            page.context.remove_listener("request", listener)
        except Exception:
            pass

    # Собираем конверсионные события
    for platform, events in pixel_events.items():
        for ev in events:
            if ev["is_conversion"] or ev["is_partial"]:
                result["conversion_events"].append(f"{platform}:{ev['event']}")

    result["new_events"] = {
        plat: [e for e in evts if not e["is_noise"]]
        for plat, evts in pixel_events.items()
        if any(not e["is_noise"] for e in evts)
    }

    return result


def _click_product_page(page, url: str, pixel_events: dict, result: dict, debug: bool) -> dict:
    """Кликает Add to Cart на product page."""
    from popup_handler import handle_popups

    # Закрываем баннеры и consent (могут появиться на новой странице)
    popup_result = handle_popups(page, verbose=debug)

    # Если consent был принят — ждём дольше пока теги загрузятся и страница перерисуется
    if popup_result["cookie_consent"] == "accepted_all":
        wait_ms = popup_result.get("wait_after_ms", 5000)
        if debug:
            print(f"       ⏳ Ждём {wait_ms}ms после consent...")
        page.wait_for_timeout(wait_ms)
    else:
        page.wait_for_timeout(500)

    atc_selector = find_add_to_cart(page, debug=debug)

    if not atc_selector:
        result["error"] = "add_to_cart_not_found"
        return result

    try:
        # Получаем элемент в зависимости от типа selector
        if atc_selector.startswith("button-text:"):
            text = atc_selector.replace("button-text:", "")
            buttons = page.get_by_role("button", name=text, exact=False).all()
            el = next((b for b in buttons if b.is_visible(timeout=200)), None)
            if el is None:
                result["error"] = "add_to_cart_visible_not_found"
                return result
        else:
            el = page.locator(atc_selector).first
            if not el.is_visible(timeout=3000):
                result["error"] = "add_to_cart_not_visible"
                return result

        # Если кнопка disabled — пробуем выбрать первый вариант продукта
        is_disabled = el.get_attribute("disabled") is not None or \
                      "disabled" in (el.get_attribute("class") or "")
        if is_disabled:
            if debug:
                print(f"       ⚠️  Кнопка disabled — ищем варианты продукта...")
            for variant_sel in [
                "input[type='radio']:not([disabled]) + label",
                ".product-form__input input[type='radio']:first-child + label",
                "[data-option-item]:first-child",
                ".swatch:first-child",
            ]:
                try:
                    v = page.locator(variant_sel).first
                    if v.is_visible(timeout=500):
                        v.click(timeout=1000)
                        page.wait_for_timeout(500)
                        if debug:
                            print(f"       ✓ Вариант выбран: [{variant_sel}]")
                        break
                except Exception:
                    pass

        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=5000)
        result["clicked"] = True
        result["action"] = f"click:add_to_cart [{atc_selector}]"

        # Ждём pixel events — web-pixels могут стрелять с задержкой
        page.wait_for_timeout(6000)

        # Читаем события из dataLayer и fbq queue (web-pixels пишут туда)
        try:
            js_events = page.evaluate("""
            () => {
                const events = [];

                // Шумовые события которые не интересуют
                const NOISE = new Set([
                    'gtm.js','gtm.init','gtm.load','gtm.dom','gtm.init_consent',
                    'page_view','user_engagement','session_start','first_visit',
                    'OneTrustLoaded','OptanonLoaded','OneTrustGroupsUpdated',
                    'dl_intelligems_script_loaded','dl_user_data',
                    'scroll','click','form_start','form_close',
                ]);

                // dataLayer — GA4 / кастомные события
                if (window.dataLayer) {
                    const seen = new Set();
                    for (const item of window.dataLayer) {
                        if (item.event && !NOISE.has(item.event) && !seen.has(item.event)) {
                            seen.add(item.event);
                            events.push({source: 'dataLayer', platform: 'Google Analytics', event: item.event});
                        }
                    }
                }

                // fbq — Meta Pixel события
                if (window.fbq && window.fbq.queue) {
                    const seen = new Set();
                    for (const item of window.fbq.queue) {
                        if (Array.isArray(item) && item[0] === 'track' && !seen.has(item[1])) {
                            seen.add(item[1]);
                            events.push({source: 'fbq.queue', platform: 'Meta', event: item[1]});
                        }
                    }
                }

                return events;
            }
            """)

            # Список конверсионных dl_ событий Shopify
            DL_CONVERSION_MAP = {
                "dl_add_to_cart":       ("Google Analytics", "add_to_cart"),
                "dl_view_item":         ("Google Analytics", "view_item"),
                "dl_begin_checkout":    ("Google Analytics", "begin_checkout"),
                "dl_purchase":          ("Google Analytics", "purchase"),
                "dl_generate_lead":     ("Google Analytics", "generate_lead"),
                "dl_search":            ("Google Analytics", "search"),
                "dl_view_item_list":    ("Google Analytics", "view_item_list"),
            }

            from step2_scan import is_conversion_event, is_partial_event, is_noise_event
            for ev in js_events:
                plat = ev["platform"]
                event_name = ev["event"]

                # dl_ события маппим на стандартные GA4
                if event_name in DL_CONVERSION_MAP:
                    mapped_plat, mapped_event = DL_CONVERSION_MAP[event_name]
                    if debug:
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"       [{ts}] 🎯 [JS→{mapped_plat}] {event_name} → {mapped_event}")
                    # Добавляем оба — оригинальный dl_ и маппированный стандартный
                    for e_name in [event_name, mapped_event]:
                        pixel_events.setdefault(mapped_plat, [])
                        if not any(e["event"] == e_name for e in pixel_events[mapped_plat]):
                            pixel_events[mapped_plat].append({
                                "event": e_name,
                                "is_conversion": is_conversion_event(mapped_plat, e_name),
                                "is_partial": is_partial_event(mapped_plat, e_name),
                                "is_noise": False,
                                "source": "js_datalayer",
                            })
                else:
                    if debug:
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"       [{ts}] 📋 [JS→{plat}] {event_name}")
                    pixel_events.setdefault(plat, [])
                    if not any(e["event"] == event_name for e in pixel_events[plat]):
                        pixel_events[plat].append({
                            "event": event_name,
                            "is_conversion": is_conversion_event(plat, event_name),
                            "is_partial": is_partial_event(plat, event_name),
                            "is_noise": is_noise_event(plat, event_name),
                            "source": "js_read",
                        })
        except Exception as e:
            if debug:
                print(f"       ⚠️  JS read error: {e}")

        if debug:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"       [{ts}] ✅ Клик выполнен: Add to Cart")

    except Exception as e:
        result["error"] = f"click_failed: {str(e)[:80]}"

    return result


def _click_listing_page(page, url: str, pixel_events: dict, result: dict, debug: bool) -> dict:
    """Кликает на первый продукт в листинге → попадает на product page → кликает Add to Cart."""

    page.wait_for_timeout(500)

    product_url = find_first_product_link(page, debug=debug)
    if not product_url:
        result["error"] = "no_product_link_found"
        return result

    try:
        # Переходим на product page
        page.goto(product_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        result["navigated_to"] = product_url

        if debug:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"       [{ts}] 🔗 Перешли на: {product_url[:80]}")

        # Кликаем Add to Cart
        atc_selector = find_add_to_cart(page, debug=debug)
        if atc_selector:
            el = page.locator(atc_selector).first
            el.scroll_into_view_if_needed(timeout=2000)
            el.click(timeout=3000)
            result["clicked"] = True
            result["action"] = f"navigate→{product_url[:60]} + click:add_to_cart"
            page.wait_for_timeout(2000)

            if debug:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"       [{ts}] ✅ Клик выполнен: Add to Cart на product page")
        else:
            result["action"] = f"navigate→{product_url[:60]} (Add to Cart не найден)"
            result["clicked"] = True  # навигация = уже взаимодействие

    except Exception as e:
        result["error"] = f"navigation_failed: {str(e)[:80]}"

    return result


# ─── CLI (standalone debug) ───────────────────────────────────────────────────

def run_standalone(url: str, page_type: str, platform: str = "shopify", debug: bool = True):
    """Запуск напрямую для отладки одной страницы."""
    from playwright.sync_api import sync_playwright
    from popup_handler import handle_popups

    print(f"\n{'═' * 65}")
    print(f"  TNC Clicker — Debug Mode")
    print(f"  URL:       {url}")
    print(f"  Type:      {page_type}")
    print(f"  Platform:  {platform}")
    print(f"{'═' * 65}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headless=False для визуального дебага
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        # Сначала homepage — как step2, чтобы consent и гео-модал закрылись там
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        print(f"🌐 Открываем homepage {base_url}...")
        page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3500)

        print(f"🍪 Обрабатываем consent и гео-баннеры...")
        handle_popups(page, verbose=True)
        page.wait_for_timeout(1000)

        # Переходим на нужную страницу
        if url != base_url:
            print(f"🌐 Переходим на {url}...")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

        print(f"\n🖱  Запускаем клики...\n")
        result = click_page(page, url, page_type, platform=platform, debug=debug)

        print(f"\n{'─' * 65}")
        print(f"  РЕЗУЛЬТАТ")
        print(f"{'─' * 65}")
        print(f"  Клик:        {'✅ выполнен' if result['clicked'] else '❌ не выполнен'}")
        print(f"  Действие:    {result['action'] or '—'}")
        if result["navigated_to"]:
            print(f"  Перешли на:  {result['navigated_to'][:80]}")
        if result["conversion_events"]:
            print(f"  🎯 Конверсии: {', '.join(result['conversion_events'])}")
        elif result["new_events"]:
            print(f"  🔸 События:   {result['new_events']}")
        else:
            print(f"  События:     не зафиксированы после клика")
        if result["error"]:
            print(f"  ⚠️  Ошибка:   {result['error']}")
        print(f"{'═' * 65}\n")

        input("Нажми Enter чтобы закрыть браузер...")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TNC Clicker — debug mode")
    parser.add_argument("url", help="URL страницы")
    parser.add_argument("page_type", help="Тип страницы: product, search_results, homepage")
    parser.add_argument("platform", nargs="?", default="shopify", help="Платформа (default: shopify)")
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--headless", action="store_true", default=False)
    args = parser.parse_args()

    run_standalone(args.url, args.page_type, args.platform, debug=args.debug)
