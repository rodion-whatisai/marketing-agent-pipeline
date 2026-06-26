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
from log import log_info, log_debug, log_error
setup_console()


TC_BASE = "https://adstransparency.google.com"
DEFAULT_REGION = "FR"

# Direct API endpoint discovered via HAR analysis (Path C, May 2026).
# Used as fallback when iframe rendering doesn't yield an /adframe or sadbundle —
# typically image-baked ads (text rendered into a single PNG inside the ad card).
# The API returns metadata + image URL; it does NOT return ad text content
# (that lives inside iframe data-p for /adframe / sadbundle, or inside pixels
# for simgad image-baked ads — OCR territory, not API).
_LOOKUP_API_URL = "https://adstransparency.google.com/anji/_/rpc/LookupService/GetCreativeById?authuser=0"
_LOOKUP_API_JS = """async ({url, payload}) => {
    const r = await fetch(url, {
        method: 'POST',
        headers: {'content-type': 'application/x-www-form-urlencoded', 'x-same-domain': '1'},
        body: payload,
        credentials: 'include',
    });
    return {status: r.status, body: await r.text()};
}"""


def _build_lookup_payload(advertiser_id: str, creative_id: str) -> str:
    """f.req=<urlencoded JSON> — payload format observed in HAR."""
    import urllib.parse
    log_debug(f"_build_lookup_payload: ar={advertiser_id} cr={creative_id}")
    body = {"1": advertiser_id, "2": creative_id, "5": {"1": 1, "2": 0, "3": 2124}}
    return "f.req=" + urllib.parse.quote(json.dumps(body, separators=(',', ':')))


def _extract_image_urls_from_api_body(body: str) -> list[str]:
    """Pull ad asset URLs out of GetCreativeById response. Two known asset types:
       - simgad: image-baked Display Ads (text rendered into PNG)
       - displayads-formats /ads/preview/content.js: Video Ads (JS-rendered preview)
    Both are treated uniformly as 'ad asset URLs' — semantic split (image vs video)
    is downstream concern. Raw text scan + dedupe preserving order."""
    urls = re.findall(r'https://tpc\.googlesyndication\.com/archive/simgad/\d+', body)
    urls += re.findall(
        r'https://displayads-formats\.googleusercontent\.com/ads/preview/content\.js[^"\s\'\\]*',
        body,
    )
    deduped = list(dict.fromkeys(urls))  # dedupe preserving order
    log_debug(f"_extract_image_urls_from_api_body: {len(deduped)} asset URL(s) from {len(body)}-char body")
    return deduped


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
        except ValueError as e:
            log_debug(f"_parse_impression_range._scale: не число '{n_str}': {e}")
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
    log_debug(f"_parse_main_meta: парсим main DOM, {len(html)} символов HTML")
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

    log_debug(
        f"_parse_main_meta: format={meta['format']!r} topic={meta['topic']!r} "
        f"first={meta['first_shown']!r} impressions_raw={meta['impressions_range_raw']!r} "
        f"targeting={len(found_targeting)} cat(s)"
    )
    return meta


# ─── data-p decoding ─────────────────────────────────────────────────────────

def _decode_data_p(data_p_value: str) -> list | None:
    """Разворачивает data-p attribute в Python структуру.
    Формат: '%.@.[type,[...]]...' возможно несколько JSON object подряд.
    Возвращает список всех parsed JSON values.
    """
    if not data_p_value:
        log_debug("_decode_data_p: пустой data-p, пропуск")
        return None
    decoded = _html.unescape(data_p_value)
    # Strip prefix like '%.@.' (Google serialization marker)
    m = re.match(r'^%[^\[]*(\[.*)$', decoded, re.DOTALL)
    if not m:
        log_debug("_decode_data_p: нет '%.@.[' префикса — не Google-сериализация, пропуск")
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
        except json.JSONDecodeError as e:
            log_debug(f"_decode_data_p: JSON decode остановлен на idx={i}: {e}")
            break
        out.append(obj)
        i = end
    log_debug(f"_decode_data_p: разобрано {len(out)} JSON-объект(ов)")
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

    log_debug(f"parse_creative: вход cr={creative_id} ar={advertiser_id} region={region} headed={headed}")
    if verbose:
        log_info(f"  [creative] {creative_id}  ar={advertiser_id}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            # Block heavy assets parser doesn't need (images / fonts / CSS / video).
            # Keeps script + document + XHR — those drive /adframe hydration.
            def _block_assets_sync(route):
                if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _block_assets_sync)
            log_debug(f"parse_creative: goto {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            log_debug("parse_creative: DOM загружен, ждём iframe или error-плейсхолдер")
            # Wait for either: iframe attached (real ad) OR error placeholder text
            # ("Can't find advertiser" / "Can't find ad"). Whichever first.
            # Saves ~30s on banned/missing ads, costs nothing for healthy ones.
            try:
                page.wait_for_function(
                    """() => {
                        if (document.querySelector('iframe[src*="/adframe"]')) return true;
                        if (document.querySelector('iframe[src*="sadbundle"]')) return true;
                        const t = document.body && document.body.innerText || '';
                        return t.indexOf("Can't find advertiser") !== -1
                            || t.indexOf("Can't find ad") !== -1;
                    }""",
                    timeout=30_000,
                )
            except Exception as e:
                log_debug(f"parse_creative: wait_for_function (iframe/error) истёк/упал: {e}")
            # Page text shows terminal placeholder — but the API may still return
            # creative data even when the public page says "Can't find advertiser"
            # (advertiser-level visibility check ≠ creative-level data availability).
            # Probe API first; only return terminal if API also has nothing.
            try:
                body_text = page.evaluate("() => document.body.innerText || ''")
                terminal_kind = None
                if "Can't find advertiser" in body_text:
                    terminal_kind = "advertiser_not_found"
                elif "Can't find ad" in body_text:
                    terminal_kind = "ad_not_found"
                if terminal_kind:
                    log_debug(f"parse_creative: terminal-плейсхолдер '{terminal_kind}', пробуем API fallback")
                    api_imgs = []
                    try:
                        payload = _build_lookup_payload(advertiser_id, creative_id)
                        resp = page.evaluate(_LOOKUP_API_JS,
                                              {"url": _LOOKUP_API_URL, "payload": payload})
                        if resp and resp.get("status") == 200 and len(resp.get("body") or "") > 100:
                            api_imgs = _extract_image_urls_from_api_body(resp.get("body") or "")
                    except Exception as e:
                        log_debug(f"parse_creative: API probe (terminal) упал: {e}")
                    if api_imgs:
                        # Recovered via API — soft-terminal (image saved, text in pixels)
                        log_debug(f"parse_creative: API вернул {len(api_imgs)} asset(ы) → text_in_image")
                        result["ad_image_urls"] = api_imgs
                        result["has_image"] = True
                        result["fetch_error"] = "text_in_image"
                    else:
                        log_debug(f"parse_creative: API пуст → terminal '{terminal_kind}'")
                        result["fetch_error"] = terminal_kind
                    browser.close()
                    return result
            except Exception as e:
                log_debug(f"parse_creative: terminal-detection блок упал: {e}")
            # Real ad — locate the iframe handle (already attached by now).
            # Two ad-iframe variants observed: /adframe (Display Ads) and sadbundle
            # (Search Ads "SearchAdsViewerRenderingUi"). Both expose data-p attrs.
            iframe_handle = None
            try:
                iframe_handle = page.wait_for_selector(
                    'iframe[src*="/adframe"], iframe[src*="sadbundle"]', timeout=5_000
                )
            except Exception as e:
                log_debug(f"parse_creative: ad-iframe selector не найден за 5с: {e}")
            log_debug(f"parse_creative: iframe_handle={'найден' if iframe_handle else 'нет'}")
            # After attach, wait for the iframe's own load event — not page networkidle
            # (TC long-polls/analytics dripping; networkidle is officially DISCOURAGED).
            if iframe_handle is not None:
                try:
                    inner = iframe_handle.content_frame()
                    if inner is not None:
                        inner.wait_for_load_state("load", timeout=15_000)
                except Exception as e:
                    log_debug(f"parse_creative: ожидание load события iframe упало: {e}")
            page.wait_for_timeout(1500)

            # Variations marker — "1 of 3 variations" → n_variations = 3
            try:
                n_var = page.evaluate(
                    "() => { const m = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return m ? parseInt(m[2], 10) : null; }"
                )
                if isinstance(n_var, int):
                    log_debug(f"parse_creative: n_variations={n_var}")
                    result["n_variations"] = n_var
            except Exception as e:
                log_debug(f"parse_creative: n_variations evaluate упал: {e}")

            main_html = page.content()
            meta = _parse_main_meta(main_html)
            result.update(meta)

            # Collect data-p from each /adframe iframe
            ad_frames = [f for f in page.frames if "/adframe" in f.url or "sadbundle" in f.url]
            result["iframe_count"] = len(ad_frames)
            log_debug(f"parse_creative: {len(ad_frames)} ad-iframe(ов) найдено")
            if not ad_frames:
                log_debug("parse_creative: ad-iframe нет → fetch_error=iframe_missing (может быть повышен ниже)")
                result["fetch_error"] = "iframe_missing"  # may be upgraded below

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
                except Exception as e:
                    log_debug(f"parse_creative: не смогли прочитать iframe content ({fr.url}): {e}")
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

            # Fallback for image-baked ads: if iframe rendering yielded nothing,
            # but main DOM has metadata, hit GetCreativeById API for the image URL.
            # Sets has_image=True and fetch_error="text_in_image" (soft-terminal).
            if not ad_frames and not all_image_urls and (result.get("first_shown") or result.get("format")):
                log_debug("parse_creative: нет iframe, но есть metadata → API fallback за image URL")
                try:
                    payload = _build_lookup_payload(advertiser_id, creative_id)
                    resp = page.evaluate(_LOOKUP_API_JS,
                                          {"url": _LOOKUP_API_URL, "payload": payload})
                    if resp and resp.get("status") == 200:
                        api_imgs = _extract_image_urls_from_api_body(resp.get("body") or "")
                        if api_imgs:
                            log_debug(f"parse_creative: API fallback вернул {len(api_imgs)} asset(ы) → text_in_image")
                            all_image_urls.extend(api_imgs)
                            result["fetch_error"] = "text_in_image"
                except Exception as e:
                    log_debug(f"parse_creative: API fallback (image-baked) упал: {e}")

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

            log_debug(
                f"parse_creative: готово cr={creative_id} — {len(all_candidates)} ad-text кандидат(ов), "
                f"{len(all_image_urls)} image URL(s), fetch_error={result['fetch_error']!r}"
            )
            browser.close()
            return result

    except Exception as e:
        log_error(f"parse_creative: упал cr={creative_id}: {type(e).__name__}: {e}")
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
    log_debug(f"_process_iframes_into_result: обрабатываем {len(iframe_htmls)} iframe HTML(ов)")
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
    log_debug(
        f"_process_iframes_into_result: {len(all_candidates)} кандидат(ов), "
        f"{len(all_filtered_out)} отфильтровано, {len(all_image_urls)} image URL(s)"
    )

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
    log_debug(f"parse_creative_with_context: вход cr={creative_id} ar={advertiser_id} region={region}")
    result = _empty_result(advertiser_id, creative_id, region)
    url = result["ad_link"]
    page = None
    try:
        page = await context.new_page()
        async def _block_assets_async(route):
            if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", _block_assets_async)
        log_debug(f"parse_creative_with_context: goto {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        log_debug("parse_creative_with_context: DOM загружен, ждём iframe или error-плейсхолдер")
        try:
            await page.wait_for_function(
                """() => {
                    if (document.querySelector('iframe[src*="/adframe"]')) return true;
                    if (document.querySelector('iframe[src*="sadbundle"]')) return true;
                    const t = document.body && document.body.innerText || '';
                    return t.indexOf("Can't find advertiser") !== -1
                        || t.indexOf("Can't find ad") !== -1;
                }""",
                timeout=30_000,
            )
        except Exception as e:
            log_debug(f"parse_creative_with_context: wait_for_function (iframe/error) истёк/упал: {e}")
        try:
            body_text = await page.evaluate("() => document.body.innerText || ''")
            terminal_kind = None
            if "Can't find advertiser" in body_text:
                terminal_kind = "advertiser_not_found"
            elif "Can't find ad" in body_text:
                terminal_kind = "ad_not_found"
            if terminal_kind:
                log_debug(f"parse_creative_with_context: terminal-плейсхолдер '{terminal_kind}', пробуем API fallback")
                api_imgs = []
                try:
                    payload = _build_lookup_payload(advertiser_id, creative_id)
                    resp = await page.evaluate(_LOOKUP_API_JS,
                                                {"url": _LOOKUP_API_URL, "payload": payload})
                    if resp and resp.get("status") == 200 and len(resp.get("body") or "") > 100:
                        api_imgs = _extract_image_urls_from_api_body(resp.get("body") or "")
                except Exception as e:
                    log_debug(f"parse_creative_with_context: API probe (terminal) упал: {e}")
                if api_imgs:
                    log_debug(f"parse_creative_with_context: API вернул {len(api_imgs)} asset(ы) → text_in_image")
                    result["ad_image_urls"] = api_imgs
                    result["has_image"] = True
                    result["fetch_error"] = "text_in_image"
                else:
                    log_debug(f"parse_creative_with_context: API пуст → terminal '{terminal_kind}'")
                    result["fetch_error"] = terminal_kind
                return result
        except Exception as e:
            log_debug(f"parse_creative_with_context: terminal-detection блок упал: {e}")
        iframe_handle = None
        try:
            iframe_handle = await page.wait_for_selector(
                'iframe[src*="/adframe"], iframe[src*="sadbundle"]', timeout=5_000
            )
        except Exception as e:
            log_debug(f"parse_creative_with_context: ad-iframe selector не найден за 5с: {e}")
        log_debug(f"parse_creative_with_context: iframe_handle={'найден' if iframe_handle else 'нет'}")
        if iframe_handle is not None:
            try:
                inner = await iframe_handle.content_frame()
                if inner is not None:
                    await inner.wait_for_load_state("load", timeout=15_000)
            except Exception as e:
                log_debug(f"parse_creative_with_context: ожидание load события iframe упало: {e}")
        await page.wait_for_timeout(1500)

        try:
            n_var = await page.evaluate(
                "() => { const m = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return m ? parseInt(m[2], 10) : null; }"
            )
            if isinstance(n_var, int):
                log_debug(f"parse_creative_with_context: n_variations={n_var}")
                result["n_variations"] = n_var
        except Exception as e:
            log_debug(f"parse_creative_with_context: n_variations evaluate упал: {e}")

        main_html = await page.content()
        meta = _parse_main_meta(main_html)
        result.update(meta)

        ad_frames = [f for f in page.frames if "/adframe" in f.url or "sadbundle" in f.url]
        result["iframe_count"] = len(ad_frames)
        log_debug(f"parse_creative_with_context: {len(ad_frames)} ad-iframe(ов) найдено")
        if not ad_frames:
            log_debug("parse_creative_with_context: ad-iframe нет → fetch_error=iframe_missing")
            result["fetch_error"] = "iframe_missing"

        iframe_htmls = []
        for fr in ad_frames:
            try:
                iframe_htmls.append(await fr.content())
            except Exception as e:
                log_debug(f"parse_creative_with_context: не смогли прочитать iframe content ({fr.url}): {e}")
                continue

        _process_iframes_into_result(result, iframe_htmls)

        # API fallback for image-baked ads (no iframe but main DOM has metadata).
        # See sync parse_creative for rationale.
        if (not ad_frames and not result.get("ad_image_urls")
                and (result.get("first_shown") or result.get("format"))):
            log_debug("parse_creative_with_context: нет iframe, но есть metadata → API fallback за image URL")
            try:
                payload = _build_lookup_payload(advertiser_id, creative_id)
                resp = await page.evaluate(_LOOKUP_API_JS,
                                            {"url": _LOOKUP_API_URL, "payload": payload})
                if resp and resp.get("status") == 200:
                    api_imgs = _extract_image_urls_from_api_body(resp.get("body") or "")
                    if api_imgs:
                        log_debug(f"parse_creative_with_context: API fallback вернул {len(api_imgs)} asset(ы) → text_in_image")
                        result["ad_image_urls"] = api_imgs
                        result["has_image"] = True
                        result["fetch_error"] = "text_in_image"
            except Exception as e:
                log_debug(f"parse_creative_with_context: API fallback (image-baked) упал: {e}")

        log_debug(
            f"parse_creative_with_context: готово cr={creative_id} — "
            f"{len(result.get('ad_text_candidates') or [])} ad-text кандидат(ов), "
            f"fetch_error={result['fetch_error']!r}"
        )
        return result

    except Exception as e:
        log_error(f"parse_creative_with_context: упал cr={creative_id}: {type(e).__name__}: {e}")
        result["fetch_error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception as e:
                log_debug(f"parse_creative_with_context: page.close() упал: {e}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("advertiser_id")
    ap.add_argument("creative_id")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    log_debug(f"main: CLI ar={args.advertiser_id} cr={args.creative_id} region={args.region} headed={args.headed}")
    r = parse_creative(args.advertiser_id, args.creative_id,
                       region=args.region, headed=args.headed, verbose=args.verbose)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
