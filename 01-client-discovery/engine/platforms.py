# -*- coding: utf-8 -*-
"""
TNC Pipeline — platforms.py: ЕДИНЫЙ реестр знаний о пиксель-платформах
=======================================================================
До 2026-07-13 знание было размазано по ТРЁМ несинхронизированным таблицам:
- scanners/base_scanner.py  → PIXEL_RULES + CONVERSION_EVENTS_TIER1/2 + NOISE_EVENTS
- scanners/shopify_scanner.py → SHOPIFY_PIXEL_PLATFORMS + SHOPIFY_PIXEL_MARKERS
- gtm_analyzer.py            → PLATFORM_SIGNATURES
Следствия: Pinterest не знал никто кроме Shopify-маркеров, Snapchat добавили в
одну таблицу и забыли в другой (баги B5/B6), ключ "Hotjar" был объявлен дважды
и второй молча затирал первый.

Теперь: одна запись на платформу здесь, старые имена — производные view-функции
(as_pixel_rules() и т.д.). Конструктор _build() кидает исключение на дубликат
ключа — класс Hotjar-бага умирает конструктивно.

ШАГ A (2026-07-13, коммит 1df5cae): чистая эквивалентность — view-функции
выдавали байт-в-байт старые таблицы (реперы снимались ДО правок).
ШАГ B (2026-07-13): контентные фиксы под стендом — Pinterest получил
network-правила (баг B5), Snapchat — GTM-сигнатуры (баг B6), слиты две строки
Hotjar (дубль ключа годами глушил первую). test_platforms_frozen.py с шага B —
снапшот ТЕКУЩЕГО реестра (детектор случайных изменений): при сознательной правке
реестра перегенерировать фикстуры и закоммитить вместе.
Порядок ключей каждого view — часть поведения (make_listeners матчит первым
совпавшим), поэтому порядки заданы явно; новые платформы добавляются В КОНЕЦ.
"""


def _build(pairs):
    """dict из списка пар с защитой от дубликатов ключей.
    Литерал {'Hotjar': ..., 'Hotjar': ...} молча терял первое значение —
    здесь такое падает сразу."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"platforms.py: дубликат ключа {key!r} — "
                             f"вторая запись затёрла бы первую (класс Hotjar-бага)")
        out[key] = value
    return out


# ─── Рекламные платформы (пиксели и теги) ─────────────────────────────────────
# Поля записи (все опциональны, view включает ключ только если поле есть):
#   pixel_rules       — network-детекция (домены-подстроки, event/id-параметры)
#   tier1 / tier2     — конверсионные события (жирные / вспомогательные)
#   noise             — события-шум (presence-пинги, не конверсии)
#   gtm_name          — имя платформы в GTM-контейнере (историческое, отличается)
#   gtm_signatures    — regex-признаки в JS контейнера
#   shopify_marker    — маркеры ВЫЗОВОВ в web-pixel коде (не голые слова — баг A1)
#   shopify_app_ids   — ID Shopify-приложений → платформа

PLATFORMS = _build([
    ("Meta", {
        "pixel_rules": {
            "domains": ["facebook.com/tr", "connect.facebook.net/en_US/fbevents",
                        "connect.facebook.net/signals/config"],
            "event_param": "ev",
            "id_param": "id",                              # facebook.com/tr?id=<id>
            "id_path_re": r"/signals/config/(\d{6,})",     # SDK config — летит даже в headless
        },
        "tier1": ["Purchase", "Lead", "InitiateCheckout", "AddToCart",
                  "CompleteRegistration", "Schedule", "Contact", "AddPaymentInfo"],
        "tier2": ["ViewContent", "Search", "Subscribe"],
        # PageView НЕ noise: показываем «Meta: PageView» (пиксель активен, шлёт baseline)
        "noise": ["fired"],
        "gtm_name": "Meta Pixel",
        "gtm_signatures": [r'fbq\s*\(', r'connect\.facebook\.net', r'fbevents\.js',
                           r'facebook\.com/tr', r'Meta Pixel'],
        "shopify_marker": ("fbevents.js", "fbq(", "connect.facebook.net", "facebook.com/tr"),
        "shopify_app_ids": ["550306007"],
    }),
    ("Google Analytics", {
        "pixel_rules": {
            # 'google-analytics.com/g/collect' substring-ловит и www., и region1. хосты —
            # реальные GA4 endpoints; старые записи оставлены для legacy UA / analytics.google.com
            # Tested: 2026-07-07 on tinytronics.nl — GA4 бил в www.google-analytics.com/g/collect,
            #         старые паттерны его не матчили (ломался на '/g/')
            "domains": ["analytics.google.com/g/collect", "google-analytics.com/collect",
                        "google-analytics.com/g/collect"],
            "event_param": "en",
            "id_param": "tid",                             # ?tid=G-XXXX
        },
        "tier1": ["purchase", "begin_checkout", "add_to_cart",
                  "generate_lead", "form_submit", "conversion"],
        "tier2": ["view_item", "view_item_list", "search",
                  "select_item", "view_promotion"],
        "noise": ["gtm.init", "gtm.init_consent", "gtm.js", "fired",
                  "page_view", "user_engagement", "session_start", "first_visit",
                  "scroll", "click", "view_item_list",
                  "form_start", "form_close"],
        "gtm_name": "Google Analytics GA4",
        "gtm_signatures": [r'G-[A-Z0-9]{6,}', r'gtag\s*\(', r'analytics\.google\.com',
                           r'google-analytics\.com/g/collect'],
        "shopify_marker": ("googletagmanager.com", "google-analytics.com", "gtag("),
        "shopify_app_ids": ["2179629271"],
    }),
    ("Google Ads", {
        "pixel_rules": {
            # ccm/collect и viewthroughconversion — presence-пинги современного gtag:
            # регистрируют платформу/ID, но page_view и т.п. глушатся noise ниже
            "domains": ["googleadservices.com/pagead/conversion",
                        "google.com/pagead/1p-conversion",
                        "google.com/ccm/collect",
                        "doubleclick.net/ccm/s/collect",
                        "doubleclick.net/pagead/viewthroughconversion",
                        "pagead/1p-user-list"],
            "event_param": "en",                           # ccm/collect несёт en=page_view
            "id_path_re": r"/(?:conversion|viewthroughconversion)/(\d{6,})",
        },
        "tier1": ["conversion"],
        "tier2": [],
        # page_view/gtag.config с ccm/collect — presence-пинг, НЕ конверсия: без этого
        # ccm-хиты ложно «озеленяли» бы GAP-страницы
        "noise": ["page_view", "gtag.config"],
        "gtm_name": "Google Ads",
        "gtm_signatures": [r'AW-\d{6,}', r'googleadservices\.com', r'conversion_id',
                           r'google\.com/pagead'],
    }),
    ("Bing/Microsoft", {
        "pixel_rules": {
            "domains": ["bat.bing.com/action", "bat.bing.com/p/action"],
            "event_param": "ea",
            "id_param": "ti",                              # ?ti=<id>
        },
        "tier1": ["purchase", "lead", "conversion"],
        "tier2": [],
        "noise": ["fired"],
        "gtm_name": "Microsoft/Bing",
        "gtm_signatures": [r'bat\.bing\.com', r'uetq\s*=', r'bing\.com/action'],
        "shopify_marker": ("bat.bing.com", "uetq"),
    }),
    ("LinkedIn", {
        "pixel_rules": {
            "domains": ["px.ads.linkedin.com", "snap.licdn.com"],
            "event_param": "conversionId",
            "id_param": "pid",                             # ?pid=<id>
        },
        # tier1/tier2 у LinkedIn исторически нет (не было в старых таблицах)
        "noise": ["fired"],
        "gtm_name": "LinkedIn Insight",
        "gtm_signatures": [r'snap\.licdn\.com', r'linkedin\.com/li', r'_linkedin_partner_id',
                           r'px\.ads\.linkedin\.com'],
        "shopify_marker": ("lintrk", "snap.licdn.com", "px.ads.linkedin.com"),
    }),
    ("TikTok", {
        "pixel_rules": {
            # i18n/pixel = загрузка SDK (events.js/config) — presence-сигнал, как fbevents у Meta.
            # Подстроки без хоста кроют региональные хосты (analytics-sg.tiktok.com и т.п.).
            # Кейс: bobbies.com — TikTok через GTM грузил i18n/pixel/events.js, старое правило
            # (только analytics.tiktok.com/api/v2/pixel) его не видело → ложный «TikTok ❌».
            "domains": ["tiktok.com/api/v2/pixel", "tiktok.com/i18n/pixel/"],
            "event_param": "event",
            "id_param": "sdkid",                           # events.js?sdkid=<PIXEL_ID>&lib=ttq
        },
        "tier1": ["Purchase", "AddToCart", "InitiateCheckout", "PlaceAnOrder"],
        "tier2": ["ViewContent"],
        "noise": ["fired"],
        "gtm_name": "TikTok Pixel",
        "gtm_signatures": [r'analytics\.tiktok\.com', r'ttq\.', r'TiktokAnalyticsObject'],
        "shopify_marker": ("ttq.load", "analytics.tiktok.com"),
        "shopify_app_ids": ["96403671"],
    }),
    ("Snapchat", {
        "pixel_rules": {
            # До 2026-07-08 правила НЕ БЫЛО вообще — «Snapchat ❌» не мог стать ✅ в принципе
            "domains": ["tr.snapchat.com", "sc-static.net/scevent"],
            "event_param": None,
        },
        "tier1": ["PURCHASE", "START_CHECKOUT", "ADD_CART", "SIGN_UP", "LEAD"],
        # tier2 у Snapchat исторически отсутствует (нет ключа в старой таблице)
        "noise": ["fired"],
        # ШАГ B (2026-07-13, баг B6): сигнатур не было — живые snaptr-теги в
        # контейнере gymshark не репортились (PIXEL_RULES добавили 07-08,
        # gtm_analyzer забыли — ровно тот класс дрейфа, ради которого реестр)
        "gtm_name": "Snapchat Pixel",
        "gtm_signatures": [r'snaptr\s*\(', r'tr\.snapchat\.com', r'sc-static\.net'],
        "shopify_marker": ("snaptr(", "tr.snapchat.com", "sc-static.net"),
    }),
    ("Pinterest", {
        # ШАГ B (2026-07-13, баг B5): network-правила не было ВООБЩЕ — живые
        # ct.pinterest.com хиты не регистрировались (gymshark tid=2618098611272,
        # pipsnacks, allbirds). Rodion подтвердил Pinterest на этих сайтах
        # Pixel Helper'ом (гейт №2, 2026-07-13).
        "pixel_rules": {
            "domains": ["ct.pinterest.com"],
            "event_param": "event",
            "id_param": "tid",
        },
        "tier1": ["checkout", "addtocart", "signup", "lead"],
        "tier2": ["viewcategory", "search", "watchvideo"],
        # pagevisit НЕ noise — как Meta PageView: показывает, что тег жив
        "noise": ["fired", "init"],
        "gtm_name": "Pinterest Tag",     # сигнатур в GTM пока нет — не детектится там
        "shopify_marker": ("pintrk", "ct.pinterest.com", "s.pinimg.com"),
        "shopify_app_ids": ["136216791"],
    }),
])

# ─── GTM-only инструменты (аналитика/чаты/маркетинг — не рекламные пиксели) ───
# Живут только в детекции GTM-контейнера. Hotjar: в старой таблице ключ был
# объявлен ДВАЖДЫ — литерал молча взял вторую запись; здесь зафиксировано
# эффективное значение, слияние строк — шаг B.

GTM_TOOLS = _build([
    # ШАГ B (2026-07-13): слиты ДВЕ строки старого литерала — первая (hjid, hj()
    # была молча затёрта дублем ключа и годами не работала
    ("Hotjar", [r'hotjar\.com', r'hjid\s*[:=]', r'hj\s*\(', r'hjSetting']),
    ("Intercom", [r'intercom\.com', r'intercomSettings']),
    ("HubSpot", [r'hubspot\.com', r'hs-scripts', r'hbspt\.']),
    ("Drift", [r'drift\.com', r'driftt\.com']),
    ("Zendesk", [r'zendesk\.com', r'zopim']),
    ("Clarity", [r'clarity\.ms', r'microsoft\.com/clarity']),
    # 'analytics\.js' убран (A2): это имя файла рантайма самого gtm.js —
    # матчился почти на каждом контейнере → фейковый Segment на 8 сайтах.
    # Tested: 2026-07-08 on allbirds/bombas/fritz-kola/gymshark/tinytronics —
    #         Segment исчез из gtm.json; синтетика cdn.segment.com детектится.
    ("Segment", [r'segment\.com', r'cdn\.segment']),
    ("Mixpanel", [r'mixpanel\.com', r'mixpanel\.init']),
    ("Amplitude", [r'amplitude\.com', r'amplitude\.init']),
    ("Klaviyo", [r'klaviyo\.com']),
    ("Mailchimp", [r'mailchimp\.com', r'chimpstatic\.com']),
    ("Optimizely", [r'optimizely\.com']),
    ("VWO", [r'vwo\.com', r'visualwebsiteoptimizer']),
    ("Stripe", [r'stripe\.com', r'stripe\.js']),
    ("Crisp", [r'crisp\.chat']),
    ("Freshchat", [r'freshchat\.com', r'freshworks\.com']),
])

# ─── Явные порядки view'ов ────────────────────────────────────────────────────
# Порядок = часть текущего поведения (make_listeners берёт ПЕРВОЕ совпадение;
# отчёты итерируют как есть). Зафиксирован по замороженным таблицам 2026-07-13.

# Новые платформы шага B (Pinterest в network, Snapchat в GTM) добавлены В КОНЕЦ
# порядков — существующие первые-совпадения make_listeners не сдвигаются.
_PIXEL_RULES_ORDER = ["Meta", "Google Analytics", "Google Ads", "Bing/Microsoft",
                      "LinkedIn", "TikTok", "Snapchat", "Pinterest"]
_TIER1_ORDER = ["Meta", "Google Analytics", "Google Ads", "Bing/Microsoft",
                "TikTok", "Snapchat", "Pinterest"]
_TIER2_ORDER = ["Meta", "Google Analytics", "Google Ads", "Bing/Microsoft",
                "TikTok", "Pinterest"]
_NOISE_ORDER = ["Meta", "Google Analytics", "Google Ads", "Bing/Microsoft",
                "TikTok", "LinkedIn", "Snapchat", "Pinterest"]
_SHOPIFY_MARKERS_ORDER = ["Meta", "Google Analytics", "TikTok", "Pinterest",
                          "Bing/Microsoft", "LinkedIn", "Snapchat"]
_SHOPIFY_APP_IDS_ORDER = ["Meta", "Google Analytics", "TikTok", "Pinterest"]
# GTM: платформы и инструменты исторически перемешаны (Hotjar сидит между
# TikTok и Microsoft/Bing — позиция первого объявления дубля)
_GTM_ORDER = ["Meta", "Google Analytics", "Google Ads", "LinkedIn", "TikTok",
              ("tool", "Hotjar"), "Bing/Microsoft",
              ("tool", "Intercom"), ("tool", "HubSpot"), ("tool", "Drift"),
              ("tool", "Zendesk"), ("tool", "Clarity"), ("tool", "Segment"),
              ("tool", "Mixpanel"), ("tool", "Amplitude"), ("tool", "Klaviyo"),
              ("tool", "Mailchimp"), ("tool", "Optimizely"), ("tool", "VWO"),
              ("tool", "Stripe"), ("tool", "Crisp"), ("tool", "Freshchat"),
              "Snapchat"]   # шаг B: Snapchat Pixel в конец GTM-детекции


# ─── Производные view'ы (старые имена таблиц) ─────────────────────────────────

def as_pixel_rules() -> dict:
    """base_scanner.PIXEL_RULES — network-детекция."""
    return {name: PLATFORMS[name]["pixel_rules"] for name in _PIXEL_RULES_ORDER}


def as_conversion_tier1() -> dict:
    """base_scanner.CONVERSION_EVENTS_TIER1."""
    return {name: PLATFORMS[name]["tier1"] for name in _TIER1_ORDER}


def as_conversion_tier2() -> dict:
    """base_scanner.CONVERSION_EVENTS_TIER2."""
    return {name: PLATFORMS[name]["tier2"] for name in _TIER2_ORDER}


def as_noise_events() -> dict:
    """base_scanner.NOISE_EVENTS."""
    return {name: PLATFORMS[name]["noise"] for name in _NOISE_ORDER}


def as_shopify_pixel_platforms() -> dict:
    """shopify_scanner.SHOPIFY_PIXEL_PLATFORMS — Shopify app-ID → платформа."""
    out = {}
    for name in _SHOPIFY_APP_IDS_ORDER:
        for app_id in PLATFORMS[name].get("shopify_app_ids", []):
            out[app_id] = name
    return out


def as_shopify_pixel_markers() -> dict:
    """shopify_scanner.SHOPIFY_PIXEL_MARKERS — маркеры вызовов в web-pixel коде."""
    return {name: PLATFORMS[name]["shopify_marker"] for name in _SHOPIFY_MARKERS_ORDER}


def as_gtm_platform_signatures() -> dict:
    """gtm_analyzer.PLATFORM_SIGNATURES — regex-признаки в JS контейнера."""
    out = {}
    for entry in _GTM_ORDER:
        if isinstance(entry, tuple):
            _, tool = entry
            out[tool] = GTM_TOOLS[tool]
        else:
            out[PLATFORMS[entry]["gtm_name"]] = PLATFORMS[entry]["gtm_signatures"]
    return out
