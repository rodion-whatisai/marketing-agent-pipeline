"""Smoke test for parse_creative patch (race + n_variations + iframe_missing).

Verifies on 4 known suspenair.fr URLs:
  - 3 previously-empty creatives → expect iframe_count >= 1, ad_text non-empty.
  - 1 already-nonempty (LSA) → expect no regression.
  - CR012036 (Video) → expect n_variations == 3 (browser DOM showed "1 of 3 variations").
"""
from utils import setup_console
setup_console()

from google_ads_creative import parse_creative
from log import log_error, log_debug, log_header

ADV = "AR11291011555627368449"
TARGETS = [
    ("CR00922399692023660545", "empty Text",   {"min_iframes": 1, "min_text": 1, "n_variations": None}),
    ("CR00339999927662804993", "empty Image",  {"min_iframes": 1, "min_text": 1, "n_variations": None}),
    ("CR01203605425824464897", "Video w/ vars",{"min_iframes": 1, "min_text": 0, "n_variations": 3}),
    ("CR00429861750280552449", "LSA control",  {"min_iframes": 1, "min_text": 1, "n_variations": None}),
]

if __name__ == "__main__":
    log_debug(f"smoke start: ADV={ADV}, {len(TARGETS)} targets")
    fail = 0
    for cr, label, want in TARGETS:
        log_header(f"{cr}  ({label})")
        log_debug(f"parsing creative {cr} (region=FR) — expects {want}")
        r = parse_creative(ADV, cr, region="FR", verbose=False)
        log_debug(f"parse_creative returned for {cr}: keys={list(r.keys())}")
        ic = r.get("iframe_count", 0)
        txt = r.get("ad_text_candidates") or []
        nv = r.get("n_variations")
        err = r.get("fetch_error")
        fmt = r.get("format")
        print(f"  iframe_count={ic}  data_p_count={r.get('iframe_data_p_count')}")
        print(f"  format={fmt}  n_variations={nv}  fetch_error={err}")
        print(f"  ad_text_candidates ({len(txt)}):")
        for t in txt[:6]:
            print(f"    - {t[:100]}")

        ok = True
        if ic < want["min_iframes"]:
            log_error(f"FAIL: iframe_count {ic} < expected {want['min_iframes']}")
            ok = False
        if len(txt) < want["min_text"]:
            log_error(f"FAIL: text count {len(txt)} < expected {want['min_text']}")
            ok = False
        if want["n_variations"] is not None and nv != want["n_variations"]:
            log_error(f"FAIL: n_variations {nv} != expected {want['n_variations']}")
            ok = False
        log_debug(f"{cr} verdict: {'PASS' if ok else 'FAIL'}")
        print(f"  -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            fail += 1

    log_header(f"Summary: {len(TARGETS) - fail}/{len(TARGETS)} pass")
