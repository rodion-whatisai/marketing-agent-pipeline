"""
TNC Business Type Classifier v2.0
==================================
Определение типа бизнеса (B2C / B2B / mixed) + модели + индустрии — ПО САЙТУ.
Только английские сайты.

Принцип v2 (комиссия + гейт 2026-07-20): читаем ПРАВИЛЬНОЕ МЕСТО, а не гадаем
по случайным словам тела страницы. Компания сама говорит кто она — в title,
meta-описании, H1, категории/bio своей FB-страницы и на странице About.
v1-словарь по всему телу давал suite→отель, garage→автосервис — выпилено.

Каскад (платим за Haiku максимум один раз, и только с разрешения Родиона):
  Слой 0 — самоописание: title + meta + H1 + FB category/bio (+ дочитка About
           если самоописание — вода). Индустрия живёт здесь.
  Слой 1 — сильные сигналы типа: язык продаж бизнесу (request a demo / contact
           sales / per seat) vs розничный (add to cart / checkout / App Store).
           Соцсети НЕ сигнал (LinkedIn есть у всех). mixed — только когда обе
           стороны имеют СИЛЬНУЮ улику (реальная вторая линия бизнеса).
  Слой 2 — Haiku: одиночный вызов на остаток, право ответить «не знаю».
  Слой 3 — обогащение из библиотеки объявлений (последний резерв, с оговоркой).

Использование:
    from business_type_classifier import classify_business
    python business_type_classifier.py                  # самотест на синтетике
    python business_type_classifier.py client-a.example       # пост-хок по папке скана
    python business_type_classifier.py --all            # по всем папкам scans/
    Флаги: --llm auto|always|never (дефолт never — Haiku только явно),
           --no-write, --no-refetch-about
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
from utils import load_env, safe_get, scan_path, HEADERS, SCANS_DIR

load_env()  # ключ из engine/.env если в окружении нет — ДО module-level чтения ниже

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_VERSION = "2.0"

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
    """Самоописание из головы страницы: title, meta description, og:type, H1."""
    bits = {"title": "", "meta_description": "", "og_type": "", "h1s": []}
    if not html:
        return bits
    html = html[:RAW_HTML_CAP]
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
    for hm in re.finditer(r"<h1[^>]*>(.{0,500}?)</h1>", html,
                          re.IGNORECASE | re.DOTALL):
        text = _WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub(" ", hm.group(1)))).strip()
        if 2 <= len(text) <= 160:
            bits["h1s"].append(text)
        if len(bits["h1s"]) >= 6:
            break
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


# ─── Слой 1: сильные сигналы типа покупателя (English-only, без соцсетей) ─────
# Горстка СИЛЬНЫХ сигналов вместо сорока слабых. Соцсети выкинуты (LinkedIn
# есть у B2C-брендов, Instagram у B2B — наличие соцсети ничего не говорит).

BUYER_SIGNALS = {
    "b2b": {
        "text": {  # язык продаж бизнесу
            r"request\s+a\s+demo|book\s+a\s+demo|schedule\s+a\s+demo|get\s+a\s+demo": 4,
            r"talk\s+to\s+sales|contact\s+sales": 4,
            r"\bb2b\b": 4,
            r"\bmoq\b|minimum\s+order\s+quantit": 4,
            r"per\s+seat\b|per\s+user\b": 3,
            r"\bwholesale\b|\bdistributors?\b|\bresellers?\b": 3,
            r"\bfor\s+teams\b|\bfor\s+business(es)?\b": 3,
            r"white\s?papers?\b": 3,
            r"\bsla\b|service.level\s+agreement": 3,
            r"\bprocurement\b|\bpurchase\s+orders?\b|\bnet\s+30\b": 3,
            r"\benterprise\b": 2,   # понижен: consumer-продукты тоже светят enterprise-план
            r"case\s+stud(y|ies)": 2,
            r"book\s+a\s+call": 2,
        },
        "url": {
            r"/(request-|book-|schedule-)?demo\b": 4,
            r"/contact-?sales\b|/contact/sales\b": 4,  # страница «связаться с продажами»
            r"/enterprise\b": 3,
            r"/wholesale\b": 3,
            r"/case-stud": 2,
        },
    },
    "b2c": {
        # ЧЁРНЫЙ СПИСОК (Родион, 2026-07-21): sign up / get started (free) / book now /
        # start learning / download / free trial / log in / pricing — НЕ сигналы типа:
        # это глаголы действия, одинаковые у B2B и B2C. Здесь только механика розницы.
        "text": {
            r"add\s+to\s+(cart|bag|basket)": 4,
            r"\bcheckout\b": 3,
            # freemium-план (не trial!): free/personal план = потребительская линия
            r"free\s+plan\b|for\s+individuals?\b|personal\s+plan\b|free\s+for\s+(individuals?|personal)": 3,
            r"shop\s+now|buy\s+now|order\s+now": 3,
            r"free\s+shipping|free\s+delivery|free\s+returns": 3,
            r"size\s+(guide|chart)": 3,
            r"%\s?off\b|\bclearance\b": 2,
            r"\bon\s+sale\b|\bsale\s+(ends?|now|items?|prices?|up\s+to)\b|\b(final|summer|winter|flash)\s+sale\b": 2,
            r"gift\s?cards?\b": 2,
            r"\bwishlist\b": 2,
            r"best\s?sellers?\b|new\s+arrivals?\b": 2,
            "__price_density__": 2,  # ≥5 ценников [$€£]\d → один сигнал
        },
        "url": {
            r"/checkout\b|/cart\b|/basket\b": 4,
            # НЕ /products/: у SaaS это страницы продуктов, не розница (miro)
            r"/collections?/|/shop\b": 3,
            r"/store-locator|/find-a-store": 2,
            r"/size-guide|/gift-cards?": 2,
        },
        "html": {  # ссылки на сторы = потребительское приложение
            r"apps\.apple\.com|itunes\.apple\.com": 3,
            r"play\.google\.com/store/apps": 3,
        },
    },
}

# Потолки вклада на группу — одна тема не должна перекричать остальные.
GROUP_CAPS = {"text": 12, "url": 10, "cta": 6, "pages": 6, "platform": 4,
              "ads": 6, "html": 10, "fb": 4}

CTA_MULTIPLIER = 1.5    # лексика на кнопке — сильнее той же фразы в абзаце
_PRICE_RE = re.compile(r"[$€£]\s?\d")
PRICE_DENSITY_MIN = 5
STRONG_WEIGHT = 3       # «сильная улика» для правила mixed (по базовому весу)

# Платформенные prior'ы (только надёжные): Shopify/OpenCart = розничный магазин
PLATFORM_PRIORS = {
    "shopify": ("b2c", 4),
    "opencart": ("b2c", 3),
}

# ─── Модель бизнеса: независимые скореры, multi-label, порог ≥5 ───────────────

MODEL_SIGNALS = {
    "app": {
        "html": {
            r"apps\.apple\.com|itunes\.apple\.com": 5,
            r"play\.google\.com/store/apps": 5,
        },
        "text": {
            r"download\s+(the\s+)?app|get\s+the\s+app": 4,
            r"app\s+store\b|google\s+play\b": 2,
        },
        "url": {r"/download\b|/get-app\b": 2},
    },
    "saas": {
        "html": {
            r'href=["\']https?://(app|dashboard|portal)\.': 4,
        },
        "text": {
            r"start\s+(your\s+)?free\s+trial": 3,
            r"per\s+month\b|/\s?mo\b": 3,
            r"sign\s?up\b": 1,
            r"log\s?in\b|sign\s?in\b": 1,
        },
        "url": {r"/integrations?\b|/api\b|/docs\b": 2, r"/pricing\b|/plans?\b": 2},
    },
    "ecom": {
        "text": {
            r"add\s+to\s+(cart|bag|basket)": 4,
            r"free\s+shipping": 2,
            "__price_density__": 2,
        },
        "url": {r"/checkout\b|/cart\b": 4, r"/collections?/|/products?/": 2},
        "platform": {"shopify": 4, "opencart": 3},
        "pages": {"checkout": 4, "product5": 2},
    },
    "services": {
        "text": {
            r"book\s+(a\s+|an\s+)?(call|appointment|consultation)": 3,
            r"our\s+(work|portfolio)\b": 2,
            r"get\s+a\s+quote|request\s+a\s+quote": 2,
            r"our\s+services\b": 2,
        },
        "url": {r"/services?\b": 3, r"/portfolio\b|/our-work\b": 2},
        "pages": {"lead_form2": 2},
    },
    "info": {
        "text": {
            r"\benroll\b": 3,
            r"\bcurriculum\b": 3,
            r"\blessons?\b|\bmodules?\b": 2,
            r"certificates?\b": 2,
            r"webinars?\b|masterclass": 1,
        },
        "url": {r"/courses?/": 4, r"/curriculum\b|/syllabus\b": 2},
    },
}

MODEL_THRESHOLD = 5
MODEL_MAX_REPORTED = 2

# ─── Слой 0: индустрия из САМООПИСАНИЯ ────────────────────────────────────────
# Матчится ТОЛЬКО по самоописанию (title + meta + H1 + FB bio + About-выжимка)
# и слагам путей — НЕ по телу страницы. В title у Acronis написано
# «Cybersecurity», а не «suite» — коллизии генерик-слов исчезают на этом корпусе.

INDUSTRY_SIGNALS = {
    r"\bbootcamps?\b|e-?learning\b|\bcurriculum\b|\benroll\b|online\s+courses?\b|\bsyllabus\b|\bcourses?\b|learn\s+[\w-]+\s+skills?\b|career\s+paths?\b": "education",
    # НЕ голое coding: «coding bootcamps / learn coding» — это education, не девтулзы
    r"\bcopilot\b|no-?code\b|\bide\b|code\s+completion|pair\s+programming|developer\s+(platform|tools?)|\bsdk\b|coding\s+(assistant|agent|tool)": "dev-tools",
    r"cyber\s?security\b|data\s+protection\b|\bantivirus\b|threat\s+(intel|detection)|anti-?fraud\b|\binfosec\b": "cybersecurity",
    r"\bskincare\b|\bcosmetics?\b|\bmakeup\b|\bbeauty\b|\bserums?\b": "beauty",
    r"\bshoes?\b|\bapparel\b|\bclothing\b|\bfootwear\b|\bsneakers?\b|\bsocks\b|\bunderwear\b|\bfashion\b": "fashion",
    r"\bcars?\b|\bvehicles?\b|\bautomotive\b|\bdealership\b|air\s+suspension\b|\bcoilovers?\b": "automotive",
    r"\bclinic\b|\bmedical\b|\bhealthcare?\b|\bwellness\b|\bsymptom|\btherapy\b|\bdental\b|women.s\s+health\b": "health",
    r"\bfitness\b|\bworkout\b|\bgym\b|athletic\s+wear": "fitness",
    r"\bhotels?\b|\bflights?\b|\btravel\b|\btrips?\b|\bitinerar|\bvacation|book\s+your\s+(stay|trip|flight)": "travel",
    r"(buy|get|sell)\s+tickets?\b|event\s+(ticketing|platform)|\bfestivals?\b|\bconcerts?\b": "events-ticketing",
    r"\bsnacks?\b|\bbeverages?\b|\bdrinks?\b|\bcoffee\b|\bcola\b|\bfood\b|\brecipes?\b|\brestaurants?\b": "food-beverage",
    r"\bflowers?\b|\bbouquets?\b|\bflorists?\b|gift\s+(shop|delivery)": "flowers-gifts",
    r"\bphotograph(y|ers?)\b|\bphotoshoots?\b|wedding\s+(photo|video)": "photography",
    r"\bbanking\b|\binsurance\b|\bloans?\b|\bmortgage\b|\binvest(ing|ment)s?\b|wealth\s+management": "finance",
    r"\bfintech\b|\bneobank\b|\bbnpl\b|buy\s+now.{0,5}pay\s+later|payment\s+(gateway|processing|acquiring|platform)|\bacquiring\b|money\s+transfers?\b|\bcalling\s+app\b": "fintech",
    r"\bcrypto(currency)?\b|\bbitcoin\b|\bethereum\b|\bblockchain\b|\bweb3\b|\bnfts?\b|\bstablecoins?\b|mining\s+pool\b|\bdefi\b": "crypto",
    r"\btrading\b|\bbrokerage\b|multi-?asset|\bcharting\b|stock\s+market": "trading",
    r"\bgames?\b|\bgaming\b|free-?to-?play|\bplayers?\b|match-?3\b": "gaming",
    r"real\s+estate\b|\bproperty\b|\bapartments?\b|\bcondos?\b|interior\s+design|home\s+design": "real-estate",
    r"\belectronics?\b|\barduino\b|raspberry\s+pi\b|\bsensors?\b|\bmicrocontrollers?\b": "electronics",
    r"\badvertising\b|\badtech\b|programmatic\b|\bretargeting\b|marketing\s+(platform|automation|intelligence)|\bseo\b|\bmartech\b": "adtech-martech",
    r"\bpayroll\b|\bhiring\b|\brecruit(ing|ment)?\b|\bhr\s+(platform|software)|talent\s+(acquisition|management)|global\s+employment": "hr",
    r"\btranslation\b|\blocalization\b|\bdubbing\b": "translation",
    r"text-?to-?image|image\s+generation|design\s+(tool|platform)|generative\s+(ai|design)|\billustrations?\b": "design-tools",
    r"cloud\s+(infrastructure|platform|computing)|\bgpu\b|\bhosting\b|dedicated\s+servers?\b|\bdata\s+centers?\b": "cloud-infra",
    r"e-?signature\b|document\s+(automation|workflow)|\besign\b|\bpdf\b": "document-automation",
    r"ride-?hailing\b|\brides?\b|scooters?\b|bike\s+sharing|\bmobility\b|\bdelivery\s+app\b": "mobility",
    r"\bdating\b|social\s+discovery\b|\bstreaming\b|live\s+video\b": "social-apps",
    r"\bsoftware\b|\bsaas\b|\bplatform\b|\bworkspace\b|\bautomation\b|\bworkflows?\b|\banalytics\b": "software",  # generic — вес ниже
}

INDUSTRY_GENERIC_TAGS = {"software"}       # вес 2 вместо 3 (fallback-тег)
INDUSTRY_SELF_DESC_WEIGHT = 3              # хит в самоописании
INDUSTRY_PATH_WEIGHT = 2                   # хит в слагах путей
INDUSTRY_MIN_SCORE = 3                     # корпус концентрированный: 1 хит в title = надёжно

# ─── ЛЕСЕНКА ТИПА (комиссия 2026-07-21): категория → адресат → механика ──────
# Сайт не пишет «мы B2B» — но всегда пишет ЧТО продаёт. Тип ВЫВОДИТСЯ:
# ступень 1 — решающая категория; ступень 2 — кого называют клиентом;
# ступень 3 — механика покупки (наблюдение кликера выше словаря); 4 — unknown.

B2C_DECISIVE_INDUSTRIES = {
    "gaming", "fashion", "beauty", "food-beverage", "travel", "photography",
    "fitness", "social-apps", "flowers-gifts", "mobility", "pets",
}
B2B_DECISIVE_INDUSTRIES = {
    "cybersecurity", "cloud-infra", "dev-tools", "adtech-martech", "hr",
    "document-automation", "translation",
}
# Спорные (решает ступень 2/3): education, fintech, crypto, trading, health,
# software, finance, electronics, real-estate, events-ticketing, design-tools...

AUDIENCE_SIGNALS = {
    "b2b": r"for\s+(teams?|business(es)?|agencies|enterprises?|developers|marketers|"
           r"recruiters|merchants|advertisers|brands)\b|your\s+(team|company|"
           r"organization|business|workforce)\b|\bmsps?\b|of\s+any\s+size\b",
    "b2c": r"no\s+experience\s+needed|from\s+scratch|your\s+career\b|\bplayers?\b|"
           r"\bstudents?\b|\btravell?ers?\b|\bguests?\b|anyone\s+can\b|"
           r"your\s+family\b|for\s+everyone\b|\byour\s+skin\b|\byour\s+home\b",
}

# FB-категория → индустрия. Неинформативные (Brand, Company, Product/service)
# сознательно НЕ маппятся — записываются в улики как есть, без голоса.
FB_CATEGORY_MAP = {
    "education website": "education", "education": "education",
    "education company": "education",
    "software": "software", "software company": "software",
    "internet software": "software",
    "clothing (brand)": "fashion", "clothing store": "fashion",
    "clothing company": "fashion", "apparel & clothing": "fashion",
    "beauty, cosmetic & personal care": "beauty", "health/beauty": "beauty",
    "cosmetics store": "beauty",
    "travel company": "travel", "hotel": "travel", "travel agency": "travel",
    "automotive": "automotive", "cars": "automotive",
    "automotive parts store": "automotive",
    "restaurant": "food-beverage", "food & beverage": "food-beverage",
    "food & beverage company": "food-beverage",
    "financial service": "finance", "bank": "finance",
    "insurance company": "finance",
    "video game": "gaming", "games/toys": "gaming", "gaming video creator": "gaming",
    "medical & health": "health", "hospital": "health",
    "e-commerce website": "ecommerce", "retail company": "retail",
    "shopping & retail": "retail",
    "photographer": "photography", "photography videography": "photography",
    "real estate": "real-estate",
    "advertising agency": "adtech-martech", "marketing agency": "adtech-martech",
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
    Возвращает [{"signal","weight","base_weight","pattern","evidence"}]."""
    hits = []
    if not text:
        return hits
    for pattern, weight in table.items():
        if pattern == "__price_density__":
            n = len(_PRICE_RE.findall(text))
            if n >= PRICE_DENSITY_MIN:
                hits.append({"signal": f"{group}:price_density({n})",
                             "weight": round(weight * multiplier, 1),
                             "base_weight": weight, "pattern": pattern,
                             "evidence": f"{n} ценников на странице"})
            continue
        m = _rx(pattern).search(text)
        if m:
            hits.append({"signal": f"{group}:{m.group(0)[:40]}",
                         "weight": round(weight * multiplier, 1),
                         "base_weight": weight, "pattern": pattern,
                         "evidence": m.group(0)[:80]})
    return hits


def _match_urls(table: dict, paths: list, group: str = "url") -> list:
    """Прогоняет {regex: вес} по списку путей. Сигнал — один раз, улика — первый путь."""
    hits = []
    for pattern, weight in table.items():
        rx = _rx(pattern)
        for p in paths:
            if rx.search(p):
                hits.append({"signal": f"{group}:{pattern[:40]}",
                             "weight": weight, "base_weight": weight,
                             "pattern": pattern, "evidence": p[:80]})
                break
    return hits


def _cap_groups(hits: list) -> tuple:
    """Суммирует веса с потолком на группу (префикс сигнала до ':')."""
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


def _has_strong(hits: list) -> bool:
    """Сильная улика: базовый вес ≥ STRONG_WEIGHT в группах text/cta/url/pages/html.
    Платформенный prior сильной уликой НЕ считается (Shopify сам по себе ≠ вторая
    линия бизнеса для правила mixed)."""
    for h in hits:
        group = h["signal"].split(":", 1)[0]
        if group in ("text", "cta", "url", "pages", "html") and \
                h.get("base_weight", 0) >= STRONG_WEIGHT:
            return True
    return False


def _score_buyer(text: str, cta_text: str, paths: list, classified: list,
                 platform: str) -> dict:
    """Слой 1: сильные сигналы типа. Соцсети НЕ участвуют (v2)."""
    sides = {}
    for side in ("b2b", "b2c"):
        # Кнопка ПЕРЕКРЫВАЕТ ту же фразу в тексте (×1.5), не суммируется с ней
        cta_hits = _match_lexicon(BUYER_SIGNALS[side]["text"], cta_text, "cta",
                                  multiplier=CTA_MULTIPLIER)
        on_cta = {h["pattern"] for h in cta_hits}
        hits = [h for h in _match_lexicon(BUYER_SIGNALS[side]["text"], text, "text")
                if h["pattern"] not in on_cta]
        hits += cta_hits
        hits += _match_urls(BUYER_SIGNALS[side]["url"], paths)
        sides[side] = hits

    # Ссылки на сторы (по сырому HTML) — b2c
    # (передаются через text-параметр отдельным вызовом в classify_business)

    # Классифицированные страницы (только позитивные улики; лидген ≠ B2B)
    types = [rec.get("type") for rec in (classified or [])]
    has_ecom = "checkout" in types or (platform or "").lower() in PLATFORM_PRIORS
    if "checkout" in types:
        sides["b2c"].append({"signal": "pages:checkout", "weight": 4,
                             "base_weight": 4, "pattern": "pages:checkout",
                             "evidence": "страница checkout в классификации"})
    n_products = types.count("product")
    # product-страницы = розница ТОЛЬКО рядом с екомом (иначе это SaaS-продукты, miro)
    if n_products >= 5 and has_ecom:
        sides["b2c"].append({"signal": f"pages:product×{n_products}", "weight": 2,
                             "base_weight": 2, "pattern": "pages:product5",
                             "evidence": f"{n_products} product-страниц (+ еком)"})

    # Платформенный prior (Shopify/OpenCart = розничный магазин)
    prior = PLATFORM_PRIORS.get((platform or "").lower())
    if prior:
        side, weight = prior
        sides[side].append({"signal": f"platform:{platform}", "weight": weight,
                            "base_weight": weight, "pattern": f"platform:{platform}",
                            "evidence": f"платформа {platform}"})

    b2b_score, b2b_hits = _cap_groups(sides["b2b"])
    b2c_score, b2c_hits = _cap_groups(sides["b2c"])
    return {"b2b": b2b_score, "b2c": b2c_score,
            "b2b_hits": b2b_hits, "b2c_hits": b2c_hits,
            "b2b_strong": _has_strong(sides["b2b"]),
            "b2c_strong": _has_strong(sides["b2c"])}


def _ladder_verdict(industry: str, self_desc: str, mech: dict,
                    observed: dict = None) -> dict:
    """ЛЕСЕНКА типа покупателя: категория → адресат → механика → unknown.
    Улики копятся с ОБЕИХ сторон по всем ступеням; mixed (правило А) — когда
    обе стороны получили сильную улику. decided_by объясняет, КАК решили."""
    ev = {"b2c": [], "b2b": []}
    self_desc = (self_desc or "").lower()

    # Ступень 1 — решающая категория («делаем мобильные игры» → покупатель игрок)
    if industry in B2C_DECISIVE_INDUSTRIES:
        ev["b2c"].append({"step": "industry", "strong": True,
                          "evidence": f"категория {industry} — потребительская"})
    elif industry in B2B_DECISIVE_INDUSTRIES:
        ev["b2b"].append({"step": "industry", "strong": True,
                          "evidence": f"категория {industry} — бизнесовая"})

    # Ступень 2 — кого сайт называет клиентом (в самоописании)
    for side, pattern in AUDIENCE_SIGNALS.items():
        m = _rx(pattern).search(self_desc)
        if m:
            ev[side].append({"step": "audience", "strong": True,
                             "evidence": m.group(0)[:60]})

    # Ступень 3 — механика покупки. Наблюдение кликера ВЫШЕ словаря.
    observed = observed or {}
    if observed.get("add_to_cart_fired"):
        ev["b2c"].append({"step": "observed", "strong": True,
                          "evidence": "клик по корзине — конверсионное событие стрельнуло"})
    if observed.get("checkout_page_seen"):
        ev["b2c"].append({"step": "observed", "strong": True,
                          "evidence": "страница checkout реально просканирована"})
    for side in ("b2c", "b2b"):
        for h in mech.get(f"{side}_hits", []):
            group = h["signal"].split(":", 1)[0]
            # Платформенный prior (Shopify) — слабая улика, НЕ повод для mixed:
            # платформа сама по себе ≠ вторая аудитория
            strong = group in ("text", "cta", "url", "pages", "html") \
                and h.get("base_weight", 0) >= 3
            ev[side].append({"step": "mechanics", "strong": strong,
                             "evidence": f"{h['signal']}"})

    def weight(side):
        return sum(3 if e["strong"] else 1 for e in ev[side])

    w_c, w_b = weight("b2c"), weight("b2b")
    p_b2c = round((w_c + 2) / (w_c + w_b + 4), 2)
    strong_c = any(e["strong"] for e in ev["b2c"])
    strong_b = any(e["strong"] for e in ev["b2b"])
    steps_c = {e["step"] for e in ev["b2c"]}
    steps_b = {e["step"] for e in ev["b2b"]}

    if not ev["b2c"] and not ev["b2b"]:
        return {"buyer_type": "unknown", "confidence": "unknown",
                "probability": {"b2c": p_b2c, "b2b": round(1 - p_b2c, 2)},
                "decided_by": "none", "evidence": ev}
    if strong_c and strong_b:
        # правило А: обе аудитории с реальными уликами → mixed
        conf = "high" if (len(steps_c) >= 2 or len(steps_b) >= 2) else "medium"
        return {"buyer_type": "mixed", "confidence": conf,
                "probability": {"b2c": p_b2c, "b2b": round(1 - p_b2c, 2)},
                "decided_by": "+".join(sorted(steps_c | steps_b)), "evidence": ev}
    winner = "b2c" if w_c >= w_b else "b2b"
    w_steps = steps_c if winner == "b2c" else steps_b
    w_strong = strong_c if winner == "b2c" else strong_b
    if "industry" in w_steps or len(w_steps) >= 2:
        conf = "high"
    elif w_strong:
        conf = "medium"
    else:
        conf = "low"
    return {"buyer_type": winner, "confidence": conf,
            "probability": {"b2c": p_b2c, "b2b": round(1 - p_b2c, 2)},
            "decided_by": "+".join(sorted(w_steps)), "evidence": ev}


def _buyer_verdict(b2c: float, b2b: float, b2c_strong: bool, b2b_strong: bool) -> dict:
    """Скор → вероятность (сглаживание Лапласа) → вердикт + confidence.
    mixed (v2) — только при реальной второй линии: ОБЕ стороны имеют сильную
    улику И обе набрали ≥6 И меньшинство ≥35% (одно залётное слово не тянет)."""
    total = b2c + b2b
    p_b2c = round((b2c + 2) / (total + 4), 2)
    p_b2b = round(1 - p_b2c, 2)

    if total < 4:
        return {"buyer_type": "unknown", "confidence": "unknown",
                "probability": {"b2c": p_b2c, "b2b": p_b2b}}
    # mixed (правило А): у КАЖДОЙ стороны сильная улика (base_weight≥3) — реальная
    # вторая аудитория, а не залётное слово. Порог меньшинства ≥3 (одна сильная
    # улика) и доля ≥25%. Enterprise+Free tier = mixed (Miro), но еком с одной
    # «wholesale» тоже mixed — под правилом А это осознанно (они и правда оптовят).
    if b2c_strong and b2b_strong and min(b2c, b2b) >= 3 \
            and (min(b2c, b2b) / total) >= 0.25:
        conf = "high" if min(b2c, b2b) >= 8 else "medium"
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
        for pattern, weight in tables.get("html", {}).items():
            m = _rx(pattern).search(html or "")
            if m:
                hits.append({"signal": f"html:{pattern[:40]}", "weight": weight,
                             "base_weight": weight, "pattern": pattern,
                             "evidence": m.group(0)[:80]})
        hits += _match_urls(tables.get("url", {}), paths)
        plat = tables.get("platform", {}).get((platform or "").lower())
        if plat:
            hits.append({"signal": f"platform:{platform}", "weight": plat,
                         "base_weight": plat, "pattern": f"platform:{platform}",
                         "evidence": f"платформа {platform}"})
        pages = tables.get("pages", {})
        if "checkout" in pages and "checkout" in types:
            hits.append({"signal": "pages:checkout", "weight": pages["checkout"],
                         "base_weight": pages["checkout"], "pattern": "pages:checkout",
                         "evidence": "страница checkout"})
        if "product5" in pages and types.count("product") >= 5:
            hits.append({"signal": "pages:product≥5", "weight": pages["product5"],
                         "base_weight": pages["product5"], "pattern": "pages:product5",
                         "evidence": f"{types.count('product')} product-страниц"})
        if "lead_form2" in pages and types.count("lead_form") >= 2:
            hits.append({"signal": "pages:lead_form≥2", "weight": pages["lead_form2"],
                         "base_weight": pages["lead_form2"], "pattern": "pages:lead_form2",
                         "evidence": f"{types.count('lead_form')} lead_form-страниц"})
        score, hits = _cap_groups(hits)
        if score > 0:
            scores[model] = score
            hits_by_model[model] = hits
    passed = sorted((m for m, s in scores.items() if s >= MODEL_THRESHOLD),
                    key=lambda m: -scores[m])[:MODEL_MAX_REPORTED]
    return {"models": passed, "scores": scores, "hits": hits_by_model}


def _score_industry(self_desc: str, paths: list, fb_category: str = None) -> dict:
    """Слой 0: индустрия из САМООПИСАНИЯ (title+meta+H1+FB bio+About), не из тела.
    Корпус концентрированный → один хит в title надёжен (порог score ≥3).
    FB-категория (если информативна) голосует +4."""
    votes, hits = {}, []
    self_desc = (self_desc or "").lower()
    path_str = " ".join(paths or [])

    for pattern, tag in INDUSTRY_SIGNALS.items():
        weight = 2 if tag in INDUSTRY_GENERIC_TAGS else INDUSTRY_SELF_DESC_WEIGHT
        matches = {m.group(0).lower() for m in _rx(pattern).finditer(self_desc)}
        if matches:
            n = min(len(matches), 2)
            votes.setdefault(tag, 0)
            votes[tag] += weight * n
            hits.append({"signal": f"industry:{tag}", "tag": tag,
                         "weight": weight * n, "source": "self_description",
                         "evidence": ", ".join(sorted(matches)[:4])})
        elif path_str:
            m = _rx(pattern).search(path_str)
            if m:
                votes.setdefault(tag, 0)
                votes[tag] += INDUSTRY_PATH_WEIGHT
                hits.append({"signal": f"industry:{tag}", "tag": tag,
                             "weight": INDUSTRY_PATH_WEIGHT, "source": "url_path",
                             "evidence": m.group(0)[:60]})

    if fb_category:
        mapped = FB_CATEGORY_MAP.get(fb_category.lower().strip())
        if mapped:
            votes.setdefault(mapped, 0)
            votes[mapped] += 4
            hits.append({"signal": "industry:fb_category", "tag": mapped,
                         "weight": 4, "source": "fb_category",
                         "evidence": f"FB: {fb_category}"})
        else:
            hits.append({"signal": "industry:fb_category_unmapped", "tag": None,
                         "weight": 0, "source": "fb_category",
                         "evidence": f"FB: {fb_category} (неинформативна)"})

    # generic-тег не должен победить специфичный при равном счёте
    def rank(t):
        return (votes[t], t not in INDUSTRY_GENERIC_TAGS)
    qualified = [t for t in votes if votes[t] >= INDUSTRY_MIN_SCORE]
    primary = max(qualified, key=rank) if qualified else None
    tags = sorted(votes, key=lambda t: -votes[t])[:3]
    return {"industry": primary, "tags": tags, "scores": votes, "hits": hits}


# ─── Слой 0+1: главная функция ────────────────────────────────────────────────

def classify_business(html: str, urls: list = None, classified: list = None,
                      platform: str = None, domain: str = "",
                      fb_category: str = None, fb_bio: str = None,
                      about_text: str = None, pricing_text: str = None,
                      observed: dict = None) -> dict:
    """Классификация по сайту (v3: лесенка категория→адресат→механика).
    Все аргументы кроме html опциональны — отсутствие входа сужает inputs_used.
    pricing_text — страница цен (Free tier=B2C, Enterprise=B2B → ключ к mixed).
    observed — наблюдение step2 («глаз» + кликер): page_eyes, add_to_cart_fired,
    checkout_page_seen. Наблюдение ставится выше словарных догадок."""
    log_step(f"Классификация типа бизнеса: {domain or '(без домена)'}", emoji="🏪")

    text = _visible_text(html)
    head = _head_bits(html or "")
    ctas = _cta_texts(html or "")
    cta_text = " | ".join(ctas).lower()
    paths = _paths_from(urls, classified)

    # Корпус самоописания: где компания сама говорит кто она.
    # + «глаз» step2: title/meta/H1/тарифные строки ОТРИСОВАННЫХ страниц
    observed = observed or {}
    eye_parts = []
    for eye in observed.get("page_eyes", [])[:10]:
        eye_parts += [eye.get("title", ""), eye.get("meta_description", ""),
                      " ".join(eye.get("h1s", [])),
                      " ".join(eye.get("plan_lines", []))]
    self_desc_parts = [head["title"], head["meta_description"],
                       " ".join(head["h1s"]), fb_bio or "", about_text or "",
                       " ".join(p for p in eye_parts if p)]
    self_desc = " ".join(p for p in self_desc_parts if p)

    log_debug(f"classify_business: self_desc={len(self_desc)} зн., text={len(text)} зн., "
              f"cta={len(ctas)}, paths={len(paths)}, platform={platform!r}, "
              f"fb_category={fb_category!r}")

    inputs_used = []
    if html:
        inputs_used.append("homepage_html")
    if head["title"] or head["meta_description"]:
        inputs_used.append("title_meta")
    if urls or classified:
        inputs_used.append("urls")
    if platform:
        inputs_used.append("platform")
    if fb_category or fb_bio:
        inputs_used.append("fb_about")
    if about_text:
        inputs_used.append("about_page")
    if pricing_text:
        inputs_used.append("pricing_page")
    if observed.get("page_eyes") or observed.get("add_to_cart_fired") \
            or observed.get("checkout_page_seen"):
        inputs_used.append("observed_step2")

    # Дочитанные страницы идут и в buyer-скоринг, не только в индустрию:
    # у Miro в About «teams of any size», на /pricing — Free tier + Enterprise
    extra = " ".join(t.lower() for t in (about_text, pricing_text) if t)
    buyer_text = (text + " " + extra) if extra else text
    buyer = _score_buyer(buyer_text, cta_text, paths, classified, platform)
    # Ссылки на сторы в сыром HTML — b2c-сигнал (группа html)
    for pattern, weight in BUYER_SIGNALS["b2c"].get("html", {}).items():
        m = _rx(pattern).search((html or "")[:RAW_HTML_CAP])
        if m:
            buyer["b2c_hits"].append({"signal": f"html:{m.group(0)[:40]}",
                                      "weight": weight, "base_weight": weight,
                                      "pattern": pattern, "evidence": m.group(0)[:80]})
            buyer["b2c"], buyer["b2c_hits"] = _cap_groups(buyer["b2c_hits"])
            buyer["b2c_strong"] = buyer["b2c_strong"] or weight >= STRONG_WEIGHT
            break

    models = _score_models(text, html or "", paths, classified, platform)
    industry = _score_industry(self_desc, paths, fb_category)

    # ЛЕСЕНКА (v3): категория → адресат → механика (наблюдение выше словаря) → unknown
    verdict = _ladder_verdict(industry["industry"], self_desc, buyer, observed)

    result = {
        "schema_version": 3,
        "buyer_type": verdict["buyer_type"],
        "buyer_probability": verdict["probability"],
        "confidence": verdict["confidence"],
        "decided_by": verdict["decided_by"],
        "buyer_evidence": verdict["evidence"],
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
        "self_description": self_desc[:600],
        "inputs_used": inputs_used,
        "llm": None,
        "classifier_version": CLASSIFIER_VERSION,
        "classified_at": datetime.now().isoformat(timespec="seconds"),
    }

    log_info(f"Лесенка: {verdict['buyer_type']} через [{verdict['decided_by']}] "
             f"(p_b2c={verdict['probability']['b2c']}, conf={verdict['confidence']}) | "
             f"модель: {', '.join(models['models']) or '—'} | "
             f"индустрия: {industry['industry'] or '—'} {industry['tags']}")
    return result


def needs_llm(result: dict) -> list:
    """Триггеры слоя 2: что именно не решили слои 0-1. Пустой список = не нужен."""
    triggers = []
    if result.get("buyer_type") == "unknown" or result.get("confidence") in ("low", "unknown"):
        triggers.append("buyer_weak")
    if not result.get("business_model"):
        triggers.append("model_empty")
    if not result.get("industry"):
        triggers.append("industry_null")
    return triggers


def needs_about_page(result: dict) -> bool:
    """Дочитка About нужна: самоописание оказалось водой (индустрия не решена)
    или тип неясен. Дешевле Haiku — идёт первой."""
    return (not result.get("industry")
            or result.get("buyer_type") == "unknown"
            or result.get("confidence") in ("low", "unknown"))


# ─── Дочитка About/Contacts (вежливая, по правилу WAF Родиона) ────────────────
# Правило: главная открылась → WAF на сайте нет. 429 = мы частим (пауза+повтор),
# 403 = бот-детект на requests (повтор Playwright'ом). Не прошло → честно
# «не дочитали». # Tested: 2026-07-20 refetch_politeness_test.py — 6/6 доменов
# открылись requests'ом с паузой 2.5с (вкл. бывшие allbirds-429, acronis-403).

ABOUT_PATH_HINTS = ("about", "company", "our-story", "our-team", "mission",
                    "who-we-are", "what-we-do")
CONTACT_PATH_HINTS = ("contact",)
PRICING_PATH_HINTS = ("pricing", "/plans", "/plan")
POLITE_PAUSE_SEC = 2.5
RETRY_429_WAIT = 6


# Страницы про деньги КЛИЕНТА (рассрочка, оплата обучения) — не про суть бизнеса:
# у tripleten /about/payment-options слова «кредит/платежи» дали ложную индустрию finance
ABOUT_PATH_EXCLUDE = ("payment", "tuition", "financing", "billing", "refund", "invoice")


def pick_about_urls(classified: list, max_n: int = 2) -> list:
    """Кандидаты дочитки из карты сайта: About-подобные первыми, Contacts следом.
    Страницы про оплату/рассрочку исключаются (ABOUT_PATH_EXCLUDE)."""
    about, contact = [], []
    for rec in (classified or []):
        path = (rec.get("path") or "").lower()
        url = rec.get("url") or ""
        if not url or any(x in path for x in ABOUT_PATH_EXCLUDE):
            continue
        if any(h in path for h in ABOUT_PATH_HINTS):
            about.append(url)
        elif any(h in path for h in CONTACT_PATH_HINTS):
            contact.append(url)
    return (about + contact)[:max_n]


def pick_pricing_url(classified: list) -> str:
    """Страница цен из карты сайта — там обе аудитории сразу (Free + Enterprise)."""
    for rec in (classified or []):
        path = (rec.get("path") or "").lower()
        if any(h in path for h in PRICING_PATH_HINTS):
            return rec.get("url") or ""
    return None


def fetch_about_text(urls: list, session: requests.Session = None,
                     text_cap: int = 1500) -> dict:
    """Вежливо дочитывает 1-2 страницы. Возвращает
    {"text": str, "fetched": [urls], "failed": [urls]} — text пуст если не вышло.
    text_cap: About — 1500 (самоописание сверху); Pricing — крупнее (тарифная
    таблица с Free/Enterprise лежит глубоко, иначе сигнал обрезается)."""
    session = session or requests.Session()
    texts, fetched, failed = [], [], []
    for url in urls:
        time.sleep(POLITE_PAUSE_SEC)
        code, body = _polite_get(session, url)
        if code == 200 and body:
            head = _head_bits(body)
            excerpt = " ".join([head["title"], head["meta_description"],
                                " ".join(head["h1s"]),
                                _visible_text(body)[:text_cap]])
            texts.append(excerpt)
            fetched.append(url)
            log_success(f"дочитка ок: {url}")
        else:
            failed.append(url)
            log_warn(f"дочитка не удалась ({code}): {url}")
    return {"text": " ".join(texts)[:text_cap + 1500], "fetched": fetched,
            "failed": failed}


def _polite_get(session: requests.Session, url: str) -> tuple:
    """GET по политике вежливости: 429 → пауза+повтор; 403/сеть → Playwright."""
    try:
        r = session.get(url, headers=HEADERS, timeout=12)
        if r.status_code == 429:
            log_debug(f"_polite_get: 429 — жду {RETRY_429_WAIT}с и повторяю")
            time.sleep(RETRY_429_WAIT)
            r = session.get(url, headers=HEADERS, timeout=12)
        if r.status_code == 200:
            return 200, r.text
        code = r.status_code
    except Exception as e:
        log_debug(f"_polite_get: requests fail {str(e)[:60]}")
        code = -1
    if code in (403, -1):
        log_debug(f"_polite_get: {code} — повтор через Playwright")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                ctx = b.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
                pg = ctx.new_page()
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=20000)
                body = pg.content()
                status = resp.status if resp else -1
                b.close()
                if status == 200:
                    return 200, body
                return status, None
        except Exception as e:
            log_debug(f"_polite_get: playwright fail {str(e)[:60]}")
    return code, None


# ─── Слой 2: Haiku ────────────────────────────────────────────────────────────

SITE_VERDICT_SYSTEM = (
    "You are a business-type classifier for a digital marketing agency. "
    "Respond with a valid JSON object only, no markdown, no explanation.")


def build_site_verdict_request(html: str, urls: list = None, classified: list = None,
                               domain: str = "", fb_category: str = None,
                               fb_bio: str = None, about_text: str = None) -> dict:
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

    fb_line = ""
    if fb_category or fb_bio:
        fb_line = f"Facebook page category: {fb_category or '—'}; bio: {fb_bio or '—'}\n"
    about_line = f"About page excerpt:\n{about_text[:1200]}\n\n" if about_text else ""

    material = (
        f"Website: {domain}\n"
        f"Title: {head['title']}\n"
        f"Meta description: {head['meta_description']}\n"
        f"H1: {' | '.join(head['h1s'][:4])}\n"
        f"{fb_line}\n"
        f"{about_line}"
        f"Visible homepage text (excerpt):\n{text}\n\n"
        f"Site page paths:\n" + "\n".join(paths) + "\n\n"
        f"Button/CTA texts:\n" + " | ".join(ctas))

    instruction = (
        "Look at all the pages and content of this website and decide:\n"
        '1. "buyer_type" — who does this site sell to: "b2c" (consumers), '
        '"b2b" (businesses) or "mixed" (both directions as REAL separate business '
        "lines, not just an enterprise plan).\n"
        '2. "probability_b2c" — number 0.0-1.0.\n'
        '3. "business_model" — subset of ["app","saas","ecom","services","info"].\n'
        '4. "industry" — one short lowercase industry tag '
        '(e.g. "education", "fashion", "cybersecurity", "fintech"), or null. '
        "Return null unless the industry is explicitly clear from the material — "
        "do NOT guess.\n"
        '5. "industry_tags" — up to 3 lowercase tags.\n'
        '6. "evidence" — for buyer/model/industry give 1-3 short verbatim quotes '
        "copied exactly from the material above.\n\n"
        'Return JSON object: {"buyer_type": "...", "probability_b2c": 0.0, '
        '"business_model": [...], "industry": "..." | null, "industry_tags": [...], '
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
    """Одиночный вызов Haiku. Форма запроса — как classify_batch_api: ретраи
    на 429, срез ```-фенсов, graceful degrade (никогда не бросает).
    ВНИМАНИЕ: платный вызов — батчи только с разрешения Родиона."""
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
    детерминированные остаются. field_sources хранит происхождение.
    Индустрия принимается только с проверенной цитатой-уликой (не гадать)."""
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
        # Индустрию от Haiku берём только если цитата-улика по индустрии
        # реально нашлась в материале — иначе это догадка, оставляем null
        if verdict.get("evidence", {}).get("industry"):
            result["industry"] = verdict["industry"]
            if verdict.get("industry_tags"):
                result["industry_tags"] = verdict["industry_tags"]
            result["field_sources"]["industry"] = "llm"
        else:
            log_debug("merge_llm_verdict: индустрия Haiku без проверенной цитаты — null")
    result["method"] = "deterministic+llm"
    result["llm"] = {
        "model": verdict.get("model_used", CLAUDE_MODEL),
        "triggered_by": triggers,
        "evidence": verdict.get("evidence", {}),
        "evidence_verified": verdict.get("evidence_verified", False),
        # Полный ответ Haiku — для гейта Родиона виден даже когда не мержился
        "verdict": {k: verdict[k] for k in
                    ("buyer_type", "probability_b2c", "business_model",
                     "industry", "industry_tags") if k in verdict},
    }
    return result


# ─── Слой 3: обогащение из библиотеки объявлений (последний резерв) ───────────

ADS_CTA_SIGNALS = {
    "b2c": {"SHOP_NOW", "BUY_NOW", "ORDER_NOW", "GET_OFFER", "BOOK_TRAVEL"},
    "b2b": {"CONTACT_US", "GET_QUOTE", "REQUEST_TIME"},
}


def enrich_from_ads(result: dict, ads_data: dict) -> dict:
    """Только когда сайт не дал достоверного вывода. Оговорка: сайт может
    обслуживать оба направления, а реклама крутиться по одному — улики
    помечены ads_library, сайт-улики не перебиваются."""
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
            hits[side] += _match_lexicon(BUYER_SIGNALS[side]["text"], ad_texts,
                                         "ads", multiplier=0.5)
            matched_cta = cta_types & ADS_CTA_SIGNALS[side]
            if matched_cta:
                hits[side].append({"signal": f"ads:cta_{'/'.join(sorted(matched_cta))}",
                                   "weight": 3 if side == "b2c" else 2,
                                   "base_weight": 3 if side == "b2c" else 2,
                                   "pattern": "ads:cta",
                                   "evidence": f"CTA объявлений: {', '.join(sorted(matched_cta))}"})
        b2c_add, _ = _cap_groups(hits["b2c"])
        b2b_add, _ = _cap_groups(hits["b2b"])
        if b2c_add or b2b_add:
            new_b2c = result["scores"]["b2c"] + b2c_add
            new_b2b = result["scores"]["b2b"] + b2b_add
            verdict = _buyer_verdict(new_b2c, new_b2b, False, False)
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
        mapped = FB_CATEGORY_MAP.get(page_category)
        if mapped:
            result["industry"] = mapped
            if mapped not in result["industry_tags"]:
                result["industry_tags"] = ([mapped] + result["industry_tags"])[:3]
            result["field_sources"]["industry"] = "ads_library"
            result["signals"]["industry"].append(
                {"signal": "ads:page_category", "tag": mapped, "weight": 3,
                 "source": "ads_library", "evidence": f"FB page_category: {page_category}"})
            applied.append("industry")
        else:
            result["signals"]["industry"].append(
                {"signal": "ads:page_category_unmapped", "tag": None, "weight": 0,
                 "source": "ads_library", "evidence": f"FB page_category: {page_category}"})

    if applied:
        result["method"] += "+ads_enriched"
        result.setdefault("caveats", []).append(
            "Поля " + ", ".join(applied) + " дополнены по библиотеке объявлений; "
            "сайт может обслуживать и другое направление.")
        log_info(f"Обогащение из ads: {', '.join(applied)}", emoji="📢")
    return result


def _load_observed(scan_dir, domain: str) -> dict:
    """Наблюдение step2 («глаз» + кликер) из <домен>_step2.json, если скан был.
    add_to_cart_fired = клик реально дал конверсионное событие (не догадка).
    checkout_page_seen = страница checkout реально просканирована.
    page_eyes = самоописания ОТРИСОВАННЫХ страниц (новые сканы; у старых нет)."""
    obs = {}
    step2_path = os.path.join(scan_dir, f"{domain}_step2.json")
    if not os.path.exists(step2_path):
        return obs
    try:
        with open(step2_path, encoding="utf-8") as f:
            step2 = json.load(f)
        pages = step2.get("all_pages") or []
        eyes, cart_fired, checkout_seen = [], False, False
        for p in pages:
            eye = p.get("page_eye")
            if eye and (eye.get("title") or eye.get("plan_lines")):
                eyes.append(eye)
            if (p.get("page_type") or "") == "checkout":
                checkout_seen = True
            conv = " ".join(p.get("conversion_events_found") or [])
            click = p.get("click_result") or {}
            click_conv = " ".join(
                " ".join(b.get("conversion_events") or [])
                for b in (click.get("buttons") or []) if isinstance(b, dict))
            if any(ev in (conv + " " + click_conv)
                   for ev in ("AddToCart", "InitiateCheckout", "Purchase",
                              "add_to_cart", "begin_checkout", "purchase")):
                cart_fired = True
        obs = {"page_eyes": eyes, "add_to_cart_fired": cart_fired,
               "checkout_page_seen": checkout_seen,
               "pages_scanned": len(pages)}
        log_debug(f"_load_observed: {len(pages)} стр., глаз={len(eyes)}, "
                  f"cart_fired={cart_fired}, checkout_seen={checkout_seen}")
    except Exception as e:
        log_debug(f"_load_observed: step2 не прочитан: {e}")
    return obs


def _load_fb_about(scan_dir) -> dict:
    """FB-самоописание из fb.json (v2-поля page_category/page_bio) или fb_deep/."""
    out = {"category": None, "bio": None}
    fb_path = os.path.join(scan_dir, "fb.json")
    if os.path.exists(fb_path):
        try:
            with open(fb_path, encoding="utf-8") as f:
                fb = json.load(f)
            for acc in (fb.get("accounts") or []):
                if acc.get("page_category"):
                    out["category"] = acc["page_category"]
                    out["bio"] = acc.get("page_bio")
                    return out
        except Exception as e:
            log_debug(f"_load_fb_about: fb.json не прочитан: {e}")
    # fb_deep: graphql.page_category (глубокий ads-скан)
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
                    out["category"] = cat
                    return out
            except Exception:
                continue
    return out


def _load_ads_data(scan_dir) -> dict:
    """Читает данные объявлений для слоя 3: тексты, CTA, page_category."""
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
        except Exception as e:
            log_debug(f"_load_ads_data: link_map не прочитан: {e}")
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
        except Exception as e:
            log_debug(f"_load_ads_data: fb.json не прочитан: {e}")
    ads["page_category"] = _load_fb_about(scan_dir)["category"]
    return ads


# ─── Пост-хок CLI ─────────────────────────────────────────────────────────────

def classify_post_hoc(domain: str, llm_mode: str = "never", write: bool = True,
                      refetch_about: bool = True) -> dict:
    """Классификация по существующей папке скана: step1.json + дотяжка главной
    (+ вежливая дочитка About если самоописание — вода). Пишет сайдкар
    scans/<домен>/business_type.json; step1.json НЕ мутируется."""
    log_header(f"Тип бизнеса (пост-хок): {domain}")
    scan_dir = SCANS_DIR / domain
    step1_path = scan_dir / f"{domain}_step1.json"
    if not step1_path.exists():
        log_error(f"Нет step1: {step1_path}")
        return {"status": "error", "error": "no_step1"}
    with open(step1_path, encoding="utf-8") as f:
        step1 = json.load(f)

    # Слим-выжимка: step1 бывает гигантским (flowwow — 363 МБ), держим только нужное
    classified = [{"url": r.get("url"), "path": r.get("path"),
                   "type": r.get("type"), "priority": r.get("priority")}
                  for r in (step1.get("classified") or [])]
    platform = (step1.get("platform") or {}).get("platform", "")
    to_scan_n = len(step1.get("to_scan") or [])
    total_found = step1.get("total_found") or 0
    scan_language = step1.get("scan_language") or (step1.get("site_language") or {}).get("lang")
    fetch_url = step1.get("scan_url_base") or step1.get("base_url") or f"https://{domain}"
    alt_url = step1.get("base_url")
    del step1

    # Пустой артефакт скана (упавший step1) — не домен
    if total_found == 0 and not classified and not to_scan_n:
        log_warn(f"{domain}: пустой step1 (упавший скан) — пропуск")
        return {"status": "skipped", "error": "empty_step1"}

    # Только английские сайты (решение Родиона 2026-07-20)
    if scan_language and scan_language not in ("en", "unknown"):
        log_warn(f"{domain}: язык скана {scan_language!r} — не английский, пропуск")
        return {"status": "skipped", "error": f"non_english_{scan_language}"}

    urls = [rec.get("url") for rec in classified if rec.get("url")]

    # Дотяжка главной: сырой HTML в сканах не хранится
    log_step(f"Дотягиваю главную: {fetch_url}", emoji="🌐")
    homepage_html, final_url = "", None
    session = requests.Session()
    r = safe_get(fetch_url)
    if r is None or r.status_code >= 400:
        if alt_url and alt_url != fetch_url:
            r = safe_get(alt_url)
    if r is not None and r.status_code < 400:
        # requests по RFC 2616 дефолтит text/* без charset в ISO-8859-1 → мохибейк.
        # Передоверяем apparent_encoding только когда charset не заявлен явно.
        if r.encoding in (None, "ISO-8859-1") and \
                "charset" not in (r.headers.get("Content-Type", "") or "").lower():
            r.encoding = r.apparent_encoding
        homepage_html, final_url = r.text, r.url
        log_success(f"Главная получена: HTTP {r.status_code}, {len(homepage_html)} байт")
    else:
        code = getattr(r, "status_code", "нет ответа")
        log_warn(f"Главная не получена ({code}) — деградация до URL/классификации")

    # FB-самоописание (категория + bio) + наблюдение step2 (глаз + кликер)
    fb_about = _load_fb_about(str(scan_dir))
    observed = _load_observed(str(scan_dir), domain)

    result = classify_business(homepage_html, urls=urls, classified=classified,
                               platform=platform, domain=domain,
                               fb_category=fb_about["category"],
                               fb_bio=fb_about["bio"], observed=observed)

    # Дочитка (вежливая, дешевле Haiku):
    #   About — если самоописание оказалось водой (нужно для индустрии/типа).
    #   Pricing — если страница цен есть и вердикт ещё не mixed: там видна вторая
    #   аудитория (Free tier=B2C + Enterprise=B2B) → ключ к mixed для SaaS (Miro).
    about_text, pricing_text = None, None
    if refetch_about:
        about_urls = pick_about_urls(classified) if needs_about_page(result) else []
        pricing_url = (pick_pricing_url(classified)
                       if result.get("buyer_type") != "mixed" else None)
        to_fetch = []
        if about_urls:
            to_fetch += [("about", u) for u in about_urls]
        if pricing_url and pricing_url not in [u for _, u in to_fetch]:
            to_fetch.append(("pricing", pricing_url))
        if to_fetch:
            log_step(f"Дочитка: {[u for _, u in to_fetch]}", emoji="📄")
            about_parts, pricing_parts, fetched_ok, failed = [], [], [], []
            for kind, url in to_fetch:
                # pricing: крупный cap (тарифная таблица глубоко); about: обычный
                got = fetch_about_text([url], session,
                                       text_cap=12000 if kind == "pricing" else 1500)
                if got["text"]:
                    (about_parts if kind == "about" else pricing_parts).append(got["text"])
                    fetched_ok += got["fetched"]
                failed += got["failed"]
            about_text = " ".join(about_parts)[:3000] or None
            pricing_text = " ".join(pricing_parts)[:14000] or None
            if about_text or pricing_text:
                result = classify_business(homepage_html, urls=urls,
                                           classified=classified, platform=platform,
                                           domain=domain,
                                           fb_category=fb_about["category"],
                                           fb_bio=fb_about["bio"],
                                           about_text=about_text,
                                           pricing_text=pricing_text,
                                           observed=observed)
                result["pages_fetched"] = fetched_ok
            if failed:
                result.setdefault("pages_failed", failed)

    # Слой 2 — Haiku (дефолт never: батчи только с разрешения Родиона)
    triggers = needs_llm(result)
    if llm_mode == "always" and not triggers:
        triggers = ["forced"]
    if triggers and llm_mode != "never":
        log_step(f"Слой Haiku: триггеры {triggers}", emoji="🤖")
        request = build_site_verdict_request(homepage_html, urls=urls,
                                             classified=classified, domain=domain,
                                             fb_category=fb_about["category"],
                                             fb_bio=fb_about["bio"],
                                             about_text=about_text)
        verdict = classify_via_haiku(request)
        result = merge_llm_verdict(result, verdict, triggers)
    elif triggers:
        log_info(f"Слоёв 0-1 не хватило ({triggers}); Haiku выключен (--llm {llm_mode})")

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
             f"conf={result.get('confidence')}) | решено через: "
             f"{result.get('decided_by', '—')}")
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
    ind_hits = [h for h in result.get("signals", {}).get("industry", []) if h.get("tag")]
    if ind_hits:
        log_info("Улики индустрии: " + "; ".join(
            f"{h['tag']}←{h.get('source', '?')}:{h['evidence'][:40]}" for h in ind_hits[:4]))
    for caveat in result.get("caveats", []):
        log_warn(caveat)


def run_all(llm_mode: str = "never", write: bool = True,
            refetch_about: bool = True) -> list:
    """Прогон по всем папкам scans/ где есть <папка>_step1.json.
    Служебные (_*) и неанглийские пропускаются. Возвращает сводные строки."""
    rows = []
    folders = sorted(p for p in SCANS_DIR.iterdir()
                     if p.is_dir() and not p.name.startswith("_"))
    targets = [p.name for p in folders if (p / f"{p.name}_step1.json").exists()]
    log_header(f"Прогон по всем сканам: {len(targets)} доменов")
    for domain in targets:
        try:
            result = classify_post_hoc(domain, llm_mode=llm_mode, write=write,
                                       refetch_about=refetch_about)
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


def _save_run(rows: list) -> None:
    """Сводка прогона — в отдельную папку scans/_business_type_<дата>/."""
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


# ─── Самотест ─────────────────────────────────────────────────────────────────

_FIXTURE_B2C = """<html lang="en"><head><title>Shoe Shop — Sneakers & Apparel</title>
<meta name="description" content="Comfortable sneakers and apparel with free shipping."></head><body>
<h1>New arrivals — sneakers and footwear</h1><p>Free shipping on orders over $50.
Sale up to 30% off apparel!</p>
<button>Add to cart</button><a href="/checkout">Checkout</a>
<p>$29.99 $39.99 $19.99 $59.99 $24.99 sizes size guide gift cards</p></body></html>"""

_FIXTURE_B2B = """<html lang="en"><head><title>DataPipe — Cybersecurity for enterprises</title>
<meta name="description" content="Threat detection and data protection platform for security teams."></head>
<body><h1>Built for teams</h1><p>Trusted by 500 companies. Read our case studies
and white paper. SLA 99.9%. Pricing per seat.</p><button>Request a demo</button>
<a href="/enterprise">Enterprise</a><button>Contact sales</button></body></html>"""

_FIXTURE_MIXED = """<html lang="en"><head><title>PhotoLab — prints & studio platform</title></head><body>
<p>Shop prints — free shipping, add to cart, $19 $29 $39 $49 $59, gift cards, sale now!</p>
<button>Add to cart</button><a href="/checkout">Checkout</a>
<p>For business: wholesale and distributors welcome. Request a demo of our studio API.
Enterprise plans per seat, case studies, white paper, SLA.</p>
<button>Talk to sales</button></body></html>"""

_FIXTURE_EMPTY = "<html><body><p>Hello.</p></body></html>"

# v1-регрессии: тело со словом suite/garage НЕ должно давать индустрию
_FIXTURE_SUITE_BODY = """<html lang="en"><head><title>Acme — Cybersecurity Platform</title>
<meta name="description" content="Threat detection for security teams."></head><body>
<p>Our product suite includes backup tours of the garage where components live.
Request a demo. Contact sales. Per seat pricing. White paper. SLA.</p>
<button>Request a demo</button></body></html>"""

# Лесенка: корп-сайт игровой студии — покупателя на сайте нет, решает КАТЕГОРИЯ
_FIXTURE_GAME_STUDIO = """<html lang="en"><head><title>Belka — mobile game developer</title>
<meta name="description" content="We create match-3 and puzzle games loved by millions."></head>
<body><h1>Our games</h1><p>Careers. Press. About us.</p></body></html>"""

# Лесенка: спорная категория (software) → решает АДРЕСАТ («for teams»)
_FIXTURE_AUDIENCE_B2B = """<html lang="en"><head><title>Taskly — work management platform</title>
<meta name="description" content="Project workspace for teams of any size."></head>
<body><h1>Plan work together</h1><p>Automation and workflows.</p></body></html>"""

# free trial НЕ должен давать B2C-сторону (нейтральный глагол) → чистый b2b
_FIXTURE_TRIAL_SAAS = """<html lang="en"><head><title>PipeGuard — Cybersecurity Platform</title>
<meta name="description" content="Threat detection for security teams."></head>
<body><button>Start your free trial</button><button>Contact sales</button>
<p>Per seat pricing. SLA.</p></body></html>"""


def _self_test() -> None:
    log_header("Самотест business_type_classifier v2")
    cases = [("b2c-магазин", _FIXTURE_B2C, "b2c", "fashion"),
             ("b2b-кибербез", _FIXTURE_B2B, "b2b", "cybersecurity"),
             ("mixed", _FIXTURE_MIXED, "mixed", None),
             ("пустой", _FIXTURE_EMPTY, "unknown", None),
             ("корп-сайт-студии", _FIXTURE_GAME_STUDIO, "b2c", "gaming"),
             ("адресат-for-teams", _FIXTURE_AUDIENCE_B2B, "b2b", None),
             ("free-trial-не-b2c", _FIXTURE_TRIAL_SAAS, "b2b", "cybersecurity")]
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
    # Одинокая кнопка не должна давать high
    lone = classify_business(
        "<h1>Acme Consulting</h1><p>We build things.</p>"
        "<button>Request a demo</button>", domain="одинокая-кнопка")
    if lone["confidence"] == "high":
        log_error(f"одинокая-кнопка: conf=high от одной фразы (скоры {lone['scores']})")
        failed += 1
    else:
        log_success(f"одинокая-кнопка: conf={lone['confidence']} (не high) — ок")
    # v1-регрессия: suite/garage/components в ТЕЛЕ не дают индустрию,
    # индустрия берётся из title (cybersecurity)
    reg = classify_business(_FIXTURE_SUITE_BODY, domain="suite-в-теле")
    if reg["industry"] == "cybersecurity" and "travel" not in (reg["industry_tags"] or []):
        log_success(f"suite-в-теле: индустрия из title = {reg['industry']} — ок")
    else:
        log_error(f"suite-в-теле: индустрия {reg['industry']}, теги {reg['industry_tags']}")
        failed += 1
    # FB-категория решает индустрию при пустом самоописании
    fbres = classify_business("<html><head><title>Client-A</title></head><body></body></html>",
                              domain="fb-категория", fb_category="Education website")
    if fbres["industry"] == "education":
        log_success("fb-категория: Education website → education — ок")
    else:
        log_error(f"fb-категория: получил {fbres['industry']}")
        failed += 1
    # Наблюдение кликера решает механику: AddToCart стрельнул → b2c-улика
    obs = classify_business(_FIXTURE_EMPTY, domain="наблюдение-кликера",
                            observed={"add_to_cart_fired": True})
    if obs["buyer_type"] == "b2c" and "observed" in obs.get("decided_by", ""):
        log_success("наблюдение-кликера: AddToCart стрельнул → b2c через observed — ок")
    else:
        log_error(f"наблюдение-кликера: {obs['buyer_type']} через {obs.get('decided_by')}")
        failed += 1
    total = len(cases) + 4
    if failed:
        log_error(f"Самотест: {failed} из {total} провалено")
        sys.exit(1)
    log_success(f"Самотест: {total}/{total} ок")


if __name__ == "__main__":
    from utils import setup_console
    setup_console()

    args = [a for a in sys.argv[1:]]
    llm_mode = "never"  # дефолт: ноль платных вызовов (гейт Haiku-трат Родиона)
    if "--llm" in args:
        i = args.index("--llm")
        llm_mode = args[i + 1] if i + 1 < len(args) else "never"
        del args[i:i + 2]
    if llm_mode not in ("auto", "always", "never"):
        log_error(f"--llm {llm_mode!r}: допустимо auto|always|never")
        sys.exit(1)
    write = "--no-write" not in args
    args = [a for a in args if a != "--no-write"]
    refetch_about = "--no-refetch-about" not in args
    args = [a for a in args if a != "--no-refetch-about"]

    if "--all" in args:
        args.remove("--all")
        if args:
            log_error(f"--all несовместим с позиционными аргументами: {args} — "
                      f"либо один домен, либо --all")
            sys.exit(1)
        run_all(llm_mode=llm_mode, write=write, refetch_about=refetch_about)
    elif args:
        if len(args) > 1:
            log_warn(f"Лишние аргументы проигнорированы: {args[1:]}")
        classify_post_hoc(args[0].replace("https://", "").replace("http://", "").strip("/"),
                          llm_mode=llm_mode, write=write, refetch_about=refetch_about)
    else:
        _self_test()
