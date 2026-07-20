"""
TNC Business Type Classifier v1.0
==================================
Определение типа бизнеса (B2C / B2B / mixed) + модели + индустрии — ПО САЙТУ.

Каскад (платим за Haiku максимум один раз):
  Слой 1 — словарный скоринг по видимому тексту / URL / платформе / соцсетям
           (бесплатно, всегда, детерминированно).
  Слой 2 — Claude Haiku: в step1 вопрос подсаживается к существующему
           URL-батчу (build_site_verdict_request), пост-хок — одиночный вызов
           (classify_via_haiku). Только когда словаря не хватило.
  Слой 3 — обогащение из библиотеки объявлений (enrich_from_ads) — последний
           резерв: сайт может обслуживать оба направления, а реклама крутиться
           только по одному, поэтому улики из ads помечены источником и
           не перебивают сильные улики сайта.

Выход — блок business_type: buyer_type + вероятность + confidence + модель +
индустрия + сигналы-улики (truth-first: каждое поле отвечает «почему»).

Использование:
    from business_type_classifier import classify_business
    python business_type_classifier.py                  # самотест на синтетике
    python business_type_classifier.py client-a.example       # пост-хок по папке скана
    python business_type_classifier.py --all            # по всем папкам scans/
    Флаги: --llm auto|always|never (дефолт auto), --no-write
"""

import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

from log import (log_debug, log_error, log_header, log_info, log_step,
                 log_success, log_warn)
from utils import load_env, safe_get, scan_path, SCANS_DIR

load_env()  # ключ из engine/.env если в окружении нет — ДО module-level чтения ниже

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_VERSION = "1.0"

VALID_BUYER_TYPES = ("b2c", "b2b", "mixed")
VALID_MODELS = ("app", "saas", "ecom", "services", "info")

# ─── Извлечение текста из HTML (regex, без новых зависимостей) ────────────────

# script/style: незакрытый тег ест до конца документа (|\Z) — как браузер,
# иначе JS утекает в «видимый текст» фейковыми уликами. </script > с пробелом —
# валидный HTML. Квантификаторы ограничены: O(k·n) на битом HTML → O(k·const).
_STRIP_BLOCKS_RE = re.compile(
    r"<(script|style)\b[^>]*>.{0,200000}?(?:</\1\s*>|\Z)"
    r"|<(noscript|svg|template)\b[^>]*>.{0,200000}?</\2\s*>",
    re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.{0,100000}?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

RAW_HTML_CAP = 400_000     # сигнал живёт в первых сотнях КБ; каппим до regex-проходов
VISIBLE_TEXT_CAP = 20_000  # защита от патологических страниц


def _visible_text(html: str) -> str:
    """Видимый текст страницы: без script/style/svg/комментариев, без тегов,
    unescape, схлопнутые пробелы, lowercase. Обрезка до VISIBLE_TEXT_CAP."""
    if not html:
        return ""
    html = html[:RAW_HTML_CAP]
    # Комментарии ДО блоков: закомментированный <script> не должен съесть контент
    text = _COMMENT_RE.sub(" ", html)
    text = _STRIP_BLOCKS_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:VISIBLE_TEXT_CAP].lower()


def _head_bits(html: str) -> dict:
    """<title>, meta description, og:type — для промпта Haiku и сигналов."""
    bits = {"title": "", "meta_description": "", "og_type": ""}
    if not html:
        return bits
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        bits["title"] = _WS_RE.sub(" ", html_lib.unescape(m.group(1))).strip()[:200]
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE) or re.search(
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
        html, re.IGNORECASE)
    if m:
        bits["meta_description"] = html_lib.unescape(m.group(1)).strip()[:300]
    m = re.search(
        r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE)
    if m:
        bits["og_type"] = m.group(1).strip().lower()
    return bits


_CTA_TAG_RE = re.compile(
    r"<(a|button)\b[^>]*>(.{0,3000}?)</\1>", re.IGNORECASE | re.DOTALL)
# lookahead: порядок атрибутов любой (value= до type= тоже), type=submit без кавычек ок
_SUBMIT_RE = re.compile(
    r'<input\b(?=[^>]*type\s*=\s*["\']?submit\b)[^>]*value\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE)


def _cta_texts(html: str, cap: int = 40) -> list:
    """Тексты <a>/<button>/<input type=submit> — короткие, дедуп, ≤cap штук."""
    if not html:
        return []
    html = html[:RAW_HTML_CAP]
    seen, out = set(), []
    for m in _CTA_TAG_RE.finditer(html):
        text = _WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub(" ", m.group(2)))).strip()
        if 2 <= len(text) <= 60 and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
            if len(out) >= cap:
                return out
    for m in _SUBMIT_RE.finditer(html):
        text = m.group(1).strip()
        if 2 <= len(text) <= 60 and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
            if len(out) >= cap:
                break
    return out


# ─── Таблицы сигналов (стиль PLATFORM_SIGNALS: {regex: вес}, потолки на группу) ─
# Матчинг — word-boundary regex по lowercase-тексту (не голый `in html`:
# "roi" сидит внутри фр. droit/croire). Каждый сигнал считается один раз.

BUYER_SIGNALS = {
    "b2b": {
        "text": {
            r"request\s+a\s+demo|book\s+a\s+demo|schedule\s+a\s+demo": 4,
            r"talk\s+to\s+sales|contact\s+sales": 4,
            r"\bb2b\b": 4,
            r"\bmoq\b|minimum\s+order\s+quantit": 4,
            r"\bfor\s+teams\b|\bfor\s+business(es)?\b": 3,
            r"\benterprise\b": 3,
            r"white\s?papers?\b": 3,
            r"\bwholesale\b|\bdistributors?\b|\bresellers?\b": 3,
            r"\bprocurement\b|\bpurchase\s+orders?\b|\bnet\s+30\b": 3,
            r"\bsla\b|service.level\s+agreement": 3,
            r"per\s+seat\b|per\s+user\b": 3,
            r"case\s+stud(y|ies)": 2,
            r"book\s+a\s+call": 2,
            r"trusted\s+by": 2,
            r"request\s+a\s+quote|get\s+a\s+quote": 2,  # consumer-сервисы тоже квотят — низкий вес
            r"\broi\b": 1,
            # FR (рынок Montreal/QC)
            r"demander\s+une\s+d[ée]mo|r[ée]server\s+une\s+d[ée]mo": 4,
            r"demander\s+une\s+soumission|obtenir\s+une\s+soumission": 3,
            r"\bdevis\b": 2,
            r"pour\s+les\s+entreprises|pour\s+les\s+[ée]quipes": 3,
            r"vente\s+en\s+gros|\bgrossistes?\b": 3,
            r"livres?\s+blancs?\b": 3,
            r"[ée]tudes?\s+de\s+cas": 2,
        },
        "url": {
            r"/(request-|book-|schedule-)?demo\b": 4,
            r"/enterprise\b": 4,
            r"/solutions?/": 3,
            r"/case-stud": 3,
            r"/integrations?\b": 3,
            r"/white-?papers?\b": 3,
            r"/for-teams?\b|/for-business\b": 3,
            r"/partners?\b": 2,
            r"/docs\b|/api\b|/developers?\b": 2,
            r"/pricing\b|/plans?\b|/tarifs?\b": 2,  # слабый: B2C SaaS/app тоже прайсят
        },
    },
    "b2c": {
        "text": {
            r"add\s+to\s+(cart|bag|basket)": 4,
            r"free\s+shipping|free\s+delivery": 3,
            r"shop\s+now|buy\s+now|order\s+now": 3,
            r"size\s+(guide|chart)": 3,
            r"\bcheckout\b": 3,
            r"%\s?off\b|\bclearance\b": 3,
            # НЕ голое \bsale\b: по-французски sale = «грязный» (nettoyage de linge sale)
            r"\bon\s+sale\b|\bsale\s+(ends?|now|items?|prices?|up\s+to)\b|\b(final|summer|winter|flash)\s+sale\b": 2,
            r"gift\s?cards?\b|gift\s+ideas?\b": 2,
            r"\bwishlist\b": 2,
            r"best\s?sellers?\b|new\s+arrivals?\b": 2,
            r"\bin\s+stock\b|returns?\s+polic": 2,
            "__price_density__": 3,  # ≥5 ценников [$€£]\d → один сигнал +3
            # FR
            r"ajouter\s+au\s+panier": 4,
            r"livraison\s+gratuite": 3,
            r"\bmagasiner\b": 2,
            r"\bsoldes?\b|\brabais\b": 2,
            r"guide\s+des\s+tailles": 3,
            r"cartes?[- ]cadeaux?": 2,
        },
        "url": {
            r"/checkout\b|/cart\b|/basket\b|/panier\b": 4,
            r"/collections?/|/products?/|/shop\b|/boutique\b": 3,
            r"/store-locator|/find-a-store": 2,
            r"/size-guide|/gift-cards?": 2,
        },
    },
}

# Потолки вклада на группу — одна тема не должна перекричать остальные.
GROUP_CAPS = {"text": 12, "url": 10, "cta": 6, "pages": 6, "social": 4,
              "platform": 4, "ads": 6, "html": 10}

CTA_MULTIPLIER = 1.5    # лексика на кнопке — сильнее той же фразы в абзаце
_PRICE_RE = re.compile(r"[$€£]\s?\d")
PRICE_DENSITY_MIN = 5

# Платформенные prior'ы: {platform: (сторона, вес)}
PLATFORM_PRIORS = {
    "shopify": ("b2c", 4),
    "opencart": ("b2c", 3),
    "wix": ("b2c", 1),
    "squarespace": ("b2c", 1),
    "framer": ("b2b", 1),
    # webflow → 0: профиль неоднозначен (client-a — Webflow и consumer)
}

# ─── Модель бизнеса: независимые скореры, multi-label, порог ≥5 ───────────────

MODEL_SIGNALS = {
    "app": {
        "html": {  # по сырому HTML (ссылки на сторы)
            r"apps\.apple\.com|itunes\.apple\.com": 5,
            r"play\.google\.com/store/apps": 5,
        },
        "text": {
            r"download\s+(the\s+)?app|get\s+the\s+app": 4,
            r"t[ée]l[ée]charge[rz]\s+l.appli": 4,
            r"app\s+store\b|google\s+play\b": 2,
        },
        "url": {r"/download\b|/get-app\b": 2},
    },
    "saas": {
        "html": {
            r'href=["\']https?://(app|dashboard|portal)\.': 4,
        },
        "text": {
            r"start\s+(your\s+)?free\s+trial|essai\s+gratuit": 3,
            r"per\s+month\b|/\s?mo\b|par\s+mois\b": 3,
            r"sign\s?up\b": 1,
            r"log\s?in\b|sign\s?in\b": 1,
        },
        "url": {r"/integrations?\b|/api\b|/docs\b": 2, r"/pricing\b|/plans?\b": 2},
    },
    "ecom": {
        "text": {
            r"add\s+to\s+(cart|bag|basket)|ajouter\s+au\s+panier": 4,
            r"free\s+shipping|livraison\s+gratuite": 2,
            "__price_density__": 2,
        },
        "url": {r"/checkout\b|/cart\b|/panier\b": 4, r"/collections?/|/products?/": 2},
        "platform": {"shopify": 4, "opencart": 3},
        "pages": {"checkout": 4, "product5": 2},
    },
    "services": {
        "text": {
            r"book\s+(a\s+|an\s+)?(call|appointment|consultation)": 3,
            r"prendre\s+rendez-vous": 3,
            r"our\s+(work|portfolio)\b|r[ée]alisations\b": 2,
            r"get\s+a\s+quote|request\s+a\s+quote|soumission|devis": 2,
            r"nos\s+services\b": 2,
        },
        "url": {r"/services?\b": 3, r"/portfolio\b|/our-work\b": 2},
        "pages": {"lead_form2": 2},
    },
    "info": {
        "text": {
            r"\benroll\b|inscri(s|vez)-": 3,
            r"\bcurriculum\b|programme\s+de\s+formation": 3,
            r"\blessons?\b|le[çc]ons?\b|\bmodules?\b": 2,
            r"certificates?\b|certificats?\b": 2,
            r"webinars?\b|masterclass": 1,
        },
        "url": {r"/courses?/|/cours/|/formations?/": 4, r"/curriculum\b|/syllabus\b": 2},
    },
}

MODEL_THRESHOLD = 5
MODEL_MAX_REPORTED = 2

# ─── Индустрия: {regex: (тег, вес)} — открытое множество читаемых тегов ───────
# Никакого маппинга на CPM-профили: выход читает адвайзер, с таблицей работает он.

INDUSTRY_SIGNALS = {
    r"\bcourses?\b|\bcours\b|\bbootcamp\b|e-?learning|\bcurriculum\b|\benroll\b|\bformations?\b|\btutors?\b|\blearn\s+to\b": ("education", 3),
    r"artificial\s+intelligence|\bmachine\s+learning\b|\bllm\b|\bcoding\b|\bprogramming\b|\b(software|web|app|for)\s+developers?\b|\bcopilot\b|\bno-?code\b": ("ai-coding", 3),
    r"\bskincare\b|\bserums?\b|\bcosmetics?\b|\bmakeup\b|\bbeaut[ée]\b|soin\s+de\s+la\s+peau|\bsalon\b|\bspa\b": ("beauty", 3),
    r"\bclothing\b|\bapparel\b|\bfootwear\b|\bsneakers?\b|\blookbook\b|\bv[êe]tements?\b|\bsocks\b|\bhoodies?\b|\bt-?shirts?\b": ("fashion", 3),
    r"\bcars?\b|\bvehicles?\b|\bautomotive\b|\b(air\s+)?suspension\s+(kits?|parts?|systems?)\b|\bcoilovers?\b|\btest\s+drive\b|essai\s+routier|\bvoitures?\b|\bgarage\b|\bdealership\b": ("automotive", 3),
    r"\bclinic\b|\bsymptoms?\b|\bdoctors?\b|\bsant[ée]\b|\bwellness\b|\btherapy\b|\bdental\b|\bmedical\b|\bpatients?\b": ("health", 3),
    r"\bfitness\b|\bworkout\b|\bgym\b|\bathletes?\b|\btraining\s+gear\b": ("sports-fitness", 3),
    r"\bhotels?\b|\bhotel\s+booking\b|\bbook\s+your\s+(stay|trip|flight)\b|\btravel\b|\btours?\b|\bdestinations?\b|\bs[ée]jours?\b|\bvoyages?\b|\bguesthouse\b|\bsuites?\b": ("tourism", 3),
    r"\b(buy|get)\s+tickets?\b|\b(concert|event|festival|match)\s+tickets?\b|\bbillets?\b|\bfestivals?\b|\bconcerts?\b|\bmarathon\b|\bstadium\b|\b[ée]v[ée]nements?\b|\bevents?\b": ("events", 2),
    r"\bsnacks?\b|\bbeverages?\b|\bdrinks?\b|\bgrocery\b|\bfoods?\b|\brecipes?\b|\bcoffee\b|\bcola\b|\bbrewery\b": ("food-beverage", 3),
    r"\binsurance\b|\bloans?\b|\bmortgage\b|\bbanking\b|\binvest(ing|ment)\b|\bfintech\b": ("finance", 3),
    r"\breal\s+estate\b|\bimmobilier\b|\bproperty\b|\bapartments?\b|\bcondos?\b": ("real-estate", 3),
    r"\belectronics?\b|\barduino\b|\braspberry\s+pi\b|\bsensors?\b|\bmicrocontrollers?\b|\bcomponents?\b": ("electronics", 3),
    r"\bfurniture\b|\bdecor\b|\bhome\s+goods\b|\bmattress\b|\bkitchenware\b": ("home-goods", 3),
    r"\bjewel(le)?ry\b|\bbijoux\b|\bwatches\b|\bdiamonds?\b": ("jewelry", 3),
    r"\bpets?\b|\bdogs?\b|\bcats?\b|\bveterinar": ("pets", 3),
    r"\bphotograph(y|ers?)\b|\bphotoshoots?\b|\bportraits?\b|\bweddings?\b": ("photography", 3),
    r"\bflowers?\b|\bbouquets?\b|\bfloral\b|\bfleurs?\b|\bflorists?\b": ("flowers", 3),
    r"\blawyers?\b|\battorneys?\b|\blegal\s+services\b|\bavocats?\b": ("legal", 3),
    r"\bmarketing\s+agency\b|\bseo\b|\bpaid\s+media\b|\bad\s+campaigns?\b": ("marketing", 2),
    r"\blocksmiths?\b|\bplumbers?\b|\belectricians?\b|\bcleaning\s+services?\b|\bmovers?\b|\brenovations?\b": ("local-services", 3),
    r"\bsoftware\b|\bplatform\b|\bautomation\b|\bworkflows?\b|\banalytics\b": ("software", 2),
}

INDUSTRY_MIN_SCORE = 4
INDUSTRY_MIN_SIGNALS = 2

# FB page_category → индустрия (подсказка Facebook, вес +3 в слое обогащения)
PAGE_CATEGORY_MAP = {
    "education": "education",
    "education website": "education",
    "software": "software",
    "software company": "software",
    "app page": "software",
    "clothing": "fashion",
    "clothing (brand)": "fashion",
    "beauty, cosmetic & personal care": "beauty",
    "travel company": "tourism",
    "hotel": "tourism",
    "cars": "automotive",
    "automotive": "automotive",
    "health/beauty": "beauty",
    "medical & health": "health",
    "restaurant": "food-beverage",
    "food & beverage": "food-beverage",
}

# FB cta_type → сторона (слой обогащения)
ADS_CTA_SIGNALS = {
    "b2c": {"SHOP_NOW", "BUY_NOW", "ORDER_NOW", "GET_OFFER", "BOOK_TRAVEL"},
    "b2b": {"CONTACT_US", "GET_QUOTE", "REQUEST_TIME"},
}


# ─── Скоринг ──────────────────────────────────────────────────────────────────

_COMPILED = {}


def _rx(pattern: str):
    """Компиляция с кэшем (таблицы читаются многократно при --all)."""
    if pattern not in _COMPILED:
        _COMPILED[pattern] = re.compile(pattern, re.IGNORECASE)
    return _COMPILED[pattern]


def _match_lexicon(table: dict, text: str, group: str, multiplier: float = 1.0) -> list:
    """Прогоняет {regex: вес} по тексту. Каждый сигнал — один раз (бинарно).
    Возвращает [{"signal","weight","evidence"}]. __price_density__ — спецслучай."""
    hits = []
    if not text:
        return hits
    for pattern, weight in table.items():
        if pattern == "__price_density__":
            n = len(_PRICE_RE.findall(text))
            if n >= PRICE_DENSITY_MIN:
                hits.append({"signal": f"{group}:price_density({n})",
                             "weight": round(weight * multiplier, 1),
                             "pattern": pattern,
                             "evidence": f"{n} ценников на странице"})
            continue
        m = _rx(pattern).search(text)
        if m:
            hits.append({"signal": f"{group}:{m.group(0)[:40]}",
                         "weight": round(weight * multiplier, 1),
                         "pattern": pattern,
                         "evidence": m.group(0)[:80]})
    return hits


def _match_urls(table: dict, paths: list, group: str = "url") -> list:
    """Прогоняет {regex: вес} по списку путей. Сигнал — один раз, улика — первый путь."""
    hits = []
    joined = None
    for pattern, weight in table.items():
        rx = _rx(pattern)
        for p in paths:
            if rx.search(p):
                hits.append({"signal": f"{group}:{pattern[:40]}",
                             "weight": weight, "evidence": p[:80]})
                break
    return hits


def _cap_groups(hits: list) -> tuple:
    """Суммирует веса с потолком на группу (префикс сигнала до ':').
    Возвращает (score, hits) — hits нетронуты, потолок только в сумме."""
    by_group = {}
    for h in hits:
        group = h["signal"].split(":", 1)[0]
        by_group.setdefault(group, 0.0)
        by_group[group] += h["weight"]
    score = sum(min(v, GROUP_CAPS.get(g, 6)) for g, v in by_group.items())
    return round(score, 1), hits


def _paths_from(urls: list = None, classified: list = None) -> list:
    """Список путей: из classified (там уже path) или из urls."""
    paths = []
    for rec in (classified or []):
        p = rec.get("path") or urlparse(rec.get("url", "")).path
        if p:
            paths.append(p.lower())
    if not paths:
        for u in (urls or []):
            p = urlparse(u).path if "://" in u else u
            if p:
                paths.append(p.lower())
    return paths


def _score_buyer(text: str, cta_text: str, paths: list, classified: list,
                 platform: str, social: dict) -> dict:
    """Слой 1 для buyer_type: собирает улики обеих сторон, считает скор."""
    sides = {}
    for side in ("b2b", "b2c"):
        # Кнопка ПЕРЕКРЫВАЕТ ту же фразу в тексте (×1.5), не суммируется с ней:
        # тексты кнопок — часть видимого текста, иначе одна кнопка даёт двойной счёт
        cta_hits = _match_lexicon(BUYER_SIGNALS[side]["text"], cta_text, "cta",
                                  multiplier=CTA_MULTIPLIER)
        on_cta = {h["pattern"] for h in cta_hits}
        hits = [h for h in _match_lexicon(BUYER_SIGNALS[side]["text"], text, "text")
                if h["pattern"] not in on_cta]
        hits += cta_hits
        hits += _match_urls(BUYER_SIGNALS[side]["url"], paths)
        sides[side] = hits

    # Классифицированные страницы (только позитивные улики; отсутствие екома ≠ B2B)
    types = [rec.get("type") for rec in (classified or [])]
    if "checkout" in types:
        sides["b2c"].append({"signal": "pages:checkout", "weight": 4,
                             "evidence": "страница checkout в классификации"})
    n_products = types.count("product")
    if n_products >= 5:
        sides["b2c"].append({"signal": f"pages:product×{n_products}", "weight": 2,
                             "evidence": f"{n_products} product-страниц"})

    # Платформенный prior
    prior = PLATFORM_PRIORS.get((platform or "").lower())
    if prior:
        side, weight = prior
        sides[side].append({"signal": f"platform:{platform}", "weight": weight,
                            "evidence": f"платформа {platform}"})

    # Соцсигналы (ссылки с сайта)
    social = social or {}
    li = social.get("linkedin") or ""
    if isinstance(li, str) and "/company/" in li:
        sides["b2b"].append({"signal": "social:linkedin_company", "weight": 2,
                             "evidence": li[:80]})
        if not social.get("instagram") and not social.get("tiktok"):
            sides["b2b"].append({"signal": "social:linkedin_only", "weight": 2,
                                 "evidence": "LinkedIn есть, IG/TikTok нет"})
    if social.get("tiktok"):
        sides["b2c"].append({"signal": "social:tiktok", "weight": 2,
                             "evidence": str(social["tiktok"])[:80]})
    if social.get("pinterest"):
        sides["b2c"].append({"signal": "social:pinterest", "weight": 2,
                             "evidence": str(social["pinterest"])[:80]})
    if social.get("instagram"):
        sides["b2c"].append({"signal": "social:instagram", "weight": 1,
                             "evidence": str(social["instagram"])[:80]})

    b2b_score, b2b_hits = _cap_groups(sides["b2b"])
    b2c_score, b2c_hits = _cap_groups(sides["b2c"])
    return {"b2b": b2b_score, "b2c": b2c_score,
            "b2b_hits": b2b_hits, "b2c_hits": b2c_hits}


def _buyer_verdict(b2c: float, b2b: float) -> dict:
    """Скор → вероятность (сглаживание Лапласа) → вердикт + confidence."""
    total = b2c + b2b
    p_b2c = round((b2c + 2) / (total + 4), 2)
    p_b2b = round(1 - p_b2c, 2)

    if total < 4:
        return {"buyer_type": "unknown", "confidence": "unknown",
                "probability": {"b2c": p_b2c, "b2b": p_b2b}}
    if min(b2c, b2b) >= 6 and (min(b2c, b2b) / total) >= 0.30:
        conf = "high" if min(b2c, b2b) >= 9 else "medium"
        return {"buyer_type": "mixed", "confidence": conf,
                "probability": {"b2c": p_b2c, "b2b": p_b2b}}
    winner = "b2c" if b2c >= b2b else "b2b"
    p_win = p_b2c if winner == "b2c" else p_b2b
    if p_win >= 0.80 and total >= 10:
        conf = "high"
    elif p_win >= 0.65 and total >= 6:
        conf = "medium"
    else:
        conf = "low"
    return {"buyer_type": winner, "confidence": conf,
            "probability": {"b2c": p_b2c, "b2b": p_b2b}}


def _score_models(text: str, html: str, paths: list, classified: list,
                  platform: str) -> dict:
    """Модели бизнеса — независимые скореры, multi-label."""
    types = [rec.get("type") for rec in (classified or [])]
    scores, hits_by_model = {}, {}
    for model, tables in MODEL_SIGNALS.items():
        hits = []
        hits += _match_lexicon(tables.get("text", {}), text, "text")
        # html-группа — по сырому HTML (ссылки на сторы, поддомены app./dashboard.)
        for pattern, weight in tables.get("html", {}).items():
            m = _rx(pattern).search(html or "")
            if m:
                hits.append({"signal": f"html:{pattern[:40]}", "weight": weight,
                             "evidence": m.group(0)[:80]})
        hits += _match_urls(tables.get("url", {}), paths)
        plat = tables.get("platform", {}).get((platform or "").lower())
        if plat:
            hits.append({"signal": f"platform:{platform}", "weight": plat,
                         "evidence": f"платформа {platform}"})
        pages = tables.get("pages", {})
        if "checkout" in pages and "checkout" in types:
            hits.append({"signal": "pages:checkout", "weight": pages["checkout"],
                         "evidence": "страница checkout"})
        if "product5" in pages and types.count("product") >= 5:
            hits.append({"signal": "pages:product≥5", "weight": pages["product5"],
                         "evidence": f"{types.count('product')} product-страниц"})
        if "lead_form2" in pages and types.count("lead_form") >= 2:
            hits.append({"signal": "pages:lead_form≥2", "weight": pages["lead_form2"],
                         "evidence": f"{types.count('lead_form')} lead_form-страниц"})
        score, hits = _cap_groups(hits)
        if score > 0:
            scores[model] = score
            hits_by_model[model] = hits
    passed = sorted((m for m, s in scores.items() if s >= MODEL_THRESHOLD),
                    key=lambda m: -scores[m])[:MODEL_MAX_REPORTED]
    return {"models": passed, "scores": scores, "hits": hits_by_model}


def _score_industry(text: str, paths: list) -> dict:
    """Индустрия: голоса тегов по лексике сайта + путям. Порог честности:
    главный тег только при score ≥4 и ≥2 РАЗНЫХ словах темы, иначе null → решает Haiku.
    Считаем ОТЛИЧАЮЩИЕСЯ слова внутри альтернации (finditer): «photography,
    photoshoots, portraits» = 3 сигнала; одинокое «software» = 1 → не проходит."""
    votes, hits = {}, []
    corpus = text + " " + " ".join(paths)
    for pattern, (tag, weight) in INDUSTRY_SIGNALS.items():
        matches = {m.group(0).lower() for m in _rx(pattern).finditer(corpus)}
        if matches:
            n = len(matches)
            votes.setdefault(tag, {"score": 0, "n": 0})
            votes[tag]["score"] += weight * min(n, 3)
            votes[tag]["n"] += n
            hits.append({"signal": f"industry:{tag}", "tag": tag,
                         "weight": weight * min(n, 3),
                         "evidence": ", ".join(sorted(matches)[:5])})
    qualified = {t: v["score"] for t, v in votes.items()
                 if v["score"] >= INDUSTRY_MIN_SCORE and v["n"] >= INDUSTRY_MIN_SIGNALS}
    primary = max(qualified, key=qualified.get) if qualified else None
    tags = sorted(votes, key=lambda t: -votes[t]["score"])[:3]
    return {"industry": primary, "tags": tags,
            "scores": {t: v["score"] for t, v in votes.items()}, "hits": hits}


# ─── Слой 1: главная функция ──────────────────────────────────────────────────

def classify_business(html: str, urls: list = None, classified: list = None,
                      platform: str = None, social: dict = None,
                      domain: str = "") -> dict:
    """Детерминированная классификация типа бизнеса по сайту.
    Все аргументы кроме html опциональны — отсутствие входа сужает inputs_used."""
    log_step(f"Классификация типа бизнеса: {domain or '(без домена)'}", emoji="🏪")

    text = _visible_text(html)
    head = _head_bits(html or "")
    ctas = _cta_texts(html or "")
    cta_text = " | ".join(ctas).lower()
    paths = _paths_from(urls, classified)
    log_debug(f"classify_business: text={len(text)} зн., cta={len(ctas)}, "
              f"paths={len(paths)}, platform={platform!r}")

    inputs_used = []
    if html:
        inputs_used.append("homepage_html")
    if urls:
        inputs_used.append("urls")
    if classified:
        inputs_used.append("classified")
    if platform:
        inputs_used.append("platform")
    if social:
        inputs_used.append("social")

    buyer = _score_buyer(text, cta_text, paths, classified, platform, social)
    verdict = _buyer_verdict(buyer["b2c"], buyer["b2b"])
    models = _score_models(text, html or "", paths, classified, platform)
    industry = _score_industry(text, paths)

    result = {
        "schema_version": 1,
        "buyer_type": verdict["buyer_type"],
        "buyer_probability": verdict["probability"],
        "confidence": verdict["confidence"],
        "scores": {"b2c": buyer["b2c"], "b2b": buyer["b2b"]},
        "business_model": models["models"],
        "model_scores": models["scores"],
        "industry": industry["industry"],
        "industry_tags": industry["tags"],
        "industry_scores": industry["scores"],
        "method": "deterministic",
        "field_sources": {
            "buyer_type": "deterministic",
            "business_model": "deterministic",
            "industry": "deterministic",
        },
        "signals": {
            "b2c": buyer["b2c_hits"],
            "b2b": buyer["b2b_hits"],
            "model": models["hits"],
            "industry": industry["hits"],
        },
        "head": head,
        "inputs_used": inputs_used,
        "llm": None,
        "classifier_version": CLASSIFIER_VERSION,
        "classified_at": datetime.now().isoformat(timespec="seconds"),
    }

    log_info(f"Словарь: {verdict['buyer_type']} "
             f"(p_b2c={verdict['probability']['b2c']}, conf={verdict['confidence']}) | "
             f"модель: {', '.join(models['models']) or '—'} | "
             f"индустрия: {industry['industry'] or '—'} {industry['tags']}")
    return result


def needs_llm(result: dict) -> list:
    """Триггеры слоя 2: что именно не решил словарь. Пустой список = не нужен."""
    triggers = []
    if result.get("buyer_type") == "unknown" or result.get("confidence") in ("low", "unknown"):
        triggers.append("buyer_weak")
    if not result.get("business_model"):
        triggers.append("model_empty")
    if not result.get("industry"):
        triggers.append("industry_null")
    return triggers


# ─── Слой 2: Haiku ────────────────────────────────────────────────────────────

SITE_VERDICT_SYSTEM = (
    "You are a business-type classifier for a digital marketing agency. "
    "Respond with a valid JSON object only, no markdown, no explanation.")


def build_site_verdict_request(html: str, urls: list = None, classified: list = None,
                               domain: str = "") -> dict:
    """Материал + инструкция для site-verdict вопроса. Используется и одиночным
    вызовом (пост-хок), и подсадкой в URL-батч step1 (после гейта)."""
    html = (html or "")[:RAW_HTML_CAP]
    text = _visible_text(html)[:3000]
    head = _head_bits(html)
    ctas = _cta_texts(html, cap=20)
    paths = []
    poi_first = sorted((classified or []), key=lambda r: r.get("priority", 9))
    for rec in poi_first:
        p = rec.get("path") or urlparse(rec.get("url", "")).path
        if p and p not in paths:
            paths.append(p)
        if len(paths) >= 40:
            break
    if not paths:
        paths = _paths_from(urls, None)[:40]

    material = (
        f"Website: {domain}\n"
        f"Title: {head['title']}\n"
        f"Meta description: {head['meta_description']}\n\n"
        f"Visible homepage text (excerpt):\n{text}\n\n"
        f"Site page paths:\n" + "\n".join(paths) + "\n\n"
        f"Button/CTA texts:\n" + " | ".join(ctas))

    instruction = (
        "Look at all the pages and content of this website and decide:\n"
        '1. "buyer_type" — who does this site sell to: "b2c" (consumers), '
        '"b2b" (businesses) or "mixed" (both directions).\n'
        '2. "probability_b2c" — number 0.0-1.0.\n'
        '3. "business_model" — subset of ["app","saas","ecom","services","info"].\n'
        '4. "industry" — one short lowercase industry tag '
        '(e.g. "education", "fashion", "automotive"), or null if unclear.\n'
        '5. "industry_tags" — up to 3 lowercase tags.\n'
        '6. "evidence" — for buyer/model/industry give 1-3 short verbatim quotes '
        "copied exactly from the material above.\n\n"
        'Return JSON object: {"buyer_type": "...", "probability_b2c": 0.0, '
        '"business_model": [...], "industry": "...", "industry_tags": [...], '
        '"evidence": {"buyer": [...], "model": [...], "industry": [...]}}')

    return {"material": material, "instruction": instruction}


def parse_site_verdict(obj, material: str) -> dict:
    """Валидация ответа Haiku: enum'ы, клэмп вероятности, проверка цитат
    (каждая улика обязана быть подстрокой отправленного материала)."""
    if not isinstance(obj, dict):
        return {}
    out = {}
    bt = str(obj.get("buyer_type", "")).lower()
    if bt in VALID_BUYER_TYPES:
        out["buyer_type"] = bt
    try:
        p = float(obj.get("probability_b2c"))
        out["probability_b2c"] = round(min(max(p, 0.0), 1.0), 2)
    except (TypeError, ValueError):
        pass
    models = obj.get("business_model") or []
    if isinstance(models, list):
        out["business_model"] = [str(m).lower() for m in models
                                 if str(m).lower() in VALID_MODELS][:MODEL_MAX_REPORTED]
    ind = obj.get("industry")
    if isinstance(ind, str) and 0 < len(ind) <= 40:
        out["industry"] = ind.lower().strip()
    tags = obj.get("industry_tags") or []
    if isinstance(tags, list):
        out["industry_tags"] = [str(t).lower().strip() for t in tags
                                if isinstance(t, str) and 0 < len(t) <= 40][:3]
    # Проверка цитат: непроверяемые выбрасываем, флаг фиксируем
    evidence, all_verified = {}, True
    material_low = (material or "").lower()
    raw_ev = obj.get("evidence") or {}
    if isinstance(raw_ev, dict):
        for key in ("buyer", "model", "industry"):
            quotes = raw_ev.get(key) or []
            kept = []
            for q in quotes if isinstance(quotes, list) else []:
                if isinstance(q, str) and q.strip() and q.strip().lower() in material_low:
                    kept.append(q.strip()[:120])
                else:
                    all_verified = False
            evidence[key] = kept
    out["evidence"] = evidence
    out["evidence_verified"] = all_verified
    return out


def classify_via_haiku(request: dict) -> dict:
    """Одиночный вызов Haiku (когда URL-батча step1 не было).
    Форма запроса — как classify_batch_api: ретраи на 429, срез ```-фенсов,
    graceful degrade (без ключа/ошибка → пустой dict, никогда не бросает)."""
    if not ANTHROPIC_API_KEY:
        log_debug("classify_via_haiku: ANTHROPIC_API_KEY не задан → слой Claude пропущен")
        return {"error": "no_api_key"}
    try:
        for attempt in range(3):
            log_debug(f"classify_via_haiku: POST к Anthropic, попытка {attempt+1}/3")
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1024,
                    "system": SITE_VERDICT_SYSTEM,
                    "messages": [{"role": "user", "content":
                                  request["material"] + "\n\n" + request["instruction"]}],
                },
                timeout=30,
            )
            log_debug(f"classify_via_haiku: HTTP {response.status_code}")
            if response.status_code == 429:
                wait = 15 * (attempt + 1)
                log_warn(f"⏳ Rate limit — жду {wait}с...")
                time.sleep(wait)
                continue
            if response.status_code != 200:
                log_warn(f"API {response.status_code}: {response.text[:80]}")
                return {"error": f"api_{response.status_code}"}
            break
        else:
            log_warn("Rate limit после 3 попыток — слой Claude пропущен")
            return {"error": "rate_limited"}

        text = response.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = re.sub(r"```json?\n?", "", text).replace("```", "").strip()
        obj = json.loads(text)
        if not isinstance(obj, dict):
            log_warn(f"Haiku вернул не-объект JSON ({type(obj).__name__}) — parse_error")
            return {"error": "parse_error_non_object"}
        parsed = parse_site_verdict(obj, request["material"])
        parsed["model_used"] = CLAUDE_MODEL
        return parsed
    except json.JSONDecodeError as e:
        log_warn(f"JSON parse error: {e}")
        return {"error": "parse_error"}
    except Exception as e:
        log_warn(f"API error: {e}")
        return {"error": "exception"}


def merge_llm_verdict(result: dict, verdict: dict, triggers: list) -> dict:
    """Ответ Haiku перекрывает ТОЛЬКО слабые поля (триггеры); сильные
    детерминированные остаются. field_sources хранит происхождение."""
    if not verdict or verdict.get("error"):
        result["llm"] = {"error": verdict.get("error", "empty")} if verdict else {"error": "empty"}
        return result
    if "buyer_weak" in triggers and verdict.get("buyer_type"):
        result["buyer_type"] = verdict["buyer_type"]
        if "probability_b2c" in verdict:
            result["buyer_probability"] = {"b2c": verdict["probability_b2c"],
                                           "b2b": round(1 - verdict["probability_b2c"], 2)}
        else:
            # Haiku дал тип без вероятности — словарная Laplace-оценка может
            # противоречить типу; вероятность не показываем
            result["buyer_probability"] = {}
        result["confidence"] = "medium"  # LLM-вердикт не претендует на high
        result["field_sources"]["buyer_type"] = "llm"
    if "model_empty" in triggers and verdict.get("business_model"):
        result["business_model"] = verdict["business_model"]
        result["field_sources"]["business_model"] = "llm"
    if "industry_null" in triggers and verdict.get("industry"):
        result["industry"] = verdict["industry"]
        if verdict.get("industry_tags"):
            result["industry_tags"] = verdict["industry_tags"]
        result["field_sources"]["industry"] = "llm"
    result["method"] = "deterministic+llm"
    result["llm"] = {
        "model": verdict.get("model_used", CLAUDE_MODEL),
        "triggered_by": triggers,
        "evidence": verdict.get("evidence", {}),
        "evidence_verified": verdict.get("evidence_verified", False),
        # Полный ответ Haiku: при --llm always (forced) поля не мержатся,
        # но для гейта Родиона ответ должен быть виден
        "verdict": {k: verdict[k] for k in
                    ("buyer_type", "probability_b2c", "business_model",
                     "industry", "industry_tags") if k in verdict},
    }
    return result


# ─── Слой 3: обогащение из библиотеки объявлений (последний резерв) ───────────

def enrich_from_ads(result: dict, ads_data: dict) -> dict:
    """Только когда сайт не дал достоверного вывода (buyer unknown/low или
    industry null). Оговорка: сайт может обслуживать оба направления, а реклама
    крутиться по одному — улики помечены ads_library, сайт-улики не перебиваются."""
    if not ads_data:
        return result
    ad_texts = " | ".join(t for t in ads_data.get("ad_texts", []) if t).lower()
    cta_types = set(ads_data.get("cta_types", []))
    page_category = (ads_data.get("page_category") or "").lower().strip()
    applied = []

    buyer_weak = result.get("buyer_type") == "unknown" or result.get("confidence") in ("low", "unknown")
    if buyer_weak and (ad_texts or cta_types):
        hits = {"b2c": [], "b2b": []}
        for side in ("b2c", "b2b"):
            # Лексика по текстам объявлений — на половинном весе
            hits[side] += _match_lexicon(BUYER_SIGNALS[side]["text"], ad_texts,
                                         "ads", multiplier=0.5)
            matched_cta = cta_types & ADS_CTA_SIGNALS[side]
            if matched_cta:
                hits[side].append({"signal": f"ads:cta_{'/'.join(sorted(matched_cta))}",
                                   "weight": 3 if side == "b2c" else 2,
                                   "evidence": f"CTA объявлений: {', '.join(sorted(matched_cta))}"})
        b2c_add, _ = _cap_groups(hits["b2c"])
        b2b_add, _ = _cap_groups(hits["b2b"])
        if b2c_add or b2b_add:
            new_b2c = result["scores"]["b2c"] + b2c_add
            new_b2b = result["scores"]["b2b"] + b2b_add
            verdict = _buyer_verdict(new_b2c, new_b2b)
            if verdict["buyer_type"] != "unknown":
                result["buyer_type"] = verdict["buyer_type"]
                result["buyer_probability"] = verdict["probability"]
                # По рекламе — не выше medium: направление рекламы ≠ весь бизнес
                result["confidence"] = "medium" if verdict["confidence"] == "high" else verdict["confidence"]
                result["scores"] = {"b2c": new_b2c, "b2b": new_b2b}
                result["field_sources"]["buyer_type"] = "ads_library"
                result["signals"]["b2c"] += hits["b2c"]
                result["signals"]["b2b"] += hits["b2b"]
                applied.append("buyer")

    if not result.get("industry") and page_category:
        mapped = PAGE_CATEGORY_MAP.get(page_category)
        if mapped:
            result["industry"] = mapped
            if mapped not in result["industry_tags"]:
                result["industry_tags"] = ([mapped] + result["industry_tags"])[:3]
            result["field_sources"]["industry"] = "ads_library"
            result["signals"]["industry"].append(
                {"signal": f"ads:page_category", "tag": mapped, "weight": 3,
                 "evidence": f"FB page_category: {page_category}"})
            applied.append("industry")
        else:
            # Немаппленная категория — фиксируем дословно с нулевым весом
            result["signals"]["industry"].append(
                {"signal": "ads:page_category_unmapped", "tag": None, "weight": 0,
                 "evidence": f"FB page_category: {page_category}"})

    if applied:
        result["method"] += "+ads_enriched"
        result.setdefault("caveats", []).append(
            "Поля " + ", ".join(applied) + " дополнены по библиотеке объявлений; "
            "сайт может обслуживать и другое направление.")
        log_info(f"Обогащение из ads: {', '.join(applied)}", emoji="📢")
    return result


def _load_ads_data(scan_dir) -> dict:
    """Читает fb_deep/ из папки скана: тексты объявлений, CTA, page_category."""
    ads = {"ad_texts": [], "cta_types": [], "page_category": None}
    link_map_path = os.path.join(scan_dir, "fb_deep", "link_map.json")
    if os.path.exists(link_map_path):
        try:
            with open(link_map_path, encoding="utf-8") as f:
                link_map = json.load(f)
            for entry in link_map.values():
                if not isinstance(entry, dict):
                    continue
                for key in ("body_text", "title", "link_description"):
                    v = entry.get(key)
                    if v and isinstance(v, str):
                        ads["ad_texts"].append(v[:300])
                ct = entry.get("cta_type")
                if ct:
                    ads["cta_types"].append(ct)
            ads["ad_texts"] = ads["ad_texts"][:50]
            log_debug(f"_load_ads_data: link_map — {len(link_map)} объявлений")
        except Exception as e:
            log_debug(f"_load_ads_data: link_map не прочитан: {e}")
    # fb.json (есть у большинства сканов, в отличие от fb_deep/): structured_ads
    # несут cta_type + тексты — единственный источник CTA-сигналов без deep-скана
    fb_path = os.path.join(scan_dir, "fb.json")
    if os.path.exists(fb_path):
        try:
            with open(fb_path, encoding="utf-8") as f:
                fb = json.load(f)
            for acc in (fb.get("accounts") or []):
                for ad in (acc.get("structured_ads") or []):
                    ct = ad.get("cta_type")
                    if ct:
                        ads["cta_types"].append(ct)
                    for key in ("body_text", "title"):
                        v = ad.get(key)
                        if v and isinstance(v, str):
                            ads["ad_texts"].append(v[:300])
            ads["ad_texts"] = ads["ad_texts"][:50]
            log_debug(f"_load_ads_data: fb.json — cta_types={len(ads['cta_types'])}")
        except Exception as e:
            log_debug(f"_load_ads_data: fb.json не прочитан: {e}")
    active_dir = os.path.join(scan_dir, "fb_deep", "active")
    if os.path.isdir(active_dir):
        for name in sorted(os.listdir(active_dir)):
            if name.endswith("_graphql.json") or not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(active_dir, name), encoding="utf-8") as f:
                    ad = json.load(f)
                cat = (ad.get("graphql") or {}).get("page_category")
                if cat:
                    ads["page_category"] = cat
                    break
            except Exception:
                continue
    return ads


# ─── Пост-хок CLI ─────────────────────────────────────────────────────────────

def classify_post_hoc(domain: str, llm_mode: str = "auto", write: bool = True) -> dict:
    """Классификация по существующей папке скана: step1.json + дотяжка главной.
    Пишет сайдкар scans/<домен>/business_type.json (канонический артефакт;
    step1.json НЕ мутируется). В golden/ не пишет никогда."""
    log_header(f"Тип бизнеса (пост-хок): {domain}")
    scan_dir = SCANS_DIR / domain
    step1_path = scan_dir / f"{domain}_step1.json"
    if not step1_path.exists():
        log_error(f"Нет step1: {step1_path}")
        return {"status": "error", "error": "no_step1"}
    with open(step1_path, encoding="utf-8") as f:
        step1 = json.load(f)

    # Слим-выжимка: step1 бывает гигантским (flowwow — 363 МБ, 1.3M записей),
    # держим только нужные поля и отпускаем полный граф
    classified = [{"url": r.get("url"), "path": r.get("path"),
                   "type": r.get("type"), "priority": r.get("priority")}
                  for r in (step1.get("classified") or [])]
    platform = (step1.get("platform") or {}).get("platform", "")
    social = step1.get("social") or {}
    to_scan_n = len(step1.get("to_scan") or [])
    total_found = step1.get("total_found") or 0
    fetch_url = step1.get("scan_url_base") or step1.get("base_url") or f"https://{domain}"
    alt_url = step1.get("base_url")
    del step1

    # Пустой артефакт скана (упавший step1, напр. домен без TLD) — не домен
    if total_found == 0 and not classified and not to_scan_n:
        log_warn(f"{domain}: пустой step1 (упавший скан) — пропуск")
        return {"status": "skipped", "error": "empty_step1"}

    urls = [rec.get("url") for rec in classified if rec.get("url")]

    # Дотяжка главной: сырой HTML в сканах не хранится
    log_step(f"Дотягиваю главную: {fetch_url}", emoji="🌐")
    homepage_html, final_url = "", None
    r = safe_get(fetch_url)
    if r is None or r.status_code >= 400:
        if alt_url and alt_url != fetch_url:
            r = safe_get(alt_url)
    if r is not None and r.status_code < 400:
        # requests по RFC 2616 дефолтит text/* без charset в ISO-8859-1 →
        # UTF-8 французские страницы превращаются в мохибейк (Ã©). Передоверяем
        # apparent_encoding только когда charset не заявлен явно.
        if r.encoding in (None, "ISO-8859-1") and \
                "charset" not in (r.headers.get("Content-Type", "") or "").lower():
            r.encoding = r.apparent_encoding
        homepage_html, final_url = r.text, r.url
        log_success(f"Главная получена: HTTP {r.status_code}, {len(homepage_html)} байт")
    else:
        code = getattr(r, "status_code", "нет ответа")
        log_warn(f"Главная не получена ({code}) — деградация до URL/классификации")

    result = classify_business(homepage_html, urls=urls, classified=classified,
                               platform=platform, social=social, domain=domain)

    # Слой 2 — Haiku
    triggers = needs_llm(result)
    if llm_mode == "always" and not triggers:
        triggers = ["forced"]
    if triggers and llm_mode != "never":
        log_step(f"Слой Haiku: триггеры {triggers}", emoji="🤖")
        request = build_site_verdict_request(homepage_html, urls=urls,
                                             classified=classified, domain=domain)
        verdict = classify_via_haiku(request)
        result = merge_llm_verdict(result, verdict, triggers)
    elif triggers:
        log_info(f"Словаря не хватило ({triggers}), но --llm never — пропуск слоя 2")

    # Слой 3 — обогащение из ads (последний резерв)
    still_weak = (result.get("buyer_type") == "unknown"
                  or result.get("confidence") in ("low", "unknown")
                  or not result.get("industry"))
    if still_weak:
        ads_data = _load_ads_data(str(scan_dir))
        if ads_data.get("ad_texts") or ads_data.get("page_category"):
            result = enrich_from_ads(result, ads_data)

    result["source"] = "post_hoc_cli"
    result["homepage_final_url"] = final_url
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")

    if write:
        golden_dir = (SCANS_DIR.parent / "golden").resolve()
        try:
            in_golden = scan_dir.resolve().is_relative_to(golden_dir)
        except (OSError, ValueError):
            in_golden = False
        if in_golden:
            log_error("Запись в golden/ запрещена — сайдкар не записан")
        else:
            out_path = scan_path(domain, "business_type.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            log_success(f"Сайдкар: {out_path}", emoji="💾")

    _print_verdict(domain, result)
    return result


def _print_verdict(domain: str, result: dict) -> None:
    """Человекочитаемый вердикт с уликами (оценка — правдой её делает Родион)."""
    p = result.get("buyer_probability", {})
    log_header(f"ОЦЕНКА: {domain}")
    log_info(f"Тип покупателя: {result.get('buyer_type')} "
             f"(p_b2c={p.get('b2c')}, p_b2b={p.get('b2b')}, "
             f"conf={result.get('confidence')})")
    log_info(f"Модель бизнеса: {', '.join(result.get('business_model') or []) or '—'}")
    log_info(f"Индустрия: {result.get('industry') or '—'} "
             f"(теги: {', '.join(result.get('industry_tags') or []) or '—'})")
    log_info(f"Метод: {result.get('method')} | входы: {', '.join(result.get('inputs_used', []))}")
    for side in ("b2c", "b2b"):
        hits = sorted(result.get("signals", {}).get(side, []),
                      key=lambda h: -h["weight"])[:5]
        if hits:
            log_info(f"Улики {side}: " + "; ".join(
                f"{h['signal']} (+{h['weight']})" for h in hits))
    for caveat in result.get("caveats", []):
        log_warn(caveat)


def run_all(llm_mode: str = "auto", write: bool = True) -> list:
    """Прогон по всем папкам scans/ где есть <папка>_step1.json.
    Служебные папки (_*) пропускаются. Возвращает сводные строки."""
    rows = []
    folders = sorted(p for p in SCANS_DIR.iterdir()
                     if p.is_dir() and not p.name.startswith("_"))
    targets = [p.name for p in folders if (p / f"{p.name}_step1.json").exists()]
    skipped = [p.name for p in folders if p.name not in targets]
    log_header(f"Прогон по всем сканам: {len(targets)} доменов")
    if skipped:
        log_debug(f"Пропущены (нет step1): {', '.join(skipped)}")
    for domain in targets:
        try:
            result = classify_post_hoc(domain, llm_mode=llm_mode, write=write)
        except Exception as e:
            log_error(f"{domain}: {e}")
            result = {"status": "error", "error": str(e)}
        top_signals = []
        for side in ("b2c", "b2b"):
            for h in sorted(result.get("signals", {}).get(side, []),
                            key=lambda h: -h["weight"])[:3]:
                top_signals.append(f"{side}:{h['signal']}(+{h['weight']})")
        rows.append({"domain": domain, **{k: result.get(k) for k in (
            "buyer_type", "buyer_probability", "confidence", "business_model",
            "industry", "industry_tags", "method", "error")},
            "top_signals": top_signals})
    _print_summary(rows)
    _save_run(rows)
    return rows


def _save_run(rows: list) -> None:
    """Сводка прогона — в отдельную папку scans/_business_type_<дата>/
    (правило: каждый прогон в своей папке, не сваливать в корень scans/)."""
    run_dir = SCANS_DIR / f"_business_type_{datetime.now().strftime('%Y-%m-%d')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "rows.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    lines = [
        "# Прогон классификатора типа бизнеса",
        "",
        f"Дата: {datetime.now().isoformat(timespec='seconds')} | "
        f"доменов: {len(rows)} | classifier v{CLASSIFIER_VERSION}",
        "",
        "Оценки для гейта Родиона — правдой вердикт делает только он.",
        "Сайдкары: `scans/<домен>/business_type.json`.",
        "",
        "| Домен | Тип | p(b2c) | Conf | Модель | Индустрия | Метод | Топ-улики |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    for r in rows:
        p = (r.get("buyer_probability") or {}).get("b2c", "")
        lines.append(
            f"| {r['domain']} | {r.get('buyer_type') or 'ERR'} | {p} "
            f"| {r.get('confidence') or '—'} "
            f"| {', '.join(r.get('business_model') or []) or '—'} "
            f"| {r.get('industry') or '—'} "
            f"| {r.get('method') or r.get('error') or ''} "
            f"| {'; '.join((r.get('top_signals') or [])[:3])} |")
    with open(run_dir / "README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log_success(f"Сводка прогона: {run_dir / 'README.md'}", emoji="💾")


def _print_summary(rows: list) -> None:
    log_header("СВОДКА ПО ВСЕМ ДОМЕНАМ")
    header = f"{'Домен':<28} {'Тип':<8} {'p_b2c':>5} {'Conf':<8} {'Модель':<14} {'Индустрия':<16} {'Метод'}"
    print(header)
    print("─" * len(header))
    for r in rows:
        p = (r.get("buyer_probability") or {}).get("b2c", "")
        models = ",".join(r.get("business_model") or []) or "—"
        print(f"{r['domain']:<28} {r.get('buyer_type') or 'ERR':<8} "
              f"{p!s:>5} {r.get('confidence') or '—':<8} {models:<14} "
              f"{r.get('industry') or '—':<16} {r.get('method') or r.get('error') or ''}")


# ─── Самотест ─────────────────────────────────────────────────────────────────

_FIXTURE_B2C = """<html lang="en"><head><title>Shoe Shop</title></head><body>
<h1>New arrivals — sneakers and footwear</h1><p>Free shipping on orders over $50.
Sale up to 30% off apparel!</p>
<button>Add to cart</button><a href="/checkout">Checkout</a>
<p>$29.99 $39.99 $19.99 $59.99 $24.99 sizes size guide gift cards</p></body></html>"""

_FIXTURE_B2B = """<html lang="en"><head><title>DataPipe — ETL for enterprises</title></head>
<body><h1>Built for teams</h1><p>Trusted by 500 companies. Read our case studies
and white paper. SLA 99.9%. Pricing per seat.</p><button>Request a demo</button>
<a href="/enterprise">Enterprise</a><a href="/solutions/finance">Solutions</a></body></html>"""

_FIXTURE_MIXED = """<html lang="en"><head><title>PhotoLab</title></head><body>
<p>Shop prints — free shipping, add to cart, $19 $29 $39 $49 $59, gift cards, sale!</p>
<button>Add to cart</button><a href="/checkout">Checkout</a>
<p>For business: wholesale and distributors welcome. Request a demo of our studio API.
Enterprise plans per seat, case studies, white paper, SLA.</p>
<button>Talk to sales</button></body></html>"""

_FIXTURE_EMPTY = "<html><body><p>Hello.</p></body></html>"


def _self_test() -> None:
    log_header("Самотест business_type_classifier")
    # (имя, фикстура, ожидаемый buyer_type, ожидаемая индустрия или None=не проверяем)
    cases = [("b2c-магазин", _FIXTURE_B2C, "b2c", "fashion"),
             ("b2b-saas", _FIXTURE_B2B, "b2b", None),
             ("mixed", _FIXTURE_MIXED, "mixed", None),
             ("пустой", _FIXTURE_EMPTY, "unknown", None)]
    failed = 0
    for name, fixture, expected, expected_industry in cases:
        result = classify_business(fixture, domain=name)
        got = result["buyer_type"]
        ok = got == expected
        if expected_industry is not None:
            ok = ok and result["industry"] == expected_industry
        (log_success if ok else log_error)(
            f"{name}: ожидал {expected}"
            + (f"/{expected_industry}" if expected_industry else "")
            + f", получил {got}/{result['industry']} "
            f"(скоры {result['scores']}, conf={result['confidence']})")
        failed += 0 if ok else 1
    # Одинокая кнопка не должна давать high (двойной счёт text+cta — фикс 2026-07-20)
    lone = classify_business(
        "<h1>Acme Consulting</h1><p>We build things.</p>"
        "<button>Request a demo</button>", domain="одинокая-кнопка")
    if lone["confidence"] == "high":
        log_error(f"одинокая-кнопка: conf=high от одной фразы (скоры {lone['scores']})")
        failed += 1
    else:
        log_success(f"одинокая-кнопка: conf={lone['confidence']} (не high) — ок")
    if failed:
        log_error(f"Самотест: {failed} из {len(cases) + 1} провалено")
        sys.exit(1)
    log_success(f"Самотест: {len(cases) + 1}/{len(cases) + 1} ок")


if __name__ == "__main__":
    from utils import setup_console
    setup_console()

    args = [a for a in sys.argv[1:]]
    llm_mode = "auto"
    if "--llm" in args:
        i = args.index("--llm")
        llm_mode = args[i + 1] if i + 1 < len(args) else "auto"
        del args[i:i + 2]
    if llm_mode not in ("auto", "always", "never"):
        log_error(f"--llm {llm_mode!r}: допустимо auto|always|never")
        sys.exit(1)
    write = "--no-write" not in args
    args = [a for a in args if a != "--no-write"]

    if "--all" in args:
        args.remove("--all")
        if args:
            log_error(f"--all несовместим с позиционными аргументами: {args} — "
                      f"либо один домен, либо --all")
            sys.exit(1)
        run_all(llm_mode=llm_mode, write=write)
    elif args:
        if len(args) > 1:
            log_warn(f"Лишние аргументы проигнорированы: {args[1:]}")
        classify_post_hoc(args[0].replace("https://", "").replace("http://", "").strip("/"),
                          llm_mode=llm_mode, write=write)
    else:
        _self_test()
