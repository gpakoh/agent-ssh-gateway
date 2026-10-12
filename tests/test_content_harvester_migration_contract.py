from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from examples.mcp_client_remote.fleet.docker_client import (  # noqa: E402
    DockerClient,
    RunResult,
)
from examples.mcp_server.mcp_infra.adapters import docker as docker_adapter  # noqa: E402

HEAD = "c" * 40
NAMESPACE = "192.168.1.103:3005/gpakoh"
TARGET_IMAGE = f"{NAMESPACE}/content-harvester-service:{HEAD}"
TARGET_IMAGE_ID = "sha256:" + "1" * 64
RUNTIME_NETWORK = "infra-quart_infra-net"
DATABASE_URL = "postgresql://harvester:super-secret@postgres/harvester"
DB_FINGERPRINT = "sha256:" + hashlib.sha256(DATABASE_URL.encode()).hexdigest()
MIGRATION_FINGERPRINT = "sha256:" + "2" * 64
NETWORK_FINGERPRINT = "sha256:" + "3" * 64
CANDIDATE = "candidate-content-harvester-migration"


@pytest.fixture(autouse=True)
def _clear_confirm_store():
    import examples.mcp_server.server as srv

    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _report(status: str, pending: list[str] | None = None) -> RunResult:
    pending = pending or []
    payload = {
        "status": status,
        "applied_versions": [] if status == "pending" else ["001", "002"],
        "pending_versions": pending,
        "total_migrations": 2,
    }
    return RunResult(json.dumps(payload), "", 3 if status == "pending" else 0)


def _plan() -> dict:
    return {
        "candidate_root": "/candidate/content-harvester",
        "env_file": "/operator/content-harvester/.env",
        "source_revision": HEAD,
        "image_namespace": NAMESPACE,
        "target_image": TARGET_IMAGE,
        "target_image_id": TARGET_IMAGE_ID,
        "runtime_network": RUNTIME_NETWORK,
        "runtime_network_fingerprint": NETWORK_FINGERPRINT,
        "database_url": DATABASE_URL,
        "database_url_fingerprint": DB_FINGERPRINT,
        "migration_contract_fingerprint": MIGRATION_FINGERPRINT,
    }


class FakeMigrationClient:
    def __init__(self, runs: list[RunResult]) -> None:
        self.runs = list(runs)
        self.calls: list[dict] = []

    def _sanitize_string(self, value: str) -> str:
        return value.replace("super-secret", "<redacted>")

    async def run_content_harvester_migration(self, **kwargs):
        self.calls.append(kwargs)
        return self.runs.pop(0)


@pytest.mark.asyncio
async def test_pending_preflight_creates_confirmation_without_database_secret(monkeypatch):
    client = FakeMigrationClient([_report("pending", ["001", "002"])])
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter.docker_migrate_content_harvester(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )

    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action is not None
    assert action.tool == "docker_migrate_content_harvester"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["database_url_fingerprint"] == DB_FINGERPRINT
    assert action.kwargs["migration_contract_fingerprint"] == MIGRATION_FINGERPRINT
    assert action.kwargs["pending_versions"] == ["001", "002"]
    serialized = json.dumps(action.kwargs, sort_keys=True)
    assert DATABASE_URL not in serialized
    assert "super-secret" not in serialized


@pytest.mark.asyncio
async def test_current_preflight_is_read_only_noop(monkeypatch):
    client = FakeMigrationClient([_report("current")])
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter.docker_migrate_content_harvester(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "current"
    assert result["result"]["mutation_required"] is False
    assert [call["mode"] for call in client.calls] == ["check"]
    import examples.mcp_server.server as srv

    assert srv._confirm_store.list_pending() == []


@pytest.mark.asyncio
async def test_unknown_apply_is_accepted_only_after_current_check(monkeypatch):
    client = FakeMigrationClient(
        [
            _report("pending", ["001", "002"]),
            RunResult("", "outer timeout", -1),
            _report("current"),
        ]
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_migrate_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        target_image_id=TARGET_IMAGE_ID,
        runtime_network_fingerprint=NETWORK_FINGERPRINT,
        database_url_fingerprint=DB_FINGERPRINT,
        migration_contract_fingerprint=MIGRATION_FINGERPRINT,
        pending_versions=["001", "002"],
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "current"
    assert result["result"]["reconciled_after_unknown"] is True
    assert [call["mode"] for call in client.calls] == ["check", "apply", "check"]


@pytest.mark.asyncio
async def test_unknown_apply_with_proven_pending_requires_fresh_preflight(monkeypatch):
    client = FakeMigrationClient(
        [
            _report("pending", ["001", "002"]),
            RunResult("", "outer timeout", -1),
            _report("pending", ["002"]),
        ]
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_migrate_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        target_image_id=TARGET_IMAGE_ID,
        runtime_network_fingerprint=NETWORK_FINGERPRINT,
        database_url_fingerprint=DB_FINGERPRINT,
        migration_contract_fingerprint=MIGRATION_FINGERPRINT,
        pending_versions=["001", "002"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MIGRATION_INCOMPLETE_RECONCILED"
    assert result["error"]["retryable"] is True
    assert result["meta"]["redacted"] is True
    assert result["result"]["report"]["pending_versions"] == ["002"]


@pytest.mark.asyncio
async def test_unknown_apply_without_trusted_check_stays_nonretryable(monkeypatch):
    client = FakeMigrationClient(
        [
            _report("pending", ["001", "002"]),
            RunResult("", "outer timeout", -1),
            RunResult("", "migration_error: another operation is active", 2),
        ]
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_migrate_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        target_image_id=TARGET_IMAGE_ID,
        runtime_network_fingerprint=NETWORK_FINGERPRINT,
        database_url_fingerprint=DB_FINGERPRINT,
        migration_contract_fingerprint=MIGRATION_FINGERPRINT,
        pending_versions=["001", "002"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MIGRATION_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert result["meta"]["redacted"] is True


@pytest.mark.asyncio
async def test_confirmation_refuses_changed_pending_set_before_apply(monkeypatch):
    client = FakeMigrationClient([_report("pending", ["002"])])
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_migration",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_migrate_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        target_image_id=TARGET_IMAGE_ID,
        runtime_network_fingerprint=NETWORK_FINGERPRINT,
        database_url_fingerprint=DB_FINGERPRINT,
        migration_contract_fingerprint=MIGRATION_FINGERPRINT,
        pending_versions=["001", "002"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MIGRATION_PRECONDITION_FAILED"
    assert [call["mode"] for call in client.calls] == ["check"]


def test_operator_database_url_parser_rejects_duplicates_and_interpolation(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HARVESTER_DATABASE_URL=postgresql://one/db\n"
        "HARVESTER_DATABASE_URL=postgresql://two/db\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly once"):
        docker_adapter._content_harvester_database_url(env_file)

    env_file.write_text("HARVESTER_DATABASE_URL=${DATABASE_SECRET}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="interpolation"):
        docker_adapter._content_harvester_database_url(env_file)


def test_migration_report_rejects_inconsistent_exit_and_payload():
    bad = RunResult(
        json.dumps(
            {
                "status": "current",
                "applied_versions": ["001"],
                "pending_versions": ["002"],
                "total_migrations": 2,
            }
        ),
        "",
        0,
    )
    assert docker_adapter._content_harvester_migration_report(bad) is None


@pytest.mark.asyncio
async def test_docker_client_passes_only_database_key_to_container_argv(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult("{}", "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)
    monkeypatch.setenv("HARVESTER_TELEGRAM_API_HASH", "must-not-be-forwarded")

    await client.run_content_harvester_migration(
        image=TARGET_IMAGE,
        runtime_network=RUNTIME_NETWORK,
        database_url=DATABASE_URL,
        mode="check",
    )

    argv = run.await_args.args[0]
    process_env = run.await_args.kwargs["env"]
    joined = " ".join(argv)
    assert DATABASE_URL not in joined
    assert "super-secret" not in joined
    assert "HARVESTER_TELEGRAM_API_HASH" not in joined
    assert argv[argv.index("--env") + 1] == "HARVESTER_DATABASE_URL"
    assert process_env["HARVESTER_DATABASE_URL"] == DATABASE_URL
    assert "content-harvester-migrate" in argv
    assert argv[-1] == "check"


class _PrepareMigrationClient:
    def _validate_project_dir(self, path: str) -> None:
        assert path

    async def image_id(self, image: str) -> str:
        assert image == TARGET_IMAGE
        return TARGET_IMAGE_ID

    async def image_revision(self, image: str) -> str:
        assert image == TARGET_IMAGE_ID
        return HEAD

    async def network_metadata(self, name: str) -> dict:
        assert name == RUNTIME_NETWORK
        return {
            "Name": RUNTIME_NETWORK,
            "Id": "n" * 64,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "Options": {},
            "Labels": {},
        }


@pytest.mark.asyncio
async def test_prepare_pins_exact_source_image_database_and_network(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    operator = tmp_path / "operator"
    candidate.mkdir()
    operator.mkdir()
    (operator / ".env").write_text(f"HARVESTER_DATABASE_URL='{DATABASE_URL}'\n", encoding="utf-8")

    registry = MagicMock()
    registry.project_info.side_effect = lambda name: (
        {"root": str(candidate), "type": "candidate-clone"}
        if name == CANDIDATE
        else {"root": str(operator), "type": "canonical"}
    )
    monkeypatch.setattr(
        docker_adapter,
        "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry" else None,
    )
    monkeypatch.setattr(
        docker_adapter,
        "_read_candidate_metadata",
        lambda _root: {
            "project_id": CANDIDATE,
            "source_project": docker_adapter._CONTENT_HARVESTER_SOURCE_PROJECT,
            "head": HEAD,
            "base_sha": HEAD,
        },
    )

    blobs = {
        f"{HEAD}:app/schema_migrations.py": "a" * 40,
        f"{HEAD}:migrations": "b" * 40,
        f"{HEAD}:pyproject.toml": "d" * 40,
    }

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return HEAD
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args[:1] == ("rev-parse",) and len(args) == 2:
            return blobs[args[1]]
        raise AssertionError(args)

    monkeypatch.setattr(docker_adapter, "_git_capture", fake_git)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: _PrepareMigrationClient())

    plan = await docker_adapter._prepare_content_harvester_migration(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        runtime_network=RUNTIME_NETWORK,
    )

    assert plan["target_image"] == TARGET_IMAGE
    assert plan["target_image_id"] == TARGET_IMAGE_ID
    assert plan["database_url"] == DATABASE_URL
    assert plan["database_url_fingerprint"] == DB_FINGERPRINT
    assert plan["migration_contract_fingerprint"].startswith("sha256:")
    assert plan["runtime_network_fingerprint"].startswith("sha256:")
