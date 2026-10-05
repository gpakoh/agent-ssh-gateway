from __future__ import annotations

import os

# Roots under which all project paths must resolve (symlink-safe).
#
# MCP_ALLOWED_PROJECT_ROOTS lists them (comma-separated absolute paths). There
# is deliberately no built-in default: the allowlist is a security boundary and
# the roots are deployment configuration, so an unset or empty value must fail
# closed rather than fall back to a host-specific layout that no other install
# would share.
PROJECT_ROOTS_ENV = "MCP_ALLOWED_PROJECT_ROOTS"


def _load_allowed_roots() -> list[str]:
    raw = os.environ.get(PROJECT_ROOTS_ENV, "").strip()
    roots = [r.strip() for r in raw.split(",") if r.strip()]
    if not roots:
        raise RuntimeError(
            f"{PROJECT_ROOTS_ENV} must list the absolute roots under which "
            "project paths may resolve (comma-separated). It has no default: "
            "an unset value would reject every path, and a hardcoded one would "
            "leak this host's layout."
        )
    return roots


ALLOWED_PROJECT_ROOTS: list[str] = _load_allowed_roots()
