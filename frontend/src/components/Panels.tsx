/* Lineage, debt, fleet, and runtime proof.
 *
 * These four answer the questions a reviewer asks after seeing one verdict: does this hold
 * across versions, how much unresolved risk is outstanding, who did the work, and can I
 * check that any of it really ran.
 */

import type {
  ApprovalRequest,
  DebtSnapshot,
  FleetAgent,
  LineageView,
  ModelVersionInfo,
  RuntimeProof,
} from "../api";
import { signed, verdictClass } from "../api";

/* ------------------------------------------------------------------ lineage */

export function Lineage({
  view,
  onSelect,
}: {
  view: LineageView;
  onSelect: (version: string) => void;
}) {
  const readings = view.entries.filter((entry) => !entry.is_expiry);

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">The same claim, across four model versions</h2>
        <p className="section__note">
          Append-only. A new result never edits an old one — it adds a row that says what it
          does to the previous one.
        </p>
      </header>

      <div className="lineage">
        {readings.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => onSelect(entry.scope.model_version)}
            className={`lineage__node lineage__node--${verdictClass(entry.status)} ${
              entry.is_expired ? "lineage__node--expired" : ""
            }`}
          >
            <div className="lineage__version">v{entry.scope.model_version}</div>
            <div className="lineage__status">{entry.status}</div>
            <div className="lineage__meta">
              effect {signed(entry.effect_size, 3)}
              <br />
              {entry.scope.distribution_version}
              <br />
              {entry.is_expired ? "evidence expired" : entry.relation.toLowerCase()}
            </div>
          </button>
        ))}
      </div>

      <div className="chips" style={{ marginTop: 14 }}>
        <span className="chip">{view.entries.length} append-only rows</span>
        <span className="chip">
          hash chain {view.chain_intact ? "intact" : "BROKEN"}
        </span>
        <span className="chip">audit priority {view.audit_priority.toFixed(2)}</span>
        {view.expired_entry_ids.length > 0 && (
          <span className="chip chip--warn">
            {view.expired_entry_ids.length} readings expired by a distribution change
          </span>
        )}
        <span className="chip">
          current:{" "}
          {view.current
            ? `v${view.current.scope.model_version} ${view.current.status}`
            : "nothing current"}
        </span>
      </div>
    </section>
  );
}

/* --------------------------------------------------------------------- debt */

export function Debt({
  snapshot,
  delta,
  history,
}: {
  snapshot: DebtSnapshot | null;
  delta: number | null;
  history: Array<{ id: string; total: number; computed_at: string }>;
}) {
  if (!snapshot) {
    return (
      <section className="section">
        <header className="section__head">
          <h2 className="section__title">Explanation Debt</h2>
        </header>
        <div className="empty">No debt has been calculated yet.</div>
      </section>
    );
  }

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">Explanation Debt</h2>
        <p className="section__note">
          A configurable operational risk score, not a scientific quantity. Weights are a
          policy choice, and every snapshot records the policy version it was computed under.
        </p>
      </header>

      <div className="grid-2">
        <div className="card">
          <p className="card__label">Current</p>
          <div>
            <span className="debt__total">{snapshot.total.toFixed(0)}</span>
            <span className="debt__scale"> / 100</span>
          </div>
          <p className="metric__note" style={{ marginTop: 8 }}>
            {delta === null
              ? "first snapshot"
              : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} since the previous snapshot`}{" "}
            · policy {snapshot.policy_version}
          </p>
          {history.length > 1 && <DebtTrend history={history} />}
        </div>

        <div className="card">
          <p className="card__label">Where it comes from</p>
          <div className="debt__rows">
            {snapshot.components.map((component) => (
              <div key={component.name} className="debt__row">
                <span className="debt__name">{component.name.replace(/_/g, " ")}</span>
                <span className="debt__bar">
                  <span
                    className="debt__fill"
                    style={{ width: `${(component.points / component.weight) * 100}%` }}
                  />
                </span>
                <span className="debt__points">
                  {component.points.toFixed(1)}
                  <span className="dim">/{component.weight.toFixed(0)}</span>
                </span>
                <span className="debt__detail">{component.detail}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function DebtTrend({
  history,
}: {
  history: Array<{ id: string; total: number; computed_at: string }>;
}) {
  const width = 320;
  const height = 60;
  const points = history.map((snapshot, index) => {
    const x = (index / Math.max(1, history.length - 1)) * width;
    const y = height - (snapshot.total / 100) * height;
    return `${x},${y}`;
  });

  return (
    <svg
      className="plot"
      viewBox={`0 0 ${width} ${height}`}
      style={{ marginTop: 16 }}
      role="img"
      aria-label={`Debt across ${history.length} snapshots, most recent ${history[history.length - 1].total.toFixed(0)} of 100.`}
    >
      <line x1={0} y1={height} x2={width} y2={height} stroke="var(--graticule)" />
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--text-dim)"
        strokeWidth={1.5}
      />
      {history.map((snapshot, index) => (
        <circle
          key={snapshot.id}
          cx={(index / Math.max(1, history.length - 1)) * width}
          cy={height - (snapshot.total / 100) * height}
          r={2}
          fill="var(--text-dim)"
        />
      ))}
    </svg>
  );
}

/* -------------------------------------------------------------------- fleet */

export function Fleet({ agents }: { agents: FleetAgent[] }) {
  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">The fleet</h2>
        <p className="section__note">
          Four roles with different authority. Read the write scopes: only the Verifier can
          write a verdict, and it is the one role that uses no language model at all.
        </p>
      </header>

      <table className="table">
        <thead>
          <tr>
            <th>Role</th>
            <th>Can write</th>
            <th>Tools</th>
            <th>Reasoner</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => (
            <tr key={agent.agent_id}>
              <td>
                <span className="mono">{agent.agent_id}</span>
                <br />
                <span className="dim" style={{ fontSize: 12 }}>
                  v{agent.version} · {agent.max_risk_level.toLowerCase().replace("_", " ")}
                </span>
              </td>
              <td className="mono">{agent.write_scopes.join(", ") || "nothing"}</td>
              <td className="mono dim">{agent.tools.join(", ") || "none"}</td>
              <td>
                {agent.uses_llm ? (
                  <span className="dim">Gemini</span>
                ) : (
                  <span className="ok">deterministic — no LLM</span>
                )}
              </td>
              <td>
                {agent.quarantined ? (
                  <span className="bad">quarantined</span>
                ) : (
                  <span className="dim">healthy</span>
                )}
                {agent.failures > 0 && (
                  <span className="dim mono"> ({agent.failures} failures)</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ------------------------------------------------------------------ runtime */

export function Runtime({
  proof,
  approvals,
  onDecide,
  versions,
}: {
  proof: RuntimeProof;
  approvals: ApprovalRequest[];
  onDecide: (id: string, approve: boolean) => void;
  versions: ModelVersionInfo[];
}) {
  const integrityClean =
    proof.integrity.lineage_chain_broken_rows.length === 0 &&
    proof.integrity.verdict_rows_broken.length === 0;

  return (
    <section className="section">
      <header className="section__head">
        <h2 className="section__title">What a reviewer can check</h2>
        <p className="section__note">
          Live counters from the running system. If nothing is happening, these do not move.
        </p>
      </header>

      <div className="grid-2">
        <div className="card">
          <p className="card__label">Asynchronous runtime</p>
          <div className="metrics">
            <Cell label="Events published" value={proof.bus.published} />
            <Cell label="Processed" value={proof.worker.events_processed} />
            <Cell
              label="Duplicates suppressed"
              value={proof.worker.duplicates_skipped}
              note="idempotency"
            />
            <Cell label="Retried" value={proof.bus.retried} />
            <Cell label="Dead-lettered" value={proof.bus.dead_lettered} />
            <Cell label="Queued" value={proof.bus.queued} />
          </div>
          <p className="metric__note" style={{ marginTop: 12 }}>
            worker <span className="mono">{proof.worker.worker_id}</span> ·{" "}
            {Object.entries(proof.worker.handled_types)
              .map(([type, count]) => `${type}×${count}`)
              .join(" · ") || "no events yet"}
          </p>
        </div>

        <div className="card">
          <p className="card__label">Durable state</p>
          <div className="metrics">
            <Cell label="Checkpointed runs" value={proof.checkpoints.runs ?? 0} />
            <Cell label="Investigations" value={proof.checkpoints.investigations ?? 0} />
            <Cell label="Evidence rows" value={proof.ledger.evidence ?? 0} />
            <Cell label="Verdicts" value={proof.ledger.verdicts ?? 0} />
            <Cell label="Lineage rows" value={proof.ledger.lineage_entries ?? 0} />
            <Cell label="Idempotency keys" value={proof.checkpoints.idempotency ?? 0} />
          </div>
          <p className="metric__note" style={{ marginTop: 12 }}>
            ledger integrity:{" "}
            {integrityClean ? (
              <span className="ok">every row hash recomputes</span>
            ) : (
              <span className="bad">TAMPERING DETECTED</span>
            )}
          </p>
        </div>
      </div>

      {approvals.length > 0 && (
        <div className="card" style={{ marginTop: 20 }}>
          <p className="card__label">Waiting for a human</p>
          {approvals.map((request) => (
            <div key={request.id} style={{ marginBottom: 14 }}>
              <p style={{ margin: "0 0 6px" }}>
                <strong>{request.action.replace(/_/g, " ")}</strong>{" "}
                <span className="dim mono">{request.investigation_id}</span>
              </p>
              <p className="plot__caption" style={{ margin: "0 0 10px" }}>
                {request.justification}
              </p>
              <div className="controls">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => onDecide(request.id, true)}
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onDecide(request.id, false)}
                >
                  Reject
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {proof.scheduled_audits.length > 0 && (
        <div className="card" style={{ marginTop: 20 }}>
          <p className="card__label">Audits Ariadne scheduled for itself</p>
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Why</th>
                <th>Priority</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {proof.scheduled_audits.map((audit) => (
                <tr key={audit.id}>
                  <td className="mono">
                    {new Date(audit.scheduled_for).toLocaleString()}
                  </td>
                  <td className="mono dim">{audit.reason_code}</td>
                  <td className="mono">{audit.priority.toFixed(2)}</td>
                  <td className="dim">{audit.executed ? "executed" : "pending"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {proof.dead_letters.length > 0 && (
        <div className="card" style={{ marginTop: 20 }}>
          <p className="card__label">Dead-lettered events</p>
          <table className="table">
            <tbody>
              {proof.dead_letters.map((letter) => (
                <tr key={letter.event_id}>
                  <td className="mono">{letter.event_type}</td>
                  <td className="bad mono">{letter.error_code}</td>
                  <td className="mono dim">after {letter.attempts} attempts</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card" style={{ marginTop: 20 }}>
        <p className="card__label">The laboratory, open to inspection</p>
        <table className="table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Formula</th>
              <th>What it does to the explanation</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((version) => (
              <tr key={version.version}>
                <td className="mono">v{version.version}</td>
                <td className="mono dim">{version.formula}</td>
                <td>{version.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="plot__caption">
          Ground truth comes from these formulas and fixed seeds — never from a language
          model. You can check any verdict on this page by hand.
        </p>
      </div>
    </section>
  );
}

function Cell({
  label,
  value,
  note,
}: {
  label: string;
  value: number;
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
