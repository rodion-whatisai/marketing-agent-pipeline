"""Batch driver: прогоняет fb_scan.py по списку доменов последовательно."""
import sys, time
from utils import setup_console
setup_console()
from log import log_error, log_debug, log_header
from fb_scan import scan_domain

DOMAINS = [
    "aerosus.fr",
    "redacted-client.example",
    "mannes.fr",
    "studioaplus.ca",
    "autonorma.fr",
]
TOP_N = 3

log_debug(f"batch_scan старт: {len(DOMAINS)} доменов, top_n={TOP_N}")
t0 = time.time()
results = {}
for i, d in enumerate(DOMAINS, 1):
    log_debug(f"[{i}/{len(DOMAINS)}] следующий домен: {d}")
    log_header(f"[{i}/{len(DOMAINS)}]  {d}")
    try:
        log_debug(f"scan_domain({d}, top_n={TOP_N}, verbose=False) старт")
        s = scan_domain(d, top_n=TOP_N, verbose=False)
        log_debug(f"scan_domain({d}) вернул результат, собираю сводку")
        results[d] = {
            "duration_s":     s.get("duration_s"),
            "active_count":   len(s.get("deep_active", [])),
            "inactive_count": len(s.get("deep_inactive", [])),
            "errors":         len(s.get("errors", [])),
            "total_ever":     (s.get("step2_listing") or {}).get("total_ever"),
        }
        log_debug(f"{d}: {results[d]}")
    except Exception as e:
        log_error(f"  FATAL: {str(e)[:200]}")
        results[d] = {"error": str(e)[:200]}

log_debug(f"все домены обработаны, формирую итоговую таблицу ({len(results)} строк)")
log_header(f"BATCH DONE — total {round(time.time()-t0, 1)}s")
for d, r in results.items():
    print(f"  {d:30}  {r}")
