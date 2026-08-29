# Components

**Framework:** React 18 + TypeScript, Vite build. **No component library** (no shadcn/ui,
MUI, Chakra, Radix, Ant) — every visual element is a plain HTML element styled with
hand-written CSS classes in `src/styles.css`. **No CSS framework** (no Tailwind) — vanilla
CSS with custom properties (`:root` tokens) and BEM-ish class names (`.verdict__status`,
`.lineage__node--supported`).

There is no separate `components/ui/` primitives folder. The closest things to shared
primitives are CSS classes applied inline via `className`:

- `.btn` / `.btn--primary` — button
- `.card` — bordered panel, `.card__label` — its eyebrow label
- `.chip` / `.chip--warn` — small pill/tag
- `.table` — data table
- `.metric` / `.metrics` (grid) — a labelled stat cell
- `.mono` — tabular-numeral monospace text (used for every number/hash/id)
- `.dim` / `.ok` / `.bad` / `.warn` — semantic text colour utilities
- `.empty` — dashed-border empty state

## Page-level React components (the real reusable units)

### `Investigation` — `src/components/Investigation.tsx`
Renders one investigation as a top-to-bottom narrative: hero (decision + explanation +
verdict badge), then a vertical "thread" of stages (Claim → Experiment → Evidence → Verdict
→ Action), each stage a `<section className="stage">` with a dot on the spine. Full source:

```tsx
/* The investigation, read top to bottom.
 *
 * decision -> explanation -> claim -> experiment -> evidence -> verdict -> action
 *
 * Ariadne's thread runs down the left with a node per stage, and takes the verdict's colour
 * from the verdict node downward. Everything shown here comes from one API response; the
 * console does not compute or infer any of it.
 */

import type { InvestigationDetail, VerdictStatus } from "../api";
import { percent, signed, verdictClass } from "../api";
import { DeltaPlot } from "./DeltaPlot";

const STAGE_ORDER = [
  "INGESTING",
  "CLAIM_EXTRACTED",
  "PROBE_PLANNED",
  "INTERVENTION_VALIDATED",
  "EXPERIMENT_RUNNING",
  "VERIFICATION",
  "LINEAGE_UPDATED",
  "DEBT_RECALCULATED",
  "GOVERNOR_ACTION",
  "COMPLETE",
];

function reached(state: string, step: string): boolean {
  if (["COMPLETE", "REVIEW", "FAILED", "QUARANTINED"].includes(state)) return true;
  return STAGE_ORDER.indexOf(state) >= STAGE_ORDER.indexOf(step);
}

interface Props {
  detail: InvestigationDetail;
}

export function Investigation({ detail }: Props) {
  const { investigation, decision, claim, experiment, evidence, verdict, action } = detail;
  const status: VerdictStatus | null = verdict?.status ?? null;
  const tone = verdictClass(status);
  const running = !["COMPLETE", "REVIEW", "FAILED", "QUARANTINED"].includes(
    investigation.state,
  );

  return (
    <>
      <section className="hero">
        <div className="card">
          <p className="card__label">Decision under investigation</p>
          <p className="decision">{decision.decision ?? "—"}</p>
          <blockquote className="quote">
            "{decision.explanation ?? "No explanation was supplied."}"
            <cite>
              {investigation.scope.model_id} v{investigation.scope.model_version} ·{" "}
              {investigation.scope.distribution_version}
            </cite>
          </blockquote>
        </div>

        <div className={`card verdict ${tone ? `verdict--${tone}` : ""}`}>
          <p className="card__label">Verdict</p>
          {verdict ? (
            <>
              <p className="verdict__status">{verdict.status}</p>
              <p className="verdict__scope">
                true of v{verdict.scope.model_version} on{" "}
                {verdict.scope.distribution_version}, under intervention protocol{" "}
                {verdict.verifier_version}
              </p>
              <div className="metrics">
                <Metric label="Effect" value={signed(verdict.effect_size)} note="mean change in score" />
                <Metric
                  label="Control"
                  value={verdict.control_effect_size === null ? "not run" : signed(verdict.control_effect_size)}
                  note="competing feature"
                />
                <Metric label="Reproducibility" value={percent(verdict.reproducibility)} note="cases matching" />
                <Metric label="Validity" value={percent(verdict.intervention_validity)} note="probe quality" />
              </div>
            </>
          ) : running ? (
            <p className="verdict__pending"><span className="spin" /> Running — no verdict yet</p>
          ) : (
            <>
              <p className="verdict__pending">No verdict</p>
              <p className="verdict__scope">{investigation.last_error ?? "This investigation produced no verdict."}</p>
            </>
          )}
        </div>
      </section>

      <div className={`thread ${tone ? `thread--${tone}` : ""}`}>
        {/* Stage: Claim - IF/THEN/SHOULD logic derived from the compiled claim */}
        {/* Stage: Experiment - three "arm" cards (baseline / intervention / control) */}
        {/* Stage: Evidence - <DeltaPlot> per-case chart + effect/CI/instability metrics */}
        {/* Stage: Verdict - reason-code chips + machine-generated rationale text */}
        {/* Stage: Action - Governor's action name + reason codes + next-audit time */}
        {/* Each stage wrapped in <Stage name hint done modifier> - see component below */}
      </div>
    </>
  );
}

function Stage({ name, hint, done, modifier, children }: {
  name: string; hint: string; done: boolean; modifier?: string; children: React.ReactNode;
}) {
  return (
    <section className={`stage ${done ? "stage--done" : ""} ${modifier ? `stage--${modifier}` : ""}`}>
      <div className="stage__head">
        <span className="stage__name">{name}</span>
        <span className="stage__hint">{hint}</span>
      </div>
      {children}
    </section>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="metric">
      <span className="metric__label">{label}</span>
      <span className="metric__value">{value}</span>
      {note && <span className="metric__note">{note}</span>}
    </div>
  );
}

function Arm({ name, what, stat, statLabel }: { name: string; what: string; stat: string; statLabel: string }) {
  return (
    <div className="arm">
      <p className="arm__name">{name}</p>
      <p className="arm__what">{what}</p>
      <p className="arm__stat">{stat}</p>
      <span className="metric__note">{statLabel}</span>
    </div>
  );
}

function Waiting({ running, what }: { running: boolean; what: string }) {
  return (
    <div className="empty">
      {running ? <><span className="spin" /> waiting for the {what}</> : <>No {what} was produced for this investigation.</>}
    </div>
  );
}
```

*(Full stage bodies elided above for length — the actual file has five `<Stage>` blocks:
Claim, Experiment, Evidence, Verdict, Action. Read `src/components/Investigation.tsx`
directly for the complete JSX if redesigning stage internals.)*

### `DeltaPlot` — `src/components/DeltaPlot.tsx`
Custom inline SVG chart (no charting library). One horizontal line per fixture case, from
baseline score to post-intervention score, plus a dashed threshold line and a lower "control
arm" band in muted grey. See full source in the repo — key facts for redesign:
- SVG `viewBox="0 0 720 <dynamic height>"`, `rowHeight=9px` per case line.
- Colour is entirely driven by CSS vars: `var(--supported|contradicted|inconclusive)`.
- No axes/legend chrome beyond inline `<text>` labels — reads as a lab instrument trace.

### `Lineage`, `Debt`, `Fleet`, `Runtime` — `src/components/Panels.tsx`
Four independent section components, each rendering one supporting panel below the main
Investigation thread:
- **`Lineage`**: horizontal row of clickable "node" cards, one per model version, coloured
  by that version's verdict — a version timeline / small-multiples strip.
- **`Debt`**: a large number (`.debt__total`, 44px) + a small trend sparkline (custom SVG
  polyline, not a library) + a horizontal stacked-bar breakdown of debt components.
- **`Fleet`**: a table of the four agent roles (Investigator/Experimenter/Verifier/Governor)
  with their write-scopes, tools, reasoner, and health status.
- **`Runtime`**: the "proof" panel — two-column grid of live counters (events published,
  processed, duplicates suppressed, etc.), plus conditional cards for pending approvals,
  scheduled audits, and dead-lettered events, plus a static reference table of the four
  target-model formulas.

Full source for all four: read `src/components/Panels.tsx` (453 lines) — not reproduced in
full here for length; the structural summary above is sufficient for a redesign brief.
