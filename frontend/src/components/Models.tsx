/* Model onboarding: register a model, and find out exactly what is still missing.
 *
 * The readiness panel is the point. Every other configuration screen answers a narrow
 * question - is this endpoint reachable, is this neutral value defensible - and none of them
 * answers the one an external team actually has: *am I done?*
 *
 * Two things this screen refuses to do.
 *
 * It never renders READY from a stored field. The gates come from `GET .../readiness`, which
 * re-derives from live state on every call, so a model whose endpoint failed overnight shows
 * as blocked the moment anyone looks. A status that only changes when someone edits a form
 * is a status that lies within a day.
 *
 * And it never shows a blocked gate without saying what to do about it. The backend supplies
 * an imperative with every failure; this renders it as the next action rather than as an
 * error message, because "NOT READY" on its own is a dead end.
 */

import { useCallback, useEffect, useState } from "react";
import { api, type Readiness, type RegisteredModel } from "../api";

const GATE_MEANING: Record<string, string> = {
  MODEL_ENDPOINT: "Ariadne can reach the model it is meant to probe",
  OUTPUT_CONTRACT: "Ariadne can find the score and explanation in the model's response",
  EXPLANATION_SOURCE: "explanations have somewhere to arrive from",
  FEATURE_SEMANTICS: "at least one feature has a defensible neutral value",
  LIFECYCLE_EVENTS: "evidence gets re-tested when the model or its data changes",
  EVIDENCE_STORE: "a verdict has somewhere to be recorded",
};

function ReadinessPanel({ readiness }: { readiness: Readiness }) {
  const passed = readiness.checks.filter((check) => check.passed).length;

  return (
    <div className="ready">
      <div className={`ready__head ready__head--${readiness.ready ? "ok" : "blocked"}`}>
        <span className="ready__status">
          {readiness.ready ? "READY FOR VERIFICATION" : "NOT READY"}
        </span>
        <span className="ready__score">
          {passed} of {readiness.checks.length} gates pass
        </span>
      </div>

      <ul className="ready__gates">
        {readiness.checks.map((check) => (
          <li
            key={check.name}
            className={`ready__gate ready__gate--${check.passed ? "pass" : "block"}`}
          >
            <span className="ready__mark" aria-hidden="true">
              {check.passed ? "✓" : "○"}
            </span>
            <div className="ready__body">
              <p className="ready__name">
                <span className="mono-v">{check.name}</span>
                <span className="ready__meaning">{GATE_MEANING[check.name] ?? ""}</span>
              </p>
              <p className="ready__detail">{check.detail}</p>
              {!check.passed && check.blocker && (
                <p className="ready__blocker">
                  <span className="sr-only">Next step: </span>→ {check.blocker}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>

      <p className="ready__foot">
        Re-derived from live state each time this is opened, never read from a stored flag.
        Checked {new Date(readiness.checked_at).toLocaleTimeString()}.
      </p>
    </div>
  );
}

export function Models() {
  const [models, setModels] = useState<RegisteredModel[]>([]);
  const [readiness, setReadiness] = useState<Record<string, Readiness>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ model_id: "", name: "", provider: "" });
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.registeredModels();
      setModels(data.models);
      const next: Record<string, Readiness> = {};
      for (const model of data.models) {
        try {
          next[model.id] = await api.readiness(model.id);
        } catch {
          /* a model whose readiness cannot be read is shown without gates rather than
             with invented ones */
        }
      }
      setReadiness(next);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const register = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.registerModel(form);
      setForm({ model_id: "", name: "", provider: "" });
      setOpen(false);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="h-section">Models</h2>
        <p className="section__note">
          A registered model is <span className="mono-v">CONFIGURING</span> until every
          readiness gate passes. Registering one does not make it verifiable, and Ariadne
          will not say it does.
        </p>
      </header>

      {error && <p className="cfg__error">{error}</p>}

      {models.length === 0 ? (
        <p className="attention__empty">
          No model registered. Connect a model to begin verifying its explanation claims.
        </p>
      ) : (
        <ul className="cfg__list">
          {models.map((model) => (
            <li key={model.id} className={`cfg__row cfg__row--${model.status === "READY" ? "OK" : "NOT_CONFIGURED"}`}>
              <div className="cfg__main">
                <p className="cfg__name">
                  {model.name}
                  <span
                    className={`cfg__status cfg__status--${model.status === "READY" ? "OK" : ""}`}
                  >
                    {model.status}
                  </span>
                </p>
                <p className="cfg__meta mono">
                  {model.model_id}
                  {model.provider && ` · ${model.provider}`}
                  {model.current_version && ` · v${model.current_version}`}
                </p>
                {readiness[model.id] && <ReadinessPanel readiness={readiness[model.id]} />}
              </div>
              <div className="cfg__actions">
                <button type="button" className="btn" onClick={() => void load()}>
                  Re-check
                </button>
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={async () => {
                    await api.deleteRegisteredModel(model.id);
                    await load();
                  }}
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <button type="button" className="btn" onClick={() => setOpen(!open)}>
        {open ? "Cancel" : "Register a model"}
      </button>

      {open && (
        <div className="cfg__form">
          <label>
            Model identifier
            <input
              value={form.model_id}
              onChange={(event) => setForm({ ...form, model_id: event.target.value })}
              placeholder="triage-model"
            />
            <span className="cfg__hint">
              Your own identifier — the one your deployment events carry.
            </span>
          </label>
          <label>
            Display name
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="Triage model"
            />
          </label>
          <label>
            Provider
            <input
              value={form.provider}
              onChange={(event) => setForm({ ...form, provider: event.target.value })}
              placeholder="vertex-ai"
            />
          </label>
          <button
            type="button"
            className="btn"
            disabled={!form.model_id || !form.name || busy}
            onClick={() => void register()}
          >
            {busy ? "registering…" : "Register (configuring until gates pass)"}
          </button>
        </div>
      )}
    </section>
  );
}
