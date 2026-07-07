"""
TNC Pipeline — Popup & Consent Handler
=======================================
Закрывает гео-баннеры и принимает ВСЕ куки перед сканированием.

Почему "Accept all":
    Мы — аудитор, не пользователь. Нам нужно видеть максимум тегов.
    "Necessary only" блокирует маркетинговые пиксели (Google Ads, некоторые Meta).
    "Accept all" позволяет увидеть полную картину трекинга.

Тайминги (на примере thebodyshop.com из HAR):
    329ms  — CMP (OneTrust) загружается
    1374ms — GTM загружен (до consent — исключение в CMP)
    2970ms — Meta PageView (до consent)
    7387ms — Google Ads (только ПОСЛЕ consent)
    → нужно ждать ~5-6s после клика на Accept

Вызов:
    from popup_handler import handle_popups
    popup_result = handle_popups(page, verbose=True)
"""

from log import log_success, log_debug, log_fire


# ─── Accept ALL cookies selectors ────────────────────────────────────────────

ACCEPT_ALL_SELECTORS = [
    # OneTrust — "Allow all" / "Accept all"
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "[id*='onetrust-accept']",

    # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button[id*='CybotCookiebot'][id*='Allow']",

    # Generic
    "button[id*='accept-all']",
    "button[class*='accept-all']",
    "button[id*='allow-all']",
    "button[class*='allow-all']",
    "button[data-testid*='accept-all']",

    # Insites cookieconsent (tinytronics.nl) — Accept = <a class="cc-btn cc-allow">
    ".cc-btn.cc-allow",
    "a.cc-allow",
]

# Тексты кнопок "Accept all" (case-insensitive, partial match)
ACCEPT_ALL_TEXTS = [
    "allow all cookies",
    "accept all cookies",
    "accept all",
    "allow all",
    "i accept",
    "agree to all",
    "ok, i agree",
    "yes, i agree",
    "got it",
    # French
    "tout accepter", "accepter tout", "tout autoriser",
    # German
    "alle akzeptieren", "alle cookies akzeptieren", "zustimmen",
    # Spanish
    "aceptar todo", "aceptar todos",
    # Italian
    "accetta tutto",
    # Dutch
    "alles accepteren",
    "accepteren",
    "cookies toestaan",
    # Insites cookieconsent: accessible name кнопки = aria-label "allow cookies",
    # а не видимый текст "Accepteren" — нужны оба варианта
    "allow cookies",
    # Portuguese
    "aceitar todos", "aceitar tudo",
]

# ─── Geo modal selectors ──────────────────────────────────────────────────────

# Тексты которые сигнализируют о гео-модале
GEO_TRIGGER_TEXTS = [
    "looks like you're in",
    "you appear to be in",
    "we noticed you're in",
    "visiting from",
    "ship to your location",
    "your country",
]

# Тексты кнопок закрытия гео-модала (крестик или "No thanks")
GEO_DISMISS_TEXTS = [
    "no thanks",
    "stay on this site",
    "continue to",
    "remain on",
    "dismiss",
    "close",
    "×",
    "no, stay",
]

GEO_CLOSE_SELECTORS = [
    # geoproapp.com (thebodyshop)
    "[id*='geopro'] button",
    "[class*='geopro'] button",
    # Generic
    "[class*='geo-modal'] button[class*='close']",
    "[class*='country-modal'] button[class*='close']",
    "[class*='location-popup'] button[class*='close']",
]


# ─── Main ─────────────────────────────────────────────────────────────────────

def handle_popups(page, verbose: bool = False) -> dict:
    """
    Закрывает гео-баннер и принимает все куки.

    Returns:
        {
            "geo_modal": "closed" | "not_found",
            "cookie_consent": "accepted_all" | "not_found",
            "wait_after_ms": int
        }
    """
    log_debug(f"handle_popups: start verbose={verbose}")
    result = {
        "geo_modal": "not_found",
        "cookie_consent": "not_found",
        "wait_after_ms": 0,
    }

    # 1. Cookie consent — принимаем всё. ПЕРВЫМ: если гео-проход бежит раньше,
    # его generic-слова ('dismiss', 'close') попадают в consent-баннер и ОТКЛОНЯЮТ
    # куки (tinytronics: клик по cc-dismiss «dismiss cookie message» = decline →
    # сайт не грузит GTM → ноль Google-запросов на весь контекст).
    # Tested: 2026-07-07 on tinytronics.nl — до фикса гео-проход декейнил consent
    log_debug("handle_popups: step 1 — пробуем принять все куки")
    if _accept_all_cookies(page, verbose):
        result["cookie_consent"] = "accepted_all"
        # Ждём загрузки тегов которые были заблокированы CMP
        # Google Ads на thebodyshop появляется через ~5s после consent
        log_debug("handle_popups: куки приняты, ждём 5000ms на загрузку тегов")
        page.wait_for_timeout(5000)
        result["wait_after_ms"] = 5000
    else:
        log_debug("handle_popups: cookie consent не найден")

    # 2. Гео-модал — закрываем крестиком / "No thanks"
    log_debug("handle_popups: step 2 — пробуем закрыть гео-модал")
    if _close_geo_modal(page, verbose):
        result["geo_modal"] = "closed"
        log_debug("handle_popups: гео-модал закрыт, ждём 400ms")
        page.wait_for_timeout(400)
    else:
        log_debug("handle_popups: гео-модал не найден")

    log_debug(f"handle_popups: done result={result}")
    return result


def _close_geo_modal(page, verbose: bool) -> bool:
    """Закрывает гео-модал. Возвращает True если закрыл."""
    log_debug("_close_geo_modal: start")

    # По селекторам
    for sel in GEO_CLOSE_SELECTORS:
        log_fire(f"_close_geo_modal: пробуем селектор [{sel}]")
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=300):
                log_debug(f"_close_geo_modal: селектор виден, кликаем [{sel}]")
                el.click(timeout=1000)
                if verbose:
                    log_success(f"       Гео-модал закрыт [{sel}]", emoji="🌍")
                return True
        except Exception as e:
            log_debug(f"_close_geo_modal: селектор [{sel}] не сработал: {e}")

    # Текстовый dismiss-проход — ТОЛЬКО если на странице есть гео-триггер:
    # generic-слова ('dismiss', 'close') иначе цепляют consent-баннеры и чужие модалы
    try:
        body_text = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
    except Exception:
        body_text = ""
    if not any(t in body_text for t in GEO_TRIGGER_TEXTS):
        log_debug("_close_geo_modal: гео-триггеров на странице нет — текстовый dismiss-проход пропущен")
        return False

    # По тексту — ищем кнопки dismiss внутри модала
    for text in GEO_DISMISS_TEXTS:
        log_fire(f"_close_geo_modal: пробуем текст кнопки '{text}'")
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.is_visible(timeout=200):
                # Consent-кнопки не трогаем (Insites cc-dismiss = ОТКЛОНИТЬ куки)
                attrs = (btn.evaluate(
                    "el => ((el.className||'')+' '+(el.id||'')+' '+(el.getAttribute('aria-label')||'')).toString()"
                ) or "").lower()
                if any(w in attrs for w in ("cookie", "consent", "cc-")):
                    log_debug(f"_close_geo_modal: '{text}' — consent-кнопка ({attrs[:60]}), пропускаю")
                    continue
                # Проверяем что это не основной CTA страницы
                # Гео-модал обычно выше или в overlay
                log_debug(f"_close_geo_modal: кнопка '{text}' видна, кликаем")
                btn.click(timeout=1000)
                if verbose:
                    log_success(f"       Гео-модал закрыт по тексту: '{text}'", emoji="🌍")
                return True
        except Exception as e:
            log_debug(f"_close_geo_modal: текст '{text}' не сработал: {e}")

    log_debug("_close_geo_modal: гео-модал не найден")
    return False


def _accept_all_cookies(page, verbose: bool) -> bool:
    """Принимает все куки. Возвращает True если кликнул."""
    log_debug("_accept_all_cookies: start")

    # По селекторам (быстрый путь)
    for sel in ACCEPT_ALL_SELECTORS:
        log_fire(f"_accept_all_cookies: пробуем селектор [{sel}]")
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                log_debug(f"_accept_all_cookies: селектор виден, кликаем [{sel}]")
                el.click(timeout=1000)
                if verbose:
                    log_success(f"       Куки приняты [{sel}]", emoji="🍪")
                return True
        except Exception as e:
            log_debug(f"_accept_all_cookies: селектор [{sel}] не сработал: {e}")

    # По тексту кнопок
    for text in ACCEPT_ALL_TEXTS:
        log_fire(f"_accept_all_cookies: пробуем текст кнопки '{text}'")
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.is_visible(timeout=300):
                log_debug(f"_accept_all_cookies: кнопка '{text}' видна, кликаем")
                btn.click(timeout=1000)
                if verbose:
                    log_success(f"       Куки приняты по тексту: '{text}'", emoji="🍪")
                return True
        except Exception as e:
            log_debug(f"_accept_all_cookies: текст '{text}' не сработал: {e}")

    log_debug("_accept_all_cookies: cookie consent не найден")
    return False
