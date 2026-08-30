/* Two screens that exist because of what the adversarial benchmark found.
 *
 * `docs/adversarial-evaluation.md` established that claim compilation is a security
 * boundary: every mis-compiled claim escaped refutation, and P(escape | extraction wrong)
 * was 1.000 offline against 0.375 when the compiler got it right. An attacker who can
 * confuse the compiler never has to face the verifier at all.
 *
 * A finding like that has a product consequence. If the compiled claim is invisible, a
 * governance team reading a verdict cannot tell whether the system tested the explanation
 * in front of them or a different claim it built from the same words. So the comparison is
 * shown, always, not hidden behind a research toggle.
 *
 * The second screen is INCONCLUSIVE. It is the project's most-defended result and the one
 * most easily misread as failure, so it says what happened, what it means for a governance
 * decision, and - where the evidence justifies it - that the route taken to reach it is one
 * the benchmark showed can be deliberately induced.
 */

import type { Claim, InvestigationDetail, Verdict } from "../api";

/* ------------------------------------------------------- explanation vs compiled claim */

export function ClaimCompilation({ claim }: { claim: Claim | null }) {
  if (!claim) return null;
  const ambiguities = claim.ambiguities ?? [];

  return (
    <section className="compile">
      <header className="compile__head">
        <h3 className="compile__title">What the model said, and what Ariadne tested</h3>
        <p className="compile__note">
          These are different objects. The verdict below is about the compiled claim — so if
          compilation was wrong, the verdict answers a question nobody asked.
        </p>
      </header>

      <div className="compile__grid">
        <div className="compile__side">
          <p className="compile__label">Model&rsquo;s explanation</p>
          <blockquote className="compile__quote evidence-quote--sm">
            {claim.source_explanation}
          </blockquote>
        </div>

        <div className="compile__arrow" aria-hidden="true">
          →
        </div>

        <div className="compile__side">
          <p className="compile__label">Compiled causal claim</p>
          <dl className="compile__fields">
            <div>
              <dt>driver</dt>
              <dd className="mono">{claim.subject}</dd>
            </div>
            <div>
              <dt>asserts primacy</dt>
              <dd className="mono">{claim.primacy_claim ? "yes" : "no"}</dd>
            </div>
            <div>
              <dt>predicted direction</dt>
              <dd className="mono">{claim.expected_direction}</dd>
            </div>
            <div>
              <dt>testability</dt>
              <dd className="mono">{claim.testability_score.toFixed(2)}</dd>
            </div>
          </dl>
        </div>
      </div>

      {ambiguities.length > 0 && (
        <div className="compile__ambiguity">
          <p className="compile__ambiguity-head">
            ⚠ The compiler recorded {ambiguities.length === 1 ? "an ambiguity" : "ambiguities"}
          </p>
          <ul>
            {ambiguities.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          <p className="compile__ambiguity-why">
            Shown because compilation sits upstream of every guarantee the protocol offers. In
            our adversarial benchmark, mis-compiled claims escaped refutation at 2.7&times; to
            4.7&times; the rate of correctly compiled ones.
          </p>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------- INCONCLUSIVE ----- */

/** Why the protocol could not settle the question, in the verifier's own reason codes. */
const REASONS: Record<string, string> = {
  INVALID_INTERVENTION:
    "The probe did not change the input in the way the claim required, so nothing it " +
    "measured bears on the claim.",
  WEAK_PERTURBATION:
    "The probe could not move the named feature far enough to test anything — usually " +
    "because the data distribution has shifted under it.",
  CONSTRAINT_VIOLATION:
    "The probe changed something it had promised to hold fixed, so any effect it measured " +
    "cannot be attributed to the claimed driver.",
  VAGUE_CLAIM:
    "The explanation did not name a single testable driver, so no intervention follows " +
    "from it and there was nothing to run.",
  INSUFFICIENT_RUNS:
    "Too few cases were run to distinguish an effect from noise.",
  MODEL_UNSTABLE:
    "The model disagreed with itself on identical inputs by more than the effect being " +
    "measured, so the experiment would have been measuring its instability.",
  EFFECT_NOT_REPRODUCIBLE:
    "The predicted effect appeared on some cases and not others — neither reproducibly " +
    "present nor reproducibly absent.",
  DIRECTION_MISMATCH:
    "The score moved reproducibly, but in the opposite direction to the one the claim " +
    "predicted. That is not support, and it is not a clean refutation either.",
  EFFECT_NOT_SEPARATED_FROM_ZERO:
    "The confidence interval on the mean effect contains zero, so the effect cannot be " +
    "distinguished from no effect at all.",
};

/**
 * Routes the adversarial benchmark showed can be *deliberately* induced.
 *
 * Deliberately narrow. Flagging every INCONCLUSIVE as suspicious would make the signal
 * worthless and would slander every honest untestable explanation - most INCONCLUSIVE
 * results are the protocol working. These two are named because `docs/adversarial-evaluation.md`
 * demonstrated them being induced on purpose: A7 phrases below the testability gate (3/3
 * offline, 2/3 against Gemini) and A4 aims at a model whose instability approaches the
 * effect (3/3 both arms).
 *
 * The wording matters. This reports that a *route* is known to be inducible, never that
 * this particular claim was an attack - the system cannot tell, and saying otherwise would
 * conflate scientific uncertainty with malice.
 */
const INDUCIBLE_ROUTES: Record<string, string> = {
  VAGUE_CLAIM:
    "A7 — an explanation phrased below the testability gate is never tested, and so can " +
    "never be refuted.",
  MODEL_UNSTABLE:
    "A4 — aiming a claim at a model whose instability approaches the effect makes the " +
    "stability gate fire before the data is examined.",
};

export function InconclusiveExplainer({ detail }: { detail: InvestigationDetail }) {
  const verdict: Verdict | null = detail.verdict;
  if (!verdict || verdict.status !== "INCONCLUSIVE") return null;

  const reasons = verdict.reason_codes.filter((code) => code in REASONS);
  const inducible = verdict.reason_codes.filter((code) => code in INDUCIBLE_ROUTES);

  return (
    <section className="inconc">
      <header className="inconc__head">
        <span className="inconc__badge">INCONCLUSIVE</span>
        <p className="inconc__lead">
          The protocol could not establish this claim. This is a scientific result, not an
          error and not a failed run.
        </p>
      </header>

      <div className="inconc__block">
        <p className="inconc__label">Why</p>
        {reasons.length > 0 ? (
          <ul className="inconc__reasons">
            {reasons.map((code) => (
              <li key={code}>
                <span className="mono dim">{code}</span>
                <span>{REASONS[code]}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="inconc__text">
            Recorded reason codes: <span className="mono">{verdict.reason_codes.join(", ")}</span>
          </p>
        )}
      </div>

      <div className="inconc__block">
        <p className="inconc__label">What this means for a governance decision</p>
        <p className="inconc__text">
          <b>Do not treat this explanation as established evidence.</b> Ariadne is not saying
          the explanation is false — it is saying this experiment could not settle it. A
          CONTRADICTED verdict would have been a stronger claim than the evidence supports,
          and manufacturing one is the failure this protocol exists to prevent.
        </p>
      </div>

      <div className="inconc__block">
        <p className="inconc__label">Evasion assessment</p>
        {inducible.length === 0 ? (
          <p className="inconc__text">
            <span className="mono">NOT ASSESSED</span> — this result was not reached by a
            route our adversarial benchmark showed can be deliberately induced.
          </p>
        ) : (
          <>
            <p className="inconc__text">
              <span className="mono inconc__elevated">ELEVATED</span> — reached by a route
              that <i>can</i> be induced on purpose:
            </p>
            <ul className="inconc__reasons">
              {inducible.map((code) => (
                <li key={code}>
                  <span className="mono dim">{code}</span>
                  <span>{INDUCIBLE_ROUTES[code]}</span>
                </li>
              ))}
            </ul>
            <p className="inconc__caveat">
              This does <b>not</b> mean this explanation was an attack. Most untestable
              explanations are honestly untestable, and Ariadne cannot distinguish
              &ldquo;untestable&rdquo; from &ldquo;untestable on purpose&rdquo;. It means the
              route is known to be exploitable, and a model that reaches it repeatedly is
              worth a governance question even though no single claim was ever refuted.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
