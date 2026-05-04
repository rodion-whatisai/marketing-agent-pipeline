"""Flatten per-creative JSON files in scans/<domain>/google_creatives/ into a CSV table.

Usage:
    python make_creatives_table.py --domain suspenair.fr
"""
import argparse
import csv
import json
from pathlib import Path

from utils import SCANS_DIR

COLUMNS = [
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


def row_from_json(d: dict) -> dict:
    targeting = d.get("targeting_categories") or []
    targeting_str = " | ".join(
        f"{t.get('sign', '')}{t.get('name', '')}" for t in targeting
    )
    ad_texts = d.get("ad_text_candidates") or []
    ad_text_str = " | ".join(ad_texts)
    images = d.get("ad_image_urls") or []
    return {
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
        "targeting": targeting_str,
        "fetch_error": d.get("fetch_error") or "",
        "ad_link": d.get("ad_link", ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--out", default=None, help="CSV output path (default: scans/<domain>/_creatives_table.csv)")
    args = ap.parse_args()

    src = SCANS_DIR / args.domain / "google_creatives"
    if not src.exists():
        raise SystemExit(f"No such dir: {src}")

    files = sorted(src.glob("CR*.json"))
    if not files:
        raise SystemExit(f"No CR*.json in {src}")

    out_path = Path(args.out) if args.out else (SCANS_DIR / args.domain / "_creatives_table.csv")
    rows = []
    n_err = 0
    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            rows.append(row_from_json(d))
        except Exception as e:
            n_err += 1
            print(f"  ! skip {fp.name}: {e}")

    rows.sort(key=lambda r: (r["advertiser_name"], r["creative_id"]))

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # Sidecar README with table note + summary stats (kept out of CSV to keep parser-friendly)
    n_with_text = sum(1 for r in rows if r["ad_text"])
    n_multivar = sum(1 for r in rows if r["n_variations"] not in ("", None))
    n_iframe_missing = sum(1 for r in rows if r["fetch_error"] == "iframe_missing")
    fmts = {}
    for r in rows:
        fmts[r["format"] or "?"] = fmts.get(r["format"] or "?", 0) + 1

    readme_path = out_path.with_suffix(".README.txt")
    readme_lines = [
        TABLE_NOTE,
        "",
        f"Source: scans/{args.domain}/google_creatives/*.json",
        f"Rows: {len(rows)} (one per creative_id, default variation only)",
        f"Rows with ad_text: {n_with_text}/{len(rows)}",
        f"Rows with n_variations > 1 (multi-variant, partial capture): {n_multivar}/{len(rows)}",
        f"Rows with fetch_error=iframe_missing: {n_iframe_missing}/{len(rows)}",
        "Format mix:",
    ]
    for fmt, n in sorted(fmts.items(), key=lambda x: -x[1]):
        readme_lines.append(f"  {fmt}: {n}")
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    print(f"\n[OK] {len(rows)} rows -> {out_path}")
    print(f"     readme    -> {readme_path}")
    if n_err:
        print(f"  ({n_err} JSON parse errors)")

    advs = {}
    for r in rows:
        advs.setdefault(r["advertiser_name"], 0)
        advs[r["advertiser_name"]] += 1
    print("\nAdvertisers:")
    for name, n in sorted(advs.items(), key=lambda x: -x[1]):
        print(f"  {n:>4d}  {name}")
    print(f"\nWith text: {n_with_text}/{len(rows)}  multi-variant: {n_multivar}/{len(rows)}  iframe_missing: {n_iframe_missing}/{len(rows)}")


if __name__ == "__main__":
    main()
