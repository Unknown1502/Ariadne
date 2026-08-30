/* The configuration plane: what a governance team connects, declares, and registers.
 *
 * Three screens, and none of them is a shell. Every button here calls a real endpoint that
 * persists real state, and the two most important behaviours are refusals:
 *
 *   "Test connection" performs actual I/O and shows the individual checks it ran. A
 *   connection is never green because a form said so - it is green because something
 *   answered, and you can read what answered.
 *
 *   A feature that cannot be intervened on says NOT TESTABLE and lists every reason. It does
 *   not fall back to a plausible default, because a neutral value nobody chose is a
 *   counterfactual nobody can defend, and the resulting verdict would be arithmetic about
 *   nothing.
 *
 * The console configures. The backend decides. Nothing in this file computes science.
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type Connection,
  type ExplanationSource,
  type FeatureSemantics,
  type ProbeResult,
} from "../api";

/* ------------------------------------------------------------------- connections ----- */

const STATUS_LABEL: Record<string, string> = {
  NOT_CONFIGURED: "not configured",
  OK: "live",
  FAILED: "failed",
  DISABLED: "disabled",
};

function ProbeReport({ result }: { result: ProbeResult }) {
  return (
    <div className={`probe ${result.ok ? "probe--ok" : "probe--bad"}`}>
      <p className="probe__head">
        {result.ok ? "✓ every check passed" : "✕ connection test failed"}
        <span className="dim mono"> · {result.latency_ms.toFixed(0)}ms</span>
      </p>
      <ul className="probe__checks">
        {result.checks.map((check) => (
          <li key={check.name}>
            <span className={check.passed ? "ok" : "bad"}>{check.passed ? "✓" : "✕"}</span>{" "}
            <span className="probe__name">{check.name}</span>
            <span className="probe__detail">{check.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Connections() {
  const [rows, setRows] = useState<Connection[]>([]);
  const [live, setLive] = useState(0);
  const [probe, setProbe] = useState<Record<string, ProbeResult>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    kind: "MODEL_ENDPOINT",
    name: "",
    transport: "IN_PROCESS",
    endpoint: "",
    model_id: "",
    model_version: "",
    project: "",
    region: "",
    credential_ref: "",
  });

  const load = useCallback(async () => {
    try {
      const data = await api.connections();
      setRows(data.connections);
      setLive(data.live);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    setBusy("create");
    setError(null);
    try {
      await api.createConnection(
        Object.fromEntries(Object.entries(form).filter(([, value]) => value !== "")),
      );
      setForm({ ...form, name: "", endpoint: "" });
      setOpen(false);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  const test = async (id: string) => {
    setBusy(id);
    setError(null);
    try {
      setProbe({ ...probe, [id]: await api.testConnection(id) });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Connections</h2>
        <p className="section__note">
          {live} of {rows.length} live. A connection is live only after a real check
          succeeded — creating one leaves it <span className="mono">not configured</span>.
        </p>
      </header>

      {error && <p className="cfg__error">{error}</p>}

      {rows.length === 0 ? (
        <p className="attention__empty">
          Nothing connected yet. Connect a model endpoint to begin verifying explanation
          claims.
        </p>
      ) : (
        <ul className="cfg__list">
          {rows.map((row) => (
            <li key={row.id} className={`cfg__row cfg__row--${row.status}`}>
              <div className="cfg__main">
                <p className="cfg__name">
                  {row.name}
                  <span className={`cfg__status cfg__status--${row.status}`}>
                    {STATUS_LABEL[row.status] ?? row.status}
                  </span>
                </p>
                <p className="cfg__meta mono">
                  {row.kind} · {row.transport}
                  {row.endpoint && ` · ${row.endpoint}`}
                  {row.project && ` · ${row.project}`}
                  {` · config v${row.configuration_version}`}
                </p>
                {row.last_error && <p className="cfg__err">{row.last_error}</p>}
                {row.last_success_at && (
                  <p className="cfg__meta dim">
                    last success {new Date(row.last_success_at).toLocaleString()}
                  </p>
                )}
                {probe[row.id] && <ProbeReport result={probe[row.id]} />}
              </div>
              <div className="cfg__actions">
                <button
                  type="button"
                  className="btn"
                  disabled={busy === row.id}
                  onClick={() => void test(row.id)}
                >
                  {busy === row.id ? "testing…" : "Test connection"}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={async () => {
                    await api.deleteConnection(row.id);
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
        {open ? "Cancel" : "Add connection"}
      </button>

      {open && (
        <div className="cfg__form">
          <label>
            Name
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="Triage model endpoint"
            />
          </label>
          <label>
            Kind
            <select
              value={form.kind}
              onChange={(event) => setForm({ ...form, kind: event.target.value })}
            >
              {["MODEL_ENDPOINT", "MODEL_REGISTRY", "DRIFT_MONITOR", "EVIDENCE_STORE"].map(
                (kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ),
              )}
            </select>
          </label>
          <label>
            Transport
            <select
              value={form.transport}
              onChange={(event) => setForm({ ...form, transport: event.target.value })}
            >
              {["IN_PROCESS", "VERTEX_AI", "HTTP", "PUBSUB"].map((transport) => (
                <option key={transport} value={transport}>
                  {transport}
                </option>
              ))}
            </select>
          </label>
          <label>
            Endpoint / topic
            <input
              value={form.endpoint}
              onChange={(event) => setForm({ ...form, endpoint: event.target.value })}
              placeholder="https://… or a Pub/Sub topic"
            />
          </label>
          <label>
            GCP project
            <input
              value={form.project}
              onChange={(event) => setForm({ ...form, project: event.target.value })}
            />
          </label>
          <label>
            Credential reference
            <input
              value={form.credential_ref}
              onChange={(event) => setForm({ ...form, credential_ref: event.target.value })}
              placeholder="projects/p/secrets/key/versions/latest"
            />
            <span className="cfg__hint">
              A <em>reference</em> to a secret, never the secret. The API refuses anything
              that looks like a live credential.
            </span>
          </label>
          <button
            type="button"
            className="btn"
            disabled={!form.name || busy === "create"}
            onClick={() => void create()}
          >
            {busy === "create" ? "creating…" : "Create (not configured until tested)"}
          </button>
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------- feature semantics ---- */

export function FeatureSemanticsPanel() {
  const [rows, setRows] = useState<FeatureSemantics[]>([]);
  const [ready, setReady] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    model_id: "synthetic-triage",
    name: "",
    description: "",
    data_type: "CONTINUOUS",
    minimum: "0",
    maximum: "1",
    neutral_strategy: "EXPLICIT",
    neutral_value: "0.5",
  });

  const load = useCallback(async () => {
    try {
      const data = await api.features();
      setRows(data.features);
      setReady(data.ready);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    setError(null);
    try {
      await api.createFeature({
        model_id: form.model_id,
        name: form.name,
        description: form.description,
        data_type: form.data_type,
        minimum: form.minimum === "" ? null : Number(form.minimum),
        maximum: form.maximum === "" ? null : Number(form.maximum),
        neutral_strategy: form.neutral_strategy,
        neutral_value: form.neutral_value === "" ? null : Number(form.neutral_value),
      });
      setForm({ ...form, name: "" });
      setOpen(false);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Intervention semantics</h2>
        <p className="section__note">
          {ready} of {rows.length} features are testable. Ariadne supplies the verification
          protocol; <b>you supply what neutralizing a feature means in your domain</b> — it
          cannot be inferred, and getting it wrong produces arithmetic that works and verdicts
          that mean nothing.
        </p>
      </header>

      {error && <p className="cfg__error">{error}</p>}

      {rows.length === 0 ? (
        <p className="attention__empty">
          No feature semantics declared. Until a feature has a defensible neutral value, no
          claim about it can be tested.
        </p>
      ) : (
        <ul className="cfg__list">
          {rows.map((row) => (
            <li
              key={row.id}
              className={`cfg__row cfg__row--${row.validated ? "OK" : "FAILED"}`}
            >
              <div className="cfg__main">
                <p className="cfg__name">
                  <span className="mono">{row.name}</span>
                  <span
                    className={`cfg__status cfg__status--${row.validated ? "OK" : "FAILED"}`}
                  >
                    {row.validated ? "intervention ready" : "not testable"}
                  </span>
                </p>
                <p className="cfg__meta mono">
                  {row.data_type}
                  {row.minimum !== null && ` · range ${row.minimum}–${row.maximum}`}
                  {` · ${row.neutral_strategy}`}
                  {row.neutral_value !== null && ` · neutral ${row.neutral_value}`}
                  {` · config v${row.configuration_version}`}
                </p>
                {row.validation_errors.length > 0 && (
                  <ul className="cfg__problems">
                    {row.validation_errors.map((problem) => (
                      <li key={problem}>{problem}</li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="cfg__actions">
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={async () => {
                    await api.deleteFeature(row.id);
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
        {open ? "Cancel" : "Declare a feature"}
      </button>

      {open && (
        <div className="cfg__form">
          <label>
            Feature name
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="urgency_marker"
            />
          </label>
          <label>
            Meaning
            <input
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              placeholder="Clinical urgency indicator"
            />
          </label>
          <label>
            Minimum
            <input
              value={form.minimum}
              onChange={(event) => setForm({ ...form, minimum: event.target.value })}
            />
          </label>
          <label>
            Maximum
            <input
              value={form.maximum}
              onChange={(event) => setForm({ ...form, maximum: event.target.value })}
            />
          </label>
          <label>
            Neutral strategy
            <select
              value={form.neutral_strategy}
              onChange={(event) =>
                setForm({ ...form, neutral_strategy: event.target.value })
              }
            >
              {["EXPLICIT", "MIDPOINT", "POPULATION_MEDIAN"].map((strategy) => (
                <option key={strategy} value={strategy}>
                  {strategy}
                </option>
              ))}
            </select>
          </label>
          <label>
            Neutral value
            <input
              value={form.neutral_value}
              onChange={(event) => setForm({ ...form, neutral_value: event.target.value })}
            />
            <span className="cfg__hint">
              The single most consequential number you supply. It defines what the
              intervention <em>is</em>.
            </span>
          </label>
          <button
            type="button"
            className="btn"
            disabled={!form.name}
            onClick={() => void create()}
          >
            Declare and validate
          </button>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------ explanation sources ---- */

export function ExplanationSources({ onIngested }: { onIngested: () => void }) {
  const [rows, setRows] = useState<ExplanationSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [text, setText] = useState(
    "Nothing else came close; the urgency score is what decided this one.",
  );

  const load = useCallback(async () => {
    try {
      setRows((await api.explanationSources()).sources);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const createDefault = async () => {
    setError(null);
    try {
      await api.createExplanationSource({
        model_id: "synthetic-triage",
        name: "Triage model response",
        source_type: "MODEL_RESPONSE",
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const ingest = async (sourceId: string) => {
    setError(null);
    setNote(null);
    try {
      const result = await api.ingestExplanation(sourceId, {
        model_version: "2.0.0",
        distribution_version: "baseline_2024.1",
        decision: "HIGH_PRIORITY",
        explanation: text,
      });
      setNote(`stored as ${result.explanation_id}, published as ${result.event_id}`);
      await load();
      onIngested();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Explanation sources</h2>
        <p className="section__note">
          Where explanations enter Ariadne. Each one is stored verbatim before anything
          interprets it — a claim is an interpretation, and an interpretation whose source was
          discarded cannot be audited.
        </p>
      </header>

      {error && <p className="cfg__error">{error}</p>}
      {note && <p className="cfg__note mono">{note}</p>}

      {rows.length === 0 ? (
        <>
          <p className="attention__empty">No explanation source registered.</p>
          <button type="button" className="btn" onClick={() => void createDefault()}>
            Register the triage model's response
          </button>
        </>
      ) : (
        <ul className="cfg__list">
          {rows.map((row) => (
            <li key={row.id} className="cfg__row cfg__row--OK">
              <div className="cfg__main">
                <p className="cfg__name">
                  {row.name}
                  <span className="cfg__status cfg__status--OK">
                    {row.received_count} received
                  </span>
                </p>
                <p className="cfg__meta mono">
                  {row.source_type} · {row.model_id}
                  {row.last_received_at &&
                    ` · last ${new Date(row.last_received_at).toLocaleTimeString()}`}
                </p>
                <label className="cfg__ingest">
                  Submit an explanation
                  <textarea
                    value={text}
                    rows={2}
                    onChange={(event) => setText(event.target.value)}
                  />
                </label>
              </div>
              <div className="cfg__actions">
                <button
                  type="button"
                  className="btn"
                  disabled={!text.trim()}
                  onClick={() => void ingest(row.id)}
                >
                  Ingest
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
