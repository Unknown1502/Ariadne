# Ariadne Investigation Console

React + TypeScript + Vite. No Tailwind, no chart library — see
[`docs/decisions.md`](../docs/decisions.md) §12 for why.

```bash
npm install
npm run dev        # http://localhost:5173, proxying /api to localhost:8080
npm run build
npm run typecheck
```

The API must be running:

```bash
cd .. && uvicorn backend.api.main:app --port 8080
```

## What this page is

An investigation read top to bottom, not a dashboard:

```
decision → explanation → claim → experiment → evidence → verdict → action
```

There is **no Analyze button**. The controls publish events — the same events a model
registry and a drift monitor publish — and then the page only reads. An investigation
appears because a background worker picked the event up.

## Design

The full plan is in `docs/decisions.md`. Two rules do most of the work:

**Saturated colour is reserved for verdicts.** Nothing decorative is coloured, so when
something on this page is red it means a claim was refuted, never that a designer wanted
emphasis.

**If a number is evidence, it is set in mono.** Prose never is. The typeface tells you
whether you are reading a measurement or a description of one.

The signature is *Ariadne's thread*: a hairline down the left of the investigation with a
node at each stage, which takes the verdict's colour from the verdict node downward. The
structure carries the state.

The evidence graphic is the page's central bet — a strip plot with one line per fixture
case, showing how far the score moved, against the effect threshold as a vertical rule. When
every line stops short of the rule, the predicted effect is reproducibly absent; when they
scatter across it, the honest answer is that we do not know. A summary statistic would have
hidden exactly that.

## Honesty constraints

These are requirements, not preferences:

- The banner reports the **real** configuration. If the reasoner is the offline
  deterministic one, it says so — never a Gemini badge over a regex.
- The only animation on the page marks genuinely in-flight work. There is no decorative
  motion to mistake it for, and `prefers-reduced-motion` is respected.
- Every verdict links to the evidence IDs behind it.
- The synthetic-laboratory disclaimer is visible at all times.
- No chart appears without a caption saying what it shows.
