"""Regression: asyncio.iscoroutinefunction → inspect.iscoroutinefunction

asyncio.iscoroutinefunction is deprecated since Python 3.12 and emits
DeprecationWarning on non-coroutine callables.  tool_registry must use
inspect.iscoroutinefunction to stay warning-free on 3.11+ and forward-compatible.
"""

import ast
import pathlib

_TOOL_REGISTRY = (
    pathlib.Path(__file__).resolve().parent.parent
    / "examples"
    / "mcp_server"
    / "mcp_infra"
    / "tool_registry.py"
)


def test_tool_registry_uses_inspect_not_asyncio():
    """Every iscoroutinefunction call must be inspect.iscoroutinefunction."""
    source = _TOOL_REGISTRY.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "iscoroutinefunction":
            assert isinstance(node.value, ast.Name), f"unexpected qualname: {ast.dump(node.value)}"
            assert node.value.id == "inspect", (
                f"tool_registry.py uses {node.value.id}.iscoroutinefunction — "
                "expected inspect.iscoroutinefunction"
            )


def test_tool_registry_has_no_asyncio_import():
    """No asyncio import should remain in tool_registry after the fix."""
    source = _TOOL_REGISTRY.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module or ""]
            assert "asyncio" not in names, (
                f"tool_registry.py still imports asyncio at line {node.lineno}"
            )
