"""
Orchestrator: end-to-end FB scan для одного домена.
Связывает Steps 1→2→3→4→5:
  Step 1  fb_page_finder    domain → FB pages + Ad Library URLs
  Step 2  fb_ads_listing    URLs → 3-pass listing → top-N library_ids per status
  Step 3  fb_ad_modal_open  library_id → opened modal
  Step 4  fb_ad_modal_expand opened modal → expanded sections
  Step 5  fb_ad_modal_parse expanded HTML → structured dict

Сохраняет per-ad JSON: scans/{domain}/fb_deep/{status}/{library_id}.json
Сохраняет summary:    scans/{domain}/fb_deep_summary.json

Standalone:
    python fb_scan.py aerosus.fr               # default top_n=5
    python fb_scan.py aerosus.fr --top 10
"""
import sys
import json
import time
import argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fb_page_finder import find_brand_pages
from fb_ads_listing import scrape_ads_listing
from fb_ad_modal_open import open_ad_modal
from fb_ad_modal_expand import expand_all_present_accordions
from fb_ad_modal_parse import parse_modal


def _save_ad_json(domain: str, status: str, library_id: str, data: dict):
    out_dir = Path("scans") / domain / "fb_deep" / status
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{library_id}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(p)


def deep_scan_one_ad(page, library_id: str, status: str, domain: str,
                      verbose: bool = True) -> dict:
    """Полный конвейер для одного объявления: open → expand → parse → save."""
    t0 = time.time()

    # Step 3: open
    open_res = open_ad_modal(library_id, page=page, verbose=verbose)
    if not open_res.get("success"):
        return {"library_id": library_id, "status": status, "success": False,
                "error": open_res.get("error", "open_failed")}

    # Step 4: expand
    exp_res = expand_all_present_accordions(page, verbose=verbose)

    # Step 5: parse
    parsed = parse_modal(exp_res["html"])
    parsed["library_id"]      = library_id  # из listing — самый надёжный
    parsed["status"]          = status
    parsed["expand_diag"]     = exp_res["diag"]
    parsed["scan_duration_s"] = round(time.time() - t0, 1)

    # Save
    saved_path = _save_ad_json(domain, status, library_id, parsed)
    parsed["_saved"] = saved_path

    return {"library_id": library_id, "status": status, "success": True,
            "data": parsed, "saved": saved_path}


def scan_domain(domain: str, top_n: int = 5, verbose: bool = True) -> dict:
    """End-to-end scan одного домена."""
    print(f"\n{'═' * 70}")
    print(f"  fb_scan: {domain}  (top_n={top_n})")
    print(f"{'═' * 70}")
    summary = {
        "domain":       domain,
        "top_n":        top_n,
        "step1_pages":  [],
        "step2_listing": None,
        "deep_active":  [],
        "deep_inactive":[],
        "errors":       [],
        "started_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s":   None,
    }
    t_start = time.time()

    # ── Step 1 ─────────────────────────────────────────────────────────
    print(f"\n→ STEP 1: find FB pages")
    pages = find_brand_pages(domain, verbose=verbose)
    summary["step1_pages"] = pages
    alive = [p for p in pages if p.get("alive")]
    if not alive:
        print(f"  ❌ нет живых FB страниц — стоп")
        summary["errors"].append("no_alive_fb_pages")
        return summary

    page0 = alive[0]
    print(f"  ✓ {page0['handle']} → display='{page0['display_name']}'")

    # ── Step 2 ─────────────────────────────────────────────────────────
    print(f"\n→ STEP 2: scrape Ad Library listing")
    listing = scrape_ads_listing(page0["ads_library_urls"],
                                  display_name=page0["display_name"],
                                  top_n=top_n, verbose=verbose)
    summary["step2_listing"] = listing
    if not listing.get("total_ever"):
        print(f"  ❌ нет объявлений (total_ever={listing.get('total_ever')})")
        summary["duration_s"] = round(time.time() - t_start, 1)
        return summary

    active_block   = listing.get("active") or {}
    inactive_block = listing.get("inactive") or {}
    active_ids   = active_block.get("library_ids") or []
    inactive_ids = inactive_block.get("library_ids") or []
    print(f"  ✓ active: {active_block.get('count')} ads, top-{len(active_ids)} ids")
    print(f"  ✓ inactive: {inactive_block.get('count')} ads, top-{len(inactive_ids)} ids")

    # ── Steps 3+4+5: deep-scan каждого ad в одном Playwright-сессии ───
    if not (active_ids or inactive_ids):
        summary["duration_s"] = round(time.time() - t_start, 1)
        return summary

    print(f"\n→ STEPS 3+4+5: deep-scan {len(active_ids)+len(inactive_ids)} ads")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        modal_page = ctx.new_page()

        for status, ids, bucket in [("active", active_ids, summary["deep_active"]),
                                      ("inactive", inactive_ids, summary["deep_inactive"])]:
            for i, lib_id in enumerate(ids, 1):
                print(f"\n  [{status} {i}/{len(ids)}] {lib_id}")
                res = deep_scan_one_ad(modal_page, lib_id, status, domain, verbose=verbose)
                if res.get("success"):
                    d = res["data"]
                    t = d["transparency"]
                    print(f"      ✓ reach={t.get('total_reach')} demos={len(t['demographics'])} "
                          f"sections={len(d['sections_present'])}/5 ({d['scan_duration_s']}s)")
                    bucket.append({
                        "library_id":    lib_id,
                        "rank":          i,
                        "saved":         res["saved"],
                        "total_reach":   t.get("total_reach"),
                        "demos_count":   len(t["demographics"]),
                        "sections":      d["sections_present"],
                        "scan_duration_s": d["scan_duration_s"],
                    })
                else:
                    print(f"      ❌ {res.get('error')}")
                    summary["errors"].append(f"{status}/{lib_id}: {res.get('error')}")

        browser.close()

    summary["duration_s"] = round(time.time() - t_start, 1)

    # ── Save summary ──────────────────────────────────────────────────
    out = Path("scans") / domain / "fb_deep_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    print(f"\n💾 summary: {out}")

    return summary


# ─── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("domain", help="domain (например aerosus.fr)")
    ap.add_argument("--top", type=int, default=5,
                    help="top-N ads per status (default 5)")
    ap.add_argument("--quiet", action="store_true", help="меньше логов")
    args = ap.parse_args()

    summary = scan_domain(args.domain, top_n=args.top, verbose=not args.quiet)

    print(f"\n{'═' * 70}")
    print(f"  DONE — duration {summary['duration_s']}s")
    print(f"{'═' * 70}")
    print(f"  active deep-scanned:   {len(summary['deep_active'])}")
    print(f"  inactive deep-scanned: {len(summary['deep_inactive'])}")
    print(f"  errors: {len(summary['errors'])}")
    if summary["errors"]:
        for e in summary["errors"][:5]:
            print(f"    - {e}")
