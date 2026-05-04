"""
TNC Google Ads Transparency Center — Mass Creative Parser (Sprint B)
====================================================================
Прогоняет parse_creative по всем sampled creatives используя N concurrent
Playwright BrowserContext'ов в одном Chromium. Idempotent — пропускает уже
сделанные creatives.

Sample-cap formula:
    n <= 100   → all
    n  > 100   → 100 + (n - 100) // 2
Cap считается от total_ads_estimate (то что Google заявил),
sample = min(cap, len(collected_creatives)).

Использование:
    python mass_run_creatives.py                          # все 10 с дефолтным cap
    python mass_run_creatives.py --workers 5
    python mass_run_creatives.py --domains aerosus.fr points.fr
    python mass_run_creatives.py --no-resume              # перепарсить всё
    python mass_run_creatives.py --dry-run                # показать план без запуска
    python mass_run_creatives.py --limit 20               # только первые 20 (для дебага)

Output (per creative):
    scans/<domain>/google_creatives/<creative_id>.json

Aggregate logs:
    scans/_mass_run_<region>_<timestamp>.log
"""

import sys
import json
import asyncio
import argparse
import random
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

from utils import SCANS_DIR, get_scan_dir, HEADERS, setup_console
setup_console()
from google_ads_creative import parse_creative_with_context


DEFAULT_REGION = "FR"
DEFAULT_WORKERS = 4

# Per-creative timeout (seconds). Если parse_creative_with_context зависает —
# рубим, отмечаем fetch_error и идём дальше.
CREATIVE_TIMEOUT = 60

# Между parsings внутри одного worker — small jitter чтобы не молотить TC синхронно.
INTER_REQUEST_JITTER = (0.3, 1.2)


# ─── Sample selection ────────────────────────────────────────────────────────

def cap_for_total(total: int | None) -> int:
    """Sample-cap formula. None / 0 / negative → 0."""
    if not total or total <= 0:
        return 0
    if total <= 100:
        return total
    return 100 + (total - 100) // 2


def select_sample_for_domain(domain_record: dict, seed: int = 42) -> list[dict]:
    """Возвращает список creatives для domain, отсэмплированных по cap formula.
    Mixing strategy: round-robin по advertiser_id для diverse coverage,
    потом random fill из остатка.
    """
    creatives = list(domain_record.get("creatives") or [])
    if not creatives:
        return []
    total_est = domain_record.get("total_ads_estimate") or 0
    cap_est = cap_for_total(total_est)
    n_target = min(cap_est, len(creatives)) if cap_est else len(creatives)
    if n_target >= len(creatives):
        return creatives

    # Group by advertiser
    by_adv = {}
    for c in creatives:
        ar = c.get("advertiser_id") or "?"
        by_adv.setdefault(ar, []).append(c)

    rng = random.Random(seed)
    for v in by_adv.values():
        rng.shuffle(v)

    # Round-robin pick
    selected = []
    queues = {k: deque(v) for k, v in by_adv.items()}
    while len(selected) < n_target and any(queues.values()):
        for ar in list(queues.keys()):
            if not queues[ar]:
                continue
            selected.append(queues[ar].popleft())
            if len(selected) >= n_target:
                break
    return selected


def build_full_plan(summary: list[dict],
                    only_domains: set[str] | None = None,
                    seed: int = 42) -> list[tuple]:
    """Возвращает список (domain, advertiser_id, creative_id) tuples для всего batch'a."""
    plan = []
    for r in summary:
        domain = r.get("domain")
        if only_domains and domain not in only_domains:
            continue
        sample = select_sample_for_domain(r, seed=seed)
        for c in sample:
            plan.append((domain, c["advertiser_id"], c["creative_id"]))
    return plan


def filter_already_done(plan: list[tuple]) -> tuple[list[tuple], int]:
    """Удаляет creatives для которых уже есть scans/<domain>/google_creatives/<cr>.json.
    Returns (remaining_plan, n_skipped)."""
    remaining = []
    skipped = 0
    for item in plan:
        domain, ar, cr = item
        out_path = get_scan_dir(domain) / "google_creatives" / f"{cr}.json"
        if out_path.exists():
            skipped += 1
            continue
        remaining.append(item)
    return remaining, skipped


# Errors worth retrying — race-prone failures, not "ad genuinely empty".
RETRYABLE_ERRORS = ("iframe_missing", "TimeoutError")

# Errors that are already final — don't promote them to "no_iframe_after_N_retries".
TERMINAL_ERRORS = ("advertiser_not_found", "ad_not_found")


MAX_RETRY_ATTEMPTS = 2  # after 2 failed retries, accept terminal "no_iframe_after_retries"


def find_misses_to_retry(summary: list[dict],
                          only_domains: set[str] | None) -> list[tuple]:
    """Scan existing JSONs, return [(domain, advertiser_id, creative_id), ...]
    for those whose fetch_error is in RETRYABLE_ERRORS *and* retry_attempts < MAX.
    Used by --retry-misses mode."""
    targets = []
    for r in summary:
        domain = r.get("domain")
        if only_domains and domain not in only_domains:
            continue
        gc_dir = get_scan_dir(domain) / "google_creatives"
        if not gc_dir.exists():
            continue
        for fp in gc_dir.glob("CR*.json"):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            err = d.get("fetch_error") or ""
            if not any(token in err for token in RETRYABLE_ERRORS):
                continue
            attempts = d.get("retry_attempts", 0) or 0
            if attempts >= MAX_RETRY_ATTEMPTS:
                continue
            ar = d.get("advertiser_id")
            cr = d.get("creative_id")
            if ar and cr:
                targets.append((domain, ar, cr))
    return targets


def archive_misses_before_retry(targets: list[tuple], round_n: int) -> int:
    """Move existing miss-JSONs to scans/_archive/<domain>/round_<N>/<cr>.json.
    Trickster pattern — improvements are reversible if retry introduces regression."""
    moved = 0
    for domain, ar, cr in targets:
        src = get_scan_dir(domain) / "google_creatives" / f"{cr}.json"
        if not src.exists():
            continue
        dst_dir = SCANS_DIR / "_archive" / domain / f"round_{round_n}"
        dst_dir.mkdir(parents=True, exist_ok=True)
        # Don't actually move — copy content, then let the new run overwrite src.
        # (If move-and-fail, we'd lose the JSON entirely. Copy-then-overwrite is safe.)
        dst = dst_dir / f"{cr}.json"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        moved += 1
    return moved


# ─── Worker pool ─────────────────────────────────────────────────────────────

async def _process_creative(context, domain: str, advertiser_id: str,
                             creative_id: str, region: str,
                             out_dir: Path, log_print,
                             retry_round: int = 0) -> dict:
    """Parse one creative + save JSON. Returns stat dict.
    retry_round: 0 for primary run, 1+ for retry passes (carries forward retry_attempts)."""
    t0 = time.monotonic()
    try:
        parsed = await asyncio.wait_for(
            parse_creative_with_context(context, advertiser_id, creative_id, region),
            timeout=CREATIVE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        parsed = {
            "advertiser_id": advertiser_id,
            "creative_id": creative_id,
            "region": region,
            "fetch_error": f"TimeoutError: > {CREATIVE_TIMEOUT}s",
        }
    elapsed = time.monotonic() - t0

    # Carry forward retry_attempts counter from existing JSON if present.
    out_path = out_dir / f"{creative_id}.json"
    prev_attempts = 0
    if retry_round > 0 and out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            prev_attempts = prev.get("retry_attempts", 0) or 0
        except Exception:
            pass

    new_attempts = prev_attempts + (1 if retry_round > 0 else 0)
    parsed["retry_attempts"] = new_attempts

    # If still erroring after MAX retries — promote to terminal error,
    # UNLESS error is already a recognised terminal kind (e.g. advertiser_not_found).
    err = parsed.get("fetch_error")
    if (err
            and not any(t in err for t in TERMINAL_ERRORS)
            and new_attempts >= MAX_RETRY_ATTEMPTS):
        parsed["fetch_error"] = f"no_iframe_after_{new_attempts}_retries"

    parsed.setdefault("_meta", {})
    parsed["_meta"]["domain"] = domain
    parsed["_meta"]["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    parsed["_meta"]["elapsed_s"] = round(elapsed, 2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    err = parsed.get("fetch_error")
    fmt = parsed.get("format") or "—"
    rng = parsed.get("impressions_range_raw") or "—"
    n_text = len(parsed.get("ad_text_candidates") or [])
    if err:
        log_print(f"  ❌ {domain:25s} {creative_id} ({elapsed:5.1f}s) ERR: {err}")
    else:
        log_print(f"  ✓ {domain:25s} {creative_id} ({elapsed:5.1f}s) "
                  f"fmt={fmt:7s} rng={rng:10s} text={n_text}")

    return {"domain": domain, "creative_id": creative_id,
            "elapsed_s": elapsed, "ok": not err, "error": err}


async def _worker(worker_id: int, browser, queue: asyncio.Queue,
                   region: str, stats: list, log_print, retry_round: int = 0):
    """One worker — owns its own BrowserContext for the duration."""
    context = await browser.new_context(user_agent=HEADERS["User-Agent"])
    log_print(f"  [w{worker_id}] context ready")
    try:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                domain, ar, cr = item
                out_dir = SCANS_DIR / domain / "google_creatives"
                stat = await _process_creative(
                    context, domain, ar, cr, region, out_dir, log_print,
                    retry_round=retry_round,
                )
                stats.append(stat)
                jitter = random.uniform(*INTER_REQUEST_JITTER)
                await asyncio.sleep(jitter)
            finally:
                queue.task_done()
    finally:
        try:
            await context.close()
        except Exception:
            pass


async def run_mass(plan: list[tuple], region: str, workers: int,
                    headed: bool, log_print, retry_round: int = 0) -> list[dict]:
    """Запускает Chromium с N workers (= N contexts)."""
    from playwright.async_api import async_playwright

    queue: asyncio.Queue = asyncio.Queue()
    for item in plan:
        await queue.put(item)
    # Sentinels — workers exit when seen
    for _ in range(workers):
        await queue.put(None)

    stats: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        try:
            tasks = [
                asyncio.create_task(
                    _worker(i + 1, browser, queue, region, stats, log_print,
                            retry_round=retry_round)
                )
                for i in range(workers)
            ]
            await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return stats


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary",
                    default=str(SCANS_DIR / "_domain_summary_fr.json"),
                    help="Domain summary JSON (output of google_ads_domain.py --file ...)")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="Concurrent browser contexts (default 4)")
    ap.add_argument("--domains", nargs="+",
                    help="Restrict to specific domains (else all in summary)")
    ap.add_argument("--limit", type=int,
                    help="Limit total creatives processed (debug)")
    ap.add_argument("--no-resume", action="store_true",
                    help="Re-process even if google_creatives/<cr>.json exists")
    ap.add_argument("--retry-misses", action="store_true",
                    help="Re-process only creatives whose existing JSON has fetch_error in "
                         "RETRYABLE_ERRORS (iframe_missing, TimeoutError). Caps workers=2 "
                         "for stability. After MAX_RETRY_ATTEMPTS, error becomes terminal.")
    ap.add_argument("--seed", type=int, default=42, help="Sample shuffle seed")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan and exit (no parsing)")
    args = ap.parse_args()

    summary_path = Path(args.summary)
    if not summary_path.exists():
        print(f"❌ summary not found: {summary_path}")
        sys.exit(1)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    only_domains = set(args.domains) if args.domains else None

    # Two modes: primary mass run vs --retry-misses (re-process only failed JSONs).
    if args.retry_misses:
        # Cap workers in retry mode (low concurrency → less race).
        if args.workers > 2:
            print(f"[retry-misses] capping workers {args.workers}→2 for stability")
            args.workers = 2
        plan = find_misses_to_retry(summary, only_domains=only_domains)
        n_total_planned = len(plan)
        retry_round = 1
        # Determine round number from existing JSONs (max retry_attempts seen + 1).
        # Simple: round 1 if any candidate has retry_attempts==0, else round 2.
        max_attempts = 0
        for domain, ar, cr in plan:
            fp = get_scan_dir(domain) / "google_creatives" / f"{cr}.json"
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                max_attempts = max(max_attempts, d.get("retry_attempts", 0) or 0)
            except Exception:
                pass
        retry_round = max_attempts + 1
        print(f"📋 Retry plan ({n_total_planned} creatives with retryable errors, round {retry_round}):")
    else:
        plan = build_full_plan(summary, only_domains=only_domains, seed=args.seed)
        n_total_planned = len(plan)
        retry_round = 0
        print(f"📋 Plan ({n_total_planned} creatives across {len({d for d, _, _ in plan})} domains):")

    # Per-domain breakdown
    by_dom = {}
    for d, _, _ in plan:
        by_dom[d] = by_dom.get(d, 0) + 1

    if not args.retry_misses:
        for r in summary:
            d = r.get("domain")
            if only_domains and d not in only_domains:
                continue
            n_sample = by_dom.get(d, 0)
            n_total = len(r.get("creatives") or [])
            est = r.get("total_ads_estimate") or 0
            print(f"     {d:30s} sample={n_sample:>4d}  collected={n_total:>4d}  total_est={est:>4d}")
    else:
        for d in sorted(by_dom):
            print(f"     {d:30s} misses={by_dom[d]:>4d}")

    # Resume filter — only in primary mode (retry mode targets are always re-processed)
    if not args.no_resume and not args.retry_misses:
        plan, n_skipped = filter_already_done(plan)
        if n_skipped:
            print(f"\n⏭  Resume: skipping {n_skipped} already-done creatives")

    if args.limit:
        plan = plan[:args.limit]
        print(f"⚙  --limit {args.limit}: processing first {len(plan)} only")

    print(f"\n→ {len(plan)} creatives queued, workers={args.workers}, region={args.region}"
          + (f", retry_round={retry_round}" if retry_round else ""))

    if args.dry_run:
        print("(dry-run, exiting)")
        return

    if not plan:
        print("✅ Nothing to do (all already parsed)")
        return

    # Archive miss-JSONs before retry overwrites them (Trickster pattern: keep a baseline)
    if args.retry_misses:
        n_archived = archive_misses_before_retry(plan, retry_round)
        print(f"🗄  Archived {n_archived} miss-JSONs to scans/_archive/<domain>/round_{retry_round}/")

    # Set up log file
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_tag = f"retry{retry_round}" if retry_round else "primary"
    log_path = SCANS_DIR / f"_mass_run_{args.region.lower()}_{log_tag}_{ts}.log"
    log_f = log_path.open("w", encoding="utf-8")

    def log_print(msg: str):
        ts2 = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts2}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    log_print(f"=== mass_run_creatives.py start ts={ts} mode={log_tag} ===")
    log_print(f"workers={args.workers} region={args.region} total={len(plan)}")

    t_start = time.monotonic()
    try:
        stats = asyncio.run(run_mass(
            plan, region=args.region, workers=args.workers,
            headed=args.headed, log_print=log_print,
            retry_round=retry_round,
        ))
    finally:
        elapsed = time.monotonic() - t_start
        log_f.close()

    n_ok = sum(1 for s in stats if s.get("ok"))
    n_err = len(stats) - n_ok
    avg = sum(s["elapsed_s"] for s in stats) / max(len(stats), 1)
    print(f"\n✅ done in {elapsed:.0f}s — {n_ok} ok / {n_err} err / {len(stats)} total"
          f" (avg {avg:.1f}s per creative)")
    print(f"   log: {log_path}")


if __name__ == "__main__":
    main()
