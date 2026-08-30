"""Backend authorization for the governance API.

Frontend permission checks are UX. This is the security boundary, and it is enforced here
because a console that hides a button has not prevented anything - the request still works
for anyone who knows the URL.

**Two modes, and the honesty endpoint reports which one is live.** When no API keys are
configured, the API runs OPEN: every request is granted the operator role, which is the right
default for `pytest` and for someone running the demo locally with no credentials to manage.
When keys are configured it runs ENFORCED, and an unknown or missing key is refused.

The mode is not a hidden flag. `/api/v1/system` publishes it, so "is this deployment
protected?" is answerable by a reviewer without reading configuration they cannot see. A
system that quietly ran open while looking protected would be worse than one with no auth at
all, because the first invites trust it has not earned.

**Roles map to what the four cognitive roles already mean.** The agent manifests already
divide authority by what each role may *write*; these are the human equivalents, and the
permission table below is deliberately the same shape - who may configure, who may review,
who may only read.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description=(
        "Role-bearing API key. Required when the deployment is in ENFORCED mode; ignored "
        "in OPEN mode. Check GET /api/v1/system -> authorization to see which is live. "
        "Roles: MODEL_ADMIN (configure), REVIEWER (decide approvals), OPERATOR (emit "
        "events), READ_ONLY. Every role may read; no role may write a verdict."
    ),
)
"""Declared through FastAPI's security machinery rather than as a bare Header so it appears
in the OpenAPI document. Auth that is enforced but undocumented is auth an integrator
discovers by receiving a 401."""


class Role(StrEnum):
    """Human roles. Distinct from `AgentRole`, which governs what software may write."""

    MODEL_ADMIN = "MODEL_ADMIN"
    """Configures connections, feature semantics and explanation sources."""

    REVIEWER = "REVIEWER"
    """Reviews evidence and decides pending approvals. Cannot reconfigure the system."""

    OPERATOR = "OPERATOR"
    """Emits lifecycle events - the role a model registry or drift monitor would hold."""

    READ_ONLY = "READ_ONLY"
    """Sees everything, changes nothing. The default for an auditor."""


class Permission(StrEnum):
    READ = "READ"
    CONFIGURE = "CONFIGURE"
    EMIT_EVENT = "EMIT_EVENT"
    DECIDE_APPROVAL = "DECIDE_APPROVAL"


GRANTS: dict[Role, frozenset[Permission]] = {
    Role.MODEL_ADMIN: frozenset(
        {Permission.READ, Permission.CONFIGURE, Permission.EMIT_EVENT}
    ),
    Role.REVIEWER: frozenset({Permission.READ, Permission.DECIDE_APPROVAL}),
    Role.OPERATOR: frozenset({Permission.READ, Permission.EMIT_EVENT}),
    Role.READ_ONLY: frozenset({Permission.READ}),
}
"""The whole policy, in one readable table.

Note what nobody has: there is no WRITE_VERDICT permission, because no human writes a
verdict. The verifier does, from measurements, and adding a role that could override it
would dissolve the property the entire system rests on."""


class Principal:
    """Who is making this request, and what they may do."""

    def __init__(self, name: str, role: Role, *, enforced: bool) -> None:
        self.name = name
        self.role = role
        self.enforced = enforced

    def may(self, permission: Permission) -> bool:
        """In OPEN mode everything is permitted, and the honesty endpoint says so.

        The alternative - running OPEN but still refusing some permissions - would be the
        worst of both: no security, and a confusing 403 for a local user who configured
        nothing. Enforcement is a deployment decision, not a per-permission one.
        """
        if not self.enforced:
            return True
        return permission in GRANTS[self.role]

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Principal({self.name!r}, {self.role}, enforced={self.enforced})"


def _configured_keys() -> dict[str, tuple[str, Role]]:
    """Parse `ARIADNE_API_KEYS` into key -> (name, role).

    Format: ``key:name:ROLE`` entries separated by commas. Read from the environment on every
    call rather than cached, so rotating a key does not require a restart - and so a test can
    change the policy without fighting a module-level singleton.

    Malformed entries are skipped rather than raising. A typo in one credential must not take
    the whole API down, and the skipped entry simply grants nothing.
    """
    raw = os.environ.get("ARIADNE_API_KEYS", "").strip()
    if not raw:
        return {}
    table: dict[str, tuple[str, Role]] = {}
    for entry in raw.split(","):
        parts = [piece.strip() for piece in entry.split(":")]
        if len(parts) != 3 or not all(parts):
            continue
        key, name, role_name = parts
        try:
            table[key] = (name, Role(role_name.upper()))
        except ValueError:
            continue
    return table


def auth_mode() -> str:
    """`ENFORCED` when keys are configured, `OPEN` otherwise. Published by /api/v1/system."""
    return "ENFORCED" if _configured_keys() else "OPEN"


def current_principal(
    x_api_key: Annotated[str | None, Depends(API_KEY_HEADER)] = None,
) -> Principal:
    """Resolve the caller, refusing an unknown key when enforcement is on."""
    keys = _configured_keys()
    if not keys:
        # No keys configured: unrestricted, and `auth_mode()` reports OPEN so nobody has
        # to infer it. The role recorded is the most privileged one for audit-trail clarity.
        return Principal("anonymous", Role.MODEL_ADMIN, enforced=False)

    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="this deployment enforces authorization; supply an X-API-Key header",
        )
    entry = keys.get(x_api_key)
    if entry is None:
        raise HTTPException(status_code=401, detail="unknown API key")
    name, role = entry
    return Principal(name, role, enforced=True)


def requires(permission: Permission):
    """A dependency that refuses a caller lacking `permission`.

    Returns 403 with the role and the permission it lacked. Saying which permission was
    missing is deliberate: an operator who cannot tell whether they were refused for the
    wrong role or the wrong endpoint will assume the endpoint is broken.
    """

    def guard(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.may(permission):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"role {principal.role} does not grant {permission}; "
                    f"this action needs one of "
                    f"{sorted(r for r, g in GRANTS.items() if permission in g)}"
                ),
            )
        return principal

    return guard


CanRead = Annotated[Principal, Depends(requires(Permission.READ))]
CanConfigure = Annotated[Principal, Depends(requires(Permission.CONFIGURE))]
CanEmit = Annotated[Principal, Depends(requires(Permission.EMIT_EVENT))]
CanDecide = Annotated[Principal, Depends(requires(Permission.DECIDE_APPROVAL))]
