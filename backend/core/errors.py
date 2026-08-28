"""Ariadne error taxonomy.

Errors are typed so the runtime can decide, deterministically, whether a failure is
retryable, whether it should be dead-lettered, and whether it invalidates evidence.
"""

from __future__ import annotations


class AriadneError(Exception):
    """Base class for every Ariadne failure."""

    retryable: bool = False


class ValidationError(AriadneError):
    """A contract or constraint was violated. Never retryable."""

    retryable = False


class InvalidStateTransition(AriadneError):
    """An investigation was asked to move to a state the machine forbids."""

    retryable = False


class InterventionRejected(ValidationError):
    """An experiment plan proposed an intervention that is out of scope or invalid."""


class ConstraintViolation(ValidationError):
    """A declared preserved constraint was not preserved within tolerance."""


class VersionMismatch(ValidationError):
    """An artifact was used against a model or distribution version it is not scoped to."""


class TargetModelError(AriadneError):
    """The synthetic target model failed to produce an output."""

    retryable = True


class AgentOutputError(AriadneError):
    """An agent returned output that does not satisfy its declared schema."""

    retryable = True


class AgentTimeout(AriadneError):
    """An agent did not respond within its budget."""

    retryable = True


class AgentResponseRejected(AriadneError):
    """The model returned something unusable for a reason that will recur on retry.

    Truncation at max_output_tokens and safety blocking both look like malformed output to a
    JSON parser, but neither is transient: every agent runs at temperature 0, so an identical
    request produces an identical truncation. Retrying burns the loop budget and bills for
    the privilege, then reports "malformed JSON" - which sends the next person debugging in
    entirely the wrong direction.
    """

    retryable = False


class LoopBudgetExceeded(AriadneError):
    """An agent exceeded its bounded retry/loop budget and must be quarantined."""

    retryable = False


class PermissionDenied(AriadneError):
    """An agent attempted an action outside its registry-declared scopes."""

    retryable = False


class AppendOnlyViolation(AriadneError):
    """Something attempted to mutate or delete historical evidence."""

    retryable = False


class StorageError(AriadneError):
    """A persistence layer failed."""

    retryable = True


class QuarantinedInput(ValidationError):
    """External text was quarantined as an injection or poisoning attempt."""


class UntestableExplanation(ValidationError):
    """The explanation is too vague to compile into a testable claim.

    Distinct from AgentOutputError on purpose. AgentOutputError means the model failed to
    comply and a retry might help. This means the model *did* comply and correctly reported
    that the explanation names no testable driver - "several factors contributed" is not a
    hypothesis. Retrying would only pressure the model into inventing one, so this is not
    retryable, and the investigation ends without a verdict rather than with a fabricated one.
    """

    retryable = False
