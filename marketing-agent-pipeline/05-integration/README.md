# 05 — Integration

> Wire the stages into one flow that can run end-to-end or block-by-block — with the
> three discovery agents able to run in parallel after the sitemap step.

> **Status:** sketch (Step 2). Described at altitude. Prose in Step 4.

## What this stage does

*[Short. The orchestration: stages as composable steps; run the whole pipeline or a
single block; parallelism where the work is independent.]*

## Code / agent boundary — where the line runs

*[Orchestration, sequencing, retries, rollback are deterministic; the agents sit inside
the steps. Handoffs are products dropped into the environment, not chatter.]*

## Human gate

*[Where a human can check or skip between stages.]*
