"""
Smoke test — прогоняет ~5 creatives через google_ads_creative.parse_creative,
собирает результат в CSV в формате template-таблицы Rodion'а (30 колонок).
Также сохраняет advertisers list per domain в Markdown.

Запуск:
    python smoke_test_creatives.py
    python smoke_test_creatives.py --sample-per-domain 2
"""

import sys
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime

from utils import SCANS_DIR, setup_console
setup_console()
from log import log_info, log_error, log_debug, log_success, log_step
from google_ads_creative import parse_creative


# Заголовки для CSV в порядке как в template Rodion'а.
# Calculated columns (Duration days / Daily impressions / Daily spends) — оставляем
# пустыми. Считай в Excel сам по raw fields.
# `Impressions` — upper bound из range (e.g. '4K – 5K' → 5000).
# Дополнительно: `_impressions_range_raw`, `_impressions_lower`, `_impressions_upper`.
TEMPLATE_HEADERS = [
    "Company", "Platform", "Format",
    "Start date", "Finish Date", "Duration days",        # Duration left empty
    "Impressions", "Reach", "Daily reach", "Daily impressions",  # Daily left empty
    "CPT", "CPM", "Daily spends",                        # Daily spends left empty
    "Geo", "Age", "Local / Wide",
    "Text", "Language", "Text.EN",
    "Creative", "Creative2", "Creative3", "Creative4", "Creative5",
    "Type of creative",
    "Landing",
    "Targeting", "Targeting2", "Targeting3", "Targeting4", "Targeting5",
    "Ad Link",
    # extras (raw data — для visibility какие границы у impressions)
    "_advertiser_id", "_advertiser_name", "_creative_id", "_topic", "_has_image",
    "_impressions_range_raw", "_impressions_lower", "_impressions_upper",
    "_times_shown_start_date", "_times_shown_end_date",
]


def parse_date(s: str | None) -> datetime | None:
    """'Nov 25, 2024' → datetime."""
    log_debug(f"parse_date: вход s={s!r}")
    if not s:
        log_debug("parse_date: пустой ввод → None")
        return None
    for fmt in ('%b %d, %Y', '%B %d, %Y'):
        try:
            dt = datetime.strptime(s, fmt)
            log_debug(f"parse_date: распознано по формату {fmt!r} → {dt}")
            return dt
        except ValueError as e:
            log_debug(f"parse_date: формат {fmt!r} не подошёл: {e}")
            continue
    log_debug(f"parse_date: ни один формат не подошёл для {s!r} → None")
    return None


def derive_platform(format_: str | None, has_image: bool) -> str:
    """Простая эвристика — позже refine."""
    log_debug(f"derive_platform: вход format_={format_!r} has_image={has_image}")
    if not format_:
        log_debug("derive_platform: format_ пустой → ''")
        return ""
    f = format_.lower()
    if f == 'text':
        log_debug("derive_platform: text → Google Search")
        return "Google Search"
    if f == 'video':
        log_debug("derive_platform: video → YouTube")
        return "YouTube"
    if f == 'image':
        log_debug("derive_platform: image → Display")
        return "Display"
    if f == 'shopping':
        log_debug("derive_platform: shopping → Shopping")
        return "Shopping"
    log_debug(f"derive_platform: неизвестный формат → pass-through {format_!r}")
    return format_


def build_row(parsed: dict, domain: str, region: str) -> dict:
    """Map parsed creative dict → row matching TEMPLATE_HEADERS.
    NOTE: Duration days / Daily impressions / Daily spends остаются пустыми —
    считай в Excel из Start/Finish/Impressions. Тулу сюда не лезет.
    """
    log_debug(f"build_row: вход domain={domain!r} region={region!r} "
              f"creative_id={parsed.get('creative_id')!r}")
    # Impressions: upper bound (consistent with Rodion's example row).
    hi = parsed.get("impressions_upper_bound")
    lo = parsed.get("impressions_lower_bound")
    log_debug(f"build_row: impressions lower={lo} upper={hi}")

    # Targeting columns — by sign with category name
    targeting = parsed.get("targeting_categories") or []
    log_debug(f"build_row: {len(targeting)} targeting categories")
    targeting_strs = []
    for t in targeting:
        if isinstance(t, dict):
            sign = t.get("sign") or ""
            targeting_strs.append(f"{sign} {t.get('name','')}".strip())
        else:
            targeting_strs.append(str(t))
    targeting_strs += [""] * 5  # pad to 5

    # Text — join ad_text_candidates с переводом строки
    text_lines = parsed.get("ad_text_candidates") or []
    log_debug(f"build_row: {len(text_lines)} ad_text_candidates")
    text_joined = "\n".join(text_lines)

    row = {h: "" for h in TEMPLATE_HEADERS}
    row.update({
        "Company": domain,
        "Platform": derive_platform(parsed.get("format"), parsed.get("has_image", False)),
        "Format": parsed.get("format") or "",
        "Start date": parsed.get("first_shown") or "",
        "Finish Date": parsed.get("last_shown") or "",
        # Duration days — empty, count yourself
        "Impressions": hi if hi is not None else "",
        # Daily impressions / Daily spends — empty, count yourself
        "Geo": region,
        "Text": text_joined,
        "Type of creative": parsed.get("type_of_creative") or "",
        "Landing": parsed.get("displayed_url") or "",
        "Targeting": targeting_strs[0],
        "Targeting2": targeting_strs[1],
        "Targeting3": targeting_strs[2],
        "Targeting4": targeting_strs[3],
        "Targeting5": targeting_strs[4],
        "Ad Link": parsed.get("ad_link") or "",
        # extra raw fields
        "_advertiser_id": parsed.get("advertiser_id") or "",
        "_advertiser_name": parsed.get("advertiser_name") or "",
        "_creative_id": parsed.get("creative_id") or "",
        "_topic": parsed.get("topic") or "",
        "_has_image": "yes" if parsed.get("has_image") else "no",
        "_impressions_range_raw": parsed.get("impressions_range_raw") or "",
        "_impressions_lower": lo if lo is not None else "",
        "_impressions_upper": hi if hi is not None else "",
        "_times_shown_start_date": parsed.get("times_shown_start_date") or "",
        "_times_shown_end_date": parsed.get("times_shown_end_date") or "",
    })
    return row


def build_advertisers_md(summary: list[dict]) -> str:
    """Markdown отчёт с advertisers per domain."""
    log_debug(f"build_advertisers_md: вход {len(summary)} domain records")
    lines = ["# Advertisers per FR domain\n",
             f"_(generated {datetime.now().isoformat(timespec='seconds')})_\n"]
    for r in summary:
        domain = r.get("domain")
        advs = r.get("advertisers") or []
        crs = r.get("creatives") or []
        total_est = r.get("total_ads_estimate")
        flags = ", ".join(r.get("flags") or [])
        lines.append(f"\n## {domain}")
        if flags:
            lines.append(f"_flags: {flags}_")
        lines.append(f"\n- Estimated total ads: {total_est or '?'}")
        lines.append(f"- Creatives collected: {len(crs)}")
        lines.append(f"- Unique advertisers: {len(advs)}")
        if advs:
            lines.append(f"\n| # | Advertiser | Verification |")
            lines.append(f"|---|---|---|")
            for i, a in enumerate(advs, 1):
                name = a.get("name") or "(no name)"
                ver = a.get("verification_status") or "?"
                lines.append(f"| {i} | {name} | {ver} |")
        else:
            lines.append(f"\n_(no data — domain not advertising in this region or hidden)_")
    return "\n".join(lines)


def select_sample(summary: list[dict], per_domain: int = 1) -> list[tuple]:
    """Returns list of (domain, advertiser_id, creative_id, region).
    Sampling: для каждого domain берём первые N creatives, причём стараемся
    взять creatives от РАЗНЫХ advertisers (если их > 1)."""
    log_debug(f"select_sample: вход {len(summary)} domains, per_domain={per_domain}")
    selected = []
    for r in summary:
        creatives = r.get("creatives") or []
        if not creatives:
            log_debug(f"select_sample: domain={r.get('domain')!r} без creatives → skip")
            continue
        domain = r["domain"]
        region = r.get("region", "FR")
        log_debug(f"select_sample: domain={domain!r} {len(creatives)} creatives")

        # Group by advertiser_id, take 1 from each (up to per_domain)
        by_adv = {}
        for c in creatives:
            ar = c.get("advertiser_id")
            if ar and ar not in by_adv:
                by_adv[ar] = c
                log_debug(f"select_sample: добавлен advertiser={ar} для {domain!r}")
            if len(by_adv) >= per_domain:
                break

        for c in by_adv.values():
            selected.append((domain, c["advertiser_id"], c["creative_id"], region))
    log_debug(f"select_sample: всего отобрано {len(selected)} creatives")
    return selected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default=str(SCANS_DIR / "_domain_summary_fr.json"))
    ap.add_argument("--sample-per-domain", type=int, default=1)
    ap.add_argument("--csv-out", default=str(SCANS_DIR / "_smoke_test_table.csv"))
    ap.add_argument("--md-out", default=str(SCANS_DIR / "_advertisers_per_domain.md"))
    ap.add_argument("--region", default="FR")
    args = ap.parse_args()

    summary_path = Path(args.summary)
    log_debug(f"main: summary_path={summary_path}")
    if not summary_path.exists():
        log_error(f"summary not found: {summary_path}")
        sys.exit(1)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    log_debug(f"main: загружено {len(summary)} domain records из summary")

    # 1) Advertisers list — quick, no Playwright
    log_step("Сборка advertisers list (без Playwright)", emoji="🌐")
    md = build_advertisers_md(summary)
    Path(args.md_out).write_text(md, encoding="utf-8")
    log_success(f"advertisers list saved: {args.md_out}")

    # 2) Smoke test sample
    sample = select_sample(summary, per_domain=args.sample_per_domain)
    log_step(f"Smoke test on {len(sample)} creatives ({args.sample_per_domain} per domain):", emoji="🔍")
    for domain, ar, cr, region in sample:
        log_debug(f"     {domain:30s}  ar={ar}  cr={cr}")

    rows = []
    for i, (domain, ar, cr, region) in enumerate(sample, 1):
        log_info(f"[{i}/{len(sample)}] parse {domain} / {cr}")
        log_debug(f"main: parse_creative(ar={ar}, cr={cr}, region={region})")
        try:
            parsed = parse_creative(ar, cr, region=region, headed=False, verbose=False)
        except Exception as e:
            log_error(f"    parse failed: {e}")
            continue
        if parsed.get("fetch_error"):
            log_error(f"    fetch_error: {parsed['fetch_error']}")
        row = build_row(parsed, domain, region)
        rows.append(row)
        log_debug(f"main: row собран для {cr}, всего rows={len(rows)}")
        # Show short preview
        preview_text = (row["Text"] or "").replace('\n', ' | ')[:80]
        rng = row["_impressions_range_raw"] or "—"
        log_success(f"    Format={row['Format']!s:6} | Range={rng:10} "
              f"| Start={row['Start date']!s:14} | {preview_text}")

    # 3) CSV out
    csv_path = Path(args.csv_out)
    log_debug(f"main: запись CSV → {csv_path} ({len(rows)} rows)")
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=TEMPLATE_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    log_success(f"smoke test CSV saved: {csv_path}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
