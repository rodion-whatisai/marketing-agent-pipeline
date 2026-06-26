"""
TNC Pipeline — Per-site Ads Library Report (HTML)
==================================================
Reads scans/{domain}/{domain}_step1.json + scans/{domain}/fb.json and produces
scans/{domain}/report_ads_library.html — a standalone human-readable report
with site identity, Facebook page, Ads Library data, and top-N ad creatives.

Usage:
    python generate_site_report.py aerosus.fr
    python generate_site_report.py aerosus.fr suspenair.fr
    python generate_site_report.py --all        # all domains that have fb.json
"""

import sys
import json
import base64
import argparse
import html
from pathlib import Path
from datetime import datetime, timezone

from log import log_error, log_debug, log_success

# UTF-8 stdout
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception as e:
            log_debug(f"reconfigure utf-8 для {_stream}: {e}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_template(text: str) -> bool:
    return bool(text) and "{{" in text and "}}" in text


def _fmt_date(unix_ts) -> str:
    if not unix_ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception as e:
        log_debug(f"_fmt_date не смог распарсить unix_ts={unix_ts!r}: {e}")
        return "—"


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _platform_confidence_note(platform_info: dict) -> str:
    conf = platform_info.get("confidence", "unknown")
    score = platform_info.get("score")
    signals = platform_info.get("signals") or []
    parts = [f"{conf} confidence"]
    if score is not None:
        # score — сумма весов совпавших сигналов (без верхней границы), не x/10
        parts.append(f"signal weight {score}")
    if signals:
        parts.append(f"signals: {', '.join(signals[:5])}")
    return "; ".join(parts)


def _render_field(text: str, display_format: str) -> str:
    """
    Returns rendered HTML for a text field, or empty string if we have nothing
    meaningful to show. For DCO/DPA ads with real text (pulled from cards),
    appends a small "(DCO example)" note so the reader knows content is dynamic.
    """
    is_dynamic = display_format in ("DCO", "DPA")
    if not text or _is_template(text):
        # No static content. For DCO — could say "(dynamic)" but keeps UI busy;
        # cleaner to return empty and let caller skip the line.
        return ""
    rendered = _esc(text).replace("\n", "<br>")
    if is_dynamic:
        rendered += ' <span class="dco-note">(DCO — example variant, actual text varies per viewer)</span>'
    return rendered


PLATFORM_DISPLAY = {
    "FACEBOOK":         "Facebook",
    "INSTAGRAM":        "Instagram",
    "MESSENGER":        "Messenger",
    "AUDIENCE_NETWORK": "Audience Network",
    "THREADS":          "Threads",
    "WHATSAPP":         "WhatsApp",
}


def _format_badge_class(fmt: str) -> str:
    return {
        "IMAGE":  "fmt-image",
        "VIDEO":  "fmt-video",
        "DCO":    "fmt-dco",
        "DPA":    "fmt-dpa",
    }.get(fmt, "fmt-other")


# ─── Image embedding (base64 data URLs for standalone portability) ───────────

_IMG_CACHE = {}  # abs path → data URL


def _image_to_data_url(scan_dir: Path, rel_or_abs_path: str) -> str:
    """Читает image file с диска и возвращает data URL (base64). Кэшируется."""
    if not rel_or_abs_path:
        return ""
    path = Path(rel_or_abs_path)
    if not path.is_absolute():
        path = (scan_dir / rel_or_abs_path).resolve()
    key = str(path)
    if key in _IMG_CACHE:
        log_debug(f"_image_to_data_url: кэш-хит для {key}")
        return _IMG_CACHE[key]
    if not path.exists():
        log_debug(f"_image_to_data_url: файл не найден {key}")
        _IMG_CACHE[key] = ""
        return ""
    try:
        data = path.read_bytes()
        ext = path.suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        mime = f"image/{ext}"
        encoded = base64.b64encode(data).decode("ascii")
        url = f"data:{mime};base64,{encoded}"
        log_debug(f"_image_to_data_url: закодировал {len(data)} байт ({mime}) из {key}")
        _IMG_CACHE[key] = url
        return url
    except Exception as e:
        log_debug(f"_image_to_data_url: чтение/кодирование {key} упало: {e}")
        _IMG_CACHE[key] = ""
        return ""


def _scan_date_iso(scan_dir: Path) -> str:
    """Берём дату сканирования из mtime fb.json (или step1.json fallback)."""
    for name in ("fb.json", ):
        p = scan_dir / name
        if p.exists():
            ts = p.stat().st_mtime
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "unknown"


# ─── CSS ──────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1c1e21;
  background: #fafafa;
  margin: 0;
  line-height: 1.5;
}
.container { max-width: 960px; margin: 0 auto; padding: 32px 24px 96px; }

header {
  border-bottom: 2px solid #e4e6eb;
  padding-bottom: 20px;
  margin-bottom: 32px;
}
header h1 {
  margin: 0 0 4px 0;
  font-size: 32px;
  font-weight: 700;
  color: #1c1e21;
}
header .subtitle {
  color: #65676b;
  font-size: 15px;
  font-weight: 500;
}
header .meta {
  margin-top: 12px;
  font-size: 13px;
  color: #8a8d91;
}
header .tldr {
  margin-top: 16px;
  padding: 12px 16px;
  background: #e7f3ff;
  border-left: 4px solid #1877f2;
  border-radius: 4px;
  font-size: 15px;
}

section { margin-bottom: 40px; }
section h2 {
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 16px 0;
  color: #1c1e21;
  border-bottom: 1px solid #e4e6eb;
  padding-bottom: 8px;
}

dl.facts { margin: 0; }
dl.facts dt { font-weight: 600; margin-top: 8px; }
dl.facts dd {
  margin: 0 0 4px 0;
  color: #1c1e21;
}
dl.facts dd small { color: #65676b; }

.kv { margin: 8px 0; }
.kv strong { display: inline-block; min-width: 130px; color: #65676b; font-weight: 500; }

a { color: #1877f2; text-decoration: none; }
a:hover { text-decoration: underline; }

code {
  background: #eceff1;
  padding: 1px 6px;
  border-radius: 4px;
  font-family: "SF Mono", Consolas, "Courier New", monospace;
  font-size: 0.92em;
}

.btn-primary {
  display: inline-block;
  background: #1877f2;
  color: #fff !important;
  padding: 10px 18px;
  border-radius: 6px;
  font-weight: 600;
  font-size: 14px;
  text-decoration: none !important;
  margin-top: 8px;
}
.btn-primary:hover { background: #166fe5; }

.note {
  font-size: 13px;
  color: #65676b;
  font-style: italic;
}

.warning-box {
  background: #fff8e1;
  border-left: 4px solid #f59e0b;
  padding: 14px 18px;
  border-radius: 4px;
  margin-bottom: 12px;
  font-size: 14px;
  line-height: 1.5;
  color: #4a3500;
}
.warning-box strong { color: #7c4a00; }
.warning-box code { background: #fff; border: 1px solid #f0e3b8; }

/* Ad cards grid */
.ads-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 20px;
  margin-top: 20px;
}
@media (max-width: 720px) {
  .ads-grid { grid-template-columns: 1fr; }
}

.ad-card {
  background: #fff;
  border: 1px solid #ddd;
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.ad-image {
  width: 100%;
  aspect-ratio: 1 / 1;
  background: #eceff1;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  position: relative;
}
.ad-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.ad-image .no-img {
  color: #90949c;
  font-size: 13px;
}

.ad-body-section {
  padding: 14px 16px 18px;
  flex: 1;
  display: flex;
  flex-direction: column;
}
.ad-badges {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
}
.fmt-image { background: #d4edda; color: #155724; }
.fmt-video { background: #d1ecf1; color: #0c5460; }
.fmt-dco   { background: #fff3cd; color: #856404; }
.fmt-dpa   { background: #e2d4f0; color: #5a2e87; }
.fmt-other { background: #eceff1; color: #65676b; }
.badge-variants { background: #f0f2f5; color: #65676b; }

.ad-title {
  font-size: 15px;
  font-weight: 700;
  margin: 0 0 6px 0;
}
.ad-body {
  font-size: 14px;
  color: #1c1e21;
  margin: 0 0 12px 0;
  line-height: 1.4;
}
.dco-note {
  color: #8a8d91;
  font-style: italic;
  font-size: 11px;
  font-weight: 400;
  display: inline-block;
  margin-left: 4px;
}
.placeholder {
  color: #8a8d91;
  font-style: italic;
}
.cta-disabled {
  background: #f0f2f5 !important;
  color: #8a8d91 !important;
  cursor: not-allowed;
}

.ad-meta {
  font-size: 12px;
  color: #65676b;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed #e4e6eb;
}
.ad-meta .kv { margin: 3px 0; }
.ad-meta .kv strong { min-width: 90px; font-size: 11px; text-transform: uppercase; }

.ad-cta {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cta-primary {
  display: block;
  background: #e4e6eb;
  color: #1c1e21 !important;
  padding: 8px 12px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 13px;
  text-align: center;
  text-decoration: none !important;
}
.cta-primary:hover { background: #d8dadf; }
.cta-detail {
  font-size: 12px;
  color: #1877f2;
  text-align: center;
  display: block;
}

/* Disclaimer */
.disclaimer {
  margin-top: 40px;
  padding: 16px 20px;
  background: #fafafa;
  border: 1px solid #e4e6eb;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.55;
  color: #65676b;
}
.disclaimer h3 {
  margin: 0 0 10px 0;
  font-size: 14px;
  font-weight: 600;
  color: #1c1e21;
}
.disclaimer p { margin: 6px 0; }
.disclaimer strong { color: #1c1e21; }
.disclaimer code { font-size: 11px; }

/* Glossary */
.glossary {
  margin-top: 48px;
  padding: 16px 20px;
  background: #f0f2f5;
  border-radius: 8px;
  font-size: 13px;
  color: #65676b;
  line-height: 1.6;
}
.glossary strong { color: #1c1e21; }
.glossary dt { font-weight: 600; color: #1c1e21; margin-top: 6px; }
.glossary dd { margin: 0 0 4px 0; }
"""


# ─── Section renderers ───────────────────────────────────────────────────────

def render_header(domain: str, step1: dict, fb: dict, scan_date: str) -> str:
    acc = (fb.get("accounts") or [{}])[0]
    n_ads = acc.get("active_ads_count")
    tldr_parts = []
    if n_ads is not None:
        tldr_parts.append(f"<strong>{n_ads}</strong> active ads on Facebook")
    if acc.get("partnership_count"):
        tldr_parts.append(f"{acc['partnership_count']} partnership ads")
    tldr = " · ".join(tldr_parts) if tldr_parts else "No active ads found"

    return f"""
<header>
  <h1>{_esc(domain)}</h1>
  <div class="subtitle">Facebook Ads Library — Competitor Intelligence Report</div>
  <div class="meta">Snapshot: {_esc(scan_date)}</div>
  <div class="tldr">{tldr}</div>
</header>
"""


def render_site_identity(step1: dict, fb: dict) -> str:
    platform_info = step1.get("platform") or {}
    platform = platform_info.get("platform", "unknown")
    platform_note = _platform_confidence_note(platform_info)

    lang_info = step1.get("site_language") or {}
    lang = lang_info.get("lang", "?")
    lang_source = lang_info.get("source", "not detected")

    site_country = fb.get("site_country", "?")
    site_country_src = fb.get("site_country_source", "")

    return f"""
<section id="identity">
  <h2>Site identity</h2>
  <dl class="facts">
    <dt>Platform</dt>
    <dd>{_esc(platform)} <small>— {_esc(platform_note)}</small></dd>
    <dt>Language</dt>
    <dd><code>{_esc(lang)}</code> <small>— detected via {_esc(lang_source)}</small></dd>
    <dt>Country</dt>
    <dd><code>{_esc(site_country)}</code> <small>— detected via {_esc(site_country_src)}</small></dd>
  </dl>
</section>
"""


def render_facebook(fb: dict) -> str:
    accounts = fb.get("accounts") or []
    alive = [a for a in accounts if a.get("alive")]
    discovery_meta = fb.get("discovery_meta") or {}

    # Case A: homepage blocked, no FB found, fallback also empty
    if not alive and discovery_meta.get("fallback_attempted") and discovery_meta.get("fallback_result") == "no_ads_found":
        status = discovery_meta.get("homepage_status") or "error"
        fetch_method = discovery_meta.get("homepage_fetch_method") or "blocked"
        keyword = discovery_meta.get("fallback_keyword") or "?"
        waf_line = ""
        if fetch_method == "blocked_by_waf":
            waf_line = ("Both direct HTTP fetch and headless-browser fetch (Playwright) were blocked — "
                        "the site is behind a WAF (Cloudflare or similar). ")
        return f"""
<section id="facebook">
  <h2>Facebook page</h2>
  <div class="warning-box">
    <strong>⚠️ Homepage blocked by WAF.</strong>
    The site returned HTTP {_esc(status)} to our scanner. {_esc(waf_line)}
    We also tried a direct Ads Library search with brand keyword <code>{_esc(keyword)}</code>
    — no active ads were found under that name either.<br><br>
    <em>Manual verification recommended.</em> Open the site in your browser, locate the FB link
    (if any) in the footer, and check Ads Library for that specific handle.
  </div>
</section>
"""
    # Case B: truly no FB found on reachable site
    if not alive:
        status = discovery_meta.get("homepage_status")
        status_line = f" (HTTP {status})" if status else ""
        return f"""
<section id="facebook">
  <h2>Facebook page</h2>
  <p class="note">No Facebook link found on the homepage{_esc(status_line)}. Site may not have an active FB presence.</p>
</section>
"""

    top = max(alive, key=lambda a: a.get("active_ads_count") or 0)
    discovery = top.get("discovery_method", "homepage_link")

    # Case C: brand-keyword fallback succeeded (homepage was blocked)
    if discovery == "brand_keyword_fallback":
        status = discovery_meta.get("homepage_status") or "blocked"
        keyword = top.get("ads_search_term") or top.get("display_name") or "?"
        note = top.get("confidence_note") or ""
        return f"""
<section id="facebook">
  <h2>Facebook page</h2>
  <div class="warning-box">
    <strong>⚠️ Unverified brand match.</strong>
    The site's homepage returned HTTP {_esc(status)}, so we couldn't confirm a Facebook link from the site directly.
    Instead, we searched Ads Library by brand keyword <code>{_esc(keyword)}</code> and found active ads under that name.<br><br>
    <em>{_esc(note)}</em>
  </div>
  <div class="kv"><strong>Search keyword</strong> <code>{_esc(keyword)}</code></div>
  <div class="kv"><strong>Discovery method</strong> Brand-keyword fallback (homepage blocked)</div>
</section>
"""

    # Case D: normal flow — FB link found on homepage
    others_html = ""
    others = [a for a in alive if a is not top]
    if others:
        items = "".join(
            f'<li>@{_esc(a.get("handle"))} — '
            f'<a href="{_esc(a.get("url"))}" target="_blank">{_esc(a.get("url"))}</a> '
            f'({a.get("active_ads_count") or 0} ads)</li>'
            for a in others
        )
        others_html = f"<p>Other FB accounts found on the site:</p><ul>{items}</ul>"

    return f"""
<section id="facebook">
  <h2>Facebook page</h2>
  <div class="kv"><strong>Handle</strong> @{_esc(top.get("handle"))}</div>
  <div class="kv"><strong>Display name</strong> {_esc(top.get("display_name"))}</div>
  <div class="kv"><strong>URL</strong>
    <a href="{_esc(top.get("url"))}" target="_blank">{_esc(top.get("url"))}</a></div>
  <div class="kv"><strong>Status</strong> Published (alive)</div>
  <div class="kv"><strong>Discovery method</strong> FB link found on homepage</div>
  {others_html}
</section>
"""


MODE_LABEL = {
    "page":                              "Advertiser-filtered (authoritative — FB filtered by page_id)",
    "keyword_filtered_by_pid_or_name":   "Keyword search + filtered by advertiser page_id or name match",
    "keyword_filtered_by_page_id":       "Keyword search + filtered by target FB page_id",
    "keyword_filtered_by_name":          "Keyword search + fuzzy filter on advertiser name (lower confidence)",
    "keyword_raw":                       "Keyword search, unfiltered (may include unrelated ads)",
    "unknown":                           "Unknown",
}


def render_ads_search(fb: dict) -> str:
    accounts = fb.get("accounts") or []
    alive = [a for a in accounts if a.get("alive")]
    if not alive:
        return ""
    top = max(alive, key=lambda a: a.get("active_ads_count") or 0)
    search_term = top.get("ads_search_term") or top.get("display_name") or ""
    count = top.get("active_ads_count")
    raw_total = top.get("raw_keyword_total") or 0
    mode = top.get("ads_library_mode") or "unknown"
    mode_label = MODE_LABEL.get(mode, mode)
    partnership = top.get("partnership_count") or 0
    ads_url = ((top.get("ads_library") or {}).get("ALL") or {}).get("active_only", "")
    page_id = top.get("page_id")

    # Query description — depends on mode
    if page_id and mode == "page":
        query_html = f'<code>view_all_page_id={_esc(page_id)}</code> (direct advertiser filter)'
    else:
        query_html = f'keyword <code>{_esc(search_term)}</code>'

    partnership_line = ""
    if partnership > 0:
        partnership_line = f'<div class="kv"><strong>Partnership ads</strong> ~{partnership} (branded content / paid collaborations)</div>'

    # Noise disclosure — показываем raw_total когда он отличается от count (т.е. мы отфильтровали)
    noise_line = ""
    if raw_total and raw_total != (count or 0) and mode != "page":
        filtered_out = raw_total - (count or 0)
        noise_line = (
            f'<div class="kv"><strong>Filtered out</strong> '
            f'<span class="dco-note">{filtered_out} keyword matches from other advertisers (noise)</span></div>'
        )

    # Warning box for low-confidence modes
    mode_warning = ""
    if mode == "keyword_filtered_by_name":
        mode_warning = (
            '<div class="warning-box">'
            '<strong>⚠️ Lower-confidence result.</strong> '
            'We used keyword search + fuzzy-matched advertiser names (since no FB page_id was known). '
            'Some matches may not actually be from this brand. Manual verification recommended.'
            '</div>'
        )
    elif mode == "keyword_raw":
        mode_warning = (
            '<div class="warning-box">'
            '<strong>⚠️ Unfiltered keyword results.</strong> '
            'No target page_id or name was available for filtering. '
            'Results may include unrelated ads that only mention the keyword.'
            '</div>'
        )

    return f"""
<section id="ads-library">
  <h2>Ads Library search</h2>
  {mode_warning}
  <div class="kv"><strong>Query</strong> {query_html}</div>
  <div class="kv"><strong>Country scope</strong> <code>ALL</code></div>
  <div class="kv"><strong>Sort order</strong> total impressions, descending (FB's ranking)</div>
  <div class="kv"><strong>Mode</strong> {_esc(mode_label)}</div>
  <div class="kv"><strong>Active ads found</strong> <strong>{count if count is not None else "?"}</strong></div>
  {noise_line}
  {partnership_line}
  <p><a href="{_esc(ads_url)}" class="btn-primary" target="_blank">Verify yourself in Ads Library →</a></p>
</section>
"""


def render_ad_card(ad: dict, idx: int, scan_dir: Path) -> str:
    fmt = ad.get("display_format") or "?"
    fmt_class = _format_badge_class(fmt)

    # Image section — inline as base64 data URL (standalone/shareable HTML)
    image_local = ad.get("image_local")
    if image_local:
        data_url = _image_to_data_url(scan_dir, image_local)
        if data_url:
            img_html = f'<img src="{data_url}" alt="Ad creative {idx}">'
        else:
            img_html = '<span class="no-img">Image file missing</span>'
    elif ad.get("video_url"):
        img_html = '<span class="no-img">Video creative<br>(not downloaded)</span>'
    else:
        img_html = '<span class="no-img">No image available</span>'

    # Variants badge
    n_variants = ad.get("n_card_variants") or 0
    variants_badge = ""
    if n_variants > 1:
        variants_badge = f'<span class="badge badge-variants">Carousel · {n_variants} variants</span>'

    # Text — skip the line if nothing meaningful
    title_html = _render_field(ad.get("title"), fmt)
    body_html = _render_field(ad.get("body_text"), fmt)

    title_block = f'<div class="ad-title">{title_html}</div>' if title_html else ""
    body_block = f'<div class="ad-body">{body_html}</div>' if body_html else ""

    # Meta
    link_url = ad.get("link_url") or ""
    link_desc = ad.get("link_description") or ""
    cta_type = (ad.get("cta_type") or "").upper()
    cta_text = ad.get("cta_text") or "Learn more"
    detail_url = ad.get("detail_url") or ""
    lib_id = ad.get("library_id") or ""
    started = _fmt_date(ad.get("start_date"))
    platforms = ad.get("platforms") or []
    platforms_str = " / ".join(PLATFORM_DISPLAY.get(p, p.title().replace("_", " ")) for p in platforms)

    # Destination: интерпретируем sentinel-значения. fb.me/ или пусто означает,
    # что объявление НЕ ведёт на внешний URL — это lead form / Messenger /
    # внутренний FB destination, зависит от cta_type.
    is_fb_internal = (link_url.rstrip("/").lower() in ("http://fb.me", "https://fb.me", "fb.me", "")
                       or link_url == "http://fb.me/")
    if is_fb_internal:
        fb_dest_label = {
            "MESSAGE_PAGE":     "Opens Messenger conversation with the page",
            "MESSENGER":        "Opens Messenger conversation",
            "SIGN_UP":          "Lead form (in-Facebook signup)",
            "GET_QUOTE":        "Lead form (in-Facebook quote request)",
            "LEARN_MORE":       "Stays on Facebook (lead form or page)",
            "SEE_DETAILS":      "Stays on Facebook (in-app detail view)",
            "WHATSAPP_MESSAGE": "Opens WhatsApp conversation",
            "":                 "Stays on Facebook (no external URL)",
        }.get(cta_type, "Stays on Facebook (in-app destination)")
        destination_html = f'<span class="placeholder">{_esc(fb_dest_label)}</span>'
        cta_link_html = f'<span class="cta-primary cta-disabled">{_esc(cta_text)} (in-FB)</span>'
    else:
        destination_html = f'<a href="{_esc(link_url)}" target="_blank">{_esc(link_url)}</a>'
        cta_link_html = f'<a href="{_esc(link_url)}" class="cta-primary" target="_blank">{_esc(cta_text)} →</a>'

    link_desc_html = ""
    if link_desc and not _is_template(link_desc):
        subtitle_rendered = _esc(link_desc)
        if fmt in ("DCO", "DPA"):
            subtitle_rendered += ' <span class="dco-note">(DCO example)</span>'
        link_desc_html = f'<div class="kv"><strong>Subtitle</strong> {subtitle_rendered}</div>'

    return f"""
<article class="ad-card">
  <div class="ad-image">{img_html}</div>
  <div class="ad-body-section">
    <div class="ad-badges">
      <span class="badge {fmt_class}">{_esc(fmt)}</span>
      {variants_badge}
    </div>
    {title_block}
    {body_block}
    <div class="ad-meta">
      <div class="kv"><strong>Destination</strong> {destination_html}</div>
      {link_desc_html}
      <div class="kv"><strong>Started</strong> {_esc(started)}</div>
      <div class="kv"><strong>Platforms</strong> {_esc(platforms_str)}</div>
    </div>
    <div class="ad-cta">
      {cta_link_html}
      <a href="{_esc(detail_url)}" class="cta-detail" target="_blank">View in Ads Library · ID {_esc(lib_id)} →</a>
    </div>
  </div>
</article>
"""


def render_ads_section(fb: dict, scan_dir: Path) -> str:
    accounts = fb.get("accounts") or []
    alive = [a for a in accounts if a.get("alive")]
    if not alive:
        return ""
    top = max(alive, key=lambda a: a.get("active_ads_count") or 0)
    ads = top.get("structured_ads") or []
    if not ads:
        return ""  # no ad cards to show — active_ads_count = 0
    total = top.get("active_ads_count") or len(ads)
    cards_html = "".join(render_ad_card(a, i + 1, scan_dir) for i, a in enumerate(ads))
    return f"""
<section id="ads">
  <h2>Top {len(ads)} ads</h2>
  <p class="note">Showing top <strong>{len(ads)}</strong> of <strong>{total}</strong> active ads — sorted by total impressions (FB's own ranking). These are what Facebook reports as the most-delivered creatives for this advertiser.</p>
  <div class="ads-grid">
    {cards_html}
  </div>
</section>
"""


def render_disclaimer(scan_date: str) -> str:
    return f"""
<section class="disclaimer">
  <h3>About this report</h3>
  <p>
    <strong>Snapshot date:</strong> {_esc(scan_date)}. Data was captured at this moment from
    <a href="https://www.facebook.com/ads/library/" target="_blank">Facebook Ads Library</a>,
    which is publicly available information about active ads on Facebook, Instagram, Messenger and Audience Network.
  </p>
  <p>
    <strong>Ads Library is live.</strong> The <em>"Verify yourself in Ads Library"</em> links in this report
    open Facebook's real-time Ads Library page. What you see there may differ from what is captured below
    — advertisers may have added new ads or ended running ones since this report was generated.
  </p>
  <p>
    <strong>Image URLs expire.</strong> Facebook's CDN image URLs (<code>scontent.fbcdn.net</code>) are signed
    and expire roughly 24–48 hours after they were fetched. The ad creatives embedded in this report were
    downloaded at snapshot time and are preserved in the file, so they remain visible indefinitely. However,
    clicking through to an individual ad's detail page (<em>"View in Ads Library · ID …"</em>) opens FB's live page —
    if the ad has since ended, it may show as "not available".
  </p>
</section>
"""


def render_raw_data(domain: str) -> str:
    return f"""
<section id="raw">
  <h2>Raw data</h2>
  <ul>
    <li><a href="{_esc(domain)}_step1.json">{_esc(domain)}_step1.json</a> — full sitemap scan result</li>
    <li><a href="fb.json">fb.json</a> — all FB account + Ads Library data</li>
    <li><a href="fb_ads_images/">fb_ads_images/</a> — downloaded ad creatives + ad_texts.txt</li>
  </ul>
</section>
"""


def render_glossary() -> str:
    return """
<section class="glossary">
  <strong>Glossary</strong>
  <dl>
    <dt>Library ID</dt>
    <dd>Facebook's unique identifier for each ad in the Ad Library archive. Use <code>facebook.com/ads/library/?id={Library ID}</code> to open the specific ad card directly.</dd>
    <dt>IMAGE</dt>
    <dd>Static image ad — fixed creative and copy.</dd>
    <dt>VIDEO</dt>
    <dd>Static video ad — fixed creative.</dd>
    <dt>DCO (Dynamic Creative Optimization)</dt>
    <dd>Facebook generates variants on the fly from the advertiser's catalog. In the stored JSON, text fields often show as template placeholders like <code>{{product.brand}}</code>. When a real user sees the ad, FB substitutes actual product info. In this report we pull a rendered example from <code>cards[]</code> when available.</dd>
    <dt>DPA (Dynamic Product Ad)</dt>
    <dd>Similar to DCO but tied specifically to a product catalog feed.</dd>
    <dt>Partnership / Branded Content</dt>
    <dd>Paid collaborations where the ad is published by a creator and sponsored by a brand.</dd>
    <dt>Platforms</dt>
    <dd>Surfaces where the ad runs: Facebook feed, Instagram, Messenger, Audience Network (3rd-party apps).</dd>
  </dl>
</section>
"""


# ─── Main ────────────────────────────────────────────────────────────────────

def build_report(domain: str) -> Path:
    log_debug(f"build_report: старт для домена {domain}")
    scan_dir = Path("scans") / domain
    step1_path = scan_dir / f"{domain}_step1.json"
    fb_path = scan_dir / "fb.json"

    if not step1_path.exists():
        raise FileNotFoundError(f"step1.json not found: {step1_path}")

    log_debug(f"build_report: читаю step1 {step1_path}")
    with open(step1_path, encoding="utf-8") as f:
        step1 = json.load(f)

    # fb.json may be absent if no FB handles were found on homepage
    # (common cases: site blocks requests with 403, or truly has no FB).
    if fb_path.exists():
        log_debug(f"build_report: читаю fb.json {fb_path}")
        with open(fb_path, encoding="utf-8") as f:
            fb = json.load(f)
    else:
        log_debug(f"build_report: fb.json отсутствует ({fb_path}) — пустой stub")
        fb = {"accounts": [], "site_country": "?", "site_country_source": "fb.json missing"}

    scan_date = _scan_date_iso(scan_dir)
    log_debug(f"build_report: snapshot date = {scan_date}")

    body_html = (
        render_header(domain, step1, fb, scan_date)
        + render_site_identity(step1, fb)
        + render_facebook(fb)
        + render_ads_search(fb)
        + render_ads_section(fb, scan_dir)
        + render_disclaimer(scan_date)
        + render_raw_data(domain)
        + render_glossary()
    )

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(domain)} — Ads Library Report</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="container">
    {body_html}
  </div>
</body>
</html>
"""
    out_path = scan_dir / f"{domain} — Ads Library Intelligence.html"
    log_debug(f"build_report: пишу отчёт ({len(full_html)} символов) → {out_path}")
    out_path.write_text(full_html, encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate per-site Ads Library HTML report")
    parser.add_argument("domains", nargs="*", help="Domains to generate reports for")
    parser.add_argument("--all", action="store_true",
                        help="Generate reports for all domains in scans/ that have fb.json")
    args = parser.parse_args()

    domains = list(args.domains)
    if args.all:
        # Все папки в scans/ где есть хотя бы step1.json
        for d in sorted(Path("scans").iterdir()):
            if d.is_dir() and (d / f"{d.name}_step1.json").exists():
                domains.append(d.name)
        domains = sorted(set(domains))

    if not domains:
        parser.print_help()
        sys.exit(1)

    log_debug(f"main: генерирую отчёты для {len(domains)} доменов")
    for dom in domains:
        log_debug(f"main: обрабатываю домен {dom}")
        try:
            out = build_report(dom)
            log_success(f"{dom} → {out}")
        except Exception as e:
            log_error(f"{dom}: {e}")


if __name__ == "__main__":
    main()
