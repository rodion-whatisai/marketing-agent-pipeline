# 01 — Client Discovery

> Find and qualify a client by hard, verifiable signals — are they spending on ads, and is
> their tracking broken — and lock that into rock-solid facts you can walk in with.

> **Status:** filled in (Step 3) — the reference block. Real working code, real runs
> (see [Evidence](#evidence--real-runs)).

> Code mentions below are **clickable links to real files in this repository**.

## What this stage consumes and produces

**Input:** a domain, or a population of domains.

**Output:** not just a pile of facts and not "a report that's nice in itself" — a
**validated report you can send to the client straight away as a first touch
(outreach)**.

That is the stage's goal. Not "make a great report" but **send a great one — meaning a
correct one, verified, with provenance and disclaimers** (see the "Guardrails and data
integrity" section below). "Validated" means: every fact is dated, labeled with how it was
obtained, and verifiable via a link to its source — a report like that is neither
embarrassing nor risky to send, and it **carries value to the client before the first
conversation even happens**. Ordinary outreach doesn't do this.

Python here is the source of truth for every agent downstream in the pipeline:
signal → validation → rock-solid fact → report → outreach.

## Lead thesis — the story of a lead

Client discovery is **search by signals and patterns**. You point the engine at a
population of sites; deterministic rules pick out the ones that fit the profile.
You re-aim it by changing the rules, not the code.

The profile in this example is deliberately narrow: **small businesses** (a one-person
agency, a local shop) that have *only just started* spending on Meta but are tracking it
wrong — a live pixel that never fires a conversion; a checkout with no `AddToCart`; budget
draining into a blind funnel. They spend → they're worth reaching; tracking is broken →
there's something concrete to sell; they're small → the decision is fast. That
combination = a **tier-1 lead**.

Re-aimed at a different rule (say, a threshold on active ads per country), the same
engine finds a different population. *Who counts as a lead is a number in a rule, not a
feeling.*

## The code / agent boundary

Across all of discovery, the agent (Claude Haiku) works **at exactly one point** —
[`page_classifier.py`](engine/page_classifier.py), and only on the URLs the
deterministic ladder failed to recognize.

| Part | Code or agent | Why |
|---|---|---|
| sitemap, platform detection, pixel event interception, FB/Google ads lookup, fuzzy match, the report | **deterministic Python** | verifiable, repeatable, cheap; zero LLM calls |
| the semantic type of a URL — only the tail the rule ladder didn't catch | **agent (Claude Haiku)**, batches of 50 | slug semantics is judgment, not a rule |
| expected events for the type · `method=` provenance · lead qualification | **deterministic Python** | fenced in after/around the agent |

The ladder (cheap and deterministic first, the agent only for the remainder):

```python
# page_classifier.py (abridged)
def classify_urls(urls, platform=""):
    for url in urls:
        fast = fast_classify(path)         # 1) patterns.json → regex FAST_RULES
        if fast: ...                       #    recognized by a rule — no agent needed
        if platform == "shopify" and ...:  # 2) Shopify slug rules (free)
            ...
        ai_needed.append((i, path, url))   # 3) only the unrecognized → Claude Haiku

    # the agent decides the type — but WHICH events to expect on it is attached
    # by a deterministic dictionary AFTER the agent, plus a provenance stamp:
    clf["expect_events"] = _get_expect_events(clf["type"])
    clf["method"] = "claude"               # visible: the agent decided this, not a rule
```

→ live code: [`classify_urls`](engine/page_classifier.py#L458) (the ladder) ·
[`_get_expect_events`](engine/page_classifier.py#L423) (fencing the agent) ·
[`FAST_RULES`](engine/page_classifier.py#L139) (deterministic patterns).

The agent decides only the *type* (a label). What to do with that type — which conversion
events ought to appear on it — is attached by the deterministic `_get_expect_events`
dictionary **after** the agent. And `method=` marks who made the decision (`patterns_json`
/ `regex` / `claude` / `no_api_key` / …). Any agent failure degrades to a safe default
(`general`); the run does not crash.

> **Footnote:** to cover more cases, more agents will be needed here and there — but the
> logic of their subordination is the same (narrow task, fenced in by deterministic code,
> product → to the next one, human gate). Not "one agent forever" but "any number of
> agents under the same discipline."

## Engineering decisions — and how we got to them

**A cumulative pattern base ([`patterns.json`](engine/patterns.json) + the interactive
[`learn.py`](engine/learn.py)).** There will be very many runs (thousands of sites), and a
pure LLM classifier burns tokens on every URL. The pattern base grows through manual
approval: every URL caught by `patterns.json`/regex is a URL you don't pay Claude for. We
ruled out pure regex (can't handle the semantics of unfamiliar slugs) and pure LLM
(expensive at scale). The ladder is the compromise: the deterministic part takes the
majority, the agent takes the tail.

**The Facebook Ads Library strategy.** Arrived at by trial, step by step: search by the
correct brand name → a plain search by ID → combining with a keyword string → cycling
through name variations. Each step meant manual inspection of HTML / network / elements to
isolate exactly the signal needed (the active ads counter). Hence, in the code
([`fb_ads_scraper.py`](engine/fb_ads_scraper.py)): routing classic-vs-new-style Page ID
(different endpoints are alive for different ID types), a keyword fallback with fuzzy
matching on the name, a noise post-filter.

**Google Transparency Center** ([`google_ads_domain.py`](engine/google_ads_domain.py) +
[`google_ads_creative.py`](engine/google_ads_creative.py))**.** The logical continuation
of FB — TC returns cleaner, more intelligible results. The same model (domain →
advertisers → creatives), debugged on FB, carries over. *Next step:* add the EU's open
ads database (which the EU obliged Google to publish) as an extra source for Europe.

**Journey simulation ([`clicker.py`](engine/clicker.py)).** A passive scan doesn't catch
events that fire only on user action. The task: check whether the funnel can be "clicked
through" to a conversion event (`AddToCart` / `InitiateCheckout`), **stopping short of an
actual payment**. For each page it was worked out by hand: which element on which site
builder triggers which event with which parameters when clicked; and if the tracker isn't
visible in one environment (GTM) — whether it's visible on the site itself and how to
read it.

**The `unverified` bucket.** A booking tool (Cal.com/Calendly) on the page — the
conversion may go server-side, invisible to the browser. Calling that a "hole" = lying.
So such pages go into `unverified` ("sort out by hand"), not into `gaps`. We don't
declare a gap where we can't verify one.

**"We couldn't read it" — honest degradation.** If a page yields neither to a plain
request nor to a real browser, we mark it `not_fetched` and recommend manual
verification. **We claim nothing about the site itself:** the response code speaks about
our visit, not about the site's defenses, and "we couldn't read it" never turns into
"there is no tracking." Stealth plugins, residential proxies, CAPTCHA solvers — out of
scope. Solutions probably exist, but that's not our game: an honest "we couldn't get
through" is more useful to the client than guessed numbers.

**Politeness instead of conclusions.** A 429 means "we're going too fast" → pause and one
retry; a 403 means "we got taken for a bot" → retry with a real browser. The rule lives in
one place (`engine/utils.py::polite_get`) and is handed out to the whole engine.

## Guardrails and data integrity

**The high-risk action at this stage is not a budget edit (there's no money here) — it's
sending a wrong or overclaimed fact to the client in the first touch.** Botching the
outreach is the stage's most expensive failure: one wrong "your pixel is broken" kills
trust before the conversation even starts.

So *what may be claimed at all* is decided by a deterministic **honesty stack** built into
the report — code, not agent discretion. Seven mechanisms, verbatim from
[`generate_site_report.py`](engine/generate_site_report.py) /
[`generate_batch_report.py`](engine/generate_batch_report.py):

1. **A dated snapshot** ([the date comes from the data file's mtime](engine/generate_site_report.py#L750)):
   > *Snapshot date: 2026-06-24 … Data was captured at this moment from Facebook Ads
   > Library … Ads Library is live … advertisers may have added new ads or ended running
   > ones since this report was generated.*

2. **An authority label on the number itself** —
   [the search mode spelled out in words](engine/generate_site_report.py#L554):
   > `page` → *Advertiser-filtered (authoritative — FB filtered by page_id)*
   > `keyword_filtered_by_name` → *fuzzy filter on advertiser name (lower confidence)*
   > `keyword_raw` → *Keyword search, unfiltered (may include unrelated ads)*

3. **Noise disclosure** (raw vs matched):
   > *Filtered out: {N} keyword matches from other advertisers (noise)*

4. **A warning on low confidence:**
   > *⚠️ Lower-confidence result. We used keyword search + fuzzy-matched advertiser names
   > … Some matches may not actually be from this brand. Manual verification recommended.*

5. **Provenance on every identity fact:**
   > *Platform — {confidence} confidence · Language — detected via {source} · Country —
   > detected via {source}*

6. **An honest "we couldn't read it"** ([batch report](engine/generate_batch_report.py#L649)):
   > *…we mark it `not_fetched` and recommend manual verification. We report what we
   > observed and make no claim about why. We do not use stealth plugins, residential
   > proxies, or captcha solvers — these are explicitly out of scope.*

7. **Independent verification baked into every fact** — a button:
   > *Verify yourself in Ads Library →* (leads to the live source page)

A fact leaves the stage only with "when this was true" and "how it was obtained" attached.
This is discovery's analog of a kill switch: in operating-model language — **trust in
data, measurability**. All seven are visible live in the real reports below.

(Plus: the tracking-scan statuses `OK / GAP / NO TRACKING / No CTA` are never mixed —
each narrative stays separate.)

## Next step — a report validator (doesn't exist yet)

The report gets generated and stamped by the honesty mechanisms above, but **it is not yet
checked for semantic coherence before sending**. Example: in the `finom.co` demo the
creatives run in several languages (FR/DE/IT/…) — normal for a pan-European advertiser,
but the tool makes no comment on it whatsoever.

The missing link is a **validator**: a pass (Python, or agent → Python) before a report
counts as "ready to send":
- *deterministic (Python):* the counters add up, the disclaimers are in place, the
  snapshot is fresh, there are ads at all;
- *judgment (agent):* "creatives in N languages," "the offer doesn't look like the
  declared profile," "the brand in Ad Library is the wrong one" → **flag**; pass/stop is
  decided by a deterministic rule.

The same principle as everywhere: the agent **flags**, the deterministic gate **decides**.
The validator is the natural next agent under the same discipline of subordination.

> Status: **planned**, not in code yet — recorded as the missing link between "the report
> got generated" and "the report may be sent."

## Decision flow — the decision line

See [decision-flow.md](decision-flow.md): signal → validation → rock-solid fact →
validated report → human gate → outreach.

## Evidence — real runs

Three real scans, **as is** (not previews). The reports were generated by the tool
([`generate_site_report.py`](engine/generate_site_report.py)) from captured `fb.json`;
the creatives are embedded in the HTML (base64, the file is self-contained — opens
locally, loads nothing). Different advertiser scale:

| Demo | Active ads | Report | Raw data |
|---|---|---|---|
| **Large** | finom.co — **188** | [report.html](evidence/finom.co/report.html) | [fb.json](evidence/finom.co/fb.json) |
| **Medium** | semrush.com — **53** | [report.html](evidence/semrush.com/report.html) | [fb.json](evidence/semrush.com/fb.json) |
| **Small (not zero)** | indrive.com — **6** | [report.html](evidence/indrive.com/report.html) | [fb.json](evidence/indrive.com/fb.json) |

Every report shows all seven honesty mechanisms from the section above: the dated
snapshot, the mode/authority label, raw-vs-matched, the *Verify yourself in Ads
Library* button. The `fb.json` next to it is the raw captured data with provenance fields
(`discovery_method`, `ads_library_mode`, `raw_keyword_total`, `homepage_fetch_method`);
that is the "record of the run" (the batch writes no separate console log per domain).

> GitHub won't render HTML this size inline — download the file and open it in a browser
> (it is self-contained). It runs locally on the author's machine; attached here as an
> artifact.
