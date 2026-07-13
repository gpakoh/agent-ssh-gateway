"""Tests for MCP toolset hash computation."""

import hashlib
import json


def _canonical_form(tool_names: list[str], schemas: dict[str, dict]) -> str:
    """Replicate the canonical JSON logic for testing without FastMCP."""
    items = [{"name": n, "inputSchema": schemas.get(n, {})} for n in tool_names]
    items.sort(key=lambda item: item["name"])
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def test_canonical_sort_order():
    """Tool names must be sorted alphabetically."""
    schema_b = {"type": "object", "properties": {"y": {"type": "string"}}}
    schema_a = {"type": "object", "properties": {"x": {"type": "string"}}}
    canonical = _canonical_form(["b_tool", "a_tool"], {"b_tool": schema_b, "a_tool": schema_a})
    parsed = json.loads(canonical)
    assert parsed[0]["name"] == "a_tool"
    assert parsed[1]["name"] == "b_tool"


def test_deterministic_hash():
    """Same tools must produce the same hash regardless of insertion order."""
    schema = {"type": "object", "properties": {"cmd": {"type": "string"}}}
    c1 = _canonical_form(["run", "stop"], {"run": schema, "stop": schema})
    c2 = _canonical_form(["stop", "run"], {"run": schema, "stop": schema})
    h1 = "sha256:" + hashlib.sha256(c1.encode()).hexdigest()
    h2 = "sha256:" + hashlib.sha256(c2.encode()).hexdigest()
    assert h1 == h2


def test_different_tools_different_hash():
    """Different tool sets must produce different hashes."""
    schema = {"type": "object"}
    c1 = _canonical_form(["a", "b"], {"a": schema, "b": schema})
    c2 = _canonical_form(["a", "c"], {"a": schema, "c": schema})
    h1 = "sha256:" + hashlib.sha256(c1.encode()).hexdigest()
    h2 = "sha256:" + hashlib.sha256(c2.encode()).hexdigest()
    assert h1 != h2


def test_prefix_is_sha256():
    """Hash must start with sha256: prefix."""
    canonical = _canonical_form(["x"], {"x": {}})
    h = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_compact_json_no_spaces():
    """Canonical JSON must have no spaces after separators."""
    canonical = _canonical_form(["t"], {"t": {"type": "object"}})
    assert ", " not in canonical
    assert ": " not in canonical
