/* The Evidence Trace — the one thing this product should be remembered by.
 *
 * Ariadne's whole argument is that a verdict is the end of a chain, not an opinion. The
 * trace makes the chain walkable: every stage the evidence passed through, in order, each
 * one opening onto the actual recorded values rather than a description of them.
 *
 * Two rules keep it honest. A stage is only marked complete when the API returned the
 * artifact for it - a claim, a plan, evidence, a verdict - so an investigation that failed
 * halfway shows exactly how far it got. And nothing here computes: every number is read
 * from the payload the verifier already produced, because a console that recalculates
 * science is a console that can disagree with its own backend.
 */

import { useState } from "react";
import type { InvestigationDetail } from "../api";

interface Stage {
  id: string;
  label: string;
  summary: string;
  done: boolean;
  rows: Array<[string, string]>;
  note?: string;
}

const pct = (value: number) => `${(value * 100).toFixed(0)}%`;

function buildStages(detail: InvestigationDetail): Stage[] {
  const { claim, experiment: plan, evidence, verdict, decision } = detail;
  const stages: Stage[] = [];

  stages.push({
    id: "explanation",
    label: "Explanation",
    summary: claim
      ? `“${claim.source_explanation.slice(0, 64)}${claim.source_explanation.length > 64 ? "…" : ""}”`
      : "no explanation recorded",
    done: Boolean(claim),
    rows: claim
      ? [
          ["decision explained", decision.decision ?? "—"],
          [
            "model",
            `${detail.investigation.scope.model_id} v${detail.investigation.scope.model_version}`,
          ],
          ["distribution", detail.investigation.scope.distribution_version],
        ]
      : [],
    note: claim
      ? "Stored verbatim before anything interpreted it. The claim below is an " +
        "interpretation of this sentence, and keeping the source is what makes that " +
        "interpretation auditable."
      : undefined,
  });

  stages.push({
    id: "claim",
    label: "Claim",
    summary: claim
      ? `${claim.subject} — ${claim.primacy_claim ? "asserted as primary driver" : "asserted as an influence"}`
      : "not compiled",
    done: Boolean(claim),
    rows: claim
      ? [
          ["driver", claim.subject],
          ["asserts primacy", claim.primacy_claim ? "yes" : "no"],
          ["predicted direction", claim.expected_direction],
          ["testability", claim.testability_score.toFixed(2)],
          ...(claim.ambiguities.length
            ? ([["ambiguities", claim.ambiguities.join(" · ")]] as Array<[string, string]>)
            : []),
        ]
      : [],
    note:
      "Compilation is a security boundary. Our adversarial benchmark found that every " +
      "mis-compiled claim escaped refutation — the verifier did not fail, it was asked a " +
      "different question.",
  });

  stages.push({
    id: "validity",
    label: "Validity",
    summary: evidence
      ? evidence.validity_score >= (plan?.validity_threshold ?? 0.9)
        ? "probe was a usable test of the claim"
        : "probe could not test the claim"
      : "not assessed",
    done: Boolean(evidence),
    rows:
      evidence && plan
        ? [
            ["validity score", evidence.validity_score.toFixed(3)],
            ["required", `≥ ${plan.validity_threshold}`],
            ["model self-agreement", evidence.instability.toFixed(4)],
            ["instability ceiling", `≤ ${plan.instability_threshold}`],
          ]
        : [],
    note:
      "Checked before any outcome is looked at. A broken probe cannot contradict a claim, " +
      "which is why every failure here yields INCONCLUSIVE rather than a verdict.",
  });

  stages.push({
    id: "intervention",
    label: "Intervention",
    summary: plan
      ? `${plan.intervention.intervention_type} ${plan.intervention.variable}` +
        (plan.intervention.value !== null ? ` → ${plan.intervention.value}` : "")
      : "not planned",
    done: Boolean(plan),
    rows: plan
      ? [
          ["variable", plan.intervention.variable],
          ["type", plan.intervention.intervention_type],
          ["value", plan.intervention.value === null ? "—" : String(plan.intervention.value)],
          ["held fixed", plan.constraints.preserved_features.join(", ") || "—"],
          ["cases", String(plan.repetitions)],
          ["seed", String(plan.seed)],
        ]
      : [],
    note:
      "What “neutralize” means is fixed by the laboratory, not proposed by the agent — " +
      "otherwise the probe could be redefined until it proved nothing while looking rigorous.",
  });

  stages.push({
    id: "control",
    label: "Control",
    summary: plan?.control
      ? `${plan.control.variable} — a feature the explanation never named`
      : "no control arm",
    done: Boolean(plan?.control),
    rows:
      plan?.control && evidence
        ? [
            ["variable", plan.control.variable],
            [
              "control effect",
              evidence.control_effect_size === null
                ? "—"
                : evidence.control_effect_size.toFixed(4),
            ],
            ["claimed-driver effect", evidence.effect_size.toFixed(4)],
          ]
        : [],
    note:
      "The arm that catches a wrong attribution. If neutralizing a feature nobody mentioned " +
      "moves the score further than the one that was, the explanation named the wrong driver.",
  });

  stages.push({
    id: "evidence",
    label: "Evidence",
    summary: evidence
      ? `effect ${evidence.effect_size.toFixed(4)}, reproducible on ${pct(evidence.reproducibility)} of cases`
      : "not produced",
    done: Boolean(evidence),
    rows: evidence
      ? [
          ["effect", evidence.effect_size.toFixed(4)],
          [
            "95% interval",
            evidence.effect_ci
              ? `[${evidence.effect_ci[0].toFixed(4)}, ${evidence.effect_ci[1].toFixed(4)}]`
              : "not computed (fewer than four observations)",
          ],
          ["reproducibility", pct(evidence.reproducibility)],
          ["baseline runs", String(evidence.baseline.n)],
          ["evidence hash", evidence.evidence_hash.slice(0, 26) + "…"],
        ]
      : [],
    note:
      "Measurements and hashes. This object has no verdict field at all — the thing that " +
      "records observations is structurally unable to record a conclusion.",
  });

  stages.push({
    id: "verdict",
    label: "Verdict",
    summary: verdict ? verdict.status : "not reached",
    done: Boolean(verdict),
    rows: verdict
      ? [
          ["status", verdict.status],
          ["reason codes", verdict.reason_codes.join(", ")],
          ["verifier", verdict.verifier_version],
          [
            "true of",
            `${verdict.scope.model_id} v${verdict.scope.model_version} on ${verdict.scope.distribution_version}`,
          ],
        ]
      : [],
    note: verdict?.rationale,
  });

  return stages;
}

export function EvidenceTrace({ detail }: { detail: InvestigationDetail }) {
  const stages = buildStages(detail);
  const [open, setOpen] = useState<string | null>(null);

  return (
    <section className="panel" style={{ marginBottom: "var(--gap-5)" }}>
      <p className="eyebrow">Evidence trace</p>
      <h2 className="h-section">How this conclusion was produced</h2>
      <p className="h-sub" style={{ marginBottom: "var(--gap-3)", fontSize: "var(--step-0)" }}>
        Every stage the evidence passed through. Open one to see what was actually recorded.
      </p>

      <div className="trace">
        <span className="trace__rail" aria-hidden="true" />
        {stages.map((stage) => {
          const isOpen = open === stage.id;
          return (
            <div
              key={stage.id}
              className={`trace__node${stage.done ? " trace__node--done" : ""}${
                isOpen ? " trace__node--open" : ""
              }`}
            >
              <span className="trace__dot" aria-hidden="true" />
              <div>
                <button
                  type="button"
                  className="trace__btn"
                  aria-expanded={isOpen}
                  onClick={() => setOpen(isOpen ? null : stage.id)}
                >
                  <span className="trace__stage">{stage.label}</span>
                  <span className="trace__summary">{stage.summary}</span>
                  <span className="trace__chev" aria-hidden="true">
                    ›
                  </span>
                </button>

                {isOpen && (
                  <div className="trace__body">
                    {stage.id === "explanation" && detail.claim && (
                      <>
                        <p className="evidence-quote evidence-quote--sm">
                          “{detail.claim.source_explanation}”
                        </p>
                        <p className="evidence-attr">
                          stated by {detail.investigation.scope.model_id} v
                          {detail.investigation.scope.model_version}
                        </p>
                      </>
                    )}
                    {stage.rows.length > 0 ? (
                      <dl className="trace__rows">
                        {stage.rows.map(([key, value]) => (
                          <div key={key} className="trace__row">
                            <dt>{key}</dt>
                            <dd className={/[0-9]/.test(value) ? "mono-v" : undefined}>
                              {value}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    ) : (
                      <p className="trace__note" style={{ borderTop: 0, paddingTop: 0 }}>
                        This stage was never reached.
                      </p>
                    )}
                    {stage.note && <p className="trace__note">{stage.note}</p>}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
