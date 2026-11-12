"""Real source-local MCP preflight, before a model or proxy is used (#684).

The subprocess executes only the Gateway-owned bounded checker, never a
project's configured MCP command. No browser access or network installations.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from examples.mcp_server.agent_tools import (
    _OPENCODE_LOCAL_MCP_PREFLIGHT_PY,
    _build_opencode_script,
)


def _check(root: Path, config: dict | None = None) -> tuple[int, dict, str]:
    if config is not None:
        (root / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", _OPENCODE_LOCAL_MCP_PREFLIGHT_PY],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode, json.loads(result.stdout), result.stderr


def _local(command: list[str]) -> dict:
    return {"mcp": {"browser-harness": {
        "type": "local", "enabled": True, "command": command,
    }}}


def test_no_project_mcp_config_is_nonblocking(tmp_path: Path):
    code, payload, err = _check(tmp_path)
    assert code == 0 and err == ""
    assert payload == {"schema_version": 1, "status": "ready", "checked": 0}


def test_jsonc_comments_trailing_commas_and_schema_url_are_supported(tmp_path: Path):
    (tmp_path / "browser_policy_mcp.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "opencode.jsonc").write_text(
        '{\n'
        '  // a normal JSONC comment\n'
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  "mcp": {\n'
        '    "browser-harness": { "type": "local",\n'
        '      "command": ["python", "browser_policy_mcp.py",],\n'
        '      "enabled": true,\n'
        '    },\n'
        '  },\n'
        '}\n',
        encoding="utf-8",
    )
    code, payload, err = _check(tmp_path)
    assert code == 0 and err == ""
    assert payload["status"] == "ready"
    assert payload["checked"] == 1


def test_unclosed_jsonc_comment_is_typed_and_secret_free(tmp_path: Path):
    (tmp_path / "opencode.jsonc").write_text(
        '/* PRIVATE_SECRET unclosed', encoding="utf-8"
    )
    code, payload, err = _check(tmp_path)
    assert code != 0 and err == ""
    assert payload["status"] == "blocked"
    assert payload["reason"] == "invalid_config"
    assert "PRIVATE_SECRET" not in json.dumps(payload)


def test_missing_source_pinned_local_mcp_script_is_typed_and_secret_free(tmp_path: Path):
    code, payload, err = _check(
        tmp_path, _local(["python", "browser_policy_mcp.py?token=PRIVATE_SECRET"])
    )
    assert code != 0 and err == ""
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_entrypoint"
    assert "PRIVATE_SECRET" not in json.dumps(payload)
    assert "browser_policy_mcp" not in json.dumps(payload)


def test_local_mcp_static_import_dependency_absent_is_bounded(tmp_path: Path):
    (tmp_path / "browser_policy_mcp.py").write_text(
        "from browser_harness_missing_preflight_684 import no_such_package\n"
    )
    code, payload, err = _check(tmp_path, _local(["python", "browser_policy_mcp.py"]))
    assert code != 0 and err == ""
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_module"


def test_local_mcp_ready_from_isolated_workspace_does_not_execute_script(tmp_path: Path):
    marker = tmp_path / "executed"
    (tmp_path / "browser_policy_mcp.py").write_text(
        "import json\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
    )
    code, payload, err = _check(tmp_path, _local(["python", "browser_policy_mcp.py"]))
    assert code == 0 and err == ""
    assert payload["status"] == "ready" and payload["checked"] == 1
    assert not marker.exists()


def test_non_python_local_mcp_command_is_not_claimed_verified(tmp_path: Path):
    code, payload, err = _check(tmp_path, _local(["npx", "-y", "browser-server"]))
    assert code == 0 and err == ""
    assert payload["status"] == "partial"
    assert payload["checked"] == 0
    assert payload["unverified"] == 1


def test_unsafe_symlink_config_is_not_read(tmp_path: Path):
    target = tmp_path / "private.json"
    target.write_text(json.dumps(_local(["python", "browser_policy_mcp.py"])))
    (tmp_path / "opencode.json").symlink_to(target)
    code, payload, err = _check(tmp_path)
    assert code != 0 and err == ""
    assert payload["reason"] == "unsafe_config"


def test_uninspected_node_mcp_preserves_existing_launch_without_false_readiness(tmp_path: Path):
    cfg = {"mcp": {"node-server": {
        "type": "local", "enabled": True,
        "command": ["npx", "--yes", "some-package"],
    }}}
    code, payload, err = _check(tmp_path, cfg)
    assert code == 0 and err == ""
    assert payload == {
        "schema_version": 1,
        "status": "partial",
        "checked": 0,
        "unverified": 1,
    }


def test_missing_python_module_is_blocked_without_import_execution(tmp_path: Path):
    code, payload, err = _check(tmp_path, _local(["python", "-m", "missing.module"]))
    assert code != 0 and err == ""
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_module"


def test_disabled_local_mcp_is_ignored(tmp_path: Path):
    cfg = _local(["python", "missing.py"])
    cfg["mcp"]["browser-harness"]["enabled"] = False
    code, payload, err = _check(tmp_path, cfg)
    assert code == 0 and err == ""
    assert payload["checked"] == 0




def test_local_mcp_script_symlink_parent_cannot_escape_workspace(tmp_path: Path):
    outside = tmp_path.parent / ("external-mcp-" + tmp_path.name)
    outside.mkdir()
    (outside / "browser_policy_mcp.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    code, payload, err = _check(tmp_path, _local(["python", "escape/browser_policy_mcp.py"]))
    assert code != 0 and err == ""
    assert payload["reason"] == "unsafe_entrypoint"


def test_local_mcp_absolute_script_outside_workspace_is_denied(tmp_path: Path):
    outside = tmp_path.parent / ("external-abs-mcp-" + tmp_path.name)
    outside.mkdir()
    script = outside / "browser_policy_mcp.py"
    script.write_text("import json\n", encoding="utf-8")
    code, payload, err = _check(tmp_path, _local(["python", str(script)]))
    assert code != 0 and err == ""
    assert payload["reason"] == "unsafe_entrypoint"


def test_python_m_submodule_probe_does_not_import_package_parent(tmp_path: Path):
    pkg = tmp_path / "local_mcp_test_pkg"
    pkg.mkdir()
    marker = tmp_path / "package-executed"
    (pkg / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (pkg / "server.py").write_text("import json\n", encoding="utf-8")
    code, payload, err = _check(tmp_path, _local(["python", "-m", "local_mcp_test_pkg.server"]))
    assert code == 0 and err == ""
    assert payload["status"] == "ready" and payload["checked"] == 1
    assert not marker.exists()

def test_runner_blocks_before_proxy_acquisition_and_model_invocation(monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", "http://proxy-parser:8080/proxy")
    script = _build_opencode_script(
        ".ai-bridge/tasks/abcdef123456", "abcdef123456", None,
        project_root="/srv/proj",
    )
    assert "OPENCODE_LOCAL_MCP_PREFLIGHT_EOF" in script
    assert script.index("OPENCODE_LOCAL_MCP_PREFLIGHT_EOF") < script.index(
        "acquire_opencode_proxy"
    )
    assert 'elif [ "$OPENCODE_LOCAL_MCP_BLOCKED" -eq 1 ]; then' in script
    assert '"opencode-local-mcp-unavailable"' in script


def test_missing_local_python_executable_blocks_before_model(tmp_path: Path):
    script = tmp_path / "browser_policy_mcp.py"
    script.write_text("import json\n", encoding="utf-8")
    missing_python = tmp_path / "missing-interpreter" / "python"
    code, payload, err = _check(
        tmp_path, _local([str(missing_python), script.name])
    )
    assert code != 0 and err == ""
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_interpreter"
    assert "missing-interpreter" not in json.dumps(payload)


def test_other_python_executable_is_not_misreported_ready(tmp_path: Path):
    marker = tmp_path / "foreign-python-executed"
    shim = tmp_path / "python"
    shim.write_text(
        "#!/bin/sh\nprintf 'executed' > " + str(marker) + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    (tmp_path / "browser_policy_mcp.py").write_text(
        "from missing_package_only_in_foreign_interpreter import value\n",
        encoding="utf-8",
    )
    code, payload, err = _check(
        tmp_path, _local([str(shim), "browser_policy_mcp.py"])
    )
    assert code == 0 and err == ""
    assert payload["status"] == "partial"
    assert payload["checked"] == 1
    assert payload["unverified"] == 1
    assert not marker.exists()
