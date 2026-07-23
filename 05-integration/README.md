# 05 — Integration

> Tie the stages into one flow that can be run whole or block by block — with three
> discovery agents able to run in parallel after the sitemap step.

> **Status:** an outline — overview only, not filled in. Worked through in depth: 01
> (code) and 02–03 (design).

## What this stage does

*[In brief. Orchestration: stages as composable steps; run the whole pipeline or a
single block; parallelism where the work is independent.]*

## The code / agent boundary — where the line runs

*[Orchestration, sequencing, retries, rollback are deterministic; the agents sit inside
the steps. Handoffs are products placed into the environment, not chatter.]*

## The human gate

*[Where a human can inspect or wave things through between stages.]*
