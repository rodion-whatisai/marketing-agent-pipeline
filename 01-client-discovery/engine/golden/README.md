# golden/ — the testbed's golden corpus

What this is and why it exists — see [TESTBED-PLAN.md](../TESTBED-PLAN.md). In short:

- **The expected results are the truth, not the scanner's current output.** `expected_<domain>.json`
  records what the scanner MUST see on the site (verified by a human). If the scanner doesn't
  currently see it — that's a known fail, and it should die out as fixes land.
- **step1 is frozen**: `golden/<domain>/step1.json` is a copy of one run; in the testbed, step2
  always runs over the same list of URLs, so the score doesn't get noisy from a live sitemap.
  Updating it is a deliberate act only (`eval_run.py --refresh-step1`).
- **Truncating the fast domains is done inside the frozen step1 itself** (editing `to_scan`,
  a `_testbed_note` marker in the file), NOT via the `--max-pages` flag — that one distorts
  the counters (bug D12). tinytronics is truncated to 2 pages: `/` (an OK case) + `/en/comment-or-suggestion`
  (a GAP case); the full list stays in `classified`.
- The corpus composition and the reasons each site was chosen — `corpus.json`.
- Score history across runs — `history.csv` (committed; it's the trust curve).

## The gate rule and the two kinds of fields (revision 2026-07-13)

**Any truth gets recorded here ONLY after an explicit "yes" from Rodion** — with the witness
evidence included in the question. Who confirms what:

| Kind | Fields | Who confirms |
|---|---|---|
| **"Ground truth about the site"** — exists independently of our code | `platforms_detected`, `platforms_forbidden`, `gtm_platforms`, `conversion_events_min`, `external_services`, `has_cta`, redirect/HTTP and consent facts | **Rodion only, via an explicit "yes"**; evidence — the witness files alongside; recorded in `verified_via` |
| **"Scanner contract"** — an agreement on how to label the truth | `page_type` (classification convention), `status` (the label given this truth), `counters`, `missing_events` | scan-derived is acceptable, honestly signed as "contract" |

Witness files: `golden/<domain>/witness_<date>.json` (a page walk: final URL+status,
pixel requests with methods and POST bodies, raw clickable texts, third-party hosts) and
`witness_journey_<date>.json` (the e-com journey product→ATC→cart→
checkout). Raw material (screenshots, full bodies) — `scans/_witness_<date>/`, gitignored.

Machine check: `python witness_check.py <domain>` — "the expected results do not contradict
the raw traffic": ✅ confirmed / ⚠ not witnessed (goes to the gate) /
❌ contradiction (exit 1).

## Format of expected_<domain>.json (schema_version 2)

ONLY stable fields are checked. A field absent from the expected file = not checked.

```json
{
  "schema_version": 2,
  "domain": "fritz-kola.de",
  "verified_by": "rodion",          // "draft" = a draft, there is NO truth in it
  "verified_date": "2026-07-15",
  "verified_against": "gate round in chat + manual cross-check",
  "verified_via": {                 // what it was confirmed with (schema v2)
    "witness": "golden/fritz-kola.de/witness_2026-07-15.json",
    "rodion_gate": "chat 2026-07-15: confirmed platforms, forbidden list, buttons",
    "rodion_manual": "Pixel Helper / Tag Assistant"
  },
  "scanner_commit": "f7e9a6b",
  "notes": "The domain 301s to fritz-kola.com, paths collapse onto the homepage.",
  "site": {
    "platform": "shopify",
    "gtm_platforms": ["Google Analytics", "Google Ads", "Meta"],
    "counters": {"gaps": 0, "oks": 1, "no_ctas": 0, "no_tracking": 0, "unverified": 0}
  },
  "pages": {
    "/": {
      "status": "OK",
      "page_type": "homepage",
      "has_cta": true,
      "platforms_detected": ["Meta", "Google Analytics", "Google Ads"],
      "external_services": [],
      "missing_events": []
    }
  }
}
```

Rules:
- `status` — normalized (OK / GAP / NO_TRACKING / NO_CTA / UNVERIFIED,
  later REDIRECTED / HTTP_ERROR), NOT the emoji string from step2.json.
- `platforms_detected` / `gtm_platforms` / `external_services` — compared as
  "expected ⊆ actual"; anything extra on the actual side is handled by the testbed's FAIL/DRIFT logic.
- `status` / `has_cta` / `counters` — exact match.
- We NEVER put volatile things into the expected results: button texts, list ordering,
  raw network requests, exact click event lists.

## How to update

- A new expected file / re-verification: `python make_expected.py <domain>` (interactive, y/n
  per page) or `--draft` (a draft with no approval, verified_by="draft").
- The site has genuinely changed (DRIFT in the testbed, confirmed by eye):
  `python make_expected.py <domain> --update` + a new verified_date.
