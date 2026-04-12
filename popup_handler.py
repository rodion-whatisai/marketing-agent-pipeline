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
    result = {
        "geo_modal": "not_found",
        "cookie_consent": "not_found",
        "wait_after_ms": 0,
    }

    # 1. Гео-модал — закрываем крестиком / "No thanks"
    if _close_geo_modal(page, verbose):
        result["geo_modal"] = "closed"
        page.wait_for_timeout(400)

    # 2. Cookie consent — принимаем всё
    if _accept_all_cookies(page, verbose):
        result["cookie_consent"] = "accepted_all"
        # Ждём загрузки тегов которые были заблокированы CMP
        # Google Ads на thebodyshop появляется через ~5s после consent
        page.wait_for_timeout(5000)
        result["wait_after_ms"] = 5000

    return result


def _close_geo_modal(page, verbose: bool) -> bool:
    """Закрывает гео-модал. Возвращает True если закрыл."""

    # По селекторам
    for sel in GEO_CLOSE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=300):
                el.click(timeout=1000)
                if verbose:
                    print(f"       🌍 Гео-модал закрыт [{sel}]")
                return True
        except Exception:
            pass

    # По тексту — ищем кнопки dismiss внутри модала
    for text in GEO_DISMISS_TEXTS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.is_visible(timeout=200):
                # Проверяем что это не основной CTA страницы
                # Гео-модал обычно выше или в overlay
                btn.click(timeout=1000)
                if verbose:
                    print(f"       🌍 Гео-модал закрыт по тексту: '{text}'")
                return True
        except Exception:
            pass

    return False


def _accept_all_cookies(page, verbose: bool) -> bool:
    """Принимает все куки. Возвращает True если кликнул."""

    # По селекторам (быстрый путь)
    for sel in ACCEPT_ALL_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                el.click(timeout=1000)
                if verbose:
                    print(f"       🍪 Куки приняты [{sel}]")
                return True
        except Exception:
            pass

    # По тексту кнопок
    for text in ACCEPT_ALL_TEXTS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=1000)
                if verbose:
                    print(f"       🍪 Куки приняты по тексту: '{text}'")
                return True
        except Exception:
            pass

    return False
