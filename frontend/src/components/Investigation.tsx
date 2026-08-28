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
            “{decision.explanation ?? "No explanation was supplied."}”
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
                <Metric
                  label="Effect"
                  value={signed(verdict.effect_size)}
                  note="mean change in score"
                />
                <Metric
                  label="Control"
                  value={
                    verdict.control_effect_size === null
                      ? "not run"
                      : signed(verdict.control_effect_size)
                  }
                  note="competing feature"
                />
                <Metric
                  label="Reproducibility"
                  value={percent(verdict.reproducibility)}
                  note="cases matching"
                />
                <Metric
                  label="Validity"
                  value={percent(verdict.intervention_validity)}
                  note="probe quality"
                />
              </div>
            </>
          ) : running ? (
            <p className="verdict__pending">
              <span className="spin" /> Running — no verdict yet
            </p>
          ) : (
            <>
              <p className="verdict__pending">No verdict</p>
              <p className="verdict__scope">
                {investigation.last_error ?? "This investigation produced no verdict."}
              </p>
            </>
          )}
        </div>
      </section>

      <div className={`thread ${tone ? `thread--${tone}` : ""}`}>
        <Stage
          name="Claim"
          hint="the explanation, compiled into something testable"
          done={reached(investigation.state, "CLAIM_EXTRACTED")}
        >
          {claim ? (
            <div className="card">
              <p className="claim-logic">
                <span>IF </span>
                <b>{claim.subject}</b>
                <span> {claim.predicate.replace(/_/g, " ")} of </span>
                <b>{claim.object}</b>
                {"\n"}
                <span>THEN neutralizing </span>
                <b>{claim.subject}</b>
                <span> while preserving </span>
                <b>{claim.preserved_constraints.join(", ") || "nothing"}</b>
                {"\n"}
                <span>SHOULD </span>
                <b>{claim.expected_direction}</b>
                <span> the {claim.object}</span>
              </p>
              <div className="chips">
                <span className="chip">
                  testability {claim.testability_score.toFixed(2)}
                </span>
                <span className="chip">confidence {claim.confidence.toFixed(2)}</span>
                <span className="chip">
                  {claim.primacy_claim ? "asserts primacy" : "asserts influence only"}
                </span>
                <span className="chip">
                  compiled by {claim.provenance.agent_id} ·{" "}
                  {claim.provenance.llm_model ?? "unknown reasoner"}
                </span>
                {claim.quarantined && (
                  <span className="chip chip--warn">
                    quarantined: {claim.quarantine_reasons.join(", ")}
                  </span>
                )}
              </div>
              {claim.ambiguities.length > 0 && (
                <p className="plot__caption">
                  Unresolved ambiguity: {claim.ambiguities.join("; ")}
                </p>
              )}
            </div>
          ) : (
            <Waiting running={running} what="claim" />
          )}
        </Stage>

        <Stage
          name="Experiment"
          hint="baseline, intervention, and a control on a competing feature"
          done={reached(investigation.state, "EXPERIMENT_RUNNING")}
        >
          {experiment ? (
            <>
              <div className="arms">
                <Arm
                  name="Baseline"
                  what="Every case, untouched."
                  stat={
                    evidence ? evidence.baseline.mean.toFixed(4) : `${experiment.repetitions} cases`
                  }
                  statLabel={evidence ? "mean score" : "planned"}
                />
                <Arm
                  name="Intervention"
                  what={`${experiment.intervention.variable} set to ${experiment.intervention.value ?? "—"}, everything else held fixed.`}
                  stat={evidence ? evidence.intervention.mean.toFixed(4) : "pending"}
                  statLabel="mean score"
                />
                <Arm
                  name="Control"
                  what={
                    experiment.control
                      ? `${experiment.control.variable} set to ${experiment.control.value ?? "—"} instead — a feature the explanation never named.`
                      : "No control was run, so a primacy claim cannot be refuted."
                  }
                  stat={
                    evidence?.control ? evidence.control.mean.toFixed(4) : "not run"
                  }
                  statLabel="mean score"
                />
              </div>
              <div className="chips">
                <span className="chip">seed {experiment.seed}</span>
                <span className="chip">{experiment.repetitions} repetitions</span>
                <span className="chip">fixtures {experiment.fixture_set}</span>
                <span className="chip">
                  preserved: {experiment.constraints.preserved_features.join(", ") || "none"}
                </span>
                <span className="chip">
                  effect threshold {experiment.min_effect_threshold.toFixed(2)}
                </span>
              </div>
            </>
          ) : (
            <Waiting running={running} what="experiment plan" />
          )}
        </Stage>

        <Stage
          name="Evidence"
          hint="what actually happened, case by case"
          done={reached(investigation.state, "VERIFICATION")}
        >
          {evidence && experiment ? (
            <div className="card">
              <DeltaPlot evidence={evidence} plan={experiment} status={status} />
              <div className="metrics" style={{ marginTop: 16 }}>
                <Metric
                  label="Effect"
                  value={signed(evidence.effect_size)}
                  note={
                    evidence.effect_ci
                      ? `95% CI ${signed(evidence.effect_ci[0], 3)} … ${signed(evidence.effect_ci[1], 3)}`
                      : "interval needs ≥4 runs"
                  }
                />
                <Metric
                  label="Control effect"
                  value={
                    evidence.control_effect_size === null
                      ? "—"
                      : signed(evidence.control_effect_size)
                  }
                  note="same units"
                />
                <Metric
                  label="Instability"
                  value={evidence.instability.toFixed(4)}
                  note="model vs itself"
                />
                <Metric
                  label="Evidence"
                  value={evidence.id}
                  note={evidence.evidence_hash.slice(0, 20) + "…"}
                />
              </div>
            </div>
          ) : (
            <Waiting running={running} what="evidence" />
          )}
        </Stage>

        <Stage
          name="Verdict"
          hint="decided by deterministic code, never by a language model"
          done={reached(investigation.state, "VERIFICATION")}
          modifier="verdict"
        >
          {verdict ? (
            <div className="card">
              <div className="chips">
                {verdict.reason_codes.map((code) => (
                  <span key={code} className="chip">
                    {code}
                  </span>
                ))}
              </div>
              <p className="plot__caption mono" style={{ marginTop: 12 }}>
                {verdict.rationale}
              </p>
              <p className="plot__caption">
                Evidence: <span className="mono">{verdict.evidence_ids.join(", ")}</span> ·
                verifier <span className="mono">{verdict.verifier_version}</span>
              </p>
            </div>
          ) : (
            <Waiting running={running} what="verdict" />
          )}
        </Stage>

        <Stage
          name="Action"
          hint="what the Governor did about it"
          done={reached(investigation.state, "GOVERNOR_ACTION")}
        >
          {action ? (
            <div className="card">
              <p className="decision" style={{ fontSize: 20 }}>
                {action.action.replace(/_/g, " ")}
              </p>
              <div className="chips">
                {action.reason_codes.map((code) => (
                  <span key={code} className="chip">
                    {code}
                  </span>
                ))}
                <span className="chip">policy {action.policy_version}</span>
                {action.required_approval && (
                  <span className="chip chip--warn">awaiting human approval</span>
                )}
              </div>
              {action.recommendation && (
                <p className="plot__caption">
                  The advisor recommended{" "}
                  <span className="mono">{action.recommendation}</span>;{" "}
                  {action.recommendation_accepted
                    ? "deterministic policy agreed."
                    : "deterministic policy overruled it."}
                </p>
              )}
              {action.next_event_at && (
                <p className="plot__caption">
                  Next audit scheduled for{" "}
                  <span className="mono">
                    {new Date(action.next_event_at).toLocaleString()}
                  </span>
                  .
                </p>
              )}
            </div>
          ) : (
            <Waiting running={running} what="governor action" />
          )}
        </Stage>
      </div>
    </>
  );
}

function Stage({
  name,
  hint,
  done,
  modifier,
  children,
}: {
  name: string;
  hint: string;
  done: boolean;
  modifier?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className={`stage ${done ? "stage--done" : ""} ${modifier ? `stage--${modifier}` : ""}`}
    >
      <div className="stage__head">
        <span className="stage__name">{name}</span>
        <span className="stage__hint">{hint}</span>
      </div>
      {children}
    </section>
  );
}

function Metric({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div className="metric">
      <span className="metric__label">{label}</span>
      <span className="metric__value">{value}</span>
      {note && <span className="metric__note">{note}</span>}
    </div>
  );
}

function Arm({
  name,
  what,
  stat,
  statLabel,
}: {
  name: string;
  what: string;
  stat: string;
  statLabel: string;
}) {
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
      {running ? (
        <>
          <span className="spin" /> waiting for the {what}
        </>
      ) : (
        <>No {what} was produced for this investigation.</>
      )}
    </div>
  );
}
