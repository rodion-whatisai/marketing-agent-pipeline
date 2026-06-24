# Marketing Agent Pipeline

> An advertising agency rebuilt as a pipeline of single-task agents: each agent does
> one narrow job and hands a finished product to the next. Not a swarm of agents
> talking to each other — a factory line of departments, where a human can check or
> skip at every handoff.

> **Status:** skeleton (Step 2). Section prose is filled in Step 3. Bracketed italics
> mark what goes where.

## The credo

*[2–4 lines. Agents for judgment, Python for the ironclad. Anything expressible as a
deterministic rule — math, limits, reconciliations — is written once in code and
tested once, so it can be trusted. The agent (token-based, non-deterministic) is left
only with what no rule can express. This breaks the "who checks the checker" regress:
the verifier of a high-risk action is deterministic code, not another agent.]*

## Why this shape

*[Short. Why not a swarm of chatty agents — coordination is where multi-agent systems
break, and this has to be built, not researched. Why high-risk verification cannot be
delegated to a second agent.]*

## The pipeline

*[One line per stage. 01 is the only stage with real, shipped code and is worked
deeply; 02 sets the template; 03–05 are sketched at altitude.]*

| Stage | What it does | Depth here |
|---|---|---|
| **01 — Client discovery** | *[find + qualify prospects from hard, verifiable signals]* | deep — real shipped code |
| **02 — Planning** | *[decompose a brief into Python-checkable vs agent-judgment parts]* | template |
| **03 — Execution** | *[move budget / launch — where money and kill / don't-kill policy live]* | sketch |
| **04 — Reporting** | *[turn results into segments and cohorts, not averages]* | sketch |
| **05 — Integration** | *[wire the stages into one runnable, partly-parallel flow]* | sketch |

## The trust ladder

*[How an agent earns the right to act: backtest on history → shadow mode →
human-in-the-loop → limited autonomy with auto-rollback. Money and "kill / don't kill"
decisions appear only at the execution stage (03), as explicit, auditable policy.]*

## How to read this repo

*[Start at 01 — it carries the real code and the fully-told decisions. Then the root
boundary idea repeats in every stage: deterministic code = ironclad, agent = judgment,
human = gate. 03–05 are intentionally short.]*
