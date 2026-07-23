# 02 — Planning

> Turn the client's settled goals into a media plan — and decompose every step in advance
> into what deterministic Python computes and checks, and where agent judgment is needed.

> **Status: designed, not in code yet.** This is a design block. Unlike 01, there are
> **no clickable links to `.py` files** here — planning so far exists as a decomposition
> (what should become a verification script and what should become the agent part), not as
> working code. Everything below marks this as a design, not a built thing.

> Why such a block belongs in the showcase: to show that the "code / agent / human"
> boundary is laid down *before* the code — it's the default way of thinking, not
> after-the-fact dressing. The same move as the "validator" node in 01, only here the
> whole stage is marked that way.

## What this stage consumes and produces

**The input is not a ready-made brief "out of nowhere."** The brief is born jointly by
**at least three "faces"**: a human on the client's side (the client / the strategist —
may be one person, that's the bottleneck) and two mirror agents on our side — the
**"client mirror"** (anticipates answers, fills in the description of the business) and
the **"strategist mirror"** (starts shaping the lens and the offer from the very first
answer). They pass the brief back and forth until it settles; the client, meanwhile,
talks to all of this as **one entity** — a single client service, not three separate
things. How exactly that works — a sketch in [brief-formation.md](brief-formation.md).

By the time planning starts computing, the input is: settled KPIs/goals + the brief + a
strategic estimate of the channel mix — "either based on historical data or based on
expertise."

**Output:** the media plan — "one long sheet," a row per line item: placement, flight
dates, the KPIs baked in, the impression → CTR → clicks → conversion events → forecast
cost calculation, a forecast per line. Each ad platform is modeled as a separate agent,
all merged into one canvas.

**Stage boundary:** collapsing all channels into the common sheet, sorting "by maximum
volume and minimum cost," and cutting the budget options (min / opt / max) we assign to
**media buying** — the step *after* planning. The block ends here; downstream is marked
"out of bounds."

## Lead thesis

Planning is modeled as **a department, not a reasoning monolith**: each ad platform is a
separate agent with its own row, and all of them are merged into one shared plan canvas.
The plan is built **on historical data or on expertise** (which comes first is an open
fork — see below).

And goal-setting rests on the **business metric, not a proxy**. An incoming request of
"we need 10 thousand clicks" is not a goal, it's proxied noise; the goal is "100 sales."
Clicks, reach, impressions are intermediate knobs; at the output we measure the client's
money. This substitution is caught by the human at the gate (below), not by an agent.

## The code / agent boundary

The same principle as across the whole pipeline: **the deterministic — in code, judgment —
in the agent, the high-risk is checked by code, the human is the gate.** In planning it
splits like this:

| Part | Code or agent | Why |
|---|---|---|
| channel mix · audience strategy (clusters / themes / angle / positioning) · the "what to say to the segment" hypothesis · targeting combos · creative requests | **agent (judgment)** | "the AI works with what isn't load-bearing" — this is "what to say / what to try," not "what to compute" |
| budget convergence · forecast mechanics (capacity → CPM → impressions → CTR → clicks → conversions → cost) · the media split across line items | **deterministic Python** | "many things are load-bearing and must be executed by a function"; the checker is "Python, not another agent" |
| weighing the brief against the business goal · plan acceptance | **human (gate)** | proxy-vs-business-goal is a judgment about intent, not a rule |

The forecast mechanics, as described: segment capacity = "80% of what Meta shows at
maximum reach" × average frequency (~3) → a mid estimate of purchasable impressions →
benchmark CPM → monthly budget per line item → impressions → CTR → clicks → conversions →
cost per target action.

**What these mechanics look like on real numbers** — a day-by-day budget breakdown,
reverse-engineered from a client's working media plan (the same funnel, taken down to the
day): [daily-budget-pacing.md](daily-budget-pacing.md). The core: the day's budget flows
inversely proportional to the period's CPL, `daily_budget = monthly × (1/CPL_d) / Σ(1/CPL)`,
and the days sum back to the monthly budget. A real sample (the Dallas ad set, 30 days) —
[daily-budget-sample.csv](daily-budget-sample.csv).

> **Caveat.** The forecast is described as a calculation, but the decomposition never
> explicitly labels it "this is Python" — assigning it to code follows from the project
> credo ("the checker is Python"), not from a separate statement. And there is no
> element-by-element "why exactly creative / message / segment choice can't be reduced to
> a rule" — only the general principle.

## Engineering decisions — and how we got to them

**A swarm of chattering agents → a pipeline of separate entities.** Multi-agent systems
"are at the stage of scientific experiments rather than construction, and we need to
build." So the agents "don't talk to each other directly; they hand each other an already
finished product."

**A checker agent → Python.** "The checker should not be another agent… but some Python."
The reason is determinism: an agent runs on tokens, its reliability isn't proven by one
lucky run, so high-risk verification can't be handed to a second agent.

**Imitating an agency, not a chain of photo studios.** The agency is "the most structured,
factory-like" of the models considered; for the AI-fication role it "fits even better
than a chain of photo studios."

**Platform = agent.** Each media plan line item is modeled as a separate agent, and "all
are merged into one big plan canvas."

**The goal is a business metric, not a proxy.** "10 thousand clicks" is discarded in
favor of "100 sales" (consideration vs performance — "different funnels, different
profits").

**The planning skill is not universal.** "A separate skill for each campaign type, for
each flight type" — not one planner for everything.

**Deliberately deferred / open** (we don't close these on the author's behalf):
- what to plan on — "a chicken-and-egg question: which comes first, expertise or
  historical results";
- business insights as a feedback loop — "not building that in yet — leaving the door
  open";
- "proper data is taken as a given."

## Guardrails and data integrity

**The one named real guardrail is the budget guard, and it's the stage's kill switch, in
code.** If the agent is given a $500 budget and comes back with $700 on the output, the
check is done **not by another agent but by Python**, comparing what was issued against
what was computed, with gradation:

- a moderate overrun → the code "initiates additional argumentation for why it isn't
  $500" (the plan moves on, carrying its justification);
- a gross one (an overrun "of 50x, roughly speaking") → **block**.

No other planning guardrails are named individually. Possible extensions (line items
summing to the issued budget; the bid within an allowed range; delta from forecast ≤ X%;
frequency caps; CPM thresholds) were discussed, but **as candidates, not a fixed
position** — so they are not written into the built list. Exact thresholds (other than
"×50, roughly") are not given as numbers.

## Open questions — what is not yet resolved

As with the validator in 01: the stage is designed, but in several places deliberately
left unclosed.

- **Where exactly planning ends** — collapsing the sheet and the budget options are
  assigned to media buying; the exact boundary is still to be cut.
- **Code vs draft** — the decomposition "already exists, being enriched"; its readiness
  level (text vs working script) and where it lives — not specified.
- **History vs expertise** at the start of a specific plan — "chicken / egg," unresolved.
- **Exact gate thresholds** — only "×50, roughly" is given.
- **Why, element by element,** creative / message / segment can't be reduced to a rule —
  there was no justification, only the general principle.
- **Money at the planning stage** — not stated directly; taken as "none is spent, the
  real spend is in execution (03)" per the project-wide decision.

## Decision flow — the decision line

See [decision-flow.md](decision-flow.md): three "faces" → the brief (a single client
service) → channel mix → human gate → plan construction (platform = agent) →
forecast → the Python budget guard → the media plan sheet; downstream (media buying) is
cut off with a dashed line. In parallel — the BI (audience) and Creative tracks.
