"""
TNC Pipeline — Platform Detector
=================================
Определяет платформу сайта по HTML, headers, assets и URL структуре.
Запускается в Step 1 до классификации страниц.

Возвращает:
    {
        "platform": "shopify",
        "confidence": "high",      # high / medium / low / unknown
        "score": 12,
        "signals": ["cdn.shopify.com", "/collections/ in sitemap", ...],
        "profile": { ... }         # expected patterns для этой платформы
    }
"""

import re
from urllib.parse import urlparse

from log import log_info, log_debug


# ─── Сигналы платформ ────────────────────────────────────────────────────────

PLATFORM_SIGNALS = {
    "shopify": {
        "weight": {
            # HTML / assets — сильные сигналы
            "cdn.shopify.com":                  5,
            "shopify.theme":                    5,
            "Shopify.theme":                    5,
            "shopify_pay":                      4,
            "myshopify.com":                    5,
            "/cdn/shop/":                       4,
            "shopify-section":                  3,
            "data-shopify":                     3,
            "window.Shopify":                   4,
            "Shopify.routes":                   4,
            # URL структура — из sitemap
            "/collections/":                    3,
            "/products/":                       2,
            "/cart":                            2,
            "/pages/":                          1,
            "/blogs/":                          1,
        },
        "threshold_high": 8,
        "threshold_medium": 4,
    },
    "wordpress": {
        "weight": {
            "wp-content":                       5,
            "wp-includes":                      5,
            "wp-json":                          4,
            "/wp-admin":                        4,
            "wordpress":                        2,
            "woocommerce":                      3,
            "wc-ajax":                          4,
            "WooCommerce":                      3,
            "wp_nonce":                         3,
            "elementor":                        2,
            "wpengine":                         3,
        },
        "threshold_high": 7,
        "threshold_medium": 3,
    },
    "opencart": {
        "weight": {
            # HTML / assets — OpenCart-специфичные пути.
            # НЕ добавлять: голое 'route=' (коллизии с другими фреймворками),
            # 'journal' (имя темы = обычное слово), 'OpenCart'/'system/storage' (0 хитов, server-side).
            # Tested: 2026-07-07 on tinytronics.nl — opencart/high (HTML 5+5+4+2 + header 4)
            "index.php?route=":                 5,
            "catalog/view/theme":               5,
            "catalog/view/javascript":          4,
            "image/cache/":                     2,
            # URL структура — из sitemap (магазины без SEO-rewrite)
            "/index.php?route=":                3,
        },
        "threshold_high": 8,
        "threshold_medium": 4,
    },
    "webflow": {
        "weight": {
            "webflow.js":                       5,
            "assets.website-files.com":         5,
            "uploads-ssl.webflow.com":          5,
            "w-webflow-":                       4,
            "wf-form":                          4,
            "data-wf-":                         3,
            "webflow.com":                      3,
        },
        "threshold_high": 6,
        "threshold_medium": 3,
    },
    "wix": {
        "weight": {
            "static.wixstatic.com":             5,
            "wixsite.com":                      5,
            "_api/wix-":                        4,
            "wix-warmup-data":                  4,
            "X-Seen-By":                        2,  # Wix header
        },
        "threshold_high": 6,
        "threshold_medium": 3,
    },
    "squarespace": {
        "weight": {
            "squarespace.com":                  5,
            "sqsp.net":                         5,
            "static1.squarespace.com":          5,
            "squarespace-cdn.com":              4,
            "sqs-":                             3,
            "data-content-field":               2,
        },
        "threshold_high": 6,
        "threshold_medium": 3,
    },
    "framer": {
        "weight": {
            "framer.com":                       4,
            "framerusercontent.com":            5,
            "framer-motion":                    3,
            "__framer":                         4,
        },
        "threshold_high": 5,
        "threshold_medium": 3,
    },
}


# ─── Профили платформ — expected patterns ────────────────────────────────────

PLATFORM_PROFILES = {
    "shopify": {
        "description": "Shopify e-commerce",
        "url_patterns": {
            "/products/":    {"type": "product",   "priority": 2},
            "/collections/": {"type": "product",   "priority": 2},
            "/cart":         {"type": "checkout",  "priority": 1},
            "/checkout":     {"type": "checkout",  "priority": 1},
            "/pages/":       {"type": "general",   "priority": 5},  # классифицируем по slug
            "/blogs/":       {"type": "blog_content", "priority": 4},
            "/account":      {"type": "technical", "priority": 5},
            "/search":       {"type": "search_results", "priority": 3},
        },
        "expected_pixels": ["Meta", "Google Analytics", "Google Ads"],
        "expected_events": {
            "product":  ["ViewContent", "view_item"],
            "checkout": ["InitiateCheckout", "begin_checkout", "Purchase", "purchase"],
            "lead_form": ["Lead", "generate_lead"],
        },
        "notes": "Shopify /pages/* = static pages, classify by slug keywords",
    },
    "wordpress": {
        "description": "WordPress CMS",
        "url_patterns": {
            "/shop/":        {"type": "product",   "priority": 2},
            "/product/":     {"type": "product",   "priority": 2},
            "/cart/":        {"type": "checkout",  "priority": 1},
            "/checkout/":    {"type": "checkout",  "priority": 1},
            "/blog/":        {"type": "blog_content", "priority": 4},
            "/?p=":          {"type": "blog_content", "priority": 4},
            "/wp-admin":     {"type": "technical", "priority": 5},
        },
        "expected_pixels": ["Meta", "Google Analytics", "Google Ads"],
        "expected_events": {
            "product":  ["ViewContent", "view_item"],
            "checkout": ["Purchase", "purchase"],
            "lead_form": ["Lead", "generate_lead"],
        },
        "notes": "Often uses GTM. Check for WooCommerce events.",
    },
    "opencart": {
        "description": "OpenCart e-commerce",
        "url_patterns": {
            "route=product/product":     {"type": "product",   "priority": 2},
            "route=product/category":    {"type": "product",   "priority": 2},
            "route=checkout/":           {"type": "checkout",  "priority": 1},
            "route=information/contact": {"type": "lead_form", "priority": 1},
            "route=account":             {"type": "technical", "priority": 5},
        },
        "expected_pixels": ["Meta", "Google Analytics", "Google Ads"],
        "expected_events": {
            "product":  ["ViewContent", "view_item"],
            "checkout": ["InitiateCheckout", "begin_checkout", "Purchase", "purchase"],
            "lead_form": ["Lead", "generate_lead"],
        },
        "notes": "SEO-rewrite прячет ?route= из фронтовых URL (tinytronics) — тогда детект по catalog/view/* asset-путям и OCSESSID-куке.",
    },
    "webflow": {
        "description": "Webflow — design-first, usually leadgen or brochure",
        "url_patterns": {},
        "expected_pixels": ["Meta", "Google Analytics"],
        "expected_events": {
            "lead_form": ["Lead", "generate_lead"],
        },
        "notes": "Typically leadgen/brochure. Forms via Webflow native or Typeform/HubSpot embed.",
    },
    "wix": {
        "description": "Wix website builder",
        "url_patterns": {},
        "expected_pixels": ["Meta", "Google Analytics"],
        "expected_events": {
            "lead_form": ["Lead"],
            "product": ["ViewContent", "AddToCart"],
        },
        "notes": "Wix has built-in analytics. Check for Wix stores.",
    },
    "squarespace": {
        "description": "Squarespace — design-first CMS",
        "url_patterns": {},
        "expected_pixels": ["Meta", "Google Analytics"],
        "expected_events": {
            "lead_form": ["Lead"],
        },
        "notes": "Limited GTM support. Usually basic pixel setup.",
    },
    "framer": {
        "description": "Framer — modern no-code, usually SaaS/startup",
        "url_patterns": {},
        "expected_pixels": ["Meta", "Google Analytics"],
        "expected_events": {
            "lead_form": ["Lead", "generate_lead"],
        },
        "notes": "Usually leadgen. Often uses Segment or Mixpanel.",
    },
    "unknown": {
        "description": "Custom / unknown platform",
        "url_patterns": {},
        "expected_pixels": [],
        "expected_events": {},
        "notes": "No platform detected. Use generic classification logic.",
    },
}


# ─── Slug keywords для Shopify /pages/* ──────────────────────────────────────

SHOPIFY_PAGE_SLUG_RULES = [
    # (regex паттерн в slug, тип, приоритет)
    # ── Size guides — ПЕРВЫМИ, иначе "plane-ole-sunday-tee-size-guide" матчится на "plan" ──
    (r"size[-_]guide|sizeguide|size[-_]chart",          "faq_support",  3),
    (r"consultation|booking|book-now|schedule|appoint", "lead_form",    1),
    (r"contact|get-in-touch|reach-us",                  "lead_form",    1),
    (r"faq|faqs|help|support|how-it-works",             "faq_support",  3),
    (r"klarna|affirm|afterpay|sezzle|payment",          "faq_support",  3),
    (r"about|our-story|team|studio|who-we-are",         "about",        3),
    (r"locations?|studios?|cities|city",                "location",     2),
    (r"refund|return|cancell?ation|shipping",           "legal",        5),
    (r"privacy|terms|legal|accessibility|cookie",       "legal",        5),
    (r"policy|policies|compliance|governance|slavery|statement", "legal", 5),
    # Slug'и которые явно не продукт — страницы сервиса/компании
    (r"review|sitemap|careers?|jobs?|press|media|investor|partner|supplier|wholesale|franchise|affiliate|ambassador|influencer-program|become-a", "about", 3),
    (r"unsubscri|success|confirm|verify|reset|activate|sign-?in|sign-?up|log-?in|log-?out|register|account", "technical", 5),
    (r"influencer|ambassador|partner|collab",           "about",        3),
    (r"collection|experience|gallery|portfolio",        "about",        3),
    # ── "plans?" → word boundary чтобы не матчить "plane", "planet" ──
    (r"pricing|(?<![a-z])plans?(?![a-z])|packages?|(?<![a-z])services?(?![a-z])", "pricing", 2),

    # ── Loyalty / membership ──────────────────────────────────────────────────
    # balance.checker ПЕРЕД gift.?card — иначе gift-cards-balance-checker → pricing
    (r"balance.checker",                                                   "technical",    5),
    (r"loyalty|rewards?|points|membership|(?<![a-z])club(?![a-z])|lybc",  "pricing",      2),
    (r"gift.?card|egift|voucher|coupon",                                   "pricing",      2),
    (r"subscri",                                                           "pricing",      2),
    (r"refer.a.friend|referral",                                           "lead_form",    1),

    # ── Search / browse ───────────────────────────────────────────────────────
    (r"sale(?!s-)|offers?|deals?|promo|discount|clearance|outlet|new.arrivals?|bestseller|most.loved|top.rated|trending|view.all|shop.all|range(?!s$)|hub", "search_results", 2),

    # ── Store / service ───────────────────────────────────────────────────────
    (r"store.locator|find.a.store|find.store",                             "location",     2),
    (r"customer.care|customer.service|help.centre|help.center",            "faq_support",  3),
    (r"glossary|sustainability|recycling|refill",                          "about",        3),
    (r"tips.advice|advice(?!r)|guides?",                                   "faq_support",  3),

    # ── Technical / account ───────────────────────────────────────────────────
    (r"(?<![a-z])account|wishlist|my.orders?|order.history|balance.checker|in.store.sign", "technical", 5),

]  # Без catch-all — неопознанные /pages/ идут в general, Claude разберётся


def classify_shopify_page(slug: str) -> dict:
    """Классифицирует Shopify /pages/[slug] по ключевым словам."""
    log_debug(f"classify_shopify_page: start slug={slug!r}")
    slug_lower = slug.lower()
    for pattern, ptype, priority in SHOPIFY_PAGE_SLUG_RULES:
        if re.search(pattern, slug_lower):
            log_debug(f"classify_shopify_page: matched pattern={pattern!r} → type={ptype} priority={priority}")
            return {"type": ptype, "priority": priority, "method": "platform_shopify"}
    log_debug(f"classify_shopify_page: no slug rule matched slug={slug_lower!r} → general (Claude разберётся)")
    return None  # не распознан slug rules — Claude разберётся


# ─── Детектор ────────────────────────────────────────────────────────────────

def detect_platform(html: str, headers: dict = None, sitemap_urls: list = None) -> dict:
    """
    Определяет платформу по HTML, headers и URL структуре из sitemap.

    Args:
        html:         HTML главной страницы (str)
        headers:      HTTP response headers (dict, опционально)
        sitemap_urls: список URL из sitemap (list, опционально)

    Returns:
        dict с полями: platform, confidence, score, signals, profile
    """
    log_debug(f"detect_platform: start html_len={len(html) if html else 0} "
              f"headers={'yes' if headers else 'no'} "
              f"sitemap_urls={len(sitemap_urls) if sitemap_urls else 0}")
    scores = {p: 0 for p in PLATFORM_SIGNALS}
    signals = {p: [] for p in PLATFORM_SIGNALS}

    # ── Анализ HTML ──────────────────────────────────────────────
    log_debug("detect_platform: фаза HTML-сигналов")
    for platform, config in PLATFORM_SIGNALS.items():
        for signal, weight in config["weight"].items():
            # URL-паттерны из sitemap проверяем отдельно ниже
            if signal.startswith("/"):
                continue
            if signal in html:
                scores[platform] += weight
                signals[platform].append(signal)
                log_debug(f"detect_platform: HTML hit {platform} +{weight} ({signal})")

    # ── Анализ headers ───────────────────────────────────────────
    if headers:
        log_debug("detect_platform: фаза анализа headers")
        headers_str = str(headers).lower()
        # Wix специфичные headers
        if "x-seen-by" in headers_str:
            scores["wix"] += 2
            signals["wix"].append("header:X-Seen-By")
            log_debug("detect_platform: header hit wix +2 (X-Seen-By)")
        # Shopify headers
        if "x-shopify" in headers_str:
            scores["shopify"] += 3
            signals["shopify"].append("header:X-Shopify")
            log_debug("detect_platform: header hit shopify +3 (X-Shopify)")
        # OpenCart session cookie (Set-Cookie: OCSESSID=...)
        if "ocsessid" in headers_str:
            scores["opencart"] += 4
            signals["opencart"].append("header:OCSESSID")
            log_debug("detect_platform: header hit opencart +4 (OCSESSID)")
    else:
        log_debug("detect_platform: headers не переданы — фаза пропущена")

    # ── Анализ URL структуры из sitemap ─────────────────────────
    if sitemap_urls:
        log_debug(f"detect_platform: фаза sitemap-сигналов ({len(sitemap_urls)} URL)")
        url_text = " ".join(sitemap_urls)
        for platform, config in PLATFORM_SIGNALS.items():
            for signal, weight in config["weight"].items():
                if not signal.startswith("/"):
                    continue
                count = url_text.count(signal)
                if count > 0:
                    # Капируем вес чтобы не было перевеса от большого каталога
                    capped = min(count, 3) * weight
                    scores[platform] += capped
                    signals[platform].append(f"sitemap:{signal}×{min(count,3)}")
                    log_debug(f"detect_platform: sitemap hit {platform} +{capped} ({signal}×{min(count,3)})")
    else:
        log_debug("detect_platform: sitemap_urls не переданы — фаза пропущена")

    # ── Определяем победителя ────────────────────────────────────
    log_debug(f"detect_platform: итоговые scores={ {p: s for p, s in scores.items() if s > 0} }")
    best_platform = max(scores, key=lambda p: scores[p])
    best_score = scores[best_platform]
    config = PLATFORM_SIGNALS[best_platform]
    log_debug(f"detect_platform: лидер={best_platform} score={best_score} "
              f"(thresholds high={config['threshold_high']} medium={config['threshold_medium']})")

    if best_score >= config["threshold_high"]:
        confidence = "high"
        log_debug(f"detect_platform: score>={config['threshold_high']} → confidence=high")
    elif best_score >= config["threshold_medium"]:
        confidence = "medium"
        log_debug(f"detect_platform: score>={config['threshold_medium']} → confidence=medium")
    elif best_score > 0:
        confidence = "low"
        log_debug("detect_platform: score>0 но ниже medium → confidence=low")
    else:
        best_platform = "unknown"
        confidence = "unknown"
        log_debug("detect_platform: score=0 → platform=unknown, confidence=unknown")

    log_debug(f"detect_platform: result platform={best_platform} confidence={confidence} score={best_score}")
    # ── Формируем результат ──────────────────────────────────────
    return {
        "platform":   best_platform,
        "confidence": confidence,
        "score":      best_score,
        "signals":    signals[best_platform] if best_platform != "unknown" else [],
        "all_scores": {p: s for p, s in scores.items() if s > 0},
        "profile":    PLATFORM_PROFILES.get(best_platform, PLATFORM_PROFILES["unknown"]),
    }


def print_platform_result(result: dict):
    """Красивый вывод результата детектора."""
    platform  = result["platform"]
    confidence = result["confidence"]
    score     = result["score"]
    signals   = result["signals"]
    profile   = result["profile"]
    log_debug(f"print_platform_result: start platform={platform} confidence={confidence} score={score}")

    CONF_EMOJI = {"high": "✅", "medium": "🟡", "low": "⚠️", "unknown": "❓"}
    emoji = CONF_EMOJI.get(confidence, "❓")

    log_info(f"  🏗  Платформа: {platform.upper()}  {emoji} {confidence}  (score: {score})")
    log_info(f"     {profile['description']}")
    if signals:
        log_info(f"     Сигналы: {', '.join(signals[:5])}")
    if result.get("all_scores") and len(result["all_scores"]) > 1:
        others = {p: s for p, s in result["all_scores"].items() if p != platform}
        if others:
            others_str = ", ".join(f"{p}:{s}" for p, s in sorted(others.items(), key=lambda x: -x[1])[:3])
            log_info(f"     Другие:  {others_str}")
    if profile.get("notes"):
        log_info(f"     💡 {profile['notes']}")
