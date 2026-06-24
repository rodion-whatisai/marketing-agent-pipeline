"""Batch driver: прогоняет fb_scan.py по списку доменов последовательно."""
import sys, time
from utils import setup_console
setup_console()
from fb_scan import scan_domain

DOMAINS = [
    "aerosus.fr",
    "redacted-client.example",
    "mannes.fr",
    "studioaplus.ca",
    "autonorma.fr",
]
TOP_N = 3

t0 = time.time()
results = {}
for i, d in enumerate(DOMAINS, 1):
    print(f"\n\n{'#' * 70}")
    print(f"#  [{i}/{len(DOMAINS)}]  {d}")
    print(f"{'#' * 70}")
    try:
        s = scan_domain(d, top_n=TOP_N, verbose=False)
        results[d] = {
            "duration_s":     s.get("duration_s"),
            "active_count":   len(s.get("deep_active", [])),
            "inactive_count": len(s.get("deep_inactive", [])),
            "errors":         len(s.get("errors", [])),
            "total_ever":     (s.get("step2_listing") or {}).get("total_ever"),
        }
    except Exception as e:
        print(f"  ❌ FATAL: {str(e)[:200]}")
        results[d] = {"error": str(e)[:200]}

print(f"\n\n{'═' * 70}")
print(f"  BATCH DONE — total {round(time.time()-t0, 1)}s")
print(f"{'═' * 70}")
for d, r in results.items():
    print(f"  {d:30}  {r}")
