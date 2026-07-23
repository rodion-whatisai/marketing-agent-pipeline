# ▶ How to run it locally (Quickstart)

Working code for stage 01 (the tracking-audit scanner). Below is everything you need to clone
it and run it yourself. Tested on Windows; the commands are cross-platform (macOS / Linux too).

> **TL;DR** (from the repository root):
> ```bash
> pip install -r requirements.txt        # dependencies
> playwright install chromium            # browser for the scan (required!)
> cd 01-client-discovery/engine          # working directory
> python step1_sitemap.py studioaplus.ca # run (example: a small site, 61 pages)
> ```
> On macOS use `python3` instead of `python` if `python` is not found.

---

## 1. Requirements

- **Python 3.11+**
- Dependencies: `pip install -r requirements.txt` (playwright, requests, anthropic, colorama)
- **Chromium for Playwright:** `playwright install chromium` — without it, step 2 (the scan) won't start.
- (optional) a Claude API key — see below. **It works without the key too**, just more coarsely.

`requirements.txt` lives in the **repository root**, not in this folder.

## 2. ⚠️ The Claude API key — and what happens without it

The page classifier works in three layers: `patterns.json` (learned paths) → regex (generic
structural rules) → **Claude Haiku** (everything the first two didn't recognize, in batches of 50).
The third layer requires the `ANTHROPIC_API_KEY` environment variable.

**If the key IS set** — full classification: URLs the rules didn't recognize go to Claude and
get an accurate type.

**If the key is NOT set** (a fresh clone with no setup) — the tool **does not crash**. The Claude
step is **silently skipped**: every URL that `patterns.json`/regex didn't recognize gets labeled
type `general` (priority 5) and **does not make it into the audit (`to_scan`)**. In practice this
means: without the key, only pages caught by the rules get scanned (contacts, pricing, checkout,
etc.), and everything non-standard is ignored. The result is still correct, just poorer. The log
will show `ANTHROPIC_API_KEY не задан — N URL → general` ("ANTHROPIC_API_KEY not set — N URLs → general").

> `.env` is **not picked up automatically** — set a real environment variable:

- **macOS / Linux (bash/zsh):**
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  ```
- **PyCharm:** Run → Edit Configurations → **Environment variables** field →
  `ANTHROPIC_API_KEY=sk-ant-...`
- **Windows PowerShell:** `$env:ANTHROPIC_API_KEY = "sk-ant-..."`

## 3. Running the pipeline (step by step)

From the `01-client-discovery/engine` folder:

```bash
# Step 1 — site map, platform, social links, Facebook Ads, page classification
python step1_sitemap.py <domain>

# Step 2 — browser scan of the selected pages (pixels, events, CTAs)
python step2_scan.py scans/<domain>/<domain>_step1.json

# Step 3 — text report + HTML
python report.py scans/<domain>/<domain>_step2.json

# Step 4 — merge the logs of all steps into a single file
python merge_logs.py <domain>
```

Results land in `scans/<domain>/`: `_step1.json`, `_step2.json`, `_report.html`, `_audit_log.txt`.

**Full run as a one-liner** (example: studioaplus.ca):
```bash
python step1_sitemap.py studioaplus.ca && python step2_scan.py scans/studioaplus.ca/studioaplus.ca_step1.json && python report.py scans/studioaplus.ca/studioaplus.ca_step2.json && python merge_logs.py studioaplus.ca
```

> A small site (≤65 pages) goes through quickly and with no questions asked. A large one
> (thousands of URLs, e.g. nissan.ie) — with the key set, step 1 spends a long time running
> classification through Claude; without the key it's fast (the Claude layer is skipped).

## 4. Logs

By default you see the **entire stream** (DEBUG level): every step, branch, and decision,
color-coded by level (INFO/OK/WARN/ERROR/DEBUG). The file log (`scans/<domain>/*_log.txt`)
has no color and carries level tags (greppable by `[ERROR]` and the like).

To quiet it down (important things only) — the `--quiet` flag or the `LOG_LEVEL` variable:
```bash
python step1_sitemap.py <domain> --quiet        # INFO and above only
LOG_LEVEL=WARN python step1_sitemap.py <domain>  # WARN/ERROR only
```

## 5. Behavior when a page didn't load

The politeness rule (`utils.polite_get`): `429` — we're requesting too fast, pause and one retry;
`403` — we've been taken for a bot, retry with a real browser. If that fails, the tool honestly
marks `homepage_fetch_method = not_fetched` and moves on (it does not invent data and draws no
conclusions about the site). Stealth/proxies/captcha solvers are **not used**, by design.

Retry kill switch: `TNC_POLITE_RETRY=0`.
