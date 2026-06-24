# 01 — Client Discovery

> Find and qualify a prospect from hard, verifiable signals — does it spend on ads, and
> is its tracking broken — and fix those into ground-truth facts you can walk in with.

> **Status:** skeleton (Step 2). This block is the exemplar; its section format is the
> template every other block follows. Prose filled in Step 3.

## What this stage produces

*[The output = ground-truth facts that justify approaching a prospect. Python here is
the source of truth for every downstream agent: signal → validation → ironclad fact.]*

## The lead thesis

*[Find businesses that just started running Meta ads but are tracking them wrong, in a
size band worth pitching → tier-1 lead. The qualifier is a tunable numeric rule (e.g.
an active-ads-per-country/month cutoff). Re-aim the whole tool at a different client
profile by changing the rule — not by changing the code's judgment. (The cutoff itself
is illustrative — not yet shipped; the shipped part is producing the hard number.)]*

## Code / agent boundary

*[The heart of this block. What is 100% deterministic Python (ironclad) and why. The
single point in all of discovery where an agent (Claude Haiku) is used — URL meaning —
and how it is fenced on both sides: a deterministic ladder in front of it, a
deterministic event-mapping behind it, a provenance stamp on every result.]*

| Part | Code or agent | Why |
|---|---|---|
| *[sitemap, platform, pixel/event capture, FB & Google ad detection, fuzzy match, reporting]* | deterministic Python | *[verifiable, repeatable, cheap]* |
| *[URL semantic type — only the leftover the rule-ladder can't catch]* | agent (Haiku) | *[judgment, not a rule]* |
| *[expected tracking events for a type; lead qualification]* | deterministic Python | *[fenced after / around the agent]* |

## Engineering decisions — and how we got here

*[Per non-trivial decision: what the problem was → why this way → what was rejected.
Sourced from the author's notes, not invented. Stubs to fill in Step 3:]*

- *[Accumulating pattern base (`patterns.json` + interactive `learn.py`): not regex-only,
  not LLM-only — token cost at scale across very many runs.]*
- *[Facebook Ads Library strategy: reached by iteration — brand-name search → ID search →
  keyword-string combination → trying variations; hence classic-vs-new-style ID routing
  and fuzzy fallback.]*
- *[Google Transparency Center: logical continuation of FB (cleaner results). Next step:
  the EU-mandated open ad database as an extra European source.]*
- *[Journey simulation (`clicker`): click through to the conversion event, stopping short
  of real payment; per-site-builder mapping of which element fires which event.]*
- *[`unverified` bucket: don't call a gap what you can't verify (booking tools → flag for
  manual review, not a false "they have a gap").]*
- *[WAF: honest "couldn't reach it → manual verification recommended"; solutions out of
  scope, stated plainly.]*

## Guardrails & data integrity

*[Provenance on every fact (`method=`, `source=`, `*_mode`, fetch-error taxonomy). Tuned
thresholds (0.85, raised from 0.75 after a real false positive). Safe degradation under
failure. Page-status taxonomy that never mixes (OK / GAP / NO TRACKING / No CTA). A
high-risk action (form submit) refused by a code rule, not by the agent.]*

## Decision flow

See [decision-flow.md](decision-flow.md) — signal → validation → ironclad fact → gate.

## Evidence (real runs)

*[Three real scans, shown as-is — large (many ads) / medium (a few) / smallest non-zero
— with their logs and outputs, so the result is concrete. Runs locally on the author's
machine; shown here as artifacts. Short, clean code excerpts illustrate the boundary.]*
