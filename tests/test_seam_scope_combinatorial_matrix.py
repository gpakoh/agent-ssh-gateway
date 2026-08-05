"""Seam test: has_required_scope() against every partial-overlap combination
of every multi-scope tool in TOOL_SCOPES — not just the specific tools
where the any()-vs-all() bug was found.

Context: has_required_scope() used any(s in token_scopes for s in required)
across a tool's *list* of required scopes. list_files/info/scan_command/
scan_file/list_tree all require ["mcp:read", "mcp:project"] — every
profile that grants mcp:project also grants mcp:read, so any() made the
mcp:project half of the requirement meaningless: a token with only
mcp:read satisfied the whole check. The bug only shows up on a *partial*
overlap between token scopes and required scopes — a token with ALL
required scopes or NONE of them gives any() and all() the same answer.
Existing single-scope-tool tests (e.g. "viewer can't call run_pytest")
never exercised a partial-overlap case at all.

This file is deliberately data-driven from TOOL_SCOPES itself (not a
hardcoded tool list) so the next tool anyone adds with 2+ required scopes
gets the same combinatorial check automatically, without needing its own
audit round to notice the same class of bug again.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from examples.mcp_server.tool_scopes import TOOL_SCOPES, has_required_scope

MULTI_SCOPE_TOOLS = {name: scopes for name, scopes in TOOL_SCOPES.items() if len(scopes) > 1}


def _partial_subsets(scopes: list[str]) -> list[list[str]]:
    """Every non-empty proper subset of scopes (missing at least one)."""
    subsets: list[list[str]] = []
    for size in range(1, len(scopes)):
        subsets.extend(list(c) for c in combinations(scopes, size))
    return subsets


PARTIAL_OVERLAP_CASES: list[tuple[str, list[str], list[str]]] = [
    (tool_name, required, subset)
    for tool_name, required in MULTI_SCOPE_TOOLS.items()
    for subset in _partial_subsets(required)
]


def test_multi_scope_tools_exist_to_check():
    """Sanity: fail loudly if TOOL_SCOPES ever loses all multi-scope
    entries — silently turning this whole file into a no-op instead of
    a real check (e.g. if someone "simplifies" every tool back down to a
    single required scope).
    """
    assert MULTI_SCOPE_TOOLS, "no multi-scope tools found — this file needs at least one to mean anything"


@pytest.mark.parametrize(
    "tool_name,required,token_scopes",
    PARTIAL_OVERLAP_CASES,
    ids=[f"{t}:{'+'.join(s)}" for t, _r, s in PARTIAL_OVERLAP_CASES],
)
def test_partial_scope_overlap_is_denied(tool_name, required, token_scopes):
    """A token holding some but not all of a tool's required scopes must
    be denied — this is exactly the case any() gets wrong and all() gets
    right.
    """
    missing = set(required) - set(token_scopes)
    assert missing, "test setup bug: token_scopes must be a proper subset of required"
    assert not has_required_scope(token_scopes, tool_name), (
        f"{tool_name} requires {required}; a token with only {token_scopes} "
        f"(missing {sorted(missing)}) must be denied, not allowed"
    )


@pytest.mark.parametrize(
    "tool_name,required",
    list(MULTI_SCOPE_TOOLS.items()),
    ids=list(MULTI_SCOPE_TOOLS.keys()),
)
def test_full_scope_set_is_allowed(tool_name, required):
    """Sanity complement: holding every required scope must still work —
    a test that only ever asserts denial would trivially pass if
    has_required_scope() denied everything unconditionally.
    """
    assert has_required_scope(list(required), tool_name)


@pytest.mark.parametrize(
    "tool_name,required",
    list(MULTI_SCOPE_TOOLS.items()),
    ids=list(MULTI_SCOPE_TOOLS.keys()),
)
def test_empty_scope_set_is_denied(tool_name, required):
    assert not has_required_scope([], tool_name)
