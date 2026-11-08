"""SSH target registry and execution tests: no real SSH and no private keys leaked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from examples.mcp_server import ssh_targets
from examples.mcp_server.mcp_infra.adapters import gateway


def _config(tmp_path: Path, *, targets: dict | None = None) -> tuple[Path, Path]:
    keys = tmp_path / "keys"
    keys.mkdir()
    for name in ("lxc103", "lxc104"):
        key = keys / name
        key.write_text("synthetic ssh private key bytes (test only)")
        key.chmod(0o600)
    conf = tmp_path / "targets.json"
    conf.write_text(json.dumps({
        "version": 1,
        "targets": targets if targets is not None else {
            "lxc103": {
                "host": "192.0.2.103", "port": 22,
                "username": "gatewayops", "key_file": "lxc103",
            },
            "lxc104": {
                "host": "192.0.2.104", "port": 2222,
                "username": "mailops", "key_file": "lxc104",
            },
        },
    }), encoding="utf-8")
    conf.chmod(0o600)
    return conf, keys


def test_explicit_two_targets_with_no_key_paths_in_inventory(tmp_path, monkeypatch):
    config, keys = _config(tmp_path)
    monkeypatch.setenv("MCP_SSH_TARGETS_FILE", str(config))
    monkeypatch.setenv("MCP_SSH_TARGET_KEY_DIR", str(keys))
    inventory = ssh_targets.target_inventory()
    assert inventory == [
        {"name": "lxc103", "host": "192.0.2.103", "port": 22,
         "username": "gatewayops"},
        {"name": "lxc104", "host": "192.0.2.104", "port": 2222,
         "username": "mailops"},
    ]
    assert "keys" not in repr(inventory)
    assert not any("key_file" in target for target in inventory)


def test_unconfigured_registry_is_empty_and_does_not_fallback_to_local_session(monkeypatch):
    monkeypatch.delenv("MCP_SSH_TARGETS_FILE", raising=False)
    assert ssh_targets.load_targets() == {}
    with pytest.raises(ssh_targets.TargetConfigError, match="Unknown or unconfigured"):
        ssh_targets.execute_target(target="lxc104", argv=["hostname"])


@pytest.mark.parametrize("invalid", [
    {"lxc104": {"host": "localhost", "port": 22, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "127.0.0.1", "port": 22, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "169.254.169.254", "port": 22, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "0.0.0.0", "port": 22, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "192.0.2.104", "port": 0, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "192.0.2.104", "port": True, "username": "ops", "key_file": "lxc104"}},
    {"lxc104": {"host": "192.0.2.104", "port": 22, "username": "root;id", "key_file": "lxc104"}},
    {"lxc104": {"host": "192.0.2.104", "port": 22, "username": "ops", "key_file": "../root"}},
    {"lxc104": {"host": "192.0.2.104", "port": 22, "username": "ops", "key_file": "/root/.ssh/id_ed25519"}},
    {"Bad Alias": {"host": "192.0.2.104", "port": 22, "username": "ops", "key_file": "lxc104"}},
])
def test_target_injection_paths_and_invalid_network_rejected(tmp_path, invalid):
    config, keys = _config(tmp_path, targets=invalid)
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.load_targets(config_file=str(config), key_dir=str(keys))


def test_private_key_permission_symlink_missing_and_config_permissions_fail_closed(tmp_path):
    config, keys = _config(tmp_path)
    key = keys / "lxc104"
    key.chmod(0o644)
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.load_targets(config_file=str(config), key_dir=str(keys))
    key.unlink()
    key.symlink_to(keys / "lxc103")
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.load_targets(config_file=str(config), key_dir=str(keys))
    key.unlink()
    key.write_text("safe synthetic key")
    key.chmod(0o600)
    config.chmod(0o666)
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.load_targets(config_file=str(config), key_dir=str(keys))
    config.chmod(0o600)
    config.write_text('{"version":1,"version":1,"targets":{}}', encoding="utf-8")
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.load_targets(config_file=str(config), key_dir=str(keys))


def test_target_command_has_new_ephemeral_sid_and_always_disconnects(tmp_path, monkeypatch):
    config, keys = _config(tmp_path)
    monkeypatch.setenv("MCP_SSH_TARGETS_FILE", str(config))
    monkeypatch.setenv("MCP_SSH_TARGET_KEY_DIR", str(keys))
    instances = []

    def factory(**kwargs):
        client = MagicMock()
        client.kwargs = kwargs
        client.execute_argv.return_value = {
            "stdout": "lxc104\n", "stderr": "", "exit_code": 0, "duration": 0.3
        }
        instances.append(client)
        return client

    result = ssh_targets.execute_target(
        target="lxc104", argv=["hostname"], client_factory=factory
    )
    assert result["stdout"] == "lxc104\n"
    client = instances[0]
    assert client.kwargs["ssh_host"] == "192.0.2.104"
    assert client.kwargs["ssh_port"] == 2222
    assert client.kwargs["ssh_user"] == "mailops"
    assert client.kwargs["session_id"] == ""
    assert client.kwargs["ssh_private_key"] == ""
    assert client.kwargs["ssh_key_path"] == str(keys / "lxc104")
    assert client._ephemeral is True
    assert client._reuse_existing is False
    assert client._idle_timeout_seconds == 60
    client.connect.assert_called_once_with()
    client.execute_argv.assert_called_once_with(
        argv=["hostname"], stdin="", timeout_s=30
    )
    client.disconnect.assert_called_once_with()

    client.execute_argv.side_effect = RuntimeError("remote unavailable")
    with pytest.raises(RuntimeError):
        ssh_targets.execute_target(
            target="lxc103", argv=["hostname"], client_factory=lambda **kw: client
        )
    assert client.disconnect.call_count == 2


@pytest.mark.parametrize("argv,stdin,timeout", [
    ([], "", 30),
    (["ls\x00bad"], "", 30),
    (["x" * 65537], "", 30),
    (["hostname"], "x" * 1048577, 30),
    (["hostname"], "", 0),
    (["hostname"], "", 121),
    (["hostname"], "", True),
])
def test_invalid_commands_rejected_before_key_or_network(tmp_path, monkeypatch, argv, stdin, timeout):
    config, keys = _config(tmp_path)
    monkeypatch.setenv("MCP_SSH_TARGETS_FILE", str(config))
    monkeypatch.setenv("MCP_SSH_TARGET_KEY_DIR", str(keys))
    with pytest.raises(ssh_targets.TargetConfigError):
        ssh_targets.execute_target(target="lxc104", argv=argv, stdin=stdin, timeout_s=timeout)


def test_mcp_tools_fail_closed_and_never_return_key_paths(monkeypatch, tmp_path):
    config, keys = _config(tmp_path)
    monkeypatch.setenv("MCP_SSH_TARGETS_FILE", str(config))
    monkeypatch.setenv("MCP_SSH_TARGET_KEY_DIR", str(keys))
    result = gateway.gateway_list_ssh_targets()
    assert result["ok"] is True
    assert result["result"]["count"] == 2
    assert "key_file" not in str(result)

    called = []
    monkeypatch.setattr(gateway, "execute_target", lambda **kwargs: (
        called.append(kwargs),
        {"stdout": "LXC104", "stderr": "", "exit_code": 0, "duration": 0.1}
    )[1])
    outcome = gateway.gateway_execute_target_argv(
        target="lxc104", argv=["hostname"], timeout_s=15
    )
    assert outcome["ok"] is True
    assert outcome["result"]["target"] == "lxc104"
    assert called == [{"target": "lxc104", "argv": ["hostname"], "stdin": "", "timeout_s": 15}]

    monkeypatch.setattr(gateway, "execute_target", lambda **_: (
        _ for _ in ()
    ).throw(ssh_targets.TargetConfigError("secret path /root/.ssh/private")))
    err = gateway.gateway_execute_target_argv(target="lxc104", argv=["hostname"])
    assert err["ok"] is False
    assert "/root/" not in str(err)


def test_target_tools_visibility_and_scope_never_safe_default():
    from examples.mcp_server.tool_modes import should_register_tool
    from examples.mcp_server.tool_scopes import get_required_scopes

    assert should_register_tool("list_ssh_targets", "mcp_client") is True
    assert should_register_tool("execute_target_argv", "mcp_client") is False
    assert should_register_tool("execute_target_argv", "mcp_client_write") is True
    assert get_required_scopes("execute_target_argv") == ["mcp:execute", "mcp:admin"]
