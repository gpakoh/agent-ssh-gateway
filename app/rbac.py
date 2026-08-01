"""RBAC: roles, permissions and tenant isolation for agent-ssh-gateway.

Roles sit on top of the existing scope system. A token may carry either
an explicit scope list (legacy behaviour, unchanged) or a role. When a
role is present, ``require_scope`` resolves the required scope against the
role's permissions instead of the raw scope list. Roles also carry a
resource selector (tenant labels): a session created by a tenant-labelled
agent inherits those labels, and non-admin roles can only list/detail
sessions whose labels intersect their selector.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Permissions — the four granular capabilities from the issue
# ---------------------------------------------------------------------------

CONNECT = "connect"
EXECUTE = "execute"
UPLOAD = "upload"
ADMIN = "admin"

ALL_PERMISSIONS = frozenset({CONNECT, EXECUTE, UPLOAD, ADMIN})

VALID_PERMISSIONS: frozenset[str] = ALL_PERMISSIONS


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Role:
    """A named bundle of permissions scoped to a resource selector.

    ``resource_selector`` is a tuple of ``"key=value"`` (or bare ``"key"``)
    labels. An empty selector matches all resources.
    """

    name: str
    permissions: frozenset[str]
    resource_selector: tuple[str, ...] = ()


BUILTIN_ROLES: dict[str, Role] = {
    "admin": Role(name="admin", permissions=ALL_PERMISSIONS),
    "operator": Role(name="operator", permissions=frozenset({CONNECT, EXECUTE, UPLOAD})),
    "viewer": Role(name="viewer", permissions=frozenset({CONNECT})),
    "custom": Role(name="custom", permissions=frozenset()),
}

VALID_ROLE_NAMES: frozenset[str] = frozenset(BUILTIN_ROLES)


# ---------------------------------------------------------------------------
# Scope → permission mapping (used when a role is present)
# ---------------------------------------------------------------------------

SCOPE_PERMISSIONS: dict[str, str] = {
    "ssh:connect": CONNECT,
    "ssh:execute": EXECUTE,
    "ssh:execute:argv": EXECUTE,
    "ssh:disconnect": EXECUTE,
    "ssh:files": UPLOAD,
    "ssh:port-check": CONNECT,
    "jobs:read": CONNECT,
    "jobs:run": EXECUTE,
    "auth:read": CONNECT,
    "diagnostics:read": CONNECT,
    "project:read": CONNECT,
    "project:patch": EXECUTE,
    "project:write": EXECUTE,
}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def get_role(name: str | None) -> Role | None:
    if name is None:
        return None
    return BUILTIN_ROLES.get(name)


def default_role_for_scopes(scopes: list[str] | tuple[str, ...] | None) -> str | None:
    """Existing/legacy tokens: an explicit wildcard scope list becomes admin.

    ``["*"]`` meant full access in the legacy scope system — those tokens
    keep full access through the admin role. Empty or explicit scope lists
    keep their original meaning: no role is attached (backward compatible).
    """
    if list(scopes or []) == ["*"]:
        return "admin"
    return None


def role_allows_scope(
    role_name: str | None, custom_permissions: frozenset[str] | None, required_scope: str
) -> bool:
    """Whether a role grants ``required_scope`` via its permissions.

    ``admin`` grants everything. For other roles the required scope is
    mapped to a permission; ``custom`` roles use ``custom_permissions``
    (empty by default → falls back to the raw scope list upstream).
    """
    if role_name is None:
        return False
    if role_name == "admin":
        return True
    if custom_permissions is not None and role_name == "custom":
        permissions = custom_permissions
    else:
        role = BUILTIN_ROLES.get(role_name)
        if role is None:
            return False
        permissions = role.permissions
    needed = SCOPE_PERMISSIONS.get(required_scope)
    if needed is None:
        return False
    return needed in permissions


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def labels_overlap(
    a: tuple[str, ...] | list[str] | None, b: tuple[str, ...] | list[str] | None
) -> bool:
    """True when the two label sets share at least one label."""
    if not isinstance(a, (tuple, list, set, frozenset)) or not isinstance(
        b, (tuple, list, set, frozenset)
    ):
        return False
    if not a or not b:
        return False
    return bool(set(a) & set(b))


def session_visible_to(session, identity) -> bool:
    """Whether ``identity`` may see ``session``.

    - master / admin role → everything
    - own sessions (created by this token fingerprint) → yes
    - role resource_selector or token tenant_labels intersect the
      session's tenant labels → yes (cross-tenant grant)
    """
    if identity.token_type == "master":
        return True
    if identity.role == "admin":
        return True
    if (
        getattr(session, "owner_type", None) == "agent"
        and getattr(session, "owner_token_fingerprint", None) == identity.fingerprint
    ):
        return True
    session_labels = getattr(session, "tenant_labels", ()) or ()
    role = get_role(identity.role)
    if role is not None and role.resource_selector:
        if labels_overlap(role.resource_selector, session_labels):
            return True
    if labels_overlap(identity.tenant_labels, session_labels):
        return True
    return False
