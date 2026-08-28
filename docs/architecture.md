# Architecture

Every diagram below is followed by what it is actually asserting, because a diagram that
only shows boxes is a diagram that cannot be wrong.

---

## System context

```mermaid
flowchart TD
    NURSE[Triage nurse] -->|reads a verdict| UI[Investigation Console]
    REG[Model registry] -->|MODEL_VERSION_DEPLOYED| BUS[(Event bus)]
    DRIFT[Drift monitor] -->|DISTRIBUTION_CHANGED| BUS
    UI -->|publishes events, never verdicts| API[Ariadne API]
    API --> BUS
    BUS --> WORKER[Worker]
    WORKER --> PIPE[Investigation pipeline]
    PIPE --> LEDGER[(Evidence ledger)]
    PIPE --> RUNTIME[(Runtime state)]
    LEDGER --> API
    RUNTIME --> API
    API -->|read-only| UI
```

**What to notice.** The console has exactly two arrows out: it publishes events and it
reads. There is no path from the UI to a verdict. The nurse's screen is a view of the
ledger, so nothing it displays can be true only on screen.

Note also who *starts* work: the model registry, not a person. The console can emit the same
event for a demo, but the production trigger is a deployment.

---

## High-level architecture

```mermaid
flowchart TD
    subgraph Reasoning["Semantic reasoning — Gemini"]
        INV[Investigator<br/>explanation → claim]
        EXP[Experimenter<br/>claim → probe design]
        ADV[Governor advisor<br/>context → recommendation]
    end

    subgraph Deterministic["Deterministic — no LLM reachable"]
        ENGINE[Experiment engine<br/>execute, validate constraints]
        VER[Verifier<br/>compute the verdict]
        LIN[Lineage<br/>append-only history]
        DEBT[Explanation Debt]
        POL[Policy engine<br/>choose the action]
    end

    EVT[Typed event] --> SM[State machine]
    SM --> INV --> EXP --> ENGINE --> VER --> LIN --> DEBT --> POL
    ADV -.recommends, never decides.-> POL
    POL --> SM
    VER --> LEDGER[(Evidence ledger)]
    SM --> RT[(Runtime checkpoints)]
```

**What to notice.** The dotted line is the whole architectural argument. The advisor's
recommendation reaches the policy engine as *data* that gets recorded next to the enforced
action, not as control flow. `GovernorDecision` carries both `recommendation` and
`recommendation_accepted`, and the schema refuses to let those two disagree with the action
— so "the model wanted to do nothing and policy overruled it" is a queryable fact.

Everything downstream of the engine is arithmetic. A reviewer can recompute any verdict from
the stored evidence.

---

## The autonomous audit

```mermaid
sequenceDiagram
    participant MR as Model registry
    participant BUS as Event bus
    participant W as Worker
    participant RT as Runtime state
    participant LIN as Lineage
    participant I as Investigator
    participant E as Experimenter
    participant TM as Target model
    participant V as Verifier
    participant G as Governor

    MR->>BUS: MODEL_VERSION_DEPLOYED v2.0.0
    BUS->>W: deliver (at least once)
    W->>RT: claim(idempotency_key)
    Note over W,RT: atomic. A redelivery finds it taken and stops.
    W->>LIN: which claims does this version reopen?
    LIN-->>W: family X, priority 0.75 (contradicted on v1)
    W->>I: compile the standing explanation
    I-->>W: Claim (testability 0.92, asserts primacy)
    W->>E: design a probe
    E-->>W: neutralize urgency, control on signal_c, seed 20260101
    W->>E: execute
    loop each fixture case
        E->>TM: baseline / intervention / control
        E->>RT: checkpoint the run
    end
    E-->>V: Evidence (hashed, no verdict field)
    V->>V: validity → sample size → stability → data
    V-->>LIN: Verdict SUPPORTED, append entry
    LIN->>G: lineage + debt
    G->>RT: schedule the next audit
```

**What to notice.** Three things happen before any science: the worker claims the
idempotency key, consults lineage, and checkpoints. That ordering is what makes the audit
both *targeted* — a claim contradicted on v1 is the one re-tested first — and *safe under
redelivery*.

Note what `Evidence` carries to the Verifier: measurements and hashes, and no verdict field.
The object that records observations is structurally unable to record a conclusion.

---

## Crash recovery

```mermaid
sequenceDiagram
    participant BUS as Event bus
    participant A as Worker A
    participant RT as Runtime state
    participant B as Worker B

    BUS->>A: event
    A->>RT: claim key
    A->>RT: checkpoint EXPERIMENT_RUNNING, runs 0..13
    A--xA: crash
    BUS->>B: redeliver (unacked)
    B->>RT: read checkpoint
    Note over B,RT: 14 runs already recorded
    B->>B: execute runs 14..23 only
    B->>RT: complete with the same idempotency key
```

**What to notice.** Worker B does not restart the experiment. Every run is checkpointed as
it completes, and run IDs are content-addressed — `run_id(experiment, kind, index)` — so a
re-executed run resolves to the same identity. `ExperimentResult.runs_reused` reports how
many were restored, and the test suite asserts on it.

This is why resumption cannot inflate a sample size, which would silently change a verdict.

---

## Investigation state machine

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> INGESTING
    INGESTING --> CLAIM_EXTRACTED
    CLAIM_EXTRACTED --> PROBE_PLANNED
    PROBE_PLANNED --> INTERVENTION_VALIDATED
    INTERVENTION_VALIDATED --> EXPERIMENT_RUNNING
    INTERVENTION_VALIDATED --> VERIFICATION: rejected probe → INCONCLUSIVE
    EXPERIMENT_RUNNING --> VERIFICATION
    VERIFICATION --> LINEAGE_UPDATED
    LINEAGE_UPDATED --> DEBT_RECALCULATED
    DEBT_RECALCULATED --> GOVERNOR_ACTION
    GOVERNOR_ACTION --> COMPLETE
    GOVERNOR_ACTION --> REVIEW: high-impact action
    REVIEW --> COMPLETE
    CLAIM_EXTRACTED --> QUARANTINED: poisoned input
    INGESTING --> FAILED
```

**What to notice.** There is no edge from `CLAIM_EXTRACTED` to `VERIFICATION`. Reaching a
verdict without running an experiment is not discouraged, it is unreachable: every
transition goes through `assert_transition`, which raises on anything the table does not
allow. A test asserts that specific edge is absent.

The `INTERVENTION_VALIDATED → VERIFICATION` shortcut is deliberate. A rejected probe is
INCONCLUSIVE evidence, not a crash.

---

## Data model

```mermaid
erDiagram
    CLAIM_FAMILY ||--o{ CLAIM : "one per model+distribution version"
    CLAIM ||--o{ EXPERIMENT : tested_by
    EXPERIMENT ||--o{ EXPERIMENT_RUN : contains
    EXPERIMENT ||--|| EVIDENCE : produces
    EVIDENCE ||--|| VERDICT : "verified into"
    VERDICT ||--|| LINEAGE_ENTRY : "appended as"
    LINEAGE_ENTRY ||--o{ LINEAGE_ENTRY : "supersedes / disputes / expires"
    LINEAGE_ENTRY }o--|| DEBT_SNAPSHOT : "scored into"
    DEBT_SNAPSHOT ||--o{ GOVERNOR_DECISION : "informs"
    GOVERNOR_DECISION ||--o| APPROVAL_REQUEST : "gates"
```

**What to notice.** `CLAIM_FAMILY` is the spine of the temporal story. A family ID is
derived from `(model_id, subject, predicate, object)` and deliberately *excludes* the
version — that exclusion is what lets one claim be followed from v1 to v4 instead of
becoming four unrelated records.

The self-referential edge on `LINEAGE_ENTRY` is how append-only works. Nothing is updated;
a new row states its relation to an old one.

---

## Storage split

```mermaid
flowchart LR
    subgraph Ledger["Evidence ledger — Cloud SQL / SQLite"]
        direction TB
        L1[claims, plans, runs]
        L2[evidence, verdicts]
        L3[lineage, debt, decisions]
        L4[audit events]
    end
    subgraph Runtime["Runtime state — Firestore / local JSON"]
        direction TB
        R1[investigation checkpoints]
        R2[idempotency records]
        R3[scheduled audits]
        R4[approval requests]
    end
    Ledger -.->|immutable, forever| KEEP[append-only]
    Runtime -.->|mutable, short-lived| RESUME[crash recovery]
```

**What to notice.** These are split because they have opposite lifecycles. Runtime state is
mutable and exists so a crashed worker can resume; evidence is immutable and exists forever.
Mixing them is how "we resumed the worker" turns into "we rewrote the verdict".

The ledger exposes no `update_*` or `delete_*` method at all — a test asserts that its
public surface contains none.

---

## What is deliberately not here

- **No message queue between agents.** Agents are called in-process by the pipeline. Putting
  a broker between them would add operational surface without changing a failure boundary,
  since the pipeline is already resumable.
- **No vector store or RAG.** Nothing here needs semantic retrieval; lineage lookups are
  exact queries on a claim family ID.
- **No separate rules service.** Policy is a frozen dataclass with a version. A service would
  make it mutable at runtime, which is precisely what the versioning exists to prevent.
