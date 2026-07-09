from examples.mcp_server.docker_confirm import ConfirmStatus, ConfirmStore


class TestConfirmStore:
    def test_create_action_returns_action(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        assert action.tool == "docker_rm"
        assert action.kwargs == {"container": "foo"}
        assert action.summary == "Remove container foo"
        assert action.risk == "high"
        assert action.consumed is False
        assert action.action_id is not None
        assert action.confirm_token is not None

    def test_confirm_valid_token(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        result, status = store.confirm_action(action.confirm_token)
        assert result is not None
        assert result.action_id == action.action_id
        assert status == ConfirmStatus.OK

    def test_confirm_consumes_token(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        store.confirm_action(action.confirm_token)
        result, status = store.confirm_action(action.confirm_token)
        assert result is None
        assert status == ConfirmStatus.CONSUMED

    def test_confirm_invalid_token(self):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        result, status = store.confirm_action("invalid-token")
        assert result is None
        assert status == ConfirmStatus.INVALID

    def test_confirm_expired_token(self, monkeypatch):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        monkeypatch.setattr("time.monotonic", lambda: 999999.0)
        result, status = store.confirm_action(action.confirm_token)
        assert result is None
        assert status == ConfirmStatus.EXPIRED

    def test_list_pending_masks_token(self):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["confirm_token"].endswith("...")
        assert len(pending[0]["confirm_token"]) > 6

    def test_list_pending_excludes_consumed(self):
        store = ConfirmStore()
        a1 = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        store.create_action("docker_prune", {"type": "container"}, "Prune containers")
        store.confirm_action(a1.confirm_token)
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool"] == "docker_prune"

    def test_cleanup_expired(self, monkeypatch):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        monkeypatch.setattr("time.monotonic", lambda: 999999.0)
        removed = store.cleanup_expired()
        assert removed == 1
        assert len(store.list_pending()) == 0

    def test_create_action_unique_tokens(self):
        store = ConfirmStore()
        a1 = store.create_action("docker_rm", {}, "a")
        a2 = store.create_action("docker_rm", {}, "b")
        assert a1.confirm_token != a2.confirm_token
        assert a1.action_id != a2.action_id

    def test_confirm_timing_attack_protection(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {}, "test")
        result, status = store.confirm_action(action.confirm_token.upper())
        assert result is None
        assert status == ConfirmStatus.INVALID


class TestConfirmStoreAdminActions:
    def test_create_exec_action(self):
        store = ConfirmStore()
        action = store.create_action(
            "docker_exec",
            {"container": "web", "command": ["ls", "-la"], "timeout": 30},
            "Exec in web: ls -la",
        )
        assert action.tool == "docker_exec"
        assert action.kwargs["container"] == "web"
        assert action.kwargs["command"] == ["ls", "-la"]

    def test_create_run_action(self):
        store = ConfirmStore()
        action = store.create_action(
            "docker_run",
            {"image": "alpine:3.20", "command": ["whoami"], "timeout": 60},
            "Run alpine:3.20: whoami",
        )
        assert action.tool == "docker_run"

    def test_create_rmi_action(self):
        store = ConfirmStore()
        action = store.create_action(
            "docker_rmi",
            {"images": ["alpine:3.20"]},
            "Remove image(s): alpine:3.20",
        )
        assert action.tool == "docker_rmi"
        assert action.kwargs["images"] == ["alpine:3.20"]

    def test_create_volume_rm_action(self):
        store = ConfirmStore()
        action = store.create_action(
            "docker_volume_rm",
            {"volumes": ["pgdata"]},
            "Remove volume(s): pgdata",
        )
        assert action.tool == "docker_volume_rm"
        assert action.kwargs["volumes"] == ["pgdata"]

    def test_confirm_exec_action_and_dispatch(self):
        """Simulate the full confirmation flow for docker_exec."""
        store = ConfirmStore()
        action = store.create_action(
            "docker_exec",
            {"container": "web", "command": ["ls"], "timeout": 30},
            "Exec in web: ls",
        )
        result, status = store.confirm_action(action.confirm_token)
        assert status == ConfirmStatus.OK
        assert result is not None
        assert result.tool == "docker_exec"
        assert result.kwargs["container"] == "web"
