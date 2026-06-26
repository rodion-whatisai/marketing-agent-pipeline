"""
HTML-репорт по результатам fb_scan.py (Steps 1→5).
Читает scans/{domain}/fb_deep_summary.json + per-ad scans/{domain}/fb_deep/{status}/{lib_id}.json

Структура отчёта:
  - Список доменов с brand-card (display_name, handle, country, totals)
  - Per-domain ad table (rank, library_id, status, reach, country, advertiser, payer, body)
  - Drilldown по клику на строку (демография таблицей, lead-form items)

Standalone:
    python fb_deep_report.py aerosus.fr redacted-client.example mannes.fr
    python fb_deep_report.py --auto    # все домены где есть fb_deep_summary.json
"""
import sys, json, html as htmllib, argparse, webbrowser
from pathlib import Path

from utils import setup_console
from log import log_info, log_error, log_success, log_debug
setup_console()


def _esc(s):
    return htmllib.escape(str(s)) if s is not None else ""


def _load_summary(domain: str):
    log_debug(f"_load_summary: domain={domain}")
    p = Path("scans") / domain / "fb_deep_summary.json"
    if not p.exists():
        log_debug(f"_load_summary: нет файла {p}")
        return None
    log_debug(f"_load_summary: читаю {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _load_ad_json(saved_path: str):
    log_debug(f"_load_ad_json: saved_path={saved_path}")
    p = Path(saved_path)
    if not p.exists():
        log_debug(f"_load_ad_json: нет файла {p}")
        return None
    log_debug(f"_load_ad_json: читаю {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _render_ad_row(rank, status, summary_row, ad_data):
    """Одна строка таблицы + скрытый drilldown."""
    log_debug(f"_render_ad_row: rank={rank} status={status} lib_id={summary_row.get('library_id')}")
    lib_id = summary_row["library_id"]
    reach = summary_row.get("total_reach")
    reach_str = f"{reach:,}" if reach else "—"
    sections = summary_row.get("sections", [])
    sec_count = f"{len(sections)}/5"

    body = (ad_data.get("meta", {}).get("body") or "")[:120]
    started = ad_data.get("meta", {}).get("started_running", "")
    advertiser = (ad_data.get("advertiser") or {}).get("name", "")
    handle = (ad_data.get("advertiser") or {}).get("handle", "")
    followers = (ad_data.get("advertiser") or {}).get("followers")
    foll_str = f"{followers:,}" if followers else "—"
    disclaim = ad_data.get("disclaimer") or {}
    payer_block = ad_data.get("advertiser_payer") or {}
    real_payer = payer_block.get("current_payer") or disclaim.get("payer", "—")
    transp = ad_data.get("transparency") or {}
    countries = ", ".join(transp.get("country_targets", []) or []) or "—"
    age = transp.get("age_range") or "—"
    gender = transp.get("gender_target") or "—"
    versions = ad_data.get("meta", {}).get("multiple_versions", 1)
    add_assets = ad_data.get("additional_assets") or {}
    has_lead = "✅" if (add_assets.get("text_items") or []) else "—"

    status_badge = ('<span class="badge active">active</span>'
                    if status == "active"
                    else '<span class="badge inactive">archive</span>')

    # Drilldown content
    demos = transp.get("demographics") or []
    demo_rows = "".join(
        f"<tr><td>{_esc(d['location'])}</td><td>{_esc(d['age'])}</td>"
        f"<td>{_esc(d['gender'])}</td><td class='r'>{d['reach']:,}</td></tr>"
        for d in demos
    )
    demo_html = (f"<table class='demo'><thead><tr><th>Country</th><th>Age</th>"
                 f"<th>Gender</th><th>Reach</th></tr></thead>"
                 f"<tbody>{demo_rows}</tbody></table>" if demos else "<i>no demographics</i>")

    log_debug(f"_render_ad_row: lib_id={lib_id} demos={len(demos)} versions={versions} has_lead={has_lead}")

    # Lead form
    lead_items = (add_assets.get("text_items") or [])[:20]
    lead_links = (add_assets.get("links") or [])[:10]
    lead_html = ""
    if lead_links or lead_items:
        log_debug(f"_render_ad_row: lib_id={lib_id} lead-form items={len(lead_items)} links={len(lead_links)}")
        links_html = "<ul>" + "".join(f"<li><a href='{_esc(u)}'>{_esc(u)}</a></li>"
                                       for u in lead_links) + "</ul>"
        items_html = "<ul>" + "".join(f"<li>{_esc(t)}</li>" for t in lead_items) + "</ul>"
        lead_html = (f"<details><summary>Lead-form ({len(lead_items)} items, "
                     f"{len(lead_links)} links)</summary>"
                     f"<b>Links:</b>{links_html}<b>Text:</b>{items_html}</details>")

    drilldown = f"""
    <tr class="drill" id="drill-{status}-{rank}" style="display:none">
      <td colspan="11">
        <div class="dd-grid">
          <div><b>Body</b><div class="body">{_esc(body)}</div></div>
          <div><b>Disclaimer</b><br>
            location: {_esc(disclaim.get('location') or '—')}<br>
            website: <a href="{_esc(disclaim.get('website',''))}">{_esc(disclaim.get('website') or '—')}</a><br>
            advertiser: {_esc(disclaim.get('advertiser') or '—')}<br>
            payer: {_esc(disclaim.get('payer') or '—')}
          </div>
          <div><b>Real Payer (юр. лицо)</b><br>
            {_esc(payer_block.get('current_advertiser','—'))}<br>
            <small>payer: {_esc(payer_block.get('current_payer','—'))}</small>
          </div>
          <div><b>Demographics ({len(demos)} rows)</b>{demo_html}</div>
        </div>
        {lead_html}
      </td>
    </tr>
    """

    return f"""
    <tr class="row" onclick="document.getElementById('drill-{status}-{rank}').style.display=
        document.getElementById('drill-{status}-{rank}').style.display=='none'?'table-row':'none'">
      <td>{rank}</td>
      <td><code>{_esc(lib_id)}</code></td>
      <td>{status_badge}</td>
      <td class="r">{reach_str}</td>
      <td>{_esc(countries)}</td>
      <td>{_esc(age)}</td>
      <td>{_esc(gender)}</td>
      <td>{_esc(advertiser)}<br><small>@{_esc(handle)} · {foll_str}f</small></td>
      <td>{_esc(real_payer)}</td>
      <td>{versions} · {has_lead}</td>
      <td><small>{_esc(started)}</small><br>{sec_count}</td>
    </tr>
    {drilldown}
    """


def _render_domain(domain: str):
    log_debug(f"_render_domain: domain={domain}")
    s = _load_summary(domain)
    if not s:
        log_debug(f"_render_domain: нет summary для {domain} — рендерю заглушку")
        return f"<section><h2>{_esc(domain)}</h2><p><i>нет fb_deep_summary.json</i></p></section>"

    pages = s.get("step1_pages", [])
    alive = [p for p in pages if p.get("alive")]
    listing = s.get("step2_listing") or {}
    page0 = alive[0] if alive else {}
    total = listing.get("total_ever") or 0
    active = (listing.get("active") or {}).get("count") or 0
    inactive = (listing.get("inactive") or {}).get("count") or 0
    log_debug(f"_render_domain: {domain} pages={len(pages)} alive={len(alive)} total={total} active={active} inactive={inactive}")

    # Brand card
    card = f"""
    <div class="card">
      <h2>{_esc(domain)}</h2>
      <table class="meta">
        <tr><td>Display name</td><td><b>{_esc(page0.get('display_name') or '—')}</b></td></tr>
        <tr><td>Handle</td><td>@{_esc(page0.get('handle') or '—')}</td></tr>
        <tr><td>FB URL</td><td><a href="{_esc(page0.get('fb_url',''))}">{_esc(page0.get('fb_url') or '—')}</a></td></tr>
        <tr><td>Country</td><td>{_esc(page0.get('country') or '—')} <small>({_esc(page0.get('country_source') or '')})</small></td></tr>
        <tr><td>Ads (total ever)</td><td><b>{total}</b></td></tr>
        <tr><td>Ads active / archive</td><td>✅ {active} / 📦 {inactive}</td></tr>
        <tr><td>Scan duration</td><td>{s.get('duration_s')}s</td></tr>
      </table>
    </div>
    """

    # Combine deep_active + deep_inactive into one table
    rows = []
    for status, key in [("active", "deep_active"), ("inactive", "deep_inactive")]:
        log_debug(f"_render_domain: {domain} рендерю {status} из ключа {key}: {len(s.get(key) or [])} ad'ов")
        for row in (s.get(key) or []):
            ad = _load_ad_json(row.get("saved", "")) or {}
            rows.append(_render_ad_row(row["rank"], status, row, ad))

    if not rows:
        log_debug(f"_render_domain: {domain} нет deep-scanned ads")
        ads_html = "<p><i>deep-scanned ads нет</i></p>"
    else:
        ads_html = f"""
        <table class="ads">
          <thead><tr>
            <th>#</th><th>Library ID</th><th>Status</th><th>EU Reach</th>
            <th>Country</th><th>Age</th><th>Gender</th>
            <th>Advertiser</th><th>Real Payer</th><th>Vers · Lead</th>
            <th>Started · Sections</th>
          </tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    return f"<section>{card}{ads_html}</section>"


def render_html(domains: list, out_path: Path) -> Path:
    log_debug(f"render_html: domains={domains} out_path={out_path}")
    sections = "\n".join(_render_domain(d) for d in domains)
    css = """
    body{font:13px/1.4 -apple-system,sans-serif;margin:20px;background:#f5f5f7;color:#1d1d1f}
    h1{margin:0 0 16px}
    section{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
    .card{display:flex;gap:24px;align-items:start;flex-wrap:wrap}
    .card h2{margin:0 0 12px;font-size:18px}
    table.meta{border-collapse:collapse;font-size:12px;margin-right:30px}
    table.meta td{padding:3px 10px 3px 0}
    table.ads{width:100%;border-collapse:collapse;margin-top:14px;font-size:12px}
    table.ads th{background:#fafafa;padding:6px 8px;text-align:left;border-bottom:1px solid #e0e0e0;font-weight:600}
    table.ads td{padding:6px 8px;border-bottom:1px solid #f0f0f0;vertical-align:top}
    tr.row{cursor:pointer}
    tr.row:hover{background:#f8f9fb}
    tr.drill td{background:#fafbfc;padding:14px}
    .dd-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:10px}
    .dd-grid b{display:block;margin-bottom:4px;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.4px}
    .body{font-style:italic;color:#333;max-width:300px}
    .badge{padding:2px 8px;border-radius:3px;font-size:10px;font-weight:600;text-transform:uppercase}
    .badge.active{background:#d4edda;color:#155724}
    .badge.inactive{background:#fff3cd;color:#856404}
    .r{text-align:right;font-variant-numeric:tabular-nums}
    table.demo{width:100%;font-size:11px;border-collapse:collapse;margin-top:6px}
    table.demo th{background:#eef0f3;padding:3px 6px;font-weight:600}
    table.demo td{padding:2px 6px;border-bottom:1px solid #ececec}
    code{font-family:Menlo,monospace;font-size:11px;color:#666}
    details{margin-top:10px;padding:8px;background:#fff;border:1px solid #ececec;border-radius:5px}
    details summary{cursor:pointer;font-weight:600;color:#444}
    details ul{margin:6px 0;padding-left:20px}
    """
    page = f"""<!doctype html><html><head><meta charset="utf-8">
    <title>FB Ads Deep Report</title><style>{css}</style></head>
    <body><h1>FB Ads Deep Report — {len(domains)} sites</h1>{sections}</body></html>"""
    out_path.write_text(page, encoding="utf-8")
    log_debug(f"render_html: записал {out_path}")
    return out_path


# ─── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*", help="domains to render")
    ap.add_argument("--auto", action="store_true",
                    help="взять все домены где есть scans/<d>/fb_deep_summary.json")
    ap.add_argument("--out", default="scans/_reports/fb_deep_report.html")
    ap.add_argument("--open", action="store_true", help="открыть в браузере")
    args = ap.parse_args()

    domains = list(args.domains)
    if args.auto:
        log_debug("standalone: --auto, ищу домены с fb_deep_summary.json в scans/")
        for p in Path("scans").glob("*/fb_deep_summary.json"):
            d = p.parent.name
            if d not in domains:
                log_debug(f"standalone: --auto добавил домен {d}")
                domains.append(d)
    if not domains:
        log_error("Нет доменов. Передай domain'ы или --auto")
        sys.exit(1)

    log_info(f"  Render: {domains}")
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    render_html(domains, out)
    log_success(f"{out}", emoji="💾")
    if args.open:
        log_debug(f"standalone: открываю в браузере {out.as_uri()}")
        webbrowser.open(out.as_uri())
