"""
TNC Pipeline — Batch Summary Report (Single Standalone HTML)
=============================================================
Reads fb.json + step1.json from a list of scanned domains and produces ONE
HTML file containing:
  - Header + TL;DR + stat cards
  - Summary table — one row per site, "Open detail →" anchors to <details> below
  - Buckets — sites grouped by status
  - 16 collapsible <details> blocks — one per site, full ad creatives + meta
  - Disclaimer + glossary

Single self-contained file (~5-10 MB), all images embedded as base64. Folder is
just for organization (so future batches can live in their own folders).

Usage:
    python generate_batch_report.py \\
        --folder competitors_fr_2026-04-24 \\
        --title "16 FR Competitors" \\
        --file domains_fr_competitors.txt
"""

import sys
import json
import argparse
import base64
import html
from pathlib import Path
from datetime import datetime, timezone

# UTF-8 stdout
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# Импортируем рендереры individual report'а — переиспользуем для detail блоков
from generate_site_report import (
    CSS as DETAIL_CSS,
    render_site_identity,
    render_facebook,
    render_ads_search,
    render_ads_section,
    render_glossary,
    _esc as _esc_detail,
    _scan_date_iso,
    _image_to_data_url,
)


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _slug(domain: str) -> str:
    """domain → safe HTML id ('aerosus.fr' → 'aerosus-fr')."""
    return domain.replace(".", "-").replace("/", "-").replace(" ", "-").lower()


def _image_data_url_thumb(path: Path) -> str:
    """Read image, return base64 data URL. Empty string if missing."""
    if not path or not path.exists():
        return ""
    try:
        data = path.read_bytes()
        ext = path.suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/{ext};base64,{b64}"
    except Exception:
        return ""


def _load_site_data(domain: str) -> dict:
    """Read fb.json + step1.json for a domain. Returns dict for both summary + detail."""
    scan_dir = Path("scans") / domain
    out = {
        "domain": domain,
        "slug": _slug(domain),
        "scan_dir": scan_dir,
        "step1": None,
        "fb": None,
        "country": "?",
        "platform": "?",
        "lang": "?",
        "fb_handle": None,
        "fb_url": None,
        "active_ads": 0,
        "raw_total": 0,
        "ads_library_mode": None,
        "fetch_method": "success",
        "homepage_status": None,
        "fallback_attempted": False,
        "fallback_result": None,
        "top_image_local_path": None,
        "ads_count_status": "no_data",
    }

    step1_p = scan_dir / f"{domain}_step1.json"
    if step1_p.exists():
        try:
            with open(step1_p, encoding="utf-8") as f:
                s1 = json.load(f)
            out["step1"] = s1
            pi = s1.get("platform") or {}
            out["platform"] = pi.get("platform", "?") if isinstance(pi, dict) else str(pi)
            li = s1.get("site_language") or {}
            out["lang"] = li.get("lang", "?") if isinstance(li, dict) else str(li)
        except Exception:
            pass

    fb_p = scan_dir / "fb.json"
    if fb_p.exists():
        try:
            with open(fb_p, encoding="utf-8") as f:
                fb = json.load(f)
            out["fb"] = fb
            out["country"] = fb.get("site_country", "?")
            meta = fb.get("discovery_meta") or {}
            out["fetch_method"] = meta.get("homepage_fetch_method", "success")
            out["homepage_status"] = meta.get("homepage_status")
            out["fallback_attempted"] = meta.get("fallback_attempted", False)
            out["fallback_result"] = meta.get("fallback_result")
            accounts = fb.get("accounts") or []
            alive = [a for a in accounts if a.get("alive")]
            if alive:
                top = max(alive, key=lambda a: a.get("active_ads_count") or 0)
                out["fb_handle"] = top.get("handle")
                out["fb_url"] = top.get("url")
                out["active_ads"] = top.get("active_ads_count") or 0
                out["raw_total"] = top.get("raw_keyword_total") or 0
                out["ads_library_mode"] = top.get("ads_library_mode")
                structured = top.get("structured_ads") or []
                if structured and structured[0].get("image_local"):
                    out["top_image_local_path"] = scan_dir / structured[0]["image_local"]
        except Exception:
            pass

    if out["fetch_method"] == "blocked_by_waf":
        out["ads_count_status"] = "blocked_by_waf"
    elif out["active_ads"] > 0:
        out["ads_count_status"] = "active"
    elif out["fb_handle"]:
        out["ads_count_status"] = "fb_no_ads"
    else:
        out["ads_count_status"] = "no_fb"

    return out


# ─── CSS (overrides + additions on top of detail CSS) ────────────────────────

BATCH_CSS = """
/* Container width — wider for table */
.container { max-width: 1200px; }

/* TL;DR + stat cards */
header .tldr {
  margin-top: 16px;
  padding: 14px 18px;
  background: #e7f3ff;
  border-left: 4px solid #1877f2;
  border-radius: 4px;
  font-size: 15px;
  line-height: 1.5;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 32px;
}
.stat-card {
  background: #fff;
  border: 1px solid #e4e6eb;
  border-radius: 8px;
  padding: 16px;
  text-align: center;
}
.stat-card .num {
  font-size: 28px;
  font-weight: 700;
  color: #1c1e21;
  line-height: 1.1;
}
.stat-card .label {
  font-size: 12px;
  color: #65676b;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin-top: 4px;
}

/* Summary table */
table.summary {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #e4e6eb;
}
table.summary thead { background: #f0f2f5; }
table.summary thead th {
  padding: 10px 12px;
  text-align: left;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #65676b;
  border-bottom: 1px solid #e4e6eb;
}
table.summary tbody td {
  padding: 12px;
  border-bottom: 1px solid #f0f2f5;
  font-size: 14px;
  vertical-align: middle;
}
table.summary tbody tr:last-child td { border-bottom: none; }
table.summary tbody tr:hover { background: #f9fafb; }

.thumb {
  width: 70px;
  height: 70px;
  border-radius: 4px;
  object-fit: cover;
  display: block;
}
.thumb-empty {
  width: 70px;
  height: 70px;
  background: #f0f2f5;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #b0b3b8;
  font-size: 11px;
}
.ads-count {
  font-size: 18px;
  font-weight: 700;
}
.ads-count.zero { color: #b0b3b8; font-weight: 400; }
.ads-count.high { color: #1877f2; }

.detail-link {
  display: inline-block;
  padding: 5px 12px;
  background: #1877f2;
  color: #fff !important;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  text-decoration: none !important;
}
.detail-link:hover { background: #166fe5; }
.detail-link.disabled {
  background: #e4e6eb;
  color: #8a8d91 !important;
  pointer-events: none;
}

/* Status badges */
.status-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  white-space: nowrap;
}
.status-active     { background: #d4edda; color: #155724; }
.status-fb_no_ads  { background: #fff3cd; color: #856404; }
.status-no_fb      { background: #f0f2f5; color: #65676b; }
.status-blocked_by_waf { background: #f8d7da; color: #721c24; }

/* Buckets */
.bucket-line {
  margin: 8px 0;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid #e4e6eb;
  border-radius: 6px;
  font-size: 14px;
  line-height: 1.5;
}
.bucket-line strong { display: inline-block; min-width: 240px; }
.bucket-line .sites { color: #65676b; font-size: 13px; }

/* Expand/Collapse toolbar */
.detail-controls {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.detail-controls button {
  padding: 6px 14px;
  background: #fff;
  border: 1px solid #ccd0d5;
  border-radius: 4px;
  font-size: 13px;
  font-weight: 600;
  color: #1c1e21;
  cursor: pointer;
  font-family: inherit;
}
.detail-controls button:hover { background: #f5f5f5; }

/* <details> sections */
details.site-detail {
  background: #fff;
  border: 1px solid #e4e6eb;
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
  scroll-margin-top: 16px;
}
details.site-detail[open] {
  border-color: #1877f2;
  box-shadow: 0 2px 8px rgba(24, 119, 242, 0.08);
}
details.site-detail > summary {
  padding: 14px 18px;
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 15px;
  user-select: none;
  background: #fafafa;
  border-bottom: 1px solid #e4e6eb;
  transition: background 0.1s;
}
details.site-detail:not([open]) > summary { border-bottom-color: transparent; }
details.site-detail > summary:hover { background: #f0f2f5; }
details.site-detail > summary::-webkit-details-marker { display: none; }
details.site-detail > summary::before {
  content: "▶";
  font-size: 10px;
  color: #65676b;
  transition: transform 0.15s;
  display: inline-block;
}
details.site-detail[open] > summary::before {
  transform: rotate(90deg);
}
.summary-domain {
  font-weight: 700;
  color: #1c1e21;
}
.summary-ads {
  font-weight: 600;
  color: #1877f2;
}
.summary-ads.zero { color: #b0b3b8; }
.summary-meta {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}

.detail-body {
  padding: 18px 22px 24px;
}
.detail-body section { margin-bottom: 28px; }
.detail-body section:last-child { margin-bottom: 0; }
.detail-body section > h2 {
  font-size: 17px;
  margin: 0 0 12px 0;
  border-bottom: 1px solid #e4e6eb;
  padding-bottom: 6px;
}

.back-to-top {
  display: inline-block;
  margin: 24px 0 4px;
  font-size: 15px;
  font-weight: 600;
  color: #1877f2;
  padding: 10px 18px;
  background: #f0f2f5;
  border-radius: 6px;
  text-decoration: none;
}
.back-to-top:hover {
  background: #e7f3ff;
  text-decoration: none;
}

/* Disclaimer (reused at bottom) */
.disclaimer {
  margin-top: 48px;
  padding: 18px 22px;
  background: #fafafa;
  border: 1px solid #e4e6eb;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.55;
  color: #65676b;
}
.disclaimer h3 { margin: 0 0 8px 0; font-size: 14px; font-weight: 600; color: #1c1e21; }
.disclaimer p { margin: 6px 0; }
.disclaimer strong { color: #1c1e21; }
.disclaimer code { font-size: 11px; }
"""

# Минимальный JS — авто-открывать <details> при навигации по hash
JS_AUTO_OPEN = """
function openHashed() {
  const hash = location.hash;
  if (!hash || hash.length < 2) return;
  try {
    const el = document.querySelector(hash);
    if (el && el.tagName === 'DETAILS') {
      el.open = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  } catch (e) { /* ignore invalid selectors */ }
}
window.addEventListener('load', openHashed);
window.addEventListener('hashchange', openHashed);

function expandAll() {
  document.querySelectorAll('details.site-detail').forEach(d => d.open = true);
}
function collapseAll() {
  document.querySelectorAll('details.site-detail').forEach(d => d.open = false);
}
"""


# ─── Renderers ───────────────────────────────────────────────────────────────

def render_summary_header(title: str, scan_date: str, n: int, sites: list) -> str:
    by_status = {"active": [], "fb_no_ads": [], "no_fb": [], "blocked_by_waf": []}
    for s in sites:
        by_status[s["ads_count_status"]].append(s)

    n_active = len(by_status["active"])
    n_fb_total = n_active + len(by_status["fb_no_ads"])
    n_blocked = len(by_status["blocked_by_waf"])
    total_ads = sum(s["active_ads"] for s in sites)

    sorted_active = sorted(by_status["active"], key=lambda s: -s["active_ads"])[:3]
    if sorted_active:
        top3_str = ", ".join(
            f'<strong>{_esc(s["domain"])}</strong> ({s["active_ads"]})'
            for s in sorted_active
        )
        top3_total = sum(s["active_ads"] for s in sorted_active)
        share_pct = round(100 * top3_total / total_ads) if total_ads else 0
        tldr = (f"<strong>{n_active}</strong> of {n} sites running active Facebook ads. "
                f"Top three: {top3_str} hold "
                f"<strong>{top3_total} of {total_ads}</strong> ads ({share_pct}%).")
    else:
        tldr = f"<strong>0</strong> of {n} sites running active Facebook ads."

    return f"""
<header id="top">
  <h1>{_esc(title)}</h1>
  <div class="subtitle">Facebook Ads Library — Competitor Intelligence Report</div>
  <div class="subtitle">Snapshot: {_esc(scan_date)} · {n} sites scanned</div>
  <div class="tldr">{tldr}</div>
</header>

<div class="stats-grid">
  <div class="stat-card"><div class="num">{n}</div><div class="label">Sites scanned</div></div>
  <div class="stat-card"><div class="num">{n_fb_total}</div><div class="label">Have FB page</div></div>
  <div class="stat-card"><div class="num">{n_active}</div><div class="label">Running ads now</div></div>
  <div class="stat-card"><div class="num">{n_blocked}</div><div class="label">Blocked by WAF</div></div>
</div>
"""


def render_summary_table(sites: list) -> str:
    sorted_sites = sorted(sites, key=lambda s: -s["active_ads"])
    rows = []
    for i, s in enumerate(sorted_sites, 1):
        thumb_html = '<div class="thumb-empty">—</div>'
        if s.get("top_image_local_path"):
            data_url = _image_data_url_thumb(s["top_image_local_path"])
            if data_url:
                thumb_html = f'<img src="{data_url}" class="thumb" alt="creative">'

        ads = s["active_ads"]
        ads_class = "high" if ads >= 10 else ("zero" if ads == 0 else "")
        ads_html = f'<div class="ads-count {ads_class}">{ads}</div>'
        if s["raw_total"] and s["raw_total"] != ads:
            ads_html += f'<div style="font-size:11px;color:#8a8d91;">filtered from {s["raw_total"]}</div>'

        status_label = {
            "active":         "Active",
            "fb_no_ads":      "FB · 0 ads",
            "no_fb":          "No FB",
            "blocked_by_waf": "WAF blocked",
        }.get(s["ads_count_status"], "?")

        # Anchor link → opens <details> via JS
        detail_link = (f'<a href="#detail-{_esc(s["slug"])}" class="detail-link">Open detail →</a>'
                       if s.get("fb") else '<span class="detail-link disabled">No detail</span>')

        handle_cell = "—"
        if s.get("fb_handle"):
            display_handle = s["fb_handle"]
            if len(display_handle) > 25:
                display_handle = display_handle[:22] + "…"
            handle_cell = f'<code>{_esc(display_handle)}</code>'

        rows.append(f"""
<tr>
  <td>{i}</td>
  <td>{thumb_html}</td>
  <td><a href="{_esc('https://' + s['domain'])}" target="_blank">{_esc(s['domain'])}</a></td>
  <td>{_esc(s['country'])}</td>
  <td>{_esc(s['platform'])}</td>
  <td>{handle_cell}</td>
  <td>{ads_html}</td>
  <td><span class="status-badge status-{s['ads_count_status']}">{_esc(status_label)}</span></td>
  <td>{detail_link}</td>
</tr>
""")

    return f"""
<section>
  <h2>All {len(sites)} sites</h2>
  <table class="summary">
    <thead>
      <tr>
        <th>#</th><th>Top creative</th><th>Site</th><th>Country</th>
        <th>Platform</th><th>FB handle</th><th>Ads</th><th>Status</th><th></th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</section>
"""


def render_buckets(sites: list) -> str:
    by_status = {"active": [], "fb_no_ads": [], "no_fb": [], "blocked_by_waf": []}
    for s in sites:
        by_status[s["ads_count_status"]].append(s)

    def render_one(label: str, status_key: str) -> str:
        bucket = sorted(by_status[status_key], key=lambda s: -s["active_ads"])
        if not bucket:
            return ""
        items = []
        for s in bucket:
            link = f'<a href="#detail-{_esc(s["slug"])}">{_esc(s["domain"])}</a>'
            if s["active_ads"] > 0:
                link += f" ({s['active_ads']})"
            items.append(link)
        return (f'<div class="bucket-line"><strong>{_esc(label)} ({len(bucket)})</strong>'
                f'<span class="sites">{", ".join(items)}</span></div>')

    return f"""
<section>
  <h2>Breakdown by status</h2>
  {render_one('Active advertisers', 'active')}
  {render_one('FB exists, 0 active ads', 'fb_no_ads')}
  {render_one('No FB link on site', 'no_fb')}
  {render_one('Blocked by WAF', 'blocked_by_waf')}
</section>
"""


def render_detail_block(s: dict) -> str:
    """Render a <details> block containing full per-site detail content."""
    domain = s["domain"]
    slug = s["slug"]
    step1 = s["step1"] or {}
    fb = s["fb"] or {"accounts": [], "site_country": "?"}
    scan_dir = s["scan_dir"]

    # Summary line for the <summary> element
    ads = s["active_ads"]
    ads_html_class = "zero" if ads == 0 else ""
    status_label = {
        "active":         "Active",
        "fb_no_ads":      "FB · 0 ads",
        "no_fb":          "No FB",
        "blocked_by_waf": "WAF blocked",
    }.get(s["ads_count_status"], "?")
    handle_str = ""
    if s.get("fb_handle"):
        h = s["fb_handle"]
        if len(h) > 30:
            h = h[:27] + "…"
        handle_str = f'<code>{_esc(h)}</code>'

    # Full detail body (re-using individual report renderers)
    detail_body = (
        render_site_identity(step1, fb)
        + render_facebook(fb)
        + render_ads_search(fb)
        + render_ads_section(fb, scan_dir)
    )

    return f"""
<details class="site-detail" id="detail-{_esc(slug)}">
  <summary>
    <span class="summary-domain">{_esc(domain)}</span>
    <span class="summary-ads {ads_html_class}">{ads} ads</span>
    {handle_str}
    <span class="summary-meta">
      <span class="status-badge status-{s['ads_count_status']}">{_esc(status_label)}</span>
    </span>
  </summary>
  <div class="detail-body">
    {detail_body}
    <a href="#top" class="back-to-top">↑ Back to summary</a>
  </div>
</details>
"""


def render_disclaimer(scan_date: str) -> str:
    return f"""
<section class="disclaimer">
  <h3>About this report</h3>
  <p>
    <strong>Snapshot:</strong> {_esc(scan_date)}. Data captured from the public
    <a href="https://www.facebook.com/ads/library/" target="_blank">Facebook Ads Library</a>.
    Each site has a collapsible detail section (click "Open detail" or the row in the list above)
    showing up to 10 active ad creatives with paired text, CTA, and landing URL. All images are
    embedded directly in this file — Facebook's CDN URLs themselves expire ~24-48h after fetch.
  </p>
  <p>
    <strong>Ads Library is live.</strong> Clicking through to "Verify yourself" links may show
    different results than what is captured here — advertisers may have added or ended ads since
    this snapshot was taken.
  </p>
  <p>
    <strong>WAF-blocked sites:</strong> when a site's WAF (Cloudflare/Akamai/etc.) blocks both our
    HTTP scanner and a headless-browser fallback, we mark the site as <code>blocked_by_waf</code>
    and recommend manual verification. We do not use stealth plugins, residential proxies, or
    captcha solvers — these are explicitly out of scope.
  </p>
</section>
"""


def build_report_html(sites: list, title: str, scan_date: str) -> str:
    n = len(sites)
    sorted_sites = sorted(sites, key=lambda s: -s["active_ads"])

    detail_blocks = "\n".join(render_detail_block(s) for s in sorted_sites)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)} — Competitor Intelligence</title>
  <style>
    {DETAIL_CSS}
    {BATCH_CSS}
  </style>
</head>
<body>
  <div class="container">
    {render_summary_header(title, scan_date, n, sites)}
    {render_summary_table(sites)}
    {render_buckets(sites)}

    <section>
      <h2>Per-site details</h2>
      <div class="detail-controls">
        <button onclick="expandAll()">Expand all</button>
        <button onclick="collapseAll()">Collapse all</button>
      </div>
      {detail_blocks}
    </section>

    {render_disclaimer(scan_date)}
    {render_glossary()}
  </div>

  <script>
{JS_AUTO_OPEN}
  </script>
</body>
</html>
"""


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate single-file batch report")
    parser.add_argument("--folder", required=True,
                        help="Batch folder name under scans/ (e.g., 'competitors_fr_2026-04-24')")
    parser.add_argument("--title", default="Competitor Intelligence",
                        help="Report title (e.g., '16 FR Competitors')")
    parser.add_argument("--domains", nargs="*", help="List of domains")
    parser.add_argument("--file", "-f", help="File with domains (one per line)")
    args = parser.parse_args()

    domains = list(args.domains or [])
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.append(line)
    if not domains:
        parser.print_help()
        sys.exit(1)

    seen = set()
    uniq = []
    for d in domains:
        d = d.strip().lower()
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    domains = uniq

    batch_dir = Path("scans") / args.folder
    batch_dir.mkdir(parents=True, exist_ok=True)

    sites = [_load_site_data(d) for d in domains]

    # Scan date — most recent fb.json mtime
    scan_dates = [s["scan_dir"].joinpath("fb.json").stat().st_mtime
                  for s in sites if s["scan_dir"].joinpath("fb.json").exists()]
    if scan_dates:
        scan_date = datetime.fromtimestamp(max(scan_dates), tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"📦 Building single-file batch report → {batch_dir}")
    print(f"   Title: {args.title}")
    print(f"   Sites: {len(sites)}")
    print(f"   Snapshot date: {scan_date}")
    print()

    html_doc = build_report_html(sites, args.title, scan_date)
    out_filename = f"{args.title}.html"
    out_path = batch_dir / out_filename
    out_path.write_text(html_doc, encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024
    size_mb = size_kb / 1024
    if size_mb >= 1:
        print(f"✅ Saved: {out_path}  ({size_mb:.1f} MB)")
    else:
        print(f"✅ Saved: {out_path}  ({size_kb} KB)")


if __name__ == "__main__":
    main()
