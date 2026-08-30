/* The console.
 *
 * One page, one question: does this AI explanation deserve trust, and on what evidence?
 *
 * The controls emit *events* — the same events a model registry or a drift monitor would
 * emit. They do not run an analysis. After you press one, the page just polls, and the
 * investigation appears because a worker picked the event up. That is the whole demo, and
 * it is why there is no "Analyze" button anywhere on this page.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type ApprovalRequest,
  type DebtSnapshot,
  type FleetAgent,
  type InvestigationDetail,
  type InvestigationRow,
  type LineageView,
  type ModelVersionInfo,
  type RuntimeProof,
  type SystemInfo,
} from "./api";
import {
  ClaimCompilation,
  InconclusiveExplainer,
} from "./components/ClaimCompilation";
import { Investigation } from "./components/Investigation";
import { Nav, useHashRoute } from "./components/Nav";
import { Debt, Fleet, Lineage, Runtime } from "./components/Panels";
import {
  Connections,
  ExplanationSources,
  FeatureSemanticsPanel,
} from "./components/Configuration";
import {
  Infrastructure,
  ModeBanner,
  NeedsAttention,
  ValidityTimeline,
  WhyVerdict,
  attentionItems,
} from "./components/Governance";

const MODEL_VERSIONS = ["1.0.0", "2.0.0", "3.0.0", "4.0.0"];
const POLL_MS = 1200;

export default function App() {
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [versions, setVersions] = useState<ModelVersionInfo[]>([]);
  const [rows, setRows] = useState<InvestigationRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<InvestigationDetail | null>(null);
  const [lineage, setLineage] = useState<LineageView | null>(null);
  const [debt, setDebt] = useState<{
    current: DebtSnapshot | null;
    delta: number | null;
    history: Array<{ id: string; total: number; computed_at: string }>;
  } | null>(null);
  const [fleet, setFleet] = useState<FleetAgent[]>([]);
  const [proof, setProof] = useState<RuntimeProof | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [distribution, setDistribution] = useState("baseline_2024.1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, goTo] = useHashRoute();

  /* One poll drives everything. The page has no state of its own to keep in sync -
     it is a view of the ledger, so re-reading is the only update mechanism it needs. */
  const refresh = useCallback(async () => {
    try {
      const [investigations, fleetData, runtimeData, debtData, approvalData] =
        await Promise.all([
          api.investigations(),
          api.fleet(),
          api.runtime(),
          api.debt(),
          api.approvals(),
        ]);
      setRows(investigations.investigations);
      setFleet(fleetData.agents);
      setProof(runtimeData);
      setDebt(debtData);
      setApprovals(approvalData.pending);
      setError(null);

      const families = await api.claimFamilies();
      if (families.families.length > 0) {
        setLineage(await api.lineage(families.families[0].claim_family_id));
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    api.system().then(setSystem).catch(() => undefined);
    api
      .models()
      .then((data) => setVersions(data.versions))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // Open on the investigation that most needs a person, not on whatever arrived last.
  //
  // The backend already computes this judgement — `priority` ranks how urgently a claim
  // deserves re-testing, and `REVIEW` is the state the Governor escalates to when it wants a
  // human decision. The console was reading neither, so it opened on the newest row and a
  // reader's first screen was decided by arrival order. For a console whose entire job is
  // routing scarce human attention, that is the wrong default.
  //
  // Completed investigations are still one click away in the list; they just do not compete
  // for the opening slot with work that is waiting on someone.
  const attentionRanked = useMemo(
    () =>
      [...rows].sort((a, b) => {
        const awaiting = (row: InvestigationRow) => (row.state === "REVIEW" ? 1 : 0);
        return awaiting(b) - awaiting(a) || b.priority - a.priority;
      }),
    [rows],
  );
  const activeId = selectedId ?? attentionRanked[0]?.id ?? null;

  // Computed once: the nav badges it, the Overview renders it.
  const attention = useMemo(
    () => attentionItems(rows, approvals.length, lineage),
    [rows, approvals.length, lineage],
  );

  useEffect(() => {
    if (!activeId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    api
      .investigation(activeId)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeId, rows]);

  const emit = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await action();
        setSelectedId(null); // follow whatever the worker produces next
        await refresh();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const testedVersions = useMemo(
    () => new Set(rows.map((row) => row.model_version)),
    [rows],
  );

  const selectVersion = useCallback(
    (version: string) => {
      const match = rows.find((row) => row.model_version === version);
      if (match) setSelectedId(match.id);
    },
    [rows],
  );

  return (
    <div className="app">
      <div className="honesty">
        <span>
          <strong>Ariadne</strong> · Synthetic Triage Decision Laboratory · not a
          clinical system
        </span>
        <span className="honesty__flags">
          {system && (
            <>
              <span>
                reasoning:{" "}
                <strong>
                  {system.reasoner.is_language_model
                    ? system.reasoner.model
                    : "offline deterministic reasoner"}
                </strong>
              </span>
              <span>
                verdicts: <strong>deterministic v{system.verifier_version}</strong>
              </span>
              <span>
                runtime:{" "}
                <strong>
                  {system.cloud.enabled ? "google cloud" : "local"} · {system.cloud.event_bus}
                </strong>
              </span>
            </>
          )}
        </span>
      </div>

      <header className="masthead">
        <p className="masthead__eyebrow">Executable Explanation Protocol</p>
        <h1>AI decision under investigation</h1>
        <p className="masthead__sub">
          A triage nurse is told a decision and given a reason. She is not an ML engineer,
          and she has no way to check it. Ariadne turns that reason into an experiment, runs
          it, and reports what happened — scoped to the model version and the data it was
          true of.
        </p>
      </header>

      <ModeBanner system={system} />

      <Nav view={view} onNavigate={goTo} attention={attention.length} />

      {view === "overview" && (
        <>
      <NeedsAttention items={attention} onSelect={(id) => { setSelectedId(id); goTo("evidence"); }} />

      <section className="card" style={{ marginBottom: 32 }}>
        <p className="card__label">Emit an event — then stop touching the console</p>
        <div className="controls">
          {MODEL_VERSIONS.map((version) => (
            <button
              key={version}
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => emit(() => api.deployVersion(version, distribution))}
            >
              deploy v{version}
              {testedVersions.has(version) ? " ↺" : ""}
            </button>
          ))}
          <button
            type="button"
            className="btn"
            disabled={busy || distribution !== "baseline_2024.1"}
            onClick={() =>
              emit(async () => {
                await api.changeDistribution("shifted_2025.2", "baseline_2024.1");
                setDistribution("shifted_2025.2");
              })
            }
          >
            distribution shift
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => emit(() => api.deployVersion("4.0.0", distribution, true))}
          >
            send a duplicate event
          </button>
        </div>
        <p className="plot__caption">
          These publish the same events a model registry and a drift monitor would publish.
          Nothing here runs an analysis — a background worker picks the event up, and the
          investigation below appears on its own. Current distribution:{" "}
          <span className="mono">{distribution}</span>.
        </p>
        {error && <p className="plot__caption bad">{error}</p>}
      </section>
        </>
      )}

      {view === "evidence" && (
        <>
      {detail ? (
        <>
          {/* Explanation vs compiled claim comes first: the verdict below is about the
              compiled claim, and the adversarial benchmark showed that a reader who cannot
              see the difference cannot tell whether the right question was tested. */}
          <ClaimCompilation claim={detail.claim} />
          <Investigation detail={detail} />
          <WhyVerdict detail={detail} />
          <InconclusiveExplainer detail={detail} />
        </>
      ) : (
        <div className="empty">
          No investigation yet. Deploy a model version above and watch one appear.
        </div>
      )}

      {rows.length > 1 && (
        <section className="section">
          <header className="section__head">
            <h2 className="section__title">Every investigation</h2>
            <p className="section__note">
              Each was started by an event, not by a person.
            </p>
          </header>
          <table className="table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Trigger</th>
                <th>Verdict</th>
                <th>Effect</th>
                <th>State</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">
                    v{row.model_version}
                    <br />
                    <span className="dim" style={{ fontSize: 11 }}>
                      {row.distribution_version}
                    </span>
                  </td>
                  <td className="mono dim">{row.trigger_event_type}</td>
                  <td>
                    {row.verdict ? (
                      <span
                        className={
                          row.verdict.status === "SUPPORTED"
                            ? "ok"
                            : row.verdict.status === "CONTRADICTED"
                              ? "bad"
                              : "warn"
                        }
                      >
                        {row.verdict.status}
                      </span>
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                  <td className="mono">
                    {row.verdict ? row.verdict.effect_size.toFixed(4) : "—"}
                  </td>
                  <td className="dim">{row.state}</td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setSelectedId(row.id)}
                    >
                      open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
        </>
      )}

      {view === "lineage" && (
        <>
          {lineage && lineage.entries.length > 0 ? (
            <>
              <ValidityTimeline
                lineage={lineage}
                onSelect={(version) => {
                  selectVersion(version);
                  goTo("evidence");
                }}
              />
              <Lineage view={lineage} onSelect={selectVersion} />
            </>
          ) : (
            <div className="empty">
              No claim has been tested yet, so there is no history to show. Emit a
              deployment event from the Overview.
            </div>
          )}
          {debt && (
            <Debt snapshot={debt.current} delta={debt.delta} history={debt.history} />
          )}
        </>
      )}

      {view === "configure" && (
        <>
          <Connections />
          <FeatureSemanticsPanel />
          <ExplanationSources onIngested={() => void refresh()} />
        </>
      )}

      {view === "infrastructure" && (
        <>
          <Infrastructure
            system={system}
            runtimeOk={(proof?.worker?.events_seen ?? 0) > 0}
          />
          {fleet.length > 0 && <Fleet agents={fleet} />}
          {proof && (
            <Runtime
              proof={proof}
              approvals={approvals}
              versions={versions}
              onDecide={(id, approve) =>
                emit(() => api.decideApproval(id, approve, "nurse-supervisor"))
              }
            />
          )}
        </>
      )}

      <footer className="footnote">
        <p>
          <strong>Scope.</strong> Ariadne measures behavioral explanation faithfulness under
          a declared intervention protocol. It does not recover hidden causal structure, and
          a verdict is only ever true of the model version, data distribution, and
          intervention it was measured on.
        </p>
        <p>
          <strong>Limits.</strong> The target model is a hand-written formula over invented
          features, chosen so ground truth is checkable. Synthetic results establish nothing
          about clinical, financial, or legal performance. Explanation Debt is a
          configurable operational risk score, not a universal quantity. High-impact actions
          require a human.
        </p>
      </footer>
    </div>
  );
}
