"""
TNC Legal Name Extractor
========================
Достаёт юридическое имя компании с её сайта (с организационным суффиксом
типа GmbH & Co. KG, SAS, B.V., LLC и т.п.) — для точного matching
с advertiser_disclosed_name в Google Ads Transparency Center.

Waterfall:
    1. Homepage → footer/copyright → ищем org-suffix pattern
    2. Mentions légales / Impressum / Legal notice → ищем там
    3. Contact / About → ищем там
    4. Terms / Privacy → ищем там
    5. Fallback: brand keyword из домена (low confidence)

Запуск:
    python legal_name_extractor.py miessler-automotive.com
    python legal_name_extractor.py --file domains_fr_competitors.txt
"""

import sys
import re
import json
import html as _html
import argparse
import requests
from urllib.parse import urlparse, urljoin
from pathlib import Path

from utils import HEADERS, normalize_url, setup_console
setup_console()


# Известные ложные срабатывания: GTM, Analytics, web frameworks, hosters, CMS.
NAME_BLACKLIST = {
    # Tech giants / analytics
    "Google", "Google Inc", "Google Ireland Limited", "Google LLC",
    "Microsoft", "Microsoft Corp", "Microsoft Corporation",
    "Apple", "Apple Inc",
    "Filament Group", "Filament Group Inc",
    "Adobe", "Adobe Inc", "Adobe Systems",
    "Amazon", "Amazon.com Inc", "Amazon Inc", "Amazon Web Services",
    "Facebook", "Meta", "Meta Platforms Inc",
    "Twitter", "X Corp",
    "Yahoo", "Yahoo Inc",
    "MongoDB", "MongoDB Inc",
    # CDN / DNS / payment
    "Cloudflare", "Cloudflare Inc",
    "Stripe", "Stripe Inc",
    "PayPal", "PayPal Inc",
    "Klaviyo", "Klaviyo Inc",
    "Intercom", "Intercom Inc",
    "Hotjar", "Hotjar Ltd",
    "Sentry", "Functional Software Inc",
    # CMS / e-com platforms
    "Shopify", "Shopify Inc",
    "Wix", "Wix.com Ltd",
    "Squarespace", "Squarespace Inc",
    "WordPress", "Automattic Inc", "Automattic",
    "Webflow", "Webflow Inc",
    "PrestaShop", "PrestaShop SA",
    "Magento", "Adobe Commerce",
    # Hosters
    "OVH", "OVH SAS", "OVH Cloud", "OVHcloud",
    "Gandi", "Gandi SAS",
    "Hostinger", "Hostinger International",
    "SiteGround", "SiteGround Capital",
    "GoDaddy", "GoDaddy Operating Company",
    "Bluehost",
    "DreamHost",
    "Hetzner", "Hetzner Online GmbH",
    "Strato", "Strato AG",
    "IONOS", "IONOS SE", "1&1", "1&1 Internet SE",
    "OVHcloud SAS",
    "Online SAS", "Scaleway", "Scaleway SAS",
    "Akamai", "Akamai Technologies Inc",
    "Fastly", "Fastly Inc",
    # JS frameworks (just in case)
    "Vue", "Angular", "React", "Next.js",
}

# Маркеры что mention идёт в hosting/legal disclosure context — игнорировать candidate
HOSTING_CONTEXT_MARKERS = [
    'hébergeur', 'hebergeur', 'hosting provider', 'hosted by',
    'host:', 'hébergement', 'hebergement',
    'gehostet bei', 'gehostet von', 'hosting:',
    'host name:',
]


def _clean_html_for_extraction(html: str) -> str:
    """
    Готовит HTML к regex-extraction:
      1. Удаляет <script>/<style>/<noscript>/<svg> блоки (мусор и ложняки)
      2. Decode HTML entities (&amp; → &, &nbsp; → space, &copy; → ©)
    """
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style\b[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<noscript\b[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<svg\b[^>]*>.*?</svg>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = _html.unescape(html)
    return html


# ─── Организационные суффиксы (длинные раньше коротких!) ─────────────────────

ORG_FORMS = [
    # Compound (must come first)
    r'GmbH\s*&\s*Co\.\s*KG',
    r'GmbH\s*&\s*Co\.\s*OHG',
    r'AG\s*&\s*Co\.\s*KG',

    # Long FR
    r'S\.A\.S\.U\.', r'SASU',
    r'S\.A\.R\.L\.', r'S\.\s*A\.\s*R\.\s*L\.',

    # Long ES/PT
    r'S\.L\.U\.', r'S\.A\.U\.',

    # Compound IT
    r'S\.r\.l\.', r'S\.p\.A\.', r'S\.a\.s\.', r'S\.n\.c\.',

    # NL/BE
    r'B\.V\.', r'N\.V\.', r'V\.O\.F\.', r'C\.V\.', r'BVBA',

    # FR (full)
    r'SAS', r'SARL', r'EURL', r'SCI', r'SCS', r'SNC', r'SCOP', r'SCA',

    # DE/AT/CH (full)
    r'GmbH', r'AG', r'KG', r'OHG', r'eG', r'UG\s*\(haftungsbeschränkt\)?', r'UG',

    # ES short
    r'S\.L\.', r'S\.A\.',

    # PT
    r'Lda\.?', r'LDA\.?',

    # US/UK
    r'L\.L\.C\.', r'LLC',
    r'L\.L\.P\.', r'LLP',
    r'P\.L\.C\.', r'plc', r'PLC',
    r'Inc\.', r'Inc',
    r'Corp\.', r'Corp',
    r'Ltd\.', r'Ltd',
    r'Limited',
    r'Co\.\s*Ltd\.',

    # EU general
    r'SE', r'SCE',
]

ORG_FORMS_RE = '(?:' + '|'.join(ORG_FORMS) + ')'

# Главный pattern: Title-cased компания + space + org-suffix
# - начинается с большой буквы (или цифры — "5 Points", "17 Points")
# - 2-100 символов до суффикса
# - lookahead: после суффикса должен быть word-boundary символ
# Name = up to 8 words, each starts with uppercase/digit, single space.
_WORD = r"[A-ZÀ-Ý0-9][A-Za-zÀ-ÿ0-9\-\.\&]*"
_NAME = r"(?:" + _WORD + r")(?:\s" + _WORD + r"){0,7}"

_PRE = r'(?:^|[\s>(\[])'
_POST = r'(?=[\s.,)\n<;|!?]|$)'

LEGAL_NAME_RE = re.compile(
    _PRE + r'(' + _NAME + r')\s+(' + ORG_FORMS_RE + r')' + _POST,
    re.UNICODE,
)


# ─── Org-suffix scoring (для confidence) ─────────────────────────────────────

# Длинные/специфические — высокий boost. Короткие (SE, AG, KG) — амбигуальные.
SUFFIX_STRENGTH = {
    'GmbH & Co. KG': 'strong', 'GmbH&Co.KG': 'strong',
    'GmbH': 'strong', 'SAS': 'strong', 'SARL': 'strong',
    'B.V.': 'strong', 'BV': 'strong',
    'S.r.l.': 'strong', 'S.p.A.': 'strong',
    'S.L.': 'strong', 'S.A.': 'strong',
    'LLC': 'strong', 'L.L.C.': 'strong',
    'Inc.': 'strong', 'Inc': 'strong',
    'Ltd': 'strong', 'Ltd.': 'strong', 'Limited': 'strong',
    'Lda': 'strong', 'LDA': 'strong',
    'EURL': 'strong', 'SASU': 'strong',
    'BVBA': 'strong',

    # Common short, but still distinct
    'AG': 'medium', 'KG': 'medium', 'OHG': 'medium',
    'UG': 'medium', 'plc': 'medium', 'PLC': 'medium',
    'LLP': 'medium', 'eG': 'medium',
    'Corp': 'medium', 'Corp.': 'medium',
    'N.V.': 'medium', 'NV': 'medium',
    'SCI': 'medium', 'SCS': 'medium', 'SNC': 'medium',
    'S.L.U.': 'strong', 'S.A.U.': 'strong',

    # Ambiguous / could match noise (English words, abbreviations)
    'SE': 'weak', 'SCE': 'weak',
}


# ─── Legal page link keywords (FR/DE/EN) ─────────────────────────────────────

LEGAL_PAGE_KEYWORDS = [
    # FR
    'mentions-legales', 'mentionslegales', 'mentions_legales', 'mentions-légales',
    'cgv', 'cgu', 'conditions-generales',
    # DE/AT/CH
    'impressum',
    # EN
    'legal-notice', 'legal_notice', 'legalnotice', 'legal-information',
    'imprint', 'legal',
    # NL
    'juridische-kennisgeving', 'algemene-voorwaarden',
    # IT
    'note-legali', 'informazioni-legali',
    # ES
    'aviso-legal', 'avisolegal',
    # PT
    'aviso-legal',
]

CONTACT_PAGE_KEYWORDS = [
    'contact', 'contacts', 'contact-us', 'contactus', 'kontakt', 'contatti',
    'about-us', 'about', 'a-propos', 'qui-sommes-nous', 'uber-uns', 'chi-siamo',
]

PRIVACY_PAGE_KEYWORDS = [
    'privacy', 'privacy-policy', 'datenschutz', 'confidentialite',
    'rgpd', 'gdpr', 'privacy-notice',
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _fetch_html(url: str, timeout: int = 10, allow_playwright_fallback: bool = True) -> tuple[str, int]:
    """
    Returns (html, status_code).
    status_code == 0 = transport error.
    Если requests возвращает 403/4xx/5xx — пробует Playwright (chromium headless).
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return (r.text, r.status_code)
        # WAF / soft block — пробуем Playwright
        if allow_playwright_fallback and r.status_code in (401, 403, 405, 406, 429, 500, 502, 503):
            pw_html = _fetch_via_playwright(url, timeout)
            if pw_html:
                return (pw_html, 200)
        return (r.text, r.status_code)
    except Exception:
        if allow_playwright_fallback:
            pw_html = _fetch_via_playwright(url, timeout)
            if pw_html:
                return (pw_html, 200)
        return ("", 0)


def _fetch_via_playwright(url: str, timeout: int = 10) -> str:
    """Playwright fallback for WAF-blocked sites. Returns rendered HTML."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            html = page.content()
            browser.close()
            return html
    except Exception:
        return ""


def _find_links(html: str, base_url: str, keywords: list) -> list[str]:
    """Достаёт ссылки на страницы по ключевым словам в href или text."""
    found = set()
    # Pattern для href с этими keywords
    for kw in keywords:
        # href="...kw..." or href="...kw..." (case-insensitive)
        pattern = re.compile(
            r'href\s*=\s*["\']([^"\']*' + re.escape(kw) + r'[^"\']*)["\']',
            re.IGNORECASE
        )
        for m in pattern.findall(html):
            full = urljoin(base_url, m)
            # Очистим anchor и query
            full = full.split('#')[0]
            found.add(full)
    return list(found)


def _score_candidate(name: str, suffix: str, context: str) -> tuple[str, list[str]]:
    """
    Возвращает (confidence: 'high'/'medium'/'low', reasons).
    Confidence строится из:
      - сила suffix
      - наличие © / Copyright / "Legal name" / "Société" в контексте
      - длина name (слишком короткое = подозрительно)
    """
    reasons = []
    suffix_clean = re.sub(r'\s+', ' ', suffix).strip()
    strength = SUFFIX_STRENGTH.get(suffix_clean, 'medium')
    reasons.append(f"suffix={suffix_clean}({strength})")

    score = {'strong': 2, 'medium': 1, 'weak': 0}[strength]

    ctx_lower = context.lower()
    if '©' in context or 'copyright' in ctx_lower:
        score += 2
        reasons.append("©/copyright nearby")
    if any(w in ctx_lower for w in ['legal name', 'raison sociale', 'firma', 'denominazione', 'razón social', 'company name']):
        score += 3
        reasons.append("legal-name marker")
    if any(w in ctx_lower for w in ['siret', 'siren', 'rcs', 'handelsregister', 'kvk', 'vat']):
        score += 2
        reasons.append("registry marker")

    if len(name) < 4:
        score -= 2
        reasons.append("short name")
    if len(name) > 80:
        score -= 1
        reasons.append("very long name")

    if score >= 4:
        return ("high", reasons)
    if score >= 2:
        return ("medium", reasons)
    return ("low", reasons)


def _extract_candidates(html: str, source_label: str) -> list[dict]:
    """Findall org-form pattern в HTML, оцениваем каждый match.
    Pre-clean: strip <script>/<style>/<svg>, decode HTML entities."""
    cleaned = _clean_html_for_extraction(html)
    candidates = []
    for m in LEGAL_NAME_RE.finditer(cleaned):
        name = re.sub(r'\s+', ' ', m.group(1)).strip(' .,-&')
        suffix = m.group(2)
        # Skip blacklist — match if name == blacklist OR name starts with "<blacklist> "
        nlow = name.lower()
        if any(nlow == bl.lower() or nlow.startswith(bl.lower() + ' ') for bl in NAME_BLACKLIST):
            continue
        # Skip всё что начинается с lowercase после первого слова — обычно мусор
        if not name or not name[0].isupper() and not name[0].isdigit():
            continue
        # Контекст: 200 chars вокруг
        start = max(0, m.start() - 200)
        end = min(len(cleaned), m.end() + 200)
        context = cleaned[start:end]
        # Skip если перед mention есть hosting marker (это disclosure про хостера, не про клиента)
        ctx_pre = cleaned[max(0, m.start() - 150):m.start()].lower()
        if any(marker in ctx_pre for marker in HOSTING_CONTEXT_MARKERS):
            continue

        confidence, reasons = _score_candidate(name, suffix, context)

        candidates.append({
            "legal_name": f"{name} {re.sub(r'\\s+', ' ', suffix)}".strip(),
            "name": name,
            "suffix": suffix,
            "source": source_label,
            "confidence": confidence,
            "reasons": reasons,
        })

    # De-dup по legal_name
    seen = set()
    unique = []
    for c in candidates:
        if c["legal_name"] not in seen:
            seen.add(c["legal_name"])
            unique.append(c)

    return unique


def _brand_keyword_from_domain(domain: str) -> str:
    """
    miessler-automotive.com → 'Miessler Automotive'
    vb-airsuspension.com   → 'Vb Airsuspension'
    fr.maxpeedingrods.com  → 'Maxpeedingrods'  (отрезаем lang prefix)
    """
    d = domain.lower().strip()
    if "://" in d:
        d = urlparse(d).netloc
    # Strip TLD
    parts = d.split('.')
    if len(parts) >= 2:
        # Если первая часть — lang prefix (fr, en, de), отрезаем
        if parts[0] in ('fr', 'en', 'de', 'es', 'it', 'nl', 'pt', 'pl', 'www'):
            base = parts[1]
        else:
            base = parts[0]
    else:
        base = d
    # Replace separators with space, capitalize words
    base = base.replace('-', ' ').replace('_', ' ')
    return ' '.join(w.capitalize() for w in base.split())


# ─── Main ────────────────────────────────────────────────────────────────────

def extract_legal_name(domain: str, verbose: bool = False) -> dict:
    """
    Главная entry-point функция.
    Returns {
        domain, legal_name, source, confidence,
        all_candidates: [...]
    }
    """
    base_url = normalize_url(domain)
    domain_clean = urlparse(base_url).netloc

    if verbose:
        print(f"  [extract] {domain_clean}  (base={base_url})")

    all_candidates = []

    # 1. Homepage
    html, status = _fetch_html(base_url)
    if verbose:
        print(f"    homepage: status={status}, html_len={len(html)}")

    if not html:
        return _result(domain_clean, None, "fetch_failed", "low", all_candidates)

    home_candidates = _extract_candidates(html, "homepage")
    all_candidates.extend(home_candidates)

    if verbose:
        print(f"    homepage candidates: {len(home_candidates)}")

    # Если уже есть high — можно остановиться, но соберём ещё с legal-страницы для надёжности
    have_high = any(c["confidence"] == "high" for c in all_candidates)

    # 2. Legal pages (mentions-legales / impressum)
    legal_links = _find_links(html, base_url, LEGAL_PAGE_KEYWORDS)
    if verbose:
        print(f"    legal links found: {len(legal_links)}")
    for link in legal_links[:3]:  # max 3 legal pages
        lhtml, lstatus = _fetch_html(link)
        if lhtml:
            cands = _extract_candidates(lhtml, f"legal_page:{link}")
            # Boost confidence — найдено на legal странице — это сильный сигнал
            for c in cands:
                if c["confidence"] == "low":
                    c["confidence"] = "medium"
                    c["reasons"].append("on legal-page → boost")
                elif c["confidence"] == "medium":
                    c["confidence"] = "high"
                    c["reasons"].append("on legal-page → boost")
            all_candidates.extend(cands)
            if verbose:
                print(f"    legal_page {link}: candidates={len(cands)}")

    # 3. Contact pages — если ничего hi-confidence ещё нет
    have_high = any(c["confidence"] == "high" for c in all_candidates)
    if not have_high:
        contact_links = _find_links(html, base_url, CONTACT_PAGE_KEYWORDS)
        if verbose:
            print(f"    contact links found: {len(contact_links)}")
        for link in contact_links[:2]:
            chtml, _ = _fetch_html(link)
            if chtml:
                cands = _extract_candidates(chtml, f"contact_page:{link}")
                all_candidates.extend(cands)

    # 4. Privacy pages — если всё ещё low
    have_medium_or_high = any(c["confidence"] in ("medium", "high") for c in all_candidates)
    if not have_medium_or_high:
        priv_links = _find_links(html, base_url, PRIVACY_PAGE_KEYWORDS)
        for link in priv_links[:2]:
            phtml, _ = _fetch_html(link)
            if phtml:
                cands = _extract_candidates(phtml, f"privacy_page:{link}")
                all_candidates.extend(cands)

    # De-dup по legal_name (оставляем самый сильный вариант)
    by_name = {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for c in all_candidates:
        existing = by_name.get(c["legal_name"])
        if existing is None or rank[c["confidence"]] > rank[existing["confidence"]]:
            by_name[c["legal_name"]] = c

    # Pre-rank: boost укороченные формы если ЕСТЬ длинная версия с тем же base.
    # Например, "Miessler Automotive GmbH" должен уступить "Miessler Automotive GmbH & Co. KG":
    # любая укороченная форма, которая является prefix'ом более длинной формы,
    # получает downgrade на 1 уровень confidence (high→medium, medium→low).
    names_list = list(by_name.keys())
    for short_name in names_list:
        for other in names_list:
            if other != short_name and other.startswith(short_name + ' '):
                # short_name — это начало другого, более длинного имени
                short = by_name[short_name]
                if short["confidence"] == "high":
                    short["confidence"] = "medium"
                    short["reasons"].append(f"prefix-of:{other!r} → downgrade")
                elif short["confidence"] == "medium":
                    short["confidence"] = "low"
                    short["reasons"].append(f"prefix-of:{other!r} → downgrade")
                break

    # Sort: confidence DESC, then suffix length DESC (longer suffix wins on ties),
    # then total name length DESC.
    deduped = sorted(
        by_name.values(),
        key=lambda c: (
            -rank[c["confidence"]],
            -len(c["suffix"]),
            -len(c["legal_name"]),
        ),
    )

    # 5. Pick best
    if deduped:
        best = deduped[0]
        return _result(
            domain_clean,
            legal_name=best["legal_name"],
            source=best["source"],
            confidence=best["confidence"],
            all_candidates=deduped,
        )

    # 6. Fallback: brand keyword из домена
    fallback = _brand_keyword_from_domain(domain_clean)
    return _result(
        domain_clean,
        legal_name=fallback,
        source="brand_keyword_fallback",
        confidence="low",
        all_candidates=[],
    )


def _result(domain, legal_name, source, confidence, all_candidates):
    return {
        "domain": domain,
        "legal_name": legal_name,
        "source": source,
        "confidence": confidence,
        "all_candidates": all_candidates,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _print_result(r: dict):
    name = r["legal_name"] or "—"
    conf = r["confidence"]
    src = r["source"]
    icon = {"high": "✅", "medium": "🟡", "low": "🟠"}.get(conf, "❓")
    print(f"{icon} {r['domain']:30s}  {name}  [{conf}]  ({src})")
    if r["all_candidates"] and len(r["all_candidates"]) > 1:
        for c in r["all_candidates"][1:]:
            print(f"     also: {c['legal_name']} [{c['confidence']}] ({c['source']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="Domain or URL")
    ap.add_argument("--file", help="File with one domain per line")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--json", action="store_true", help="Print full JSON")
    args = ap.parse_args()

    if args.file:
        domains = []
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                domains.append(line)
    elif args.target:
        domains = [args.target]
    else:
        ap.error("Provide a domain or --file")

    results = []
    for d in domains:
        r = extract_legal_name(d, verbose=args.verbose)
        results.append(r)
        if not args.json:
            _print_result(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
