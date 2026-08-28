/* The evidence graphic.
 *
 * One horizontal segment per fixture case, running from its baseline score to its score
 * after the intervention. A vertical rule marks the minimum effect the claim has to
 * produce to count.
 *
 * This is the whole verdict, visible without reading a number: when every segment stops
 * short of the rule, the predicted effect is reproducibly absent. When every segment
 * crosses it, the effect is real. When they scatter across it, the honest answer is that
 * we do not know - which is exactly what INCONCLUSIVE means and what a summary statistic
 * would have hidden.
 *
 * The control arm is drawn in the same units directly underneath, so "the feature the
 * explanation did not mention moved the score more" is a thing you can see rather than a
 * claim you have to accept.
 */

import type { Evidence, ExperimentPlan, VerdictStatus } from "../api";

interface Props {
  evidence: Evidence;
  plan: ExperimentPlan;
  status: VerdictStatus | null;
}

const COLORS: Record<string, string> = {
  SUPPORTED: "var(--supported)",
  CONTRADICTED: "var(--contradicted)",
  INCONCLUSIVE: "var(--inconclusive)",
};

export function DeltaPlot({ evidence, plan, status }: Props) {
  const baseline = evidence.baseline.scores;
  const intervened = evidence.intervention.scores;
  const control = evidence.control?.scores ?? null;
  const threshold = plan.min_effect_threshold;

  if (!baseline.length || baseline.length !== intervened.length) {
    return <p className="dim">No paired runs were recorded for this experiment.</p>;
  }

  const deltas = baseline.map((value, index) => intervened[index] - value);
  const controlDeltas = control ? baseline.map((v, i) => control[i] - v) : null;

  const allValues = [...deltas, ...(controlDeltas ?? []), threshold, -threshold];
  const bound = Math.max(0.05, ...allValues.map(Math.abs)) * 1.15;

  const width = 720;
  const rowHeight = 9;
  const gap = 24;
  const topPad = 26;
  const controlTop = topPad + deltas.length * rowHeight + gap;
  const height =
    controlTop + (controlDeltas ? controlDeltas.length * rowHeight : 0) + 34;

  const x = (value: number) => ((value + bound) / (2 * bound)) * width;
  const zero = x(0);
  const accent = status ? COLORS[status] : "var(--text-dim)";

  const meets = (delta: number) => delta <= -threshold;

  return (
    <div>
      <svg
        className="plot"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`Per-case change in priority score after neutralizing ${plan.intervention.variable}. ${deltas.filter(meets).length} of ${deltas.length} cases moved at least ${threshold}.`}
      >
        {/* threshold band: everything left of the rule counts as a real decrease */}
        <rect
          x={0}
          y={topPad - 12}
          width={x(-threshold)}
          height={height - topPad - 10}
          fill={accent}
          opacity={0.06}
        />
        <line
          x1={x(-threshold)}
          x2={x(-threshold)}
          y1={topPad - 14}
          y2={height - 24}
          stroke={accent}
          strokeWidth={1}
          strokeDasharray="3 3"
        />
        <text x={x(-threshold) + 5} y={topPad - 17} fill={accent}>
          effect threshold −{threshold.toFixed(2)}
        </text>

        <line
          x1={zero}
          x2={zero}
          y1={topPad - 14}
          y2={height - 24}
          stroke="var(--graticule)"
          strokeWidth={1}
        />
        <text x={zero + 5} y={height - 12}>
          no change
        </text>

        {/* intervention arm */}
        <text x={0} y={topPad - 17}>
          neutralize {plan.intervention.variable} — {deltas.length} cases
        </text>
        {deltas.map((delta, index) => {
          const y = topPad + index * rowHeight;
          const from = Math.min(zero, x(delta));
          const to = Math.max(zero, x(delta));
          return (
            <g key={`i-${index}`}>
              <line
                x1={from}
                x2={to}
                y1={y}
                y2={y}
                stroke={meets(delta) ? accent : "var(--faint)"}
                strokeWidth={3}
                strokeLinecap="butt"
              />
              <circle
                cx={x(delta)}
                cy={y}
                r={2.2}
                fill={meets(delta) ? accent : "var(--faint)"}
              />
            </g>
          );
        })}

        {/* control arm, same units, directly comparable */}
        {controlDeltas && (
          <>
            <text x={0} y={controlTop - 9}>
              control: neutralize {plan.control?.variable} — same cases
            </text>
            {controlDeltas.map((delta, index) => {
              const y = controlTop + index * rowHeight;
              const from = Math.min(zero, x(delta));
              const to = Math.max(zero, x(delta));
              return (
                <line
                  key={`c-${index}`}
                  x1={from}
                  x2={to}
                  y1={y}
                  y2={y}
                  stroke="var(--text-dim)"
                  strokeWidth={3}
                  opacity={0.55}
                />
              );
            })}
          </>
        )}
      </svg>

      <p className="plot__caption">
        Each line is one test case: how far the priority score moved when{" "}
        <span className="mono">{plan.intervention.variable}</span> was set to its neutral
        value, with every other feature held fixed.{" "}
        <strong>
          {deltas.filter(meets).length} of {deltas.length}
        </strong>{" "}
        cases moved at least {threshold.toFixed(2)}
        {plan.control
          ? `. The lower band shows the same cases with ${plan.control.variable} neutralized instead.`
          : "."}
      </p>
    </div>
  );
}
