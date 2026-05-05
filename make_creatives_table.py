"""Flatten per-creative JSON files in scans/<domain>/google_creatives/ into a CSV table.

Usage:
    python make_creatives_table.py --domain suspenair.fr
    python make_creatives_table.py --aggregate           # all domains, single CSV
    python make_creatives_table.py --aggregate --out scans/_all_creatives.csv
"""
import argparse
import csv
import json
from pathlib import Path

from utils import SCANS_DIR

COLUMNS = [
    "domain",
    "creative_id",
    "advertiser_name",
    "advertiser_id",
    "advertiser_based_in",
    "format",
    "topic",
    "type_of_creative",
    "first_shown",
    "last_shown",
    "impressions_range_raw",
    "impressions_lower_bound",
    "impressions_upper_bound",
    "times_shown_start_date",
    "times_shown_end_date",
    "displayed_url",
    "has_image",
    "n_images",
    "n_variations",
    "ad_text",
    "ad_image_urls",
    "targeting",
    "fetch_error",
    "ad_link",
]

# Documented in CLAUDE.md: parser extracts only the default variation. When
# n_variations > 1, the other N-1 ad-copy variants are NOT in this table.
TABLE_NOTE = (
    "NOTE: ad_text column = default variation only. "
    "If n_variations > 1, only 1 of N variants is captured (rest visible in TC via ad_link). "
    "n_variations = None means single-variation ad (full text captured)."
)


def row_from_json(d: dict, domain: str = "") -> dict:
    targeting = d.get("targeting_categories") or []
    targeting_str = " | ".join(
        f"{t.get('sign', '')}{t.get('name', '')}" for t in targeting
    )
    ad_texts = d.get("ad_text_candidates") or []
    ad_text_str = " | ".join(ad_texts)
    images = d.get("ad_image_urls") or []
    return {
        "domain": domain or (d.get("_meta") or {}).get("domain", ""),
        "creative_id": d.get("creative_id", ""),
        "advertiser_name": d.get("advertiser_name", ""),
        "advertiser_id": d.get("advertiser_id", ""),
        "advertiser_based_in": d.get("advertiser_based_in", ""),
        "format": d.get("format", ""),
        "topic": d.get("topic", ""),
        "type_of_creative": d.get("type_of_creative", ""),
        "first_shown": d.get("first_shown", ""),
        "last_shown": d.get("last_shown", ""),
        "impressions_range_raw": d.get("impressions_range_raw", ""),
        "impressions_lower_bound": d.get("impressions_lower_bound", ""),
        "impressions_upper_bound": d.get("impressions_upper_bound", ""),
        "times_shown_start_date": d.get("times_shown_start_date", ""),
        "times_shown_end_date": d.get("times_shown_end_date", ""),
        "displayed_url": d.get("displayed_url", ""),
        "has_image": d.get("has_image", False),
        "n_images": len(images),
        "n_variations": d.get("n_variations") if d.get("n_variations") is not None else "",
        "ad_text": ad_text_str,
        "ad_image_urls": " | ".join(images),
        "targeting": targeting_str,
        "fetch_error": d.get("fetch_error") or "",
        "ad_link": d.get("ad_link", ""),
    }


def collect_rows(domains: list[str]) -> tuple[list[dict], int]:
    """Read CR*.json from each domain dir, return (rows, n_parse_errors)."""
    rows = []
    n_err = 0
    for domain in domains:
        src = SCANS_DIR / domain / "google_creatives"
        if not src.exists():
            continue
        for fp in sorted(src.glob("CR*.json")):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                rows.append(row_from_json(d, domain=domain))
            except Exception as e:
                n_err += 1
                print(f"  ! skip {fp.name}: {e}")
    return rows, n_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", help="Single domain (e.g. suspenair.fr)")
    ap.add_argument("--aggregate", action="store_true",
                    help="All domains under scans/* with google_creatives/ subdir")
    ap.add_argument("--out", default=None, help="CSV output path")
    args = ap.parse_args()

    if not args.domain and not args.aggregate:
        raise SystemExit("Specify either --domain <name> or --aggregate")
    if args.domain and args.aggregate:
        raise SystemExit("--domain and --aggregate are mutually exclusive")

    if args.domain:
        domains = [args.domain]
        default_out = SCANS_DIR / args.domain / "_creatives_table.csv"
        source_label = f"scans/{args.domain}/google_creatives/*.json"
    else:
        # Auto-discover domains under scans/*/google_creatives/
        domains = sorted([
            p.parent.name for p in SCANS_DIR.glob("*/google_creatives")
            if not p.parent.name.startswith("_") and p.is_dir()
        ])
        default_out = SCANS_DIR / "_all_creatives_table.csv"
        source_label = f"scans/<{len(domains)} domains>/google_creatives/*.json"

    rows, n_err = collect_rows(domains)
    if not rows:
        raise SystemExit(f"No CR*.json found in {len(domains)} domain(s)")

    rows.sort(key=lambda r: (r["domain"] or "", r["advertiser_name"] or "", r["creative_id"] or ""))

    out_path = Path(args.out) if args.out else default_out
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # Sidecar README + summary stats
    n_with_text = sum(1 for r in rows if r["ad_text"])
    n_with_image = sum(1 for r in rows if r["ad_image_urls"])
    n_multivar = sum(1 for r in rows if r["n_variations"] not in ("", None))
    n_text_in_image = sum(1 for r in rows if r["fetch_error"] == "text_in_image")
    n_iframe_missing = sum(1 for r in rows if r["fetch_error"] == "iframe_missing")
    fmts = {}
    for r in rows:
        fmts[r["format"] or "?"] = fmts.get(r["format"] or "?", 0) + 1
    by_domain = {}
    for r in rows:
        by_domain[r["domain"]] = by_domain.get(r["domain"], 0) + 1

    readme_path = out_path.with_suffix(".README.txt")
    readme_lines = [
        TABLE_NOTE,
        "",
        f"Source: {source_label}",
        f"Rows: {len(rows)} (one per creative_id, default variation only)",
        f"  with ad_text:                {n_with_text} ({n_with_text/len(rows)*100:.1f}%)",
        f"  with ad_image_urls:          {n_with_image} ({n_with_image/len(rows)*100:.1f}%)",
        f"  multi-variant (partial):     {n_multivar}",
        f"  text_in_image (OCR needed):  {n_text_in_image}",
        f"  iframe_missing (residual):   {n_iframe_missing}",
        "",
        "Format mix:",
    ]
    for fmt, n in sorted(fmts.items(), key=lambda x: -x[1]):
        readme_lines.append(f"  {fmt}: {n}")
    if len(domains) > 1:
        readme_lines.append("")
        readme_lines.append("Per-domain breakdown:")
        for d, n in sorted(by_domain.items(), key=lambda x: -x[1]):
            readme_lines.append(f"  {d}: {n}")
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    print(f"\n[OK] {len(rows)} rows -> {out_path}")
    print(f"     readme    -> {readme_path}")
    if n_err:
        print(f"  ({n_err} JSON parse errors)")

    print(f"\nWith text: {n_with_text}/{len(rows)}"
          f"  with image: {n_with_image}/{len(rows)}"
          f"  text_in_image: {n_text_in_image}"
          f"  iframe_missing: {n_iframe_missing}")
    if len(domains) > 1:
        print(f"\nDomains: {len(by_domain)}")
        for d, n in sorted(by_domain.items(), key=lambda x: -x[1]):
            print(f"  {n:>4d}  {d}")


if __name__ == "__main__":
    main()
