"""
TNC Google Ads Transparency Center — Creative Parser
====================================================
Открывает creative URL и достаёт:
  - Main DOM: First shown, Last shown, Format, Topic, Targeting categories
  - iframes /adframe: ad text variants (headlines, descriptions), displayed URL,
    business address, has_image flag

Запуск:
    python google_ads_creative.py AR02518758092993200129 CR15804659568224501761
    python google_ads_creative.py AR... CR... --region FR --headed
"""

import sys
import re
import json
import html as _html
import argparse
from urllib.parse import urlparse
from pathlib import Path

from utils import HEADERS, scan_path, setup_console
setup_console()


TC_BASE = "https://adstransparency.google.com"
DEFAULT_REGION = "FR"


# ─── Page DOM helpers ────────────────────────────────────────────────────────

# Ловим строки типа "First shown: Nov 20, 2024" или "First shown:</strong> Nov 20, 2024"
_DATE_RE = re.compile(
    r'<strong>(First shown|Last shown):</strong>\s*([A-Za-zÀ-ÿ]+\s+\d+,?\s+\d{4})',
    re.IGNORECASE,
)
_FORMAT_RE = re.compile(
    r'<strong>Format:</strong>\s*([A-Za-zÀ-ÿ\s]+?)</div>',
    re.IGNORECASE,
)
_TOPIC_RE = re.compile(
    r'<strong>Topic[^<]*</strong>\s*([^<]+?)</div>',
    re.IGNORECASE,
)

# "0 – 1K" / "4K – 5K" / "1M – 5M" — region-impression-count
_IMPRESSION_RANGE_RE = re.compile(
    r'<div[^>]*class="[^"]*region-impression-count[^"]*"[^>]*>\s*([^<]+?)\s*</div>',
    re.IGNORECASE,
)

# "...from Nov 20, 2024 to Feb 2, 2026" — Times shown date range (для bounds)
_TIMES_SHOWN_DATES_RE = re.compile(
    r'shown in the selected location from\s+'
    r'([A-Za-zÀ-ÿ]+\s+\d+,?\s+\d{4})\s+to\s+'
    r'([A-Za-zÀ-ÿ]+\s+\d+,?\s+\d{4})',
    re.IGNORECASE,
)


def _parse_impression_range(s: str) -> dict:
    """
    '0 – 1K' → {lower: 0, upper: 1000, raw: '0 – 1K'}
    '4K – 5K' → {lower: 4000, upper: 5000, raw: '4K – 5K'}
    '1.5M – 5M' → {lower: 1500000, upper: 5000000, ...}
    '10K – 50K' → {lower: 10000, upper: 50000, ...}
    """
    out = {"lower": None, "upper": None, "raw": s}
    if not s:
        return out
    m = re.search(
        r'([\d\.,]+)\s*([KM]?)\s*[–—\-]\s*([\d\.,]+)\s*([KM]?)',
        s,
    )
    if not m:
        return out

    def _scale(n_str, suf):
        try:
            n = float(n_str.replace(',', ''))
        except ValueError:
            return None
        suf_u = (suf or '').upper()
        if suf_u == 'K':
            n *= 1000
        elif suf_u == 'M':
            n *= 1_000_000
        return int(n)

    out["lower"] = _scale(m.group(1), m.group(2))
    out["upper"] = _scale(m.group(3), m.group(4))
    return out


def _parse_main_meta(html: str) -> dict:
    """Extract dates / format / topic / targeting / impressions from main DOM HTML."""
    meta = {
        "first_shown": None,
        "last_shown": None,
        "format": None,
        "topic": None,
        "targeting_categories": [],
        "advertiser_name": None,
        "advertiser_legal_name": None,
        "advertiser_based_in": None,
        # Impressions / Times shown
        "impressions_range_raw": None,
        "impressions_lower_bound": None,
        "impressions_upper_bound": None,
        "times_shown_start_date": None,
        "times_shown_end_date": None,
    }

    for m in _DATE_RE.finditer(html):
        label = m.group(1).lower()
        date_str = m.group(2).strip()
        if "first" in label:
            meta["first_shown"] = date_str
        else:
            meta["last_shown"] = date_str

    m = _FORMAT_RE.search(html)
    if m:
        meta["format"] = _clean_text(m.group(1))

    m = _TOPIC_RE.search(html)
    if m:
        meta["topic"] = _clean_text(m.group(1))

    # Times shown impression range — "0 – 1K" / "4K – 5K" / etc.
    m = _IMPRESSION_RANGE_RE.search(html)
    if m:
        rng = _parse_impression_range(_clean_text(m.group(1)))
        meta["impressions_range_raw"] = rng["raw"]
        meta["impressions_lower_bound"] = rng["lower"]
        meta["impressions_upper_bound"] = rng["upper"]

    # Times shown date range (отдельно от First/Last shown!)
    m = _TIMES_SHOWN_DATES_RE.search(html)
    if m:
        meta["times_shown_start_date"] = m.group(1).strip()
        meta["times_shown_end_date"] = m.group(2).strip()

    # Advertiser name (advertiser-info-card)
    m = re.search(r'<div[^>]*class="[^"]*advertiser-name[^"]*"[^>]*>([^<]+?)</div>', html)
    if m:
        meta["advertiser_name"] = _clean_text(m.group(1))

    m = re.search(
        r'<div[^>]*class="[^"]*legal-name[^"]*"[^>]*>(.+?)</div>',
        html, re.DOTALL,
    )
    if m:
        block = m.group(1)
        block = re.sub(r'<strong>[^<]*</strong>', '', block)
        block = re.sub(r'<[^>]+>', '', block)
        meta["advertiser_legal_name"] = _clean_text(block)

    # Based in: <strong>Based in:</strong> Germany OR similar pattern
    m = re.search(r'Based in[:\s]*</strong>?\s*([A-Za-zÀ-ÿ\s]+?)(?:</div>|<)', html)
    if m:
        meta["advertiser_based_in"] = _clean_text(m.group(1))

    # Targeting categories с +/- знаками.
    # DOM: <div class="targeting-row geography-targeting-row">
    #        <div class="included">  <material-icon icon="add"> </material-icon> </div>
    #        <span>Geographic locations</span>
    #      </div>
    # Sign: '+' = included, '-' = excluded, '+-' = mixed.
    glossary_start = html.find('<strong>Demographic info:</strong>')
    if glossary_start < 0:
        glossary_start = len(html)
    upper_html = html[:glossary_start]

    TARGETING_ROW_TO_NAME = [
        ('demographic-targeting-row', 'Demographic info'),
        ('geography-targeting-row', 'Geographic locations'),
        ('contextual-targeting-row', 'Contextual signals'),
        ('customer-list-targeting-row', 'Customer lists'),
        ('topics-of-interest-targeting-row', 'Topics of interest'),
        ('topics-targeting-row', 'Topics of interest'),  # alternate naming
    ]
    found_targeting = []
    for row_class, name in TARGETING_ROW_TO_NAME:
        # Grab the block — from row start to row-end (next targeting-row OR end of audience block)
        pattern = (
            r'<div[^>]*class="[^"]*\btargeting-row\b[^"]*\b'
            + re.escape(row_class) +
            r'\b[^"]*"[^>]*>(.{0,3000}?)</span>\s*</div>'
        )
        m = re.search(pattern, upper_html, re.DOTALL)
        if not m:
            continue
        block = m.group(1)
        has_included = ('class="included"' in block or 'icon="add"' in block)
        has_excluded = ('class="excluded"' in block or 'icon="remove"' in block)
        if has_included and has_excluded:
            sign = '+-'
        elif has_included:
            sign = '+'
        elif has_excluded:
            sign = '-'
        else:
            continue
        # de-dup (Topics may match dual aliases)
        if not any(t["name"] == name for t in found_targeting):
            found_targeting.append({"name": name, "sign": sign})
    meta["targeting_categories"] = found_targeting

    return meta


# ─── data-p decoding ─────────────────────────────────────────────────────────

def _decode_data_p(data_p_value: str) -> list | None:
    """Разворачивает data-p attribute в Python структуру.
    Формат: '%.@.[type,[...]]...' возможно несколько JSON object подряд.
    Возвращает список всех parsed JSON values.
    """
    if not data_p_value:
        return None
    decoded = _html.unescape(data_p_value)
    # Strip prefix like '%.@.' (Google serialization marker)
    m = re.match(r'^%[^\[]*(\[.*)$', decoded, re.DOTALL)
    if not m:
        return None
    json_text = m.group(1)
    decoder = json.JSONDecoder()
    out = []
    i = 0
    while i < len(json_text):
        # skip whitespace
        while i < len(json_text) and json_text[i].isspace():
            i += 1
        if i >= len(json_text):
            break
        try:
            obj, end = decoder.raw_decode(json_text, idx=i)
        except json.JSONDecodeError:
            break
        out.append(obj)
        i = end
    return out or None


def _walk_strings(x, out=None, min_len=2) -> list[str]:
    """Recursive collector — все строки длиннее min_len, не URL'ы."""
    if out is None:
        out = []
    if isinstance(x, str):
        if len(x) >= min_len and not x.startswith(('http://', 'https://')):
            out.append(x)
    elif isinstance(x, list):
        for v in x:
            _walk_strings(v, out, min_len)
    elif isinstance(x, dict):
        for v in x.values():
            _walk_strings(v, out, min_len)
    return out


def _collect_image_urls(payload) -> list[str]:
    """В data-p часто лежат business photos на lh3.googleusercontent.com."""
    urls = []

    def walk(x):
        if isinstance(x, str):
            if 'googleusercontent.com' in x or 'gstatic.com/images' in x:
                urls.append(x)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(payload)
    return urls


# UI labels внутри iframe-render (не часть ad copy) — multilingual
_UI_LABELS_BLACKLIST = {
    # English
    'visit website', 'visit site', 'website', 'call', 'directions',
    'get directions', 'open hours', 'distance', 'reviews', 'rating',
    'open now', 'closed', 'hours', 'today',
    # German
    'website besuchen', 'anrufen', 'route', 'öffnungszeiten',
    'entfernung', 'bewertungen', 'jetzt geöffnet', 'geöffnet',
    # French
    'visiter le site', 'appeler', 'itinéraire', 'horaires',
    'avis', 'note', 'ouvert', 'fermé',
    # Italian
    'visita il sito', 'chiama', 'indicazioni', 'recensioni',
    # Spanish
    'visitar sitio', 'llamar', 'cómo llegar', 'opiniones',
    # Dutch
    'bezoek website', 'bellen', 'beoordelingen',
    # Caps variants automatically handled via lower()
}

# Адресные/бизнес-метa патtern'ы — служебные, не ad copy
_ADDRESS_PATTERNS = [
    r'^\d{4,6}$',                                        # postal code
    r'^[A-Z]{2}$',                                       # country code
    r'^\d{15,}$',                                        # google internal IDs
    r'^[A-Za-zÀ-ÿ\-\s]+\s+\d+[a-zA-Z]?$',                # "Fabrikstraße 16" (street + number)
    r'^Gewerbegebiet\b',                                 # German industrial area marker
    r'^Z\.A\.|^ZAC\b|^ZI\b',                              # FR zone artisanale/industrielle
]
_ADDRESS_REGEXES = [re.compile(p) for p in _ADDRESS_PATTERNS]

# Бизнес-категории (Google's business listing categories) — служебное, не ad copy
_BUSINESS_CATEGORY_HINTS = [
    'Kfz-Ersatzteilgeschäft', 'Autowerkstatt', 'Garage',
    'Magasin de pièces auto', 'Garage automobile',
    'Auto parts store', 'Repair shop',
]


def _is_likely_ad_copy(s: str) -> bool:
    """True если строка похожа на real ad headline/description."""
    s_strip = s.strip()
    if len(s_strip) < 3:
        return False
    # UI label?
    if s_strip.lower() in _UI_LABELS_BLACKLIST:
        return False
    # Address / numeric / category?
    for r in _ADDRESS_REGEXES:
        if r.match(s_strip):
            return False
    if s_strip in _BUSINESS_CATEGORY_HINTS:
        return False
    # Pure number (any length)
    if re.match(r'^\d+$', s_strip):
        return False
    # All caps single word (likely UI button) — heuristic
    if len(s_strip) <= 12 and s_strip.isupper() and ' ' not in s_strip:
        return False
    return True


def _classify_strings(strings: list[str]) -> dict:
    """Простая классификация собранных строк:
      - displayed_url    (выглядит как domain.tld без protocol)
      - postal_code      (digits 4-6)
      - country_code     (2 letter caps)
      - candidates       (potential headline/description, фильтрованные)
      - filtered_out     (UI labels / address / category — для debug)
    """
    result = {
        "displayed_url": None,
        "postal_code": None,
        "country_code": None,
        "candidates": [],
        "filtered_out": [],
    }
    seen_cand = set()
    seen_filt = set()
    for s in strings:
        s_strip = s.strip()
        if not s_strip:
            continue
        # Domain-like: has dot, no spaces, no slashes, ends in TLD-ish
        if (re.match(r'^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+$', s_strip)
                and not result["displayed_url"]):
            result["displayed_url"] = s_strip
            continue
        # Postal code
        if re.match(r'^\d{4,6}$', s_strip) and not result["postal_code"]:
            result["postal_code"] = s_strip
            continue
        # Country code
        if re.match(r'^[A-Z]{2}$', s_strip) and not result["country_code"]:
            result["country_code"] = s_strip
            continue
        # Real ad copy?
        if _is_likely_ad_copy(s_strip):
            if s_strip not in seen_cand:
                seen_cand.add(s_strip)
                result["candidates"].append(s_strip)
        else:
            if s_strip not in seen_filt:
                seen_filt.add(s_strip)
                result["filtered_out"].append(s_strip)
    return result


# ─── Main parser ─────────────────────────────────────────────────────────────

def parse_creative(advertiser_id: str, creative_id: str,
                    region: str = DEFAULT_REGION,
                    headed: bool = False, verbose: bool = False) -> dict:
    """Open creative URL → parse main DOM + iframes /adframe.
    Returns full dict ready to map to report row."""
    from playwright.sync_api import sync_playwright

    url = f"{TC_BASE}/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"
    result = {
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
        "region": region,
        "ad_link": url,
        # main DOM
        "first_shown": None,
        "last_shown": None,
        "format": None,
        "topic": None,
        "targeting_categories": [],
        "advertiser_name": None,
        "advertiser_legal_name": None,
        "advertiser_based_in": None,
        # impressions
        "impressions_range_raw": None,
        "impressions_lower_bound": None,
        "impressions_upper_bound": None,
        "times_shown_start_date": None,
        "times_shown_end_date": None,
        # ad content
        "ad_text_candidates": [],     # all unique strings — headlines/descriptions
        "displayed_url": None,
        "ad_image_urls": [],          # для has_image флага, не для exfiltration
        "has_image": False,
        "type_of_creative": "Site",   # default
        # variations: "1 of N variations" marker; None if absent (single-variation ad).
        "n_variations": None,
        # debug
        "iframe_count": 0,
        "iframe_data_p_count": 0,
        "fetch_error": None,
    }

    if verbose:
        print(f"  [creative] {creative_id}  ar={advertiser_id}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_selector('creative-details', timeout=15_000)
            except Exception:
                pass
            # Iframe attach: 30s timeout (was 10s — too tight under concurrent load).
            iframe_handle = None
            try:
                iframe_handle = page.wait_for_selector(
                    'iframe[src*="/adframe"]', timeout=30_000
                )
            except Exception:
                pass
            # After attach, wait for the iframe's own load event — not page networkidle
            # (TC long-polls/analytics dripping; networkidle is officially DISCOURAGED).
            if iframe_handle is not None:
                try:
                    inner = iframe_handle.content_frame()
                    if inner is not None:
                        inner.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    pass
            page.wait_for_timeout(1500)

            # Variations marker — "1 of 3 variations" → n_variations = 3
            try:
                n_var = page.evaluate(
                    "() => { const m = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return m ? parseInt(m[2], 10) : null; }"
                )
                if isinstance(n_var, int):
                    result["n_variations"] = n_var
            except Exception:
                pass

            main_html = page.content()
            meta = _parse_main_meta(main_html)
            result.update(meta)

            # Collect data-p from each /adframe iframe
            ad_frames = [f for f in page.frames if "/adframe" in f.url]
            result["iframe_count"] = len(ad_frames)
            if not ad_frames:
                result["fetch_error"] = "iframe_missing"

            all_candidates = []
            all_filtered_out = []
            all_image_urls = []

            # Если у нас уже есть advertiser_name/legal_name — добавим в blacklist
            advertiser_blacklist = set()
            if result["advertiser_name"]:
                advertiser_blacklist.add(result["advertiser_name"].lower())
            if result["advertiser_legal_name"]:
                advertiser_blacklist.add(result["advertiser_legal_name"].lower())

            for fr in ad_frames:
                try:
                    fhtml = fr.content()
                except Exception:
                    continue
                for dp in re.findall(r'data-p="([^"]+)"', fhtml):
                    payloads = _decode_data_p(dp)
                    if not payloads:
                        continue
                    result["iframe_data_p_count"] += 1
                    for payload in payloads:
                        strings = _walk_strings(payload)
                        cls = _classify_strings(strings)
                        if cls["displayed_url"] and not result["displayed_url"]:
                            result["displayed_url"] = cls["displayed_url"]
                        for c in cls["candidates"]:
                            # exclude exact advertiser name matches
                            if c.lower() in advertiser_blacklist:
                                if c not in all_filtered_out:
                                    all_filtered_out.append(c)
                                continue
                            # exclude "Von <advertiser_name>" / "From <advertiser_name>"
                            cl = c.lower()
                            if any(cl == prefix + ' ' + name
                                   for prefix in ('von', 'from', 'by', 'de', 'di')
                                   for name in advertiser_blacklist):
                                if c not in all_filtered_out:
                                    all_filtered_out.append(c)
                                continue
                            if c not in all_candidates:
                                all_candidates.append(c)
                        for f in cls["filtered_out"]:
                            if f not in all_filtered_out:
                                all_filtered_out.append(f)
                        for img in _collect_image_urls(payload):
                            if img not in all_image_urls:
                                all_image_urls.append(img)

            result["ad_text_candidates"] = all_candidates
            result["ad_text_filtered_out"] = all_filtered_out
            result["ad_image_urls"] = all_image_urls
            result["has_image"] = len(all_image_urls) > 0

            # Type of creative — default Site, refine via displayed URL pattern
            disp = result["displayed_url"] or ""
            if 'play.google.com' in disp or 'apps.apple.com' in disp:
                result["type_of_creative"] = "App"
            else:
                result["type_of_creative"] = "Site"

            browser.close()
            return result

    except Exception as e:
        result["fetch_error"] = f"{type(e).__name__}: {e}"
        return result


def _clean_text(s: str) -> str:
    s = _html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip(' .,:-')
    return s


# ─── Post-processing (shared between sync & async) ───────────────────────────

def _process_iframes_into_result(result: dict, iframe_htmls: list[str]) -> None:
    """Process collected iframe HTMLs into result['ad_text_candidates'] etc.
    Mutates result in place. Used by both sync parse_creative and async variant."""
    all_candidates = []
    all_filtered_out = []
    all_image_urls = []

    advertiser_blacklist = set()
    if result.get("advertiser_name"):
        advertiser_blacklist.add(result["advertiser_name"].lower())
    if result.get("advertiser_legal_name"):
        advertiser_blacklist.add(result["advertiser_legal_name"].lower())

    for fhtml in iframe_htmls:
        for dp in re.findall(r'data-p="([^"]+)"', fhtml):
            payloads = _decode_data_p(dp)
            if not payloads:
                continue
            result["iframe_data_p_count"] = result.get("iframe_data_p_count", 0) + 1
            for payload in payloads:
                strings = _walk_strings(payload)
                cls = _classify_strings(strings)
                if cls["displayed_url"] and not result.get("displayed_url"):
                    result["displayed_url"] = cls["displayed_url"]
                for c in cls["candidates"]:
                    if c.lower() in advertiser_blacklist:
                        if c not in all_filtered_out:
                            all_filtered_out.append(c)
                        continue
                    cl = c.lower()
                    if any(cl == prefix + ' ' + name
                           for prefix in ('von', 'from', 'by', 'de', 'di')
                           for name in advertiser_blacklist):
                        if c not in all_filtered_out:
                            all_filtered_out.append(c)
                        continue
                    if c not in all_candidates:
                        all_candidates.append(c)
                for f in cls["filtered_out"]:
                    if f not in all_filtered_out:
                        all_filtered_out.append(f)
                for img in _collect_image_urls(payload):
                    if img not in all_image_urls:
                        all_image_urls.append(img)

    result["ad_text_candidates"] = all_candidates
    result["ad_text_filtered_out"] = all_filtered_out
    result["ad_image_urls"] = all_image_urls
    result["has_image"] = len(all_image_urls) > 0

    disp = result.get("displayed_url") or ""
    if 'play.google.com' in disp or 'apps.apple.com' in disp:
        result["type_of_creative"] = "App"
    else:
        result["type_of_creative"] = "Site"


def _empty_result(advertiser_id: str, creative_id: str, region: str) -> dict:
    url = f"{TC_BASE}/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"
    return {
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
        "region": region,
        "ad_link": url,
        "first_shown": None,
        "last_shown": None,
        "format": None,
        "topic": None,
        "targeting_categories": [],
        "advertiser_name": None,
        "advertiser_legal_name": None,
        "advertiser_based_in": None,
        "impressions_range_raw": None,
        "impressions_lower_bound": None,
        "impressions_upper_bound": None,
        "times_shown_start_date": None,
        "times_shown_end_date": None,
        "ad_text_candidates": [],
        "displayed_url": None,
        "ad_image_urls": [],
        "has_image": False,
        "type_of_creative": "Site",
        "n_variations": None,
        "iframe_count": 0,
        "iframe_data_p_count": 0,
        "fetch_error": None,
    }


# ─── Async parser (для concurrent orchestrator) ───────────────────────────────

async def parse_creative_with_context(context, advertiser_id: str,
                                       creative_id: str,
                                       region: str = DEFAULT_REGION) -> dict:
    """Async-версия parse_creative — переиспользует existing playwright BrowserContext.
    Используется в mass_run_creatives.py для concurrent обхода (3-5 workers/contexts).

    NOTE: context должен быть async_playwright BrowserContext.
    """
    result = _empty_result(advertiser_id, creative_id, region)
    url = result["ad_link"]
    page = None
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector('creative-details', timeout=15_000)
        except Exception:
            pass
        iframe_handle = None
        try:
            iframe_handle = await page.wait_for_selector(
                'iframe[src*="/adframe"]', timeout=30_000
            )
        except Exception:
            pass
        if iframe_handle is not None:
            try:
                inner = await iframe_handle.content_frame()
                if inner is not None:
                    await inner.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
        await page.wait_for_timeout(1500)

        try:
            n_var = await page.evaluate(
                "() => { const m = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return m ? parseInt(m[2], 10) : null; }"
            )
            if isinstance(n_var, int):
                result["n_variations"] = n_var
        except Exception:
            pass

        main_html = await page.content()
        meta = _parse_main_meta(main_html)
        result.update(meta)

        ad_frames = [f for f in page.frames if "/adframe" in f.url]
        result["iframe_count"] = len(ad_frames)
        if not ad_frames:
            result["fetch_error"] = "iframe_missing"

        iframe_htmls = []
        for fr in ad_frames:
            try:
                iframe_htmls.append(await fr.content())
            except Exception:
                continue

        _process_iframes_into_result(result, iframe_htmls)
        return result

    except Exception as e:
        result["fetch_error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("advertiser_id")
    ap.add_argument("creative_id")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    r = parse_creative(args.advertiser_id, args.creative_id,
                       region=args.region, headed=args.headed, verbose=args.verbose)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
