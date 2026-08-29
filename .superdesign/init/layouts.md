# Layouts

**No separate layout components exist.** This is a single-page app with no router, no nav
bar, no sidebar, no header/footer components as distinct files. `App.tsx` *is* the entire
page shell — there is nothing above it except `main.tsx`'s root render.

## Root shell — `src/main.tsx`

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

## Page shell — `src/App.tsx` (also the only "layout")

Structure, top to bottom, inside `<div className="app">` (max-width 1180px, centered):

1. **`.honesty`** — a thin top strip (11px mono, uppercase, letter-spaced) stating the
   system's real runtime config: reasoning provider, verifier version, cloud/local +
   event-bus transport. This is a deliberate "nothing here is faked" disclosure bar, not
   decoration — it must stay legible and always-visible.
2. **`.masthead`** — eyebrow label + H1 ("AI decision under investigation") + one paragraph
   of scene-setting copy (the triage-nurse framing).
3. **Event-emission card** (`.card`, `style={{marginBottom: 32}}`) — the interactive control
   panel: 4 "deploy vX.0.0" buttons + "distribution shift" + "send a duplicate event",
   followed by an explanatory caption and inline error text.
4. **`<Investigation>`** (conditional) or **`.empty`** state — the main content.
5. **"Every investigation" table** (conditional, only if >1 investigation exists) — a list of
   all runs with model/trigger/verdict/effect/state columns and an "open" button per row.
6. **`<Lineage>`** (conditional) — version-timeline strip.
7. **`<Debt>`** (conditional) — Explanation Debt panel.
8. **`<Fleet>`** (conditional) — agent roster table.
9. **`<Runtime>`** (conditional) — live proof-of-execution panel.
10. **`.footnote`** — two paragraphs of scope/limits disclaimer text, small and muted, at the
    very bottom.

## Data flow (relevant for redesign — determines what states the UI must handle)

- One `refresh()` function polls 5 endpoints in parallel every 1200ms (`POLL_MS`) via
  `window.setInterval`. There is no websocket/SSE — pure polling.
- The page has **no client state that isn't a mirror of server state** except: `selectedId`
  (which investigation row is open), `distribution` (current distribution string, client-
  tracked to gate the "distribution shift" button), `busy` (disables buttons during an
  emit), `error` (last caught error message, shown inline).
- `busy` disables ALL emit buttons at once during any in-flight action — no per-button
  loading state today. Worth reconsidering in a redesign (per-button spinner vs. global
  lock) since multiple distinct actions exist.
- Every section below the hero is conditionally rendered only once its data exists — so the
  page visibly *grows* as a user's first investigation completes and panels populate. A
  redesign should preserve (or deliberately improve) this progressive-reveal behavior rather
  than always rendering empty shells.
