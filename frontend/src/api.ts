/* API client.
 *
 * The console reads the ledger and emits events. It never computes a verdict, and there is
 * no endpoint here that would let it - which is the point: what you see on screen is the
 * recorded evidence, not a rendering decision.
 */

export type VerdictStatus = "SUPPORTED" | "CONTRADICTED" | "INCONCLUSIVE";

export interface Scope {
  model_id: string;
  model_version: string;
  distribution_version: string;
}

export interface VerdictSummary {
  status: VerdictStatus;
  effect_size: number;
  control_effect_size: number | null;
  reproducibility: number;
  intervention_validity: number;
  reason_codes: string[];
}

export interface InvestigationRow {
  id: string;
  state: string;
  model_version: string;
  distribution_version: string;
  trigger_event_type: string;
  priority: number;
  verdict: VerdictSummary | null;
  created_at: string;
  updated_at: string;
  last_error: string | null;
}

export interface Claim {
  id: string;
  claim_family_id: string;
  subject: string;
  predicate: string;
  object: string;
  expected_direction: string;
  primacy_claim: boolean;
  target_variables: string[];
  preserved_constraints: string[];
  assumptions: string[];
  ambiguities: string[];
  testability_score: number;
  confidence: number;
  audit_priority: number;
  quarantined: boolean;
  quarantine_reasons: string[];
  source_explanation: string;
  provenance: {
    agent_id: string;
    llm_model: string | null;
    prompt_version: string | null;
    attempts: number;
  };
}

export interface InterventionSpec {
  variable: string;
  intervention_type: string;
  value: number | null;
  delta: number | null;
}

export interface ExperimentPlan {
  id: string;
  intervention: InterventionSpec;
  control: InterventionSpec | null;
  constraints: { preserved_features: string[]; tolerance: number };
  fixture_set: string;
  repetitions: number;
  seed: number;
  min_effect_threshold: number;
  reproducibility_threshold: number;
  validity_threshold: number;
  // Both are sent by /api/v1/investigations/{id} and were simply missing from this type,
  // so the console could not explain two of the four gates it renders verdicts for.
  instability_threshold: number;
  protocol_version: string;
  confounders: string[];
  invalid_conditions: string[];
}

export interface RunSummary {
  kind: string;
  n: number;
  mean: number;
  stdev: number;
  minimum: number;
  maximum: number;
  scores: number[];
}

export interface Evidence {
  id: string;
  baseline: RunSummary;
  intervention: RunSummary;
  control: RunSummary | null;
  effect_size: number;
  effect_ci: [number, number] | null;
  control_effect_size: number | null;
  reproducibility: number;
  validity_score: number;
  instability: number;
  evidence_hash: string;
  input_hashes: string[];
  output_hashes: string[];
}

export interface Verdict {
  id: string;
  status: VerdictStatus;
  behavioral_support: number;
  intervention_validity: number;
  reproducibility: number;
  contradiction_score: number;
  effect_size: number;
  control_effect_size: number | null;
  expected_direction: string;
  observed_direction: string;
  evidence_ids: string[];
  reason_codes: string[];
  rationale: string;
  verifier_version: string;
  scope: Scope;
}

export interface DebtComponent {
  name: string;
  ratio: number;
  weight: number;
  points: number;
  detail: string;
}

export interface DebtSnapshot {
  id: string;
  total: number;
  previous_total: number | null;
  policy_version: string;
  components: DebtComponent[];
  computed_at: string;
}

export interface GovernorDecision {
  id: string;
  action: string;
  reason_codes: string[];
  rationale: string;
  policy_version: string;
  debt_total: number | null;
  recommendation: string | null;
  recommendation_accepted: boolean;
  required_approval: boolean;
  next_event_at: string | null;
}

export interface InvestigationDetail {
  investigation: {
    id: string;
    state: string;
    priority: number;
    trigger_event_id: string;
    trigger_event_type: string;
    created_at: string;
    updated_at: string;
    last_error: string | null;
    scope: Scope;
  };
  decision: { decision: string | null; explanation: string | null };
  claim: Claim | null;
  experiment: ExperimentPlan | null;
  evidence: Evidence | null;
  verdict: Verdict | null;
  debt: DebtSnapshot | null;
  action: GovernorDecision | null;
}

export interface LineageEntry {
  id: string;
  scope: Scope;
  status: VerdictStatus;
  relation: string;
  effect_size: number;
  reproducibility: number;
  intervention_validity: number;
  valid_from: string;
  valid_until: string | null;
  expired_reason: string | null;
  verdict_id: string;
  evidence_ids: string[];
  is_expiry: boolean;
  is_expired: boolean;
}

export interface LineageView {
  claim_family_id: string;
  as_of: string;
  current: LineageEntry | null;
  statuses_by_version: Record<string, VerdictStatus>;
  expired_entry_ids: string[];
  audit_priority: number;
  chain_intact: boolean;
  entries: LineageEntry[];
}

export interface SystemInfo {
  disclaimer: string;
  reasoner: { provider: string; model: string; is_language_model: boolean };
  cloud: {
    enabled: boolean;
    event_bus: string;
    runtime_store: string;
    database: string;
    project: string | null;
    region: string;
  };
  policy_version: string;
  verifier_version: string;
  protocol_version: string;
}

export interface FleetAgent {
  agent_id: string;
  version: string;
  role: string;
  capabilities: string[];
  read_scopes: string[];
  write_scopes: string[];
  tools: string[];
  max_risk_level: string;
  uses_llm: boolean;
  healthy: boolean;
  quarantined: boolean;
  failures: number;
}

export interface RuntimeProof {
  bus: {
    published: number;
    delivered: number;
    duplicates_suppressed: number;
    retried: number;
    dead_lettered: number;
    failed: number;
    queued: number;
  };
  worker: {
    worker_id: string;
    events_seen: number;
    events_processed: number;
    duplicates_skipped: number;
    investigations_started: number;
    failures: number;
    handled_types: Record<string, number>;
  };
  checkpoints: Record<string, number>;
  ledger: Record<string, number>;
  dead_letters: Array<{
    event_id: string;
    event_type: string;
    error_code: string;
    attempts: number;
  }>;
  scheduled_audits: Array<{
    id: string;
    claim_family_id: string;
    scheduled_for: string;
    priority: number;
    reason_code: string;
    executed: boolean;
  }>;
  integrity: {
    lineage_chain_broken_rows: string[];
    verdict_rows_broken: string[];
  };
  activity: Array<{ at: string; kind: string; detail: string }>;
}

export interface ApprovalRequest {
  id: string;
  decision_id: string;
  investigation_id: string;
  action: string;
  justification: string;
  status: string;
  requested_at: string;
}

export interface ModelVersionInfo {
  version: string;
  formula: string;
  noise_scale: number;
  description: string;
}

const BASE = "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${path}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as T;
}


/* -- operator configuration ----------------------------------------------------------
   Connections, feature semantics and explanation sources: what an organisation supplies
   before Ariadne can verify anything about their model. Every field here is served by the
   backend; the console never invents one. */

export type ConnectionStatus = "NOT_CONFIGURED" | "OK" | "FAILED" | "DISABLED";

export interface ProbeCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ProbeResult {
  connection_id: string;
  ok: boolean;
  checks: ProbeCheck[];
  error: string | null;
  latency_ms: number;
  checked_at: string;
}

export interface Connection {
  id: string;
  kind: string;
  name: string;
  transport: string;
  endpoint: string;
  model_id: string;
  model_version: string;
  project: string;
  region: string;
  credential_ref: string;
  enabled: boolean;
  status: ConnectionStatus;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  probe_detail: Record<string, string>;
  configuration_version: number;
}

export interface FeatureSemantics {
  id: string;
  model_id: string;
  name: string;
  description: string;
  data_type: string;
  minimum: number | null;
  maximum: number | null;
  allowed_values: string[];
  neutral_strategy: string;
  neutral_value: number | null;
  neutral_category: string;
  validated: boolean;
  validation_errors: string[];
  configuration_version: number;
}

export interface ExplanationSource {
  id: string;
  model_id: string;
  name: string;
  source_type: string;
  endpoint: string;
  enabled: boolean;
  received_count: number;
  last_received_at: string | null;
}

export interface ReceivedExplanation {
  id: string;
  source_id: string;
  model_id: string;
  model_version: string;
  distribution_version: string;
  decision: string;
  explanation: string;
  received_at: string;
}

export const api = {
  system: () => request<SystemInfo>("/api/v1/system"),

  // -- configuration -----------------------------------------------------------------

  connections: () =>
    request<{ connections: Connection[]; live: number; total: number }>(
      "/api/v1/connections",
    ),

  createConnection: (body: Record<string, unknown>) =>
    request<Connection>("/api/v1/connections", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Really talks to the other side. The only thing that can make a connection live.
   *
   * Sends an explicit empty body. Google's front end rejects a POST with no
   * `Content-Length` header with HTTP 411 before the request ever reaches Cloud Run -
   * browsers happen to send `Content-Length: 0`, but relying on that would make the API
   * unusable from any client that does not. */
  testConnection: (id: string) =>
    request<ProbeResult>(`/api/v1/connections/${id}/test`, {
      method: "POST",
      body: "{}",
    }),

  deleteConnection: async (id: string) => {
    const response = await fetch(`${BASE}/api/v1/connections/${id}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`${response.status} deleting ${id}`);
  },

  features: (modelId?: string) =>
    request<{ features: FeatureSemantics[]; ready: number; total: number }>(
      `/api/v1/feature-semantics${modelId ? `?model_id=${modelId}` : ""}`,
    ),

  createFeature: (body: Record<string, unknown>) =>
    request<FeatureSemantics>("/api/v1/feature-semantics", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  validateFeature: (id: string) =>
    request<{
      feature_id: string;
      testable: boolean;
      problems: string[];
      resolved_neutral: number | string | null;
      reason: string | null;
    }>(`/api/v1/feature-semantics/${id}/validate`, { method: "POST", body: "{}" }),

  deleteFeature: async (id: string) => {
    const response = await fetch(`${BASE}/api/v1/feature-semantics/${id}`, {
      method: "DELETE",
    });
    if (!response.ok) throw new Error(`${response.status} deleting ${id}`);
  },

  explanationSources: () =>
    request<{ sources: ExplanationSource[]; total: number }>("/api/v1/explanation-sources"),

  createExplanationSource: (body: Record<string, unknown>) =>
    request<ExplanationSource>("/api/v1/explanation-sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  ingestExplanation: (sourceId: string, body: Record<string, unknown>) =>
    request<{ accepted: boolean; explanation_id: string; event_id: string; note: string }>(
      `/api/v1/explanation-sources/${sourceId}/ingest`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  explanations: () =>
    request<{ explanations: ReceivedExplanation[]; total: number }>("/api/v1/explanations"),


  models: (modelId = "synthetic-triage") =>
    request<{ model_id: string; versions: ModelVersionInfo[]; disclaimer: string }>(
      `/api/v1/models/${modelId}`,
    ),

  investigations: () =>
    request<{ investigations: InvestigationRow[] }>("/api/v1/investigations"),

  investigation: (id: string) =>
    request<InvestigationDetail>(`/api/v1/investigations/${id}`),

  claimFamilies: () =>
    request<{
      families: Array<{
        claim_family_id: string;
        audit_priority: number;
        statuses_by_version: Record<string, VerdictStatus>;
      }>;
    }>("/api/v1/claim-families"),

  lineage: (familyId: string, at?: string) =>
    request<LineageView>(
      `/api/v1/lineage/${familyId}${at ? `?at=${encodeURIComponent(at)}` : ""}`,
    ),

  debt: (modelId = "synthetic-triage") =>
    request<{
      current: DebtSnapshot | null;
      delta: number | null;
      rendered: string | null;
      history: Array<{ id: string; total: number; computed_at: string }>;
    }>(`/api/v1/debt/${modelId}`),

  fleet: () => request<{ agents: FleetAgent[] }>("/api/v1/fleet"),

  runtime: () => request<RuntimeProof>("/api/v1/runtime"),

  approvals: () => request<{ pending: ApprovalRequest[] }>("/api/v1/approvals"),

  decideApproval: (id: string, approve: boolean, decidedBy: string) =>
    request<ApprovalRequest>(`/api/v1/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ approve, decided_by: decidedBy }),
    }),

  deployVersion: (modelVersion: string, distributionVersion: string, duplicate = false) =>
    request<{ accepted: boolean; event_id: string; idempotency_key: string }>(
      "/api/v1/events/model-version-deployed",
      {
        method: "POST",
        body: JSON.stringify({
          model_version: modelVersion,
          distribution_version: distributionVersion,
          duplicate,
        }),
      },
    ),

  changeDistribution: (distributionVersion: string, previous: string) =>
    request<{ accepted: boolean; event_id: string }>(
      "/api/v1/events/distribution-changed",
      {
        method: "POST",
        body: JSON.stringify({
          distribution_version: distributionVersion,
          previous_distribution_version: previous,
          drift_score: 0.72,
          affected_features: ["urgency_marker"],
        }),
      },
    ),

  sendExplanation: (explanation: string, modelVersion: string) =>
    request<{ accepted: boolean; event_id: string }>(
      "/api/v1/events/explanation-received",
      {
        method: "POST",
        body: JSON.stringify({ explanation, model_version: modelVersion }),
      },
    ),
};

export function verdictClass(status: VerdictStatus | null | undefined): string {
  if (status === "SUPPORTED") return "supported";
  if (status === "CONTRADICTED") return "contradicted";
  if (status === "INCONCLUSIVE") return "inconclusive";
  return "";
}

export function signed(value: number, digits = 4): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function percent(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}
