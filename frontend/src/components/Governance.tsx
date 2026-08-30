/* The governance control plane.
 *
 * The console's original framing was a laboratory readout: here is an investigation, here
 * are its numbers. That answers "what happened" for someone who already knows what Ariadne
 * is. It does not answer the question an ML governance team actually opens a tool with,
 * which is "what needs me, and why?"
 *
 * Everything here derives from the API. Nothing is computed as science in the browser - the
 * verifier is authoritative and the frontend's job is to display, explain, and route
 * attention. If a number appears on screen it came from `/api/v1/*`, and if it cannot be
 * derived from there it is not shown.
 */

import type {
  Evidence,
  ExperimentPlan,
  InvestigationDetail,
  InvestigationRow,
  LineageView,
  SystemInfo,
} from "../api";

/* ------------------------------------------------------------------ mode honesty ----- */

/** What the deployment actually is, read from the honesty endpoint rather than assumed. */
export function ModeBanner({ system }: { system: SystemInfo | null }) {
  if (!system) return null;
  const cloudLive = system.cloud.enabled;
  const reasonerLive = system.reasoner.is_language_model;

  return (
    <div className={`mode mode--${cloudLive ? "live" : "local"}`}>
      <span className="mode__tag">{cloudLive ? "LIVE" : "LOCAL"}</span>
      <span className="mode__detail">
        {cloudLive ? (
          <>
            Connected to Google Cloud project <b>{system.cloud.project}</b> in{" "}
            <b>{system.cloud.region}</b>. Events, checkpoints and evidence are real.
          </>
        ) : (
          <>Running entirely in-process. No cloud service is attached.</>
        )}
      </span>
      <span className={`mode__reasoner ${reasonerLive ? "ok" : "warn"}`}>
        {reasonerLive ? (
          <>reasoner: {system.reasoner.model}</>
        ) : (
          <>reasoner: offline deterministic — not a language model</>
        )}
      </span>
    </div>
  );
}

/* --------------------------------------------------------------- needs attention ----- */

export interface AttentionItem {
  severity: "contradicted" | "inconclusive" | "approval" | "stale";
  headline: string;
  detail: string;
  investigationId: string | null;
}

/**
 * What a governance team should look at, ranked by what it costs to ignore.
 *
 * Deliberately not a metrics wall. A contradicted explanation means a model is in production
 * with a stated reason that failed its test - that outranks every count on the page.
 */
export function attentionItems(
  rows: InvestigationRow[],
  approvals: number,
  lineage: LineageView | null,
): AttentionItem[] {
  const items: AttentionItem[] = [];

  for (const row of rows) {
    if (row.verdict?.status === "CONTRADICTED") {
      items.push({
        severity: "contradicted",
        headline: `Explanation contradicted on v${row.model_version}`,
        detail:
          "The stated driver failed its test while a control moved the score more. " +
          "The model is deployed with a reason the evidence rejects.",
        investigationId: row.id,
      });
    }
  }
  if (approvals > 0) {
    items.push({
      severity: "approval",
      headline: `${approvals} governance action${approvals > 1 ? "s" : ""} awaiting a human`,
      detail:
        "The Governor escalated rather than acting. Nothing proceeds until someone decides.",
      investigationId: null,
    });
  }
  if (lineage && lineage.current === null && lineage.entries?.length) {
    items.push({
      severity: "stale",
      headline: "No current evidence for this claim",
      detail:
        "Every reading has expired — the distribution moved out from under them. " +
        "The old verdicts remain true about the data they were measured on.",
      investigationId: null,
    });
  }
  for (const row of rows) {
    if (row.verdict?.status === "INCONCLUSIVE") {
      items.push({
        severity: "inconclusive",
        headline: `Cannot establish the claim on v${row.model_version}`,
        detail:
          "The experiment ran but could not settle the question. This is a result, " +
          "not a failure — and it means no conclusion should be drawn either way.",
        investigationId: row.id,
      });
    }
  }
  return items;
}

const SEVERITY_MARK: Record<AttentionItem["severity"], string> = {
  contradicted: "✕",
  approval: "⚠",
  stale: "◷",
  inconclusive: "?",
};

export function NeedsAttention({
  items,
  onSelect,
}: {
  items: AttentionItem[];
  onSelect: (id: string) => void;
}) {
  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Needs attention</h2>
        <p className="section__note">
          Ranked by what it costs to ignore, not by when it happened.
        </p>
      </header>
      {items.length === 0 ? (
        <p className="attention__empty">
          Nothing is waiting on a person. Every standing claim has current evidence.
        </p>
      ) : (
        <ul className="attention">
          {items.map((item, index) => (
            <li key={index} className={`attention__item attention__item--${item.severity}`}>
              <span className="attention__mark" aria-hidden="true">
                {SEVERITY_MARK[item.severity]}
              </span>
              <div className="attention__body">
                <p className="attention__headline">
                  <span className="sr-only">{item.severity}: </span>
                  {item.headline}
                </p>
                <p className="attention__detail">{item.detail}</p>
              </div>
              {item.investigationId && (
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => onSelect(item.investigationId as string)}
                >
                  Review evidence
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------- why a verdict --- */

interface Comparison {
  label: string;
  measured: string;
  bar: string;
  passed: boolean | null;
  reading: string;
}

/**
 * Reconstruct the verifier's reasoning from the numbers it recorded.
 *
 * This does not re-derive the verdict - `verify()` already decided, and duplicating that
 * logic in the browser is how a UI starts disagreeing with its own backend. It explains the
 * decision that was made, using the same quantities and thresholds the plan carries.
 */
export function explainVerdict(evidence: Evidence, plan: ExperimentPlan): Comparison[] {
  const pct = (value: number) => `${(value * 100).toFixed(0)}%`;
  const rows: Comparison[] = [
    {
      label: "Intervention validity",
      measured: evidence.validity_score.toFixed(3),
      bar: `≥ ${plan.validity_threshold}`,
      passed: evidence.validity_score >= plan.validity_threshold,
      reading:
        evidence.validity_score >= plan.validity_threshold
          ? "The probe changed what it promised to change, and nothing else."
          : "The probe did not perturb the input enough to test anything. Nothing about the claim follows.",
    },
    {
      label: "Effect on the named driver",
      measured: evidence.effect_size.toFixed(4),
      bar: `|effect| ≥ ${plan.min_effect_threshold}`,
      passed: Math.abs(evidence.effect_size) >= plan.min_effect_threshold,
      reading: "How far the score moved when the claimed driver was neutralized.",
    },
    {
      label: "Effect on the control",
      measured:
        evidence.control_effect_size === null
          ? "not run"
          : evidence.control_effect_size.toFixed(4),
      bar: "should be smaller",
      passed:
        evidence.control_effect_size === null
          ? null
          : Math.abs(evidence.control_effect_size) < Math.abs(evidence.effect_size),
      reading:
        "A feature the explanation never mentioned. If it moves the score more, the " +
        "explanation named the wrong driver.",
    },
    {
      label: "Reproducibility",
      measured: pct(evidence.reproducibility),
      bar: `≥ ${pct(plan.reproducibility_threshold)}`,
      passed: evidence.reproducibility >= plan.reproducibility_threshold,
      reading:
        "The share of cases where the predicted effect actually appeared. A mean can " +
        "clear the bar while most individual cases do not.",
    },
    {
      label: "Model self-agreement",
      measured: evidence.instability.toFixed(4),
      bar: `≤ ${plan.instability_threshold}`,
      passed: evidence.instability <= plan.instability_threshold,
      reading:
        "How much the model disagreed with itself on identical inputs. A model noisier " +
        "than the effect cannot be probed at all.",
    },
  ];
  if (evidence.effect_ci) {
    const [low, high] = evidence.effect_ci;
    rows.push({
      label: "95% interval on the effect",
      measured: `[${low.toFixed(4)}, ${high.toFixed(4)}]`,
      bar: "must exclude 0",
      passed: !(low <= 0 && 0 <= high),
      reading:
        "A seeded bootstrap over the paired differences. If the interval contains zero, " +
        "the effect is not separable from no effect at all.",
    });
  }
  rows.push({
    label: "Cases run",
    measured: String(plan.repetitions),
    bar: "",
    passed: null,
    reading: "Every case runs in all three arms, paired, in the same order.",
  });
  return rows;
}

const VERDICT_MEANING: Record<string, string> = {
  SUPPORTED: "The evidence supports the claim, within the scope stated below.",
  CONTRADICTED: "The evidence rejects the claim.",
  INCONCLUSIVE: "The experiment could not establish whether the claim is true.",
};

export function WhyVerdict({ detail }: { detail: InvestigationDetail }) {
  const { verdict, evidence, experiment: plan } = detail;
  if (!verdict || !evidence || !plan) return null;
  const comparisons = explainVerdict(evidence, plan);

  return (
    <section className="why">
      <header className="why__head">
        <h3 className="why__title">Why {verdict.status}?</h3>
        <p className="why__meaning">{VERDICT_MEANING[verdict.status]}</p>
      </header>

      <table className="why__table">
        <thead>
          <tr>
            <th scope="col">Quantity</th>
            <th scope="col">Measured</th>
            <th scope="col">Required</th>
            <th scope="col">
              <span className="sr-only">Outcome</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {comparisons.map((row) => (
            <tr key={row.label}>
              <th scope="row">
                {row.label}
                <span className="why__reading">{row.reading}</span>
              </th>
              <td className="mono">{row.measured}</td>
              <td className="mono dim">{row.bar}</td>
              <td className="why__mark">
                {row.passed === null ? (
                  <span className="dim">—</span>
                ) : (
                  <span className={row.passed ? "ok" : "bad"}>
                    {row.passed ? "✓ met" : "✕ not met"}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="why__codes">
        Reason codes:{" "}
        {verdict.reason_codes.map((code) => (
          <span key={code} className="chip chip--code">
            {code}
          </span>
        ))}
      </p>
      <p className="why__scope">
        True of <b>{verdict.scope.model_id}</b> <b>v{verdict.scope.model_version}</b> on{" "}
        <b>{verdict.scope.distribution_version}</b>, under protocol{" "}
        {plan.protocol_version}, verifier {verdict.verifier_version}. Not claimed
        beyond that scope.
      </p>
    </section>
  );
}

/* ------------------------------------------------------- explanation validity over time */

/**
 * The same claim, tracked across model versions.
 *
 * The strongest thing the product has to say, and it needs no interpretation: one sentence
 * shipped with four model versions and got four different answers. Evidence is not
 * transferred forward - it is re-established or it expires.
 */
export function ValidityTimeline({
  lineage,
  onSelect,
}: {
  lineage: LineageView | null;
  onSelect: (version: string) => void;
}) {
  if (!lineage) return null;
  const versions = Object.entries(lineage.statuses_by_version).sort(([a], [b]) =>
    a.localeCompare(b, undefined, { numeric: true }),
  );
  if (versions.length === 0) return null;

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Explanation validity over time</h2>
        <p className="section__note">
          One claim, every model version it has been tested against. Nothing is overwritten.
        </p>
      </header>
      <ol className="timeline">
        {versions.map(([version, status]) => (
          <li key={version} className={`timeline__step timeline__step--${status}`}>
            <button type="button" className="timeline__btn" onClick={() => onSelect(version)}>
              <span className="timeline__version mono">v{version}</span>
              <span className="timeline__status">
                {status === "SUPPORTED" ? "✓" : status === "CONTRADICTED" ? "✕" : "?"} {status}
              </span>
            </button>
          </li>
        ))}
      </ol>
      <p className="timeline__note">
        {lineage.current === null ? (
          <>
            <b>No current evidence.</b> A distribution change expired every reading above.
            They remain true about the data they were measured on — which is why they are
            still here.
          </>
        ) : (
          <>
            Current evidence: <b>v{lineage.current.scope.model_version}</b> on{" "}
            <b>{lineage.current.scope.distribution_version}</b>. Hash chain{" "}
            {lineage.chain_intact ? "intact" : "BROKEN"}.
          </>
        )}
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------- infrastructure --- */

/** What is actually wired, straight from the honesty endpoint. Never decorative. */
export function Infrastructure({
  system,
  runtimeOk,
}: {
  system: SystemInfo | null;
  runtimeOk: boolean;
}) {
  if (!system) return null;
  const cloud = system.cloud;
  const services: Array<{ name: string; status: string; live: boolean; purpose: string }> = [
    {
      name: "Cloud Run",
      status: cloud.enabled ? "LIVE" : "not in use",
      live: cloud.enabled,
      purpose: "Serves this console and the API, and hosts the worker in the same container.",
    },
    {
      name: "Pub/Sub",
      status: cloud.enabled ? `LIVE — ${cloud.event_bus}` : `local — ${cloud.event_bus}`,
      live: cloud.enabled && cloud.event_bus === "pubsub",
      purpose: "Delivers deployment and drift events. At-least-once; Ariadne adds exactly-once work.",
    },
    {
      name: "Firestore",
      status: cloud.enabled ? `LIVE — ${cloud.runtime_store}` : `local — ${cloud.runtime_store}`,
      live: cloud.enabled && cloud.runtime_store === "firestore",
      purpose: "Idempotency claims, checkpoints, scheduled re-audits, approval requests.",
    },
    {
      name: "Cloud SQL",
      status: cloud.enabled ? `LIVE — ${cloud.database}` : `local — ${cloud.database}`,
      live: cloud.enabled && cloud.database === "cloud-sql",
      purpose: "The append-only evidence ledger. No update or delete API exists.",
    },
    {
      name: "Vertex AI",
      status: system.reasoner.is_language_model
        ? `LIVE — ${system.reasoner.model}`
        : "not in use — offline reasoner",
      live: system.reasoner.is_language_model,
      purpose: "Compiles an explanation into a testable claim. Never reachable by the verifier.",
    },
  ];

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Infrastructure</h2>
        <p className="section__note">
          Read from <span className="mono">/api/v1/system</span>, which reports the real
          configuration. If it says not in use, it is not in use.
        </p>
      </header>
      <ul className="infra">
        {services.map((service) => (
          <li key={service.name} className="infra__row">
            <span className={`infra__dot ${service.live ? "ok" : "off"}`} aria-hidden="true" />
            <span className="infra__name">{service.name}</span>
            <span className={`infra__status mono ${service.live ? "ok" : "dim"}`}>
              {service.status}
            </span>
            <span className="infra__purpose">{service.purpose}</span>
          </li>
        ))}
      </ul>
      <p className="infra__note">
        Worker event loop: {runtimeOk ? "processing" : "idle"}. Project{" "}
        <span className="mono">{cloud.project ?? "—"}</span>, region{" "}
        <span className="mono">{cloud.region}</span>.
      </p>
    </section>
  );
}
