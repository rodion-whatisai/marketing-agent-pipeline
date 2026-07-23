# 03 — Execution

> Move budget on a live campaign — scale / kill / hold — so that every movement of money
> passes a deterministic gate, leaves a trail, and can be rolled back. This is the
> **apex** of the whole discipline: here the agent touches money for the first time, so
> "the high-risk is checked by code, not by a second agent" stops being a slogan.

> **Status: designed, not in code yet.** A design block, like 02. No Meta API, no real
> account; the thresholds (target CPA, +20%, window lengths) are illustrative and tuned
> per client. Part of the reasoning is "how I would build the system," not the product of
> manual debugging: marked as such in the text.

> **The SCOPE of the `engine/` is the performance stream** (purchases / conversions).
> Lead-gen is a separate stream ([`attention-needed/`](attention-needed/)); awareness /
> reach / engagement are outside execution. Routing by campaign objective — the "Two
> streams" section below.

> **The engine's conceptual skeleton** (types · Meta stubs · signals · decision + JSON
> audit; runs on stubs, not live) — [`engine/`](engine/).

## Two streams by campaign objective + the router

The first thing in execution is to look at the campaign **objective** and route it to the
right stream. Different objectives mean different metrics and different policy — **one
engine must not judge them all**:

| objective | stream | where it lives | what we judge by |
|---|---|---|---|
| `OUTCOME_SALES` / purchases | **performance** | [`engine/`](engine/) | CPA · ROAS · basket · scale/kill/hold |
| `OUTCOME_LEADS` | **lead-gen** | [`attention-needed/`](attention-needed/) | CPL vs the city benchmark · AttentionNeeded triage |
| awareness / reach / traffic / engagement | — | — | outside execution scope (we mark it, we don't stay silent) |
| unrecognized | — | → to a human | unknown objective — into the manual review queue |

The router ([`router.py`](router.py)) only routes — beyond it, each stream lives by its
own logic. "And that's our starting point." Run: `python router.py` (a mixed portfolio →
a breakdown by stream).

## Provenance — lifted from a live spreadsheet operation

Neither stream is toy code. Their deterministic logic is **reconstructed from real
working spreadsheets**, where formulas and functions prepared the data and made the
decisions by hand (natural intelligence in the agent's seat): the planning workbook (~20
tabs: benchmark → daily plan) and the reporting dashboard (plan-vs-actual by region → a
new daily budget per city). A walkthrough of the reporting-side formulas —
[fact-pacing-budget.md](fact-pacing-budget.md) (+ data sample
[reporting-fact-sample.csv](reporting-fact-sample.csv)). Ported to Python, these formulas
are the **rebar** of the structure: the load-bearing deterministic part on top of which
the agent stays thin. The same principle as the whole block: *Python digests → the agent
eats a ready signal; a money agent = Python guards with a thin agent tail.*

## What this stage consumes and produces

**Input:** the campaign's portfolio of ad sets with metrics (`spend, CPA, ROAS,
purchases, CTR, CVR, CPM, learning_status, budget, last_edit, creative_age,
attribution_lag, AOV/basket, anchor SKU availability`) + the campaign's goal and budget.
Example — [mock-adsets.csv](mock-adsets.csv) with 5 ad sets (table below).

**Output:** for each ad set — a **scale / kill / hold / → human approval** decision + a
budget movement, each of which passed the deterministic gate, was written to the audit
log, and is reversible (rollback). Plus the **campaign forecast**: will the portfolio
deliver the planned result and spend the budget.

## Lead thesis — this is where the money starts

Until now the agent only read and proposed. In execution it **touches the budget** — the
most irreversible action in the pipeline. So the discipline of "the agent judges, Python
gates, the human approves" is not decoration here — it's protection against burning
money.

And here is what turns out when you decompose real UA logic (scale / kill / hold) into
"rule vs judgment": **almost everything is a rule.** Attribution windows, ranks, ratios,
trends, thresholds, the forecast — deterministic. The agent stays thin (phrase the
"why," propose within the gate); the human stays on the irreversible (kill, a large
budget, manual inspection). A money-moving agent is mostly **Python guards with a thin
agent tail**, not "a smart agent entrusted with the wallet."

## The code / agent / human boundary

| Part | Who | Why |
|---|---|---|
| attribution windows (×3) + their average, smearing spend forward, weighting the result by expected inflow | **Python** | arithmetic over time, not judgment |
| total / blended CPA·ROAS, the utility ratio (% of sales ÷ % of budget) | **Python** | aggregation and ratios |
| detecting "budget not being spent," the +20%/step scale cap, pacing | **Python** | thresholds and limits |
| rank dissonance (median CTR/CVR), the CPA trend, the 2×5-day protocol, B-ranks | **Python** | ranks and trends over a table |
| the `ROAS = AOV / CPA` decomposition, the per-ad basket profile | **Python** | decomposition, aggregation |
| the "innocence check" (attribution · site · stock) before a kill | **Python** | a deterministic checklist |
| the campaign forecast (will deliver the result + will spend the budget) | **Python** | a projection over numbers |
| the "why" an ad pulls cheap / full baskets; phrasing the reason for the audit; proposing an action within the gate | **agent (thin)** | hypothesis, not rule |
| manual site inspection + Clarity sessions (the dissonance→site branch); approving a kill / a large budget; restocking | **human** | irreversible / outside advertising |

## Decision policy — scale / kill / hold

The agent **proposes**, the deterministic rule **gates**. The thresholds below are
illustrative.

- **Scale** — if the ad set consistently **spends its full budget** (see the 5-day
  spend) and is efficient (CPA < target, utility > 1) → raise the budget **gradually, at
  most +20%/step**. If the budget **isn't being spent**, managing it is pointless (it's
  hitting a delivery ceiling, not a budget one) — scale won't help.
- **Kill** — low ROAS + high spend + low CTR + CPA a multiple over target (case **B**),
  **but only after passing the "innocence check"** (below). Granularity: at the ad set
  level → **fix the creative first**; at the individual ad level → **switch it off**.
- **Hold** — low spend + learning → **don't touch** (an edit resets learning), barring
  strong signals. The protocol: **window 1 (5 d)** with no improvement in the CPA trend →
  record the judgment, hold for another window; **window 2 (+5 d)**: flat → **kill**, up →
  **hold**, down → **kill**. Each judgment is written as a snapshot (like the brief
  stream in 02).

## The "innocence check" — a checklist before any kill

A "bad CPA/ROAS" signal is not yet the ad's fault. Before the agent kills an ad set, the
code must rule out three external causes (execution's version of "absence of signal ≠
signal" from 01):

1. **Attribution** — the conversions haven't arrived yet. If most of the spend is fresh
   and the attribution window hasn't closed (case **D**: 5 days ago it showed 0, now
   18) → not kill, **wait**.
2. **Site** — **dissonance**: the ad set is in the top half on CTR but the bottom half on
   CVR. The creative pulled people in, but on the site they didn't find it / didn't buy.
   Good CVR + bad CTR → change the creative; good CTR + bad CVR → a **site** problem →
   to a human for manual inspection + Clarity.
3. **Stock** — the **anchor SKU** (the one the basket starts with) is out of stock.
   Anchor OOS → the basket collapses, sales drop, the ad is not at fault → **block the
   kill + flag**.

Only if all three come back "innocent" does the kill reach the gate.

## Confidence / attribution

We don't judge fresh spend by conversions that haven't matured:
- we look at the data under **3 different attribution windows** and compute the **average
  window**;
- we **smear recent spend forward** across the window (what's spent today yields
  conversions in ~N days) and **weight the observed result by the expected inflow** —
  only then compute the CPA/ROAS used for the decision;
- if it's **same-day click→purchase** (no lag) — last-click is reliable, we believe our
  eyes.

> *The author has not "clicked through" these cases by hand — this is reasoning about how
> he would build an ad set management system, not the result of manual debugging.*

## The structure of the result — basket and stock

"Purchase" is not atomic: it's a **basket of several products** or a **single item**.
Hence:

- **`ROAS = AOV / CPA`** — an anomalous ROAS with a normal CPA is more often about the
  **basket** (what was bought) than about cost. We compute a **per-ad basket profile**
  (average order value, items per basket, product mix) from the orders tied to the ad
  set.
- In the mock, AOV is held flat at $60 (a simplification). If **D**'s baskets are large
  (AOV $120) → ROAS is 1.44, not 0.72 — not a bleeder; if **A** sells a cheap single
  item (AOV $25) → ROAS is 1.25, not 3.0 — over-scaled. **The basket flips both kill and
  scale.**
- Deeper still — **margin**: revenue ≠ profit, basket composition hits the margin.
  *(next, out of scope.)*

## Portfolio + the campaign forecast

- **The utility ratio** = an ad set's % of sales ÷ % of budget. >1 — pulls above its
  weight; <1 — ballast (see the table below: A = 1.79, B = 0.39).
- **The campaign forecast** (a campaign-level guardrail): after edits to the ad sets,
  check — **will the portfolio deliver the planned result AND spend the campaign
  budget**. The anti-pattern: locally over-optimize the ad sets → the campaign
  underspends and underdelivers. Local optimization must not break global delivery.

## Guardrails — what the agent may NOT do

- **THE AGENT NEVER DELETES** — not a campaign, not an ad set, not an ad, not data.
  "Kill" = pause / switch off / archive, **not** deletion. Deletion is irreversible and
  breaks both audit and rollback; the whole discipline rests on the prior state always
  being preserved.
- no money movements past the gate; a kill only after the "innocence check";
- scale — **at most +20%/step**; no-edit during learning; spend caps (daily / account);
- portfolio invariants (the sum = the budget, no over-concentration, a per-client cap);
- anything large / irreversible (killing a campaign, a big budget shift, the refresh on
  **E** at $700/day) — not auto, but into the **human queue**.

## Human approval · audit · rollback

- **Approval layer** — a deterministic router: small / reversible within the guards is
  **auto-approved**; large / irreversible → the **human queue**.
- **Audit** — an append-only record for every decision: `{ts, ad_set, signals, which
  rule fired, innocence check, the agent's proposal, the gate's verdict, the human's
  action, before/after}`. The provenance of 01 + the snapshot stream of 02, applied to
  money: every dollar traces back to a rule and an approval.
- **Rollback** — every action stores its prior state; auto-revert if a guard fires after
  the action, or at the push of a human's button.

## Backtest / shadow / policy-sim — how to earn the right to move money

The same trust ladder as in the root [README](../README.md), but about real money:
- **backtest** — run the policy over history: would it have helped or hurt;
- **shadow mode** — live, but it only **logs** decisions, doesn't apply them; reconciled
  against the human;
- **policy simulation** — run a policy change against the mock dataset before deploying.

The agent earns the right to auto-move money only by climbing this ladder.

## The mock dataset (5 ad sets)

Synthetic, illustrative — [mock-adsets.csv](mock-adsets.csv). AOV held flat at $60,
target CPA ~$30. Each ad set is its own story (winner / kill / learning / attribution
trap / fatigued creative).

| Ad set | Spend 7d | Budget/d | Purch | CPA | ROAS | CTR | CVR | CPM | Learning | Creative | Last edit | Attr lag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A** winner | $4,200 | $600 | 210 | $20 | 3.0 | 2.8% | 2.5% | $14 | done | 21 d | 12 d | low |
| **B** bleeder | $3,000 | $300 | 33 | $91 | 0.66 | 0.9% | 2.4% | $20 | done | 40 d | 18 d | low |
| **C** learning | $280 | $80 | 6 | $47 | 1.28 | 1.9% | 1.8% | $16 | learning | 4 d | 2 d | medium |
| **D** attribution | $1,500 | $250 | 18 | $83 | 0.72 | 2.2% | 0.7% | $13 | done | 15 d | 9 d | **high** |
| **E** fatigued | $5,800 | $700 | 145 | $40 | 1.5 | 1.1%↓ | 5.5% | $24↑ | done | 38 d | 25 d | low |

**Total / utility:**

| Ad set | % budget | % sales | Utility (% sales ÷ % budget) |
|---|---|---|---|
| **A** | 28% | 51% | **1.79** ⭐ |
| **B** | 20% | 8% | **0.39** |
| **C** | 2% | 1.5% | 0.77 |
| **D** | 10% | 4% | **0.43** * |
| **E** | 39% | 35% | 0.90 |
| **TOTAL** | **$14,780** spend · **412** purch | — | blended **CPA $35.9** · **ROAS 1.67** · budget **$1,930/d** |

`*` D = 0.43 is depressed by the attribution lag — it will climb as conversions flow in
(which is exactly its case).

## The engine sketch — `engine/`

A conceptual Python skeleton of the policy engine — [engine/](engine/): types
(`schema.py`) · Meta API **stubs** with real endpoints (`meta_api.py`) · deterministic
signals (`signals.py`) · decision + JSON audit (`policy.py`). Runs on stubs
(`python engine/policy.py`), **not on a live account** — a sketch, not production.

It embodies the principle of **"Python digests → the agent eats ready signals"** (LLMs
read raw data poorly) and covers all 9 criteria of the reviewer's test (delayed
attribution · premature-kill defense · creative/site/offer/stock · budget conservation ·
campaign delivery · audit · human gate · agent-vs-code · mock-history).

The run over the 5 ad sets (gate 1 innocence → gate 2 learning → gate 3 efficiency):
A→`scale`, B→`send_to_human` (creative), C→`hold` (learning), D→`hold` (attribution
hasn't matured — wait), E→`send_to_human` (creative fatigue). `test_policy.py` pins these
outcomes.

## Rule constructor + config intake

The engine is a **rule constructor**: read the maximum (Advantage+, CBO/ABO, bid
strategy / cost cap / ROAS goal, attribution window, placements, backend) → a candidate
rule → test → promote into the permanent Python hierarchy (Python or a human confirms "it
got better"). The "under test" pool keeps refreshing — the same move as `patterns.json` +
`learn.py` in 01.

The optimal **sequence** for weighing all the arguments is a research question; for now
the code takes maximum data + simple models (on/off/change, all else being equal). That
is the answer to "are ABO/CBO/Advantage+ accounted for": reading them — yes; combining
them optimally — research.

See [rule-constructor.md](rule-constructor.md) · `python engine/rules.py` (the rule
registry + a sample intake config).

## How CBO / ABO / Advantage+ change the decision layer

Where the budget lives determines what can be "scaled" at all:

- **ABO** (budget on the ad set) — `scale` raises the specific ad set's budget. The
  default in the mock.
- **CBO / Advantage+** (budget on the campaign) — Meta distributes the budget across ad
  sets **itself**. Pulling on an ad set's budget is **pointless**: the optimizer pours it
  back. The move has to happen at the **campaign** level (or via the non-budget levers —
  targeting, creative, bid).

So under CBO an efficient ad set doesn't go into ad-set-level `scale` but into the
**campaign-level** queue. Recorded as a **mandatory** reserved rule
(`rules.RESERVED → cbo_no_adset_budget`) and checked in `policy.decide` (gate 3) before
any scale.

## Roadmap — what to build first (in 2 weeks)

From safe to risky (the right to move money is earned up the trust ladder):
**read-only Meta insights → policy shadow mode (logs only) → audit log → human approval
UI → and only then write actions.** Details and tracking — [to-do.md](to-do.md).

## The ring back to 01

The "site problem" (dissonance) and "stock" branches land in stage
**[01](../01-client-discovery/README.md)**: the scanner already loads the page and
catches a dead Add-to-Cart / an "out of stock." Execution caught the signal → pinged
discovery. The pipeline closes into a ring.

## Open questions

- exact thresholds (target CPA, attribution windows, +20%, hold/kill window lengths) —
  tuned per client, illustrative here;
- **margin** (revenue ≠ profit) — the next layer, out of scope;
- **Meta API** integration + a stock feed — infrastructure, not built;
- the "anchor SKU" and the basket profile require order-level data — available in
  e-comm, not in the mock.
