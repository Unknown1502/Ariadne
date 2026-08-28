"""The Investigator (prompt 04).

Turns "Urgency marker was the primary driver." into a claim that can be tested:

    IF urgency_marker is the primary driver
    THEN neutralizing urgency_marker while preserving signal_b and signal_c
    SHOULD lower the priority score by at least the declared threshold

This is the one job in Ariadne that genuinely needs a language model. Deciding whether a
sentence asserts primacy or mere influence, spotting that "several factors contributed" is
untestable as written, noticing an unstated assumption - that is semantic work, and rules
handle it badly.

What the Investigator cannot do is equally deliberate. It cannot run an experiment, write
evidence, or state a verdict; its manifest grants it a write scope on claims and nothing
else. So the worst case for a poisoned explanation is a badly-compiled claim, which the
verifier will then correctly rate as untestable or unsupported. The blast radius stops at
this file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.agents.audit import AuditSink
from backend.agents.base import AgentBase, ReasoningOutcome
from backend.agents.llm import LLMClient, LLMRequest
from backend.agents.prompts import CLAIM_COMPILER_SYSTEM, build_claim_prompt
from backend.agents.registry import INVESTIGATOR_MANIFEST
from backend.agents.sanitizer import sanitize_explanation, sanitize_identifier
from backend.core.agent_contracts import AgentManifest
from backend.core.clock import Clock
from backend.core.enums import ExpectedDirection, LineageRelation, RiskLevel
from backend.core.errors import UntestableExplanation, ValidationError
from backend.core.hashing import sha256_hex
from backend.core.ids import claim_family_id, claim_id
from backend.core.schemas import Claim, VersionScope
from backend.experiment_engine.distributions import FEATURE_NAMES
from backend.lineage.service import LineageService

VALID_DIRECTIONS = {d.value for d in ExpectedDirection}

MIN_TESTABILITY = 0.30
"""Below this, a claim carries too little structure for any experiment to mean much.
Matches the verifier's own VAGUE_CLAIM gate so the two agree on what 'untestable' means."""


class Investigator(AgentBase):
    """Compiles an explanation into a Claim."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        manifest: AgentManifest = INVESTIGATOR_MANIFEST,
        lineage: LineageService | None = None,
        clock: Clock | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        super().__init__(manifest, llm, clock=clock, audit=audit)
        self._lineage = lineage

    def compile_claim(
        self,
        *,
        explanation: str,
        decision: str,
        scope: VersionScope,
        investigation_id: str,
        valid_from: datetime | None = None,
    ) -> tuple[Claim, ReasoningOutcome]:
        """Compile one explanation. Raises LoopBudgetExceeded if the model cannot comply."""
        self.require_write("claim")
        moment = valid_from or self._clock.now()

        # 1. External text is data. Sanitize before it goes anywhere near a prompt.
        sanitized = sanitize_explanation(explanation)
        if not sanitized.text:
            raise ValidationError("explanation is empty after sanitization")

        # 2. Prior lineage, if we are allowed to read it, sets audit priority.
        prior_lineage, audit_priority, prior_status = self._recall(scope, investigation_id)

        request = LLMRequest(
            system=CLAIM_COMPILER_SYSTEM,
            user=build_claim_prompt(
                explanation=sanitized.text,
                decision=decision,
                model_id=scope.model_id,
                model_version=scope.model_version,
                distribution_version=scope.distribution_version,
                available_features=list(FEATURE_NAMES),
                prior_lineage=prior_lineage,
                audit_priority=audit_priority,
            ),
            task="compile_claim",
            context={"explanation": sanitized.text, "decision": decision},
            temperature=0.0,
        )

        _, outcome = self.reason(
            request, investigation_id=investigation_id, validate=self._validate
        )
        payload = outcome.payload

        subject = sanitize_identifier(str(payload["subject"]))
        family = claim_family_id(
            scope.model_id,
            subject,
            sanitize_identifier(str(payload["predicate"])),
            sanitize_identifier(str(payload["object"])),
        )

        claim = Claim(
            id=claim_id(family, scope.model_version, scope.distribution_version),
            claim_family_id=family,
            investigation_id=investigation_id,
            scope=scope,
            source_explanation=sanitized.text,
            source_explanation_hash=sha256_hex(sanitized.text),
            source_decision=decision[:128],
            subject=subject,
            predicate=sanitize_identifier(str(payload["predicate"])),
            object=sanitize_identifier(str(payload["object"])),
            expected_direction=ExpectedDirection(payload["expected_direction"]),
            expected_effect=None,
            primacy_claim=bool(payload.get("primacy_claim", False)),
            target_variables=[sanitize_identifier(v) for v in payload["target_variables"]],
            preserved_constraints=[
                sanitize_identifier(v) for v in payload.get("preserved_constraints", [])
            ],
            assumptions=[str(a)[:200] for a in payload.get("assumptions", [])][:16],
            ambiguities=[str(a)[:200] for a in payload.get("ambiguities", [])][:16],
            testability_score=float(payload["testability_score"]),
            confidence=float(payload["confidence"]),
            audit_priority=audit_priority,
            prior_verdict=prior_status,
            valid_from=moment,
            provenance=outcome.provenance,
            quarantined=sanitized.is_suspicious,
            quarantine_reasons=sanitized.quarantine_reasons,
        )
        return claim, outcome

    # -- validation --------------------------------------------------------------------

    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Semantic checks on the model's output, before a Claim is constructed.

        Raising here is a *retryable* signal: the base class treats it as a malformed
        response and asks again, up to the loop budget. That is the right response to a
        model that hallucinated a feature name - ask again, then give up loudly.
        """
        required = (
            "subject", "predicate", "object", "expected_direction", "target_variables",
            "testability_score", "confidence",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"claim output is missing required keys: {missing}")

        direction = str(payload["expected_direction"]).lower()
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"expected_direction must be one of {sorted(VALID_DIRECTIONS)}, "
                f"got {direction!r}"
            )
        payload["expected_direction"] = direction

        testability = float(payload.get("testability_score", 0.0) or 0.0)
        subject = sanitize_identifier(str(payload["subject"]))
        if subject not in FEATURE_NAMES:
            if testability < MIN_TESTABILITY:
                # The model complied and reported that the explanation names no testable
                # driver. That is a legitimate finding about the explanation, not a
                # malformed response, so it is not retried - retrying would only push the
                # model into inventing a driver that was never claimed.
                raise UntestableExplanation(
                    f"the explanation names no testable driver "
                    f"(subject={subject!r}, testability={testability:.2f}); "
                    f"laboratory features are {list(FEATURE_NAMES)}"
                )
            raise ValueError(
                f"subject {subject!r} is not a laboratory feature; "
                f"available: {list(FEATURE_NAMES)}"
            )

        targets = payload.get("target_variables") or []
        if not isinstance(targets, list) or not targets:
            raise ValueError("target_variables must be a non-empty list")
        cleaned_targets = [sanitize_identifier(str(v)) for v in targets]
        unknown = [v for v in cleaned_targets if v not in FEATURE_NAMES]
        if unknown:
            raise ValueError(f"target_variables contains unknown features: {unknown}")
        if subject not in cleaned_targets:
            raise ValueError(f"subject {subject!r} must appear in target_variables")
        payload["target_variables"] = cleaned_targets

        preserved = [
            sanitize_identifier(str(v)) for v in payload.get("preserved_constraints", [])
        ]
        overlap = set(preserved) & set(cleaned_targets)
        if overlap:
            raise ValueError(
                f"a variable cannot be both intervened on and preserved: {sorted(overlap)}"
            )
        payload["preserved_constraints"] = [v for v in preserved if v in FEATURE_NAMES]

        for field in ("testability_score", "confidence"):
            try:
                value = float(payload[field])
            except (TypeError, ValueError):
                raise ValueError(f"{field} must be numeric, got {payload[field]!r}") from None
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field} must lie in [0, 1], got {value}")
            payload[field] = value

        return payload

    # -- memory ------------------------------------------------------------------------

    def _recall(
        self, scope: VersionScope, investigation_id: str
    ) -> tuple[list[dict[str, Any]], float, Any]:
        """Retrieve prior verdicts recorded for this model.

        This is what makes the autonomous audit targeted rather than an exhaustive sweep: a
        claim contradicted on an earlier version is the one most worth re-testing when a new
        version ships.

        Note the ordering problem this has to work around - the claim family is a function
        of the compiled claim, which does not exist yet. So the Investigator recalls every
        family recorded for the *model*, hands the model that history as context, and the
        highest priority among them sets this investigation's priority. Guessing a single
        family up front would silently miss any explanation that compiles to a different one.
        """
        if self._lineage is None:
            return [], 0.5, None

        self.check_tool(
            "lineage.get_family",
            risk=RiskLevel.READ_ONLY,
            investigation_id=investigation_id,
            arguments={"model_id": scope.model_id},
        )

        rows: list[dict[str, Any]] = []
        priorities: list[float] = []
        prior_status = None

        for family in self._lineage.families_for_model(scope.model_id):
            view = self._lineage.view(family)
            for entry in view.entries:
                if entry.relation is LineageRelation.EXPIRES:
                    continue
                rows.append(
                    {
                        "model_version": entry.scope.model_version,
                        "distribution_version": entry.scope.distribution_version,
                        "status": str(entry.status),
                        "effect_size": entry.effect_size,
                    }
                )
            priorities.append(self._lineage.audit_priority(family))
            if view.current is not None and prior_status is None:
                prior_status = view.current.status

        priority = max(priorities) if priorities else 0.5
        return rows, priority, prior_status
