# Extractable Components

This app has no atomic UI-primitive layer (no `Button`, `Card`, `Badge` as separate
components — those are CSS classes applied ad hoc). The extractable units are the five
page-section React components. None are true "layout" components (no nav/sidebar/footer as
components — see `layouts.md`), so everything below is categorized `feature` rather than
`layout`/`basic`.

## InvestigationHero + VerdictBadge (currently inline inside `Investigation`, not yet split)
- Source: `src/components/Investigation.tsx` (top ~40 lines)
- Category: feature
- Description: Two-column hero — left card shows the decision + quoted explanation; right
  card shows the large verdict badge (status word, scope line, 4-metric grid) or a pending/
  no-verdict state.
- Extractable props: `decision` (string), `explanation` (string), `scope` (string), `verdict`
  (status/effect/control/reproducibility/validity | null), `running` (boolean)
- Hardcoded: label text ("Decision under investigation", "Verdict"), the four metric labels

## InvestigationThread (the stage timeline)
- Source: `src/components/Investigation.tsx` (remainder of file)
- Category: feature
- Description: Vertical spine with 5 stages (Claim, Experiment, Evidence, Verdict, Action),
  each a bordered dot on the spine; dots after the verdict stage inherit the verdict's colour.
- Extractable props: `stages` (array of {name, hint, done, content}), `tone`
  (supported|contradicted|inconclusive|null)
- Hardcoded: the 5 stage names/hints, per-stage internal layout (arms grid, chip lists, etc.)

## DeltaPlot
- Source: `src/components/DeltaPlot.tsx`
- Category: feature (data visualization)
- Description: Custom SVG per-case delta chart with threshold line and control-arm band —
  see `components.md` for detail. Not a wrapper around a charting library; hand-rolled SVG.
- Extractable props: `evidence`, `plan`, `status`
- Hardcoded: all colour-by-status logic, axis label text, row height (9px)

## LineageStrip
- Source: `src/components/Panels.tsx` (`Lineage` export)
- Category: feature
- Description: Horizontal scrollable row of clickable version-nodes, coloured by verdict,
  each showing effect size + distribution + relation ("supersedes"/"expired"/etc.)
- Extractable props: `entries` (array), `onSelect` (callback), `chainIntact` (boolean),
  `auditPriority` (number)
- Hardcoded: chip copy ("append-only rows", "hash chain intact")

## DebtPanel
- Source: `src/components/Panels.tsx` (`Debt` export, + internal `DebtTrend`)
- Category: feature
- Description: Large debt-total number + custom SVG sparkline trend + horizontal
  stacked-bar-per-component breakdown.
- Extractable props: `snapshot`, `delta`, `history`
- Hardcoded: "/100" scale label, bar-fill colour (currently flat `--text-dim`, no
  per-component colour coding — candidate improvement)

## FleetTable
- Source: `src/components/Panels.tsx` (`Fleet` export)
- Category: feature
- Description: Table of the 4 agent roles with write-scopes/tools/reasoner/health columns.
- Extractable props: `agents` (array)
- Hardcoded: column headers

## RuntimeProofPanel
- Source: `src/components/Panels.tsx` (`Runtime` export, + internal `Cell`)
- Category: feature
- Description: The largest/densest panel — two stat-grids (async runtime counters, durable
  state counters), plus three conditional sub-cards (pending human approvals with
  Approve/Reject buttons, scheduled self-audits table, dead-letter table), plus a static
  reference table of the four target-model formulas.
- Extractable props: `proof`, `approvals`, `onDecide`, `versions`
- Hardcoded: all section copy, column headers

## EventControlPanel (currently inline inside `App.tsx`, not yet split)
- Source: `src/App.tsx` (the `.card` right after `.masthead`)
- Category: feature — **this is the primary interactive surface of the whole app**
- Description: Row of 6 buttons (deploy v1–v4, distribution shift, send duplicate) that fire
  API calls with no analysis performed client-side; a "↺" suffix marks already-tested
  versions; a single `busy` boolean disables the whole row during any in-flight call.
- Extractable props: `versions`, `testedVersions`, `distribution`, `busy`, `onDeploy`,
  `onShift`, `onDuplicate`
- Hardcoded: button label copy, the explanatory caption paragraph
