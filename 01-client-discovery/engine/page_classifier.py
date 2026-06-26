"""
TNC Page Classifier v2.0
========================
Гибридный классификатор:
1. Regex fast-path — очевидные случаи (checkout, contact, privacy...)
2. Claude Haiku — всё остальное, батчами по 50 URL

Использование:
    from page_classifier import classify_urls, classify_url
    python page_classifier.py
"""

import re
import os
import json
import time
import requests
from urllib.parse import urlparse

from log import log_info, log_warn, log_error, log_debug, log_success

# ─── Конфигурация ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 50

# ─── Загрузка patterns.json ───────────────────────────────────────────────────

_PATTERNS_CACHE = None
_PATTERNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns.json")


def load_patterns() -> dict:
    """Загружает patterns.json, кэширует в память."""
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is not None:
        log_debug("load_patterns: cache hit")
        return _PATTERNS_CACHE
    log_debug(f"load_patterns: загружаю {_PATTERNS_FILE}")
    try:
        with open(_PATTERNS_FILE, "r", encoding="utf-8") as f:
            _PATTERNS_CACHE = json.load(f)
        log_debug(f"load_patterns: загружено {len(_PATTERNS_CACHE)} паттернов")
    except FileNotFoundError as e:
        log_debug(f"load_patterns: файл не найден, пустая база: {e}")
        _PATTERNS_CACHE = {}
    return _PATTERNS_CACHE


def save_pattern(pattern: str, page_type: str, priority: int,
                 description: str, example_url: str = "", source: str = "manual"):
    """Сохраняет новый паттерн в patterns.json."""
    patterns = load_patterns()
    if pattern not in patterns:
        patterns[pattern] = {
            "type": page_type,
            "priority": priority,
            "description": description,
            "examples": [example_url] if example_url else [],
            "added": __import__("datetime").date.today().isoformat(),
            "source": source,
        }
    else:
        # Обновляем пример если его нет
        if example_url and example_url not in patterns[pattern].get("examples", []):
            patterns[pattern].setdefault("examples", []).append(example_url)

    with open(_PATTERNS_FILE, "w", encoding="utf-8") as f:
        json.dump(patterns, f, indent=2, ensure_ascii=False)

    global _PATTERNS_CACHE
    _PATTERNS_CACHE = patterns


def patterns_classify(path: str, full_url: str = "") -> dict | None:
    """Ищет совпадение пути в patterns.json."""
    log_debug(f"patterns_classify: start path={path}")
    patterns = load_patterns()
    # Нормализуем — убираем расширение .html/.php и trailing slash
    path_lower = path.lower().rstrip("/")
    path_no_ext = re.sub(r'\.(html|php|htm|asp|aspx)$', '', path_lower)

    # Точное совпадение (с расширением и без)
    for check_path in [path_lower, path_no_ext]:
        if check_path in patterns:
            p = patterns[check_path]
            log_debug(f"patterns_classify: точное совпадение {check_path} → {p['type']} (слой patterns.json)")
            if full_url and full_url not in p.get("examples", []):
                save_pattern(check_path, p["type"], p["priority"],
                            p["description"], full_url, "auto")
            return {
                "type": p["type"],
                "priority": p["priority"],
                "method": "patterns_json",
                "description": p.get("description", ""),
            }

    # Совпадение по первому сегменту (без расширения)
    first_seg = "/" + path_no_ext.lstrip("/").split("/")[0] if path_no_ext else ""
    if first_seg and first_seg != path_no_ext and first_seg in patterns:
        p = patterns[first_seg]
        log_debug(f"patterns_classify: совпадение по первому сегменту {first_seg} → {p['type']} (слой patterns.json)")
        return {
            "type": p["type"],
            "priority": p["priority"],
            "method": "patterns_json",
            "description": p.get("description", ""),
        }

    log_debug(f"patterns_classify: нет совпадения в patterns.json для {path}")
    return None

# ─── Типы страниц ─────────────────────────────────────────────────────────────

PAGE_TYPES_DOC = """
Page types and priorities (lower number = more important):

PRIORITY 1 — Conversion funnel:
  checkout        /cart, /checkout, /payment, /order, /buy, /basket
  booking_confirm /thank-you, /success, /confirm, /complete, /receipt
  quote           /quote, /get-quote, /estimate
  lead_form       /contact, /demo, /signup, /register, /trial, /book, /apply

PRIORITY 2 — Landing pages:
  homepage        root URL only
  pricing         /pricing, /plans, /rates, /subscription
  location        /locations, /city, /store, /near-me, /map
  product         individual product/listing pages, /vehicles, /land, /camp
  use_case        /use-cases, /solutions, /for-teams, /industry
  search_results  /search, /browse, /explore, /filter, /find

PRIORITY 3 — Supporting:
  faq_support     /faq, /help, /support, /guides, /how-to, /docs
  careers         /careers, /jobs, /hiring
  about           /about, /team, /story, /press

PRIORITY 4 — Content:
  blog_content    /blog, /news, /articles, /videos, /podcast

PRIORITY 5 — Skip:
  legal           /privacy, /terms, /cookie, /legal
  technical       /api, /sitemap, /robots, /admin
  general         everything else
"""

# ─── Regex fast-path — только 100% однозначные случаи ────────────────────────

FAST_RULES = [
    (5, "technical",       [r"/sitemap", r"/robots\.txt", r"/wp-admin", r"/cdn-cgi", r"/\.well-known", r"/login(?:/|$)", r"/account(?:/|$)", r"/feed(?:/|$)", r"/rss(?:/|$)", r"/webhook", r"/oauth", r"/api/"]),
    (5, "legal",           [r"/privacy", r"/terms", r"/cookie", r"/gdpr", r"/tos", r"/policy(?:/|\.html|$)", r"/legal"]),
    (1, "checkout",        [r"/checkout(?:/|$)", r"/cart(?:/|$)", r"/basket(?:/|$)", r"/bag(?:/|$)"]),
    (1, "booking_confirm", [r"/thank-you", r"/thankyou", r"/order-confirm", r"/payment/success"]),
    (1, "lead_form",       [r"/contact(?:/|$)", r"/contact-us", r"/sign-up(?:/|$)", r"/signup(?:/|$)", r"/contacts(?:/|$)", r"/proposal(?:/|\.html|$)", r"/get-quote", r"/request(?:/|$)", r"/get-started", r"/book-demo", r"/schedule-demo", r"/request-demo", r"/start(?:/|$)", r"/try(?:/|$)", r"/join(?:/|$)", r"/register(?:/|$)", r"/apply(?:/|$)", r"/onboarding(?:/|$)", r"/create-account",
                           r"/book(?:/|$)", r"/booking(?:/|$)", r"/book-now", r"/reserve(?:/|$)",
                           # French (CA/QC)
                           r"/nous-contacter", r"/contactez-nous", r"/coordonnees", r"/coordonn\u00e9es",
                           r"/reservation(?:/|$)", r"/r\u00e9servation(?:/|$)", r"/reserver(?:/|$)", r"/r\u00e9server(?:/|$)",
                           r"/commencer(?:/|$)", r"/prendre-rendez-vous", r"/rendez-vous(?:/|$)"]),
    (1, "quote",           [r"/obtenez-un-devis", r"/obtenir-un-devis", r"/soumission(?:/|$)", r"/devis(?:/|$)"]),
    (2, "homepage",        [r"^/?$", r"^/home/?$", r"^/index\."]),
    (2, "product",         [r"/products/[^/]", r"/collections/[^/]+/products/", r"/items/", r"/listing/"]),
    (2, "location",        [r"/locations/", r"/stores/", r"/store/"]),
    (2, "use_case",        [r"/features(?:/|$)", r"/integrations(?:/|$)", r"/solutions(?:/|$)", r"/use-cases(?:/|$)", r"/use-case(?:/|$)"]),
    (2, "pricing",         [r"/pricing(?:/|$)", r"/plans(?:/|$)", r"/loyalty(?:/|$)", r"/upgrade(?:/|$)", r"/billing(?:/|$)", r"/subscription(?:/|$)",
                           r"/tarifs?(?:/|$)", r"/forfaits?(?:/|$)", r"/abonnement(?:/|$)"]),
    (2, "search_results",  [r"/product-category/", r"/search(?:/|$)", r"/browse(?:/|$)",
                           r"/recherche(?:/|$)", r"/portfolios?(?:/|$)"]),
    (3, "faq_support",     [r"/faq(?:/|$)", r"/faqs(?:/|$)", r"/help(?:/|$)", r"/support(?:/|$)", r"/delivery(?:/|$)", r"/returns(?:/|$)", r"/shipping(?:/|$)", r"/how-to", r"/guides?(?:/|$)",
                           r"/liens-utiles", r"/foire-aux-questions", r"/livraison(?:/|$)", r"/retours?(?:/|$)"]),
    (3, "about",           [r"/about(?:/|$)", r"/about-us", r"/team(?:/|$)", r"/story(?:/|$)", r"/press(?:/|$)", r"/testimonials(?:/|$)",
                           r"/a-propos", r"/notre-equipe", r"/notre-histoire", r"/qui-sommes-nous"]),
    (3, "careers",         [r"/careers?(?:/|$)", r"/jobs(?:/|$)", r"/hiring",
                           r"/carrieres?(?:/|$)", r"/emplois?(?:/|$)"]),
    (3, "blog_content",    [r"/blog/", r"/blogs/", r"/news/[a-z]", r"/articles/[a-z]", r"/videos(?:/|$)", r"/resources(?:/|$)",
                           r"/nouvelles/", r"/actualites?/", r"/blogue/"]),
]


def fast_classify(path: str, full_url: str = "") -> dict | None:
    log_debug(f"fast_classify: start path={path}")
    path_lower = path.lower()

    # 0. Normalize language prefix — strip /fr/, /en/, /de/, /es/, /zh-cn/ etc.
    #    Only strips if there's a following segment (lookahead (?=/)) so /fr alone is untouched.
    normalized = re.sub(r'^/[a-z]{2}(?:-[a-z]{2,4})?(?=/)', '', path_lower)
    if normalized != path_lower:
        log_debug(f"fast_classify: язык-префикс снят {path_lower} → {normalized}")
    path_lower = normalized

    # 1. patterns.json — наша накопленная база знаний
    p = patterns_classify(path_lower, full_url)
    if p:
        log_debug(f"fast_classify: слой patterns.json сработал для {path_lower} → {p['type']}")
        p["expect_events"] = _get_expect_events(p["type"])
        return p

    # 2. Regex fast-path — очевидные структурные паттерны
    for priority, page_type, patterns in FAST_RULES:
        for pattern in patterns:
            if re.search(pattern, path_lower):
                log_debug(f"fast_classify: слой regex сработал для {path_lower} → {page_type} (pattern {pattern})")
                return {
                    "type": page_type,
                    "priority": priority,
                    "method": "regex",
                    "matched_pattern": pattern,
                }
    log_debug(f"fast_classify: ни patterns.json, ни regex не распознали {path_lower} → нужен Claude")
    return None


# ─── Claude Haiku ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a URL classifier for a digital marketing agency.
Classify URLs into page types to understand what tracking events should fire.
Always respond with valid JSON array only, no explanation, no markdown."""


def build_prompt(urls: list, site_context: str = "") -> str:
    url_list = "\n".join(f"{i+1}. {u}" for i, u in enumerate(urls))
    return f"""Classify these URLs from: {site_context or 'a website'}

{PAGE_TYPES_DOC}

IMPORTANT classification rules:
- A URL with a descriptive slug under /blog/ = blog_content
- A URL with a product name slug (color, material, object) = product
- Words like "success", "books", "quotes", "discovery", "cartoon" in PRODUCT NAME slugs do NOT indicate checkout/lead/search
- /success-story, /books-and-reading = general (not booking_confirm or lead_form)
- /quotes-wallpaper, /cartoon-mural = product (not quote or checkout)
- /discovery-collection = product (not search_results)
- Only classify as checkout/booking_confirm if URL explicitly indicates a transaction step
- /near-me suffix = search_results (not location)
- /use-cases/, /use-case/ = use_case

STRUCTURAL PATTERNS — apply universally across all site types:

Geographic vs product distinction:
- /[category]/[country-or-city-name] = location (listing filtered by geography)
- /[category]/[descriptive-multi-word-slug] = product (specific item listing)
- /boat-rental/france = location, /boat-rental/france/nice = location
- /boat-rental/dufour-460-grand-large-2019 = product (specific boat)

For-sale / for-rent / for-hire suffix:
- /[anything]-for-sale = product (not search_results)
- /[anything]-for-rent = product
- /[anything]-for-hire = product
- /vans-for-sale = product, /boats-for-hire = product

Locale prefix pattern:
- /[locale]/[category]/[long-slug] = product (e.g. /en-CA/land/quebec-some-place-abc123)
- /[locale]/[category] = search_results (e.g. /en-CA/camping-near-me)
- /[locale]/[short-word] = location or general

Activity + city pattern:
- /[activity]/[city-name]-[activity] = product (specific experience in a city)
- /scavenger-hunts/new-york-city = product
- /team-building/london-team-building = product

Long descriptive slug rule:
- If a slug has 4+ hyphen-separated words AND follows a category = product
- /wall-murals/midnight-forest-dark-blue = product
- /experiences/romantic-sunset-cruise-monaco = product

Short path = category/listing page:
- /[single-word] or /[two-words] without further slug = search_results or location
- /search, /browse, /explore, /discover = search_results
- /[category] alone = pricing or search_results depending on context

Digital products & subscriptions:
- /bundle/[slug] = product (specific game/content bundle)
- /bundles = search_results (bundle catalog)
- /course/[slug] = product (specific course)
- /courses = search_results (course catalog)  
- /store/[slug] = product (specific item in store)
- /store = search_results (store homepage)
- /game/[slug] = product (specific game)
- /[name]-premium, /[name]-pro, /[name]-plus = pricing (subscription plan)
- /freecourse, /free-course, /free-trial, /free-[anything] = lead_form
- /academy = pricing or lead_form (membership program)
- /membership = pricing
- /subscribe = lead_form or pricing

Enterprise/brand websites (Dove, Unilever-style):
- /[locale]/[category].html = search_results (category page with .html extension)
- /[locale]/[category]/[product-name].html = product (specific product)
- /[locale]/stories/[slug].html = blog_content
- /[locale]/collections.html = search_results
- .html extension generally = older CMS, classify by path content

Booking/marketplace platforms (daypassapp, similar):
- /[amenity-type]/[city-name] = location (filtered by city)
- /location/[city] = location
- /[city-name] alone = location (if site is geo-based marketplace)
- /blog-posts/[slug] = blog_content (same as /blog/)

Makerspace/co-working/venue (happylab-style):
- /[locale]/workshops/[category]/[machine-or-topic] = product (specific workshop)
- /[locale]/membership or /mitgliedschaft = pricing
- /[locale]/ausstattung or /equipment = product (equipment/facility listing)
- /[city-code]/ prefix alone = location (e.g. /en_vie/ = Vienna, /en_ber/ = Berlin)

Subscription app websites (papumba, headspace, similar):
- /pricing, /plans, /subscribe = pricing
- /features, /what-you-get = use_case
- /[lang]/ prefix alone without further path = general (localization root)
- /download, /get-app, /get-started = lead_form

Shopify platform (fixed URL structure — always):
- /products/[slug] = product (individual product page)
- /collections/[slug] = search_results (product category/collection listing)
- /collections/[collection]/products/[slug] = product (same as /products/[slug], just accessed via collection)
- /blogs/[blog-name]/[post-slug] = blog_content
- /blogs/[blog-name] = blog_content (blog index)
- /cart = checkout
- /checkout = checkout

Shopify /pages/[slug] classification rules (apply in order, first match wins):
- slug contains: size-guide, size-chart, sizeguide → faq_support
- slug contains: consultation, booking, book-now, schedule, appoint → lead_form
- slug contains: contact, get-in-touch, reach-us → lead_form
- slug contains: faq, faqs, help, support, how-it-works → faq_support
- slug contains: klarna, affirm, afterpay, sezzle, payment → faq_support
- slug contains: about, our-story, team, studio, who-we-are → about
- slug contains: location, studio, cities, city → location
- slug contains: refund, return, cancellation, shipping → legal
- slug contains: privacy, terms, legal, accessibility, cookie → legal
- slug contains: policy, policies, compliance, governance, slavery, statement → legal
- slug contains: review, sitemap, careers, jobs, press, media, investor, supplier, wholesale, affiliate → about
- slug contains: unsubscri, success-confirm, verify, reset, sign-in, sign-up, log-in, register → technical
- slug contains: influencer, ambassador, partner, collab → about
- slug contains: collection, experience, gallery, portfolio → about
- slug contains: pricing, packages, plans (as whole word, not "planet") → pricing
- slug looks like a product name (material/ingredient/object words, e.g. coconut-body-butter, shea-hand-cream, vitamin-c-serum) → product
- slug is generic service/utility word (hub, club, refer-a-friend, loyalty, rewards, gift-card, store-locator, in-store, custom-account) → general
- anything else that reads like a product name → product

Geo/map services (chargemap, similar):
- /map = search_results (interactive map is a search interface)
- /networks/[slug] = location (stations by network)
- /stations/[id-slug] = product (specific charging station)
- /pass = pricing (subscription/membership card)
- /[locale]/map = search_results

Platform-specific patterns (detect by URL structure):

WordPress / WooCommerce:
- /product/[slug] = product
- /product-category/[slug] = search_results
- /shop = search_results
- /shop/[slug] = search_results or product (depends on depth)
- /my-account = technical
- /wp-content/, /wp-admin/, /wp-json/ = technical
- /?add-to-cart= = checkout
- /wc-api/ = technical

Squarespace:
- /shop/p/[product-name] = product
- /shop/c/[category] = search_results
- /shop = search_results
- /blog/[post-slug] = blog_content
- /config/ = technical

Wix:
- /product-page/[slug] = product
- /shop/p/[slug] = product
- /shop/c/[category] = search_results
- /post/[slug] = blog_content
- /blank-[alphanumeric] = general (auto-generated Wix URL, usually about/policy page)

Webflow (CMS):
- /[cms-collection]/[slug] = product (Webflow CMS items - could be case studies, team, products)
- /blog/[slug] = blog_content
- /case-studies/[slug] = use_case
- /team/[name] = about

URLs:
{url_list}

Return JSON array with exactly {len(urls)} objects in same order:
[{{"type": "blog_content", "priority": 4, "reason": "blog post"}}, ...]"""


def classify_batch_api(urls: list, site_context: str = "") -> list:
    log_debug(f"classify_batch_api: start {len(urls)} URL site_context={site_context!r}")
    if not ANTHROPIC_API_KEY:
        log_debug("classify_batch_api: ANTHROPIC_API_KEY не задан → fallback general (слой Claude пропущен)")
        return [{"type": "general", "priority": 5, "method": "no_api_key"} for _ in urls]

    try:
        for attempt in range(3):
            log_debug(f"classify_batch_api: POST к Anthropic, попытка {attempt+1}/3")
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 2048,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": build_prompt(urls, site_context)}],
                },
                timeout=30,
            )
            log_debug(f"classify_batch_api: HTTP {response.status_code}")

            if response.status_code == 429:
                wait = 15 * (attempt + 1)
                log_warn(f"⏳ Rate limit — жду {wait}с...")
                time.sleep(wait)
                continue

            if response.status_code != 200:
                log_warn(f"API {response.status_code}: {response.text[:80]}")
                return [{"type": "general", "priority": 5, "method": "api_error"} for _ in urls]

            break
        else:
            log_warn(f"Rate limit после 3 попыток — пропускаю батч")
            return [{"type": "general", "priority": 5, "method": "rate_limited"} for _ in urls]

        text = response.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            log_debug("classify_batch_api: снимаю markdown-обёртку ```")
            text = re.sub(r"```json?\n?", "", text).replace("```", "").strip()

        results = json.loads(text)
        log_debug(f"classify_batch_api: распарсил {len(results)} классификаций от Claude")
        for r in results:
            r["method"] = "claude"
            r.setdefault("expect_events", _get_expect_events(r.get("type", "general")))
        return results

    except json.JSONDecodeError as e:
        log_warn(f"JSON parse error: {e}")
        return [{"type": "general", "priority": 5, "method": "parse_error"} for _ in urls]
    except Exception as e:
        log_warn(f"API error: {e}")
        return [{"type": "general", "priority": 5, "method": "exception"} for _ in urls]


def _get_expect_events(page_type: str) -> list:
    return {
        "checkout":        ["Purchase", "InitiateCheckout", "AddPaymentInfo"],
        "booking_confirm": ["Purchase", "Lead", "CompleteRegistration"],
        "quote":           ["Lead", "InitiateCheckout"],
        "lead_form":       ["Lead", "Contact", "CompleteRegistration"],
        "homepage":        ["ViewContent"],
        "pricing":         ["ViewContent", "InitiateCheckout"],
        "location":        ["ViewContent", "Search"],
        "product":         ["ViewContent", "AddToCart"],
        "use_case":        ["ViewContent", "Lead"],
        "search_results":  ["Search", "ViewContent"],
    }.get(page_type, [])


# ─── Главные функции ──────────────────────────────────────────────────────────

def classify_url(url: str, site_context: str = "") -> dict:
    """Классифицирует один URL."""
    log_debug(f"classify_url: start url={url}")
    path = urlparse(url).path if "://" in url else url
    path = path or "/"

    fast = fast_classify(path, full_url=url)
    if fast:
        log_debug(f"classify_url: распознано локально (слой {fast.get('method')}) → {fast['type']}")
        fast["description"] = fast.get("description") or fast["type"].replace("_", " ").title()
        fast["expect_events"] = _get_expect_events(fast["type"])
        return fast

    log_debug(f"classify_url: локально не распознано → слой Claude для {path}")
    results = classify_batch_api([path], site_context)
    result = results[0] if results else {"type": "general", "priority": 5}
    result["description"] = result.get("type", "general").replace("_", " ").title()
    result.setdefault("expect_events", _get_expect_events(result.get("type", "general")))
    return result


def classify_urls(urls: list, site_context: str = "", show_progress: bool = True, platform: str = "", skip_ai: bool = False) -> list:
    """Классифицирует список URL.
    Порядок: regex/patterns → platform slug rules → Claude (только остаток).

    skip_ai=True — стадия Claude пропускается целиком: всё, что не распознал
    бесплатный regex/slug-слой, помечается general (method="regex_only").
    Нужно для «ворот» на больших сайтах: сначала бесплатно считаем POI,
    и зовём дорогой API только если их меньше бюджета скана.
    """
    from platform_detector import classify_shopify_page

    log_debug(f"classify_urls: start {len(urls)} URL platform={platform!r} show_progress={show_progress}")
    results = [{"type": "general", "priority": 5, "method": "unprocessed"} for _ in urls]
    ai_needed = []

    for i, url in enumerate(urls):
        path = urlparse(url).path if "://" in url else url
        path = path or "/"
        log_debug(f"classify_urls: [{i}] url={url} path={path}")

        # 1. Regex + patterns
        fast = fast_classify(path, full_url=url)
        if fast:
            log_debug(f"classify_urls: [{i}] локальный слой ({fast.get('method')}) → {fast['type']}")
            fast.update({
                "url": url, "path": path,
                "description": fast["type"].replace("_", " ").title(),
                "expect_events": _get_expect_events(fast["type"]),
            })
            results[i] = fast
            continue

        # 2. Shopify slug rules — до Claude, бесплатно
        if platform == "shopify" and path.startswith("/pages/"):
            slug = path.replace("/pages/", "").lstrip("/").split("/")[0]
            log_debug(f"classify_urls: [{i}] Shopify slug-слой, slug={slug}")
            slug_result = classify_shopify_page(slug)
            if slug_result is not None:
                log_debug(f"classify_urls: [{i}] Shopify slug-слой сработал → {slug_result['type']}")
                slug_result.update({
                    "url": url, "path": path,
                    "description": slug_result["type"].replace("_", " ").title(),
                    "expect_events": _get_expect_events(slug_result["type"]),
                })
                results[i] = slug_result
                continue

        # 3. Claude — только то что не распознали выше
        log_debug(f"classify_urls: [{i}] не распознано локально → откладываю в ai_needed")
        ai_needed.append((i, path, url))

    if show_progress:
        import datetime
        slug_count  = sum(1 for r in results if r.get("method") == "platform_shopify")
        regex_count = len(urls) - len(ai_needed) - slug_count

        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        log_info(f"[{ts()}] Классификация завершена:")
        log_info(f"[{ts()}]   ⚡ Regex/patterns : {regex_count} URL")
        if slug_count:
            log_info(f"[{ts()}]   🏪 Slug rules    : {slug_count} URL (Shopify /pages/*)")
        if ai_needed and skip_ai:
            log_info(f"[{ts()}]   ⚡ Regex-only    : {len(ai_needed)} URL → general (Claude пропущен)")
        elif ai_needed:
            log_info(f"[{ts()}]   🤖 Claude нужен  : {len(ai_needed)} URL → отправляем батч...")
        else:
            log_success(f"[{ts()}]   Claude не нужен — всё распознано локально")

    if ai_needed and skip_ai:
        log_debug(f"classify_urls: skip_ai=True — {len(ai_needed)} URL НЕ отправляю в Claude, помечаю general")
        if show_progress:
            log_info(f"   ⚡ Regex-only режим: {len(ai_needed)} нераспознанных URL → general (Claude не зову)")
        for orig_idx, path, url in ai_needed:
            results[orig_idx] = {
                "type": "general", "priority": 5,
                "url": url, "path": path,
                "description": "General page",
                "expect_events": [], "method": "regex_only",
            }
        ai_needed = []

    if ai_needed:
        if not ANTHROPIC_API_KEY:
            log_warn(f"ANTHROPIC_API_KEY не задан — {len(ai_needed)} URL → general")
            for orig_idx, path, url in ai_needed:
                results[orig_idx] = {
                    "type": "general", "priority": 5,
                    "url": url, "path": path,
                    "description": "General page",
                    "expect_events": [], "method": "no_api_key",
                }
        else:
            import datetime
            def ts():
                return datetime.datetime.now().strftime("%H:%M:%S")

            for batch_start in range(0, len(ai_needed), BATCH_SIZE):
                batch = ai_needed[batch_start:batch_start + BATCH_SIZE]
                paths = [p for _, p, _ in batch]
                log_debug(f"classify_urls: батч Claude #{batch_start+1}, {len(batch)} URL")

                t_start = datetime.datetime.now()
                if show_progress:
                    batch_end = batch_start + len(batch)
                    log_info(f"[{ts()}] 🤖 Claude ← {len(batch)} URL (#{batch_start+1}–{batch_end}):")
                    for _, p, _ in batch:
                        log_info(f"[{ts()}]      {p}")

                classifications = classify_batch_api(paths, site_context)

                elapsed = (datetime.datetime.now() - t_start).total_seconds()
                if show_progress:
                    log_info(f"[{ts()}] 🤖 Claude → ответил за {elapsed:.1f}s:")
                    for (_, path, _), clf in zip(batch, classifications):
                        log_info(f"[{ts()}]      {path} → {clf.get('type','?')} (priority {clf.get('priority','?')})")

                for (orig_idx, path, url), clf in zip(batch, classifications):
                    clf.update({
                        "url": url, "path": path,
                        "description": clf.get("type", "general").replace("_", " ").title(),
                    })
                    clf.setdefault("expect_events", _get_expect_events(clf.get("type", "general")))
                    results[orig_idx] = clf

                if batch_start + BATCH_SIZE < len(ai_needed):
                    time.sleep(3)

    return results


def get_page_priority_label(priority: int) -> str:
    return {1: "🔴 CRITICAL", 2: "🟠 HIGH", 3: "🟡 MEDIUM",
            4: "🟢 LOW", 5: "⚪ SKIP"}.get(priority, "⚪ SKIP")


def classify_page_content(html: str, page=None) -> dict:
    """Анализирует HTML страницы на наличие CTA элементов."""
    log_debug(f"classify_page_content: start html_len={len(html)} page={'yes' if page else 'no'}")
    CTA_HIGH = [
        r"book\s+now", r"reserve\s+now", r"rent\s+now", r"buy\s+now",
        r"add\s+to\s+cart", r"add\s+to\s+bag", r"checkout", r"pay\s+now",
        r"get\s+a\s+quote", r"get\s+a\s+demo", r"request\s+a\s+demo",
        r"schedule\s+a\s+", r"sign\s+up\s+free", r"start\s+free\s+trial",
        r"try\s+for\s+free", r"apply\s+now", r"contact\s+us",
        r"check\s+availability", r"become\s+a\s+host",
    ]
    CTA_FORM = [
        r"first\s+name", r"last\s+name", r"email\s+address",
        r"phone\s+number", r"how\s+can\s+we\s+help",
        r"add\s+dates", r"add\s+guests", r"check.?in", r"check.?out",
        r"select\s+date", r"search\s+destinations",
    ]

    html_lower = html.lower()
    high = [p for p in CTA_HIGH if re.search(p, html_lower)]
    form = [p for p in CTA_FORM if re.search(p, html_lower)]

    forms_count = 0
    interactive_elements = []
    if page:
        try:
            forms_count = page.locator("form").count()
            buttons = page.locator(
                "button, input[type='submit'], [class*='btn'], [class*='cta']"
            ).all()
            for btn in buttons[:20]:
                try:
                    text = btn.inner_text(timeout=500).strip()
                    if text and len(text) < 60:
                        interactive_elements.append(text)
                except Exception as e:
                    log_debug(f"classify_page_content: не прочитал текст кнопки: {e}")
        except Exception as e:
            log_debug(f"classify_page_content: не собрал form/button DOM: {e}")

    is_poi = bool(high or form or forms_count > 0)
    reasons = []
    if high: reasons.append(f"CTA: {', '.join(high[:2])}")
    if form: reasons.append(f"form fields: {', '.join(form[:2])}")
    if forms_count: reasons.append(f"{forms_count} form(s)")

    return {
        "ctas": {"high": high, "medium": [], "form": form},
        "forms_count": forms_count,
        "interactive_elements": list(set(interactive_elements))[:10],
        "is_page_of_interest": is_poi,
        "poi_reason": " | ".join(reasons) if reasons else None,
    }


# ─── CLI тест ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        for url in sys.argv[1:]:
            result = classify_url(url)
            label = get_page_priority_label(result["priority"])
            print(f"{url} → {result['type']} {label}")
            if result.get("reason"):
                print(f"  reason: {result['reason']}")
        sys.exit(0)

    test_cases = [
        # ── Базовые паттерны ─────────────────────────────────────
        ("/", "homepage"),
        ("/contact", "lead_form"),
        ("/contacts", "lead_form"),
        ("/checkout", "checkout"),
        ("/cart", "checkout"),
        ("/thank-you", "booking_confirm"),
        ("/privacy", "legal"),
        ("/login", "technical"),

        # ── Wallsauce (e-commerce обои) ───────────────────────────
        ("/blog/cartoon-wallpaper", "blog_content"),
        ("/sports-wallpapers/success-not-for-lazy", "product"),
        ("/designer-wallpaper-murals/books-books-books", "product"),
        ("/wall-murals-wallpaper/quotes-wallpaper", "product"),
        ("/designer-wallpaper-murals/discovery", "product"),

        # ── Bandago (van rental) ──────────────────────────────────
        ("/vehicles/sprinter-15-passenger", "product"),
        ("/locations/miami", "location"),
        ("/use-cases/bands", "use_case"),
        ("/vans-for-sale", "product"),
        ("/quote/2-vehicle", "quote"),
        ("/success-story", "general"),

        # ── Hipcamp (outdoor marketplace) ────────────────────────
        ("/en-CA/land/quebec-slug-mxvhlywo", "product"),
        ("/en-CA/camping-near-me", "search_results"),
        ("/en-CA/search/5-beach", "search_results"),
        ("/become-a-host", "lead_form"),

        # ── Shopify (keepblooming) ────────────────────────────────
        ("/products/ruby-bloom-bouquet", "product"),
        ("/collections/bouquets", "product"),
        ("/collections/all-flowers/products/red-roses", "product"),
        ("/pages/about-us", "about"),
        ("/blogs/news/how-to-care-for-roses", "faq_support"),
        ("/delivery", "faq_support"),
        ("/returns", "faq_support"),
        ("/loyalty", "pricing"),

        # ── GuitarZoom (online courses) ───────────────────────────
        ("/course/music-theory-for-life-20", "product"),
        ("/courses", "search_results"),
        ("/guitarzoom-premium", "pricing"),
        ("/freecourse", "lead_form"),

        # ── IndieGala (game bundles) ──────────────────────────────
        ("/bundle/huge-pixel-bundle", "product"),
        ("/bundles", "search_results"),
        ("/store", "search_results"),
        ("/store/die-young", "product"),

        # ── DayPass (hotel pools marketplace) ────────────────────
        ("/pool/miami", "location"),
        ("/location/dubai", "location"),
        ("/faq", "faq_support"),
        ("/blog-posts/hotel-day-pass-near-me", "blog_content"),

        # ── Happylab (makerspace) ─────────────────────────────────
        ("/en_vie/workshops/einschulungen/laser-cutter", "product"),
        ("/en_vie/ausstattung", "product"),
        ("/en_vie/membership", "pricing"),

        # ── Dove (brand site) ────────────────────────────────────
        ("/us/en/products.html", "product"),
        ("/us/en/washing-and-bathing.html", "search_results"),
        ("/us/en/stories/about-dove/the-best-care.html", "blog_content"),

        # ── Chargemap ────────────────────────────────────────────
        ("/en-gb/map", "search_results"),
        ("/en-us/networks/plugin", "location"),

        # ── WooCommerce ───────────────────────────────────────────
        ("/product/wireless-headphones", "product"),
        ("/product-category/electronics", "search_results"),
        ("/shop", "product"),
    ]

    paths = [tc[0] for tc in test_cases]
    site = "mixed B2C sites: wallsauce.com, bandago.com, hipcamp.com, keepbloomingflowers.ca, guitarzoom.com, indiegala.com, daypassapp.com, happylab.at, dove.com, chargemap.com"

    print("\n" + "=" * 80)
    print(f"PAGE CLASSIFIER v2.0 — {'🤖 Claude API' if ANTHROPIC_API_KEY else '⚠️  NO API KEY'}")
    print("=" * 80)

    results = classify_urls(paths, site_context=site)

    print(f"\n  {'PATH':<45} {'EXPECTED':<20} {'GOT':<20} {'M'}")
    print("─" * 90)

    ok = fail = 0
    for (path, expected), result in zip(test_cases, results):
        got = result.get("type", "general")
        label = get_page_priority_label(result.get("priority", 5))
        method = result.get("method", "?")[:6]
        mark = "✓" if got == expected else "✗"
        if got == expected: ok += 1
        else: fail += 1
        print(f"  {path:<45} {expected:<20} {got:<20} {method} {mark}")

    print(f"\n  {ok}/{ok+fail}" + (" ✅" if not fail else f"  ❌ {fail} ошибок"))
    print("=" * 80)
