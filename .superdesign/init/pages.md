# Pages

Single page. Full dependency tree:

## / (only page)
Entry: `src/App.tsx`
Dependencies:
- `src/api.ts` — typed fetch client + helper functions (`signed`, `percent`, `verdictClass`);
  no components, just types + `fetch()` wrappers and formatting helpers. All API calls are
  relative paths (`BASE = ""`), proxied by nginx in production.
- `src/components/Investigation.tsx` — the main content component (see `components.md`)
  - `src/components/DeltaPlot.tsx` — the evidence chart (imported by Investigation)
  - imports types + `percent`/`signed`/`verdictClass` from `../api`
- `src/components/Panels.tsx` — exports `Lineage`, `Debt`, `Fleet`, `Runtime` (see
  `components.md`)
  - imports types + `signed`/`verdictClass` from `../api`
- `src/styles.css` — imported once, in `main.tsx`, globally (not CSS modules, not scoped)

No lazy-loading, no code-splitting, no Suspense boundaries — everything above ships in one
bundle (172 KB JS / 54 KB gzipped per the last local build).

## Candidate `--context-file` set for redesigning this page
Everything above, plus (for tokens/typography) `src/styles.css` and `index.html`
(font links). That is the whole app — there is no larger codebase to trim from; passing all
six files above is well within budget.
