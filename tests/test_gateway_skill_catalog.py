"""Bounded discovery, exact instruction loading, and task-isolated worker context."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.skill_pack import PACK_ROOT, SKILLS, SkillPackError, read_skill, skill_metadata, snapshot
from app.worker_skills import worker_skill_script_lines
from examples.mcp_server.gateway_catalog import GROUPS, build_catalog, group_for


def tools():
    return [
        SimpleNamespace(
            name=name, description=description, parameters={"type": "object", "properties": {}}
        )
        for name, description in [
            ("health", "Check Gateway readiness.\nA long detail must not enter summary."),
            ("gitea_get_action_run", "Read one Gitea Actions run."),
            ("gitea_dispatch_workflow", "Dispatch an approved workflow."),
            ("docker_exec", "Execute a guarded Docker command."),
            ("run_pytest", "Run project tests."),
            ("unclassified_future_tool", "Future capability."),
        ]
    ]


def test_overview_is_small_and_has_skills_without_instruction_bodies():
    result = build_catalog(tools())
    assert result["tools"] == []
    assert len(result["skills"]) == 4
    assert sum(g["tool_count"] for g in result["groups"]) == len(tools())
    assert "content" not in json.dumps(result)
    assert "input_schema" not in json.dumps(result)
    assert len(json.dumps(result, ensure_ascii=False)) < 10_000
    assert any(g["id"] == "sites" and g["skill_count"] == 2 for g in result["groups"])


def test_group_contains_only_live_registered_tools_and_real_availability():
    result = build_catalog(
        tools(),
        group="repositories",
        unavailable={"gitea_dispatch_workflow": "dependency unavailable"},
    )
    assert {r["name"] for r in result["tools"]} == {
        "gitea_get_action_run",
        "gitea_dispatch_workflow",
    }
    assert (
        next(r for r in result["tools"] if r["name"] == "gitea_dispatch_workflow")["available"]
        is False
    )
    assert "gitea_merge_pull_request" not in json.dumps(result)


def test_query_finds_skills_and_tools_without_expanding_bodies():
    assert {s["name"] for s in build_catalog(tools(), query="видео")["skills"]} == {
        "site-scroll-video"
    }
    assert [r["name"] for r in build_catalog(tools(), query="guarded")["tools"]] == ["docker_exec"]
    result = build_catalog(tools(), group="discovery")
    assert result["tools"][0]["summary"] == "Check Gateway readiness."


def test_schema_requires_one_exact_name_and_is_never_an_executor():
    result = build_catalog(tools(), tool_name="docker_exec", include_schema=True)
    assert result["tools"][0]["input_schema"]["type"] == "object"
    assert result["skills"] == []
    with pytest.raises(ValueError, match="exact tool_name"):
        build_catalog(tools(), include_schema=True)
    assert build_catalog(tools(), tool_name="absent")["tools"] == []


def test_pagination_is_complete_and_deterministic():
    first = build_catalog(tools(), query="", group="repositories", limit=1)
    second = build_catalog(tools(), group="repositories", offset=first["next_offset"], limit=1)
    assert first["tools"][0]["name"] != second["tools"][0]["name"]
    assert second["next_offset"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"group": "missing"},
        {"limit": True},
        {"limit": 0},
        {"limit": 51},
        {"offset": False},
        {"offset": -1},
        {"offset": 10001},
        {"query": "x" * 101},
        {"role": "admin"},
    ],
)
def test_invalid_catalog_inputs_fail_early(changes):
    with pytest.raises(ValueError):
        build_catalog(tools(), **changes)


def test_every_tool_has_one_group_including_unknown_future_tools():
    for tool in tools():
        assert group_for(tool.name) in GROUPS
    assert group_for("unclassified_future_tool") == "other"


def test_role_aware_metadata_and_reading_do_not_grant_worker_controller_actions():
    pack = snapshot()
    assert len(skill_metadata(pack)) == 4
    assert len(skill_metadata(pack, "worker")) == 3
    with pytest.raises(ValueError, match="selected role"):
        read_skill("gateway-finish-task", role="worker")
    skill = read_skill(
        "gateway-code-quality", role="worker", expected_pack_sha256=pack["pack_sha256"]
    )
    assert "не выполнять commit/push/merge/release" in skill["content"]
    assert skill["pack_sha256"] == pack["pack_sha256"]
    assert skill["sha256"]


def test_read_instruction_and_advertised_reference_only():
    assert "Приёмка" in read_skill("site-scroll-video", "references/implementation.md")["content"]
    for resource in (
        "../SKILL.md",
        "/etc/passwd",
        "references/../../secret",
        "https://example.com",
        "missing.md",
    ):
        with pytest.raises(ValueError):
            read_skill("site-design", resource)
    with pytest.raises(ValueError):
        read_skill("../site-design")
    with pytest.raises(ValueError, match="changed"):
        read_skill("site-design", expected_pack_sha256="0" * 64)


@pytest.fixture
def pack_copy(tmp_path):
    root = tmp_path / "skills"
    shutil.copytree(PACK_ROOT, root)
    return root


def test_pack_hash_changes_with_reference_and_rejects_stale_discovery(pack_copy):
    before = snapshot(pack_copy)["pack_sha256"]
    p = pack_copy / "site-design/references/review.md"
    p.write_text(p.read_text() + "\nExtra accepted check\n")
    assert snapshot(pack_copy)["pack_sha256"] != before
    with pytest.raises(ValueError, match="changed"):
        read_skill("site-design", root=pack_copy, expected_pack_sha256=before)


@pytest.mark.parametrize("damage", ["symlink", "oversize", "missing", "wrong-name", "bad-yaml"])
def test_invalid_release_pack_fails_closed(pack_copy, tmp_path, damage):
    p = pack_copy / "site-design/SKILL.md"
    if damage == "symlink":
        p.unlink()
        outside = tmp_path / "private"
        outside.write_text("PRIVATE_SENTINEL")
        p.symlink_to(outside)
    elif damage == "oversize":
        p.write_text("x" * 40_000)
    elif damage == "missing":
        p.unlink()
    elif damage == "wrong-name":
        p.write_text(p.read_text().replace("name: site-design", "name: wrong-name"))
    else:
        p.write_text("---\nname: [\n---\n")
    with pytest.raises(SkillPackError) as caught:
        snapshot(pack_copy)
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def materialize(td, config=None):
    env = dict(os.environ)
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    if config is not None:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    script = (
        'td="$1"\n'
        + "\n".join(worker_skill_script_lines())
        + '\nprintf "%s" "$OPENCODE_CONFIG_CONTENT"\n'
    )
    return subprocess.run(
        ["sh", "-c", script, "skill-test", str(td)], env=env, capture_output=True, text=True
    )


def test_worker_context_is_task_local_and_preserves_existing_configuration(tmp_path):
    first = tmp_path / "task-one"
    second = tmp_path / "task-two"
    first.mkdir()
    second.mkdir()
    original = {
        "model": "provider/model",
        "permission": {"skill": {"*": "deny"}},
        "skills": {"paths": ["/existing"], "urls": ["https://example.invalid/skills"]},
    }
    a = materialize(first, original)
    b = materialize(second)
    assert a.returncode == b.returncode == 0, a.stderr + b.stderr
    resolved = json.loads(a.stdout)
    assert resolved["permission"] == original["permission"]
    assert resolved["model"] == original["model"]
    assert resolved["skills"]["urls"] == original["skills"]["urls"]
    assert resolved["skills"]["paths"][0] == "/existing"
    assert resolved["skills"]["paths"][-1] != json.loads(b.stdout)["skills"]["paths"][-1]
    receipt = json.loads((first / "skills-receipt.json").read_text())
    assert receipt["files_available"] is True and receipt["native_loader_verified"] is False
    assert "gateway-finish-task" not in receipt["skills"]
    assert len(receipt["skills"]) == 3
    folder = Path(resolved["skills"]["paths"][-1])
    assert len(list(folder.glob("*/SKILL.md"))) == 3
    assert not list(first.glob(".opencode*"))
    again = materialize(first, original)
    assert again.returncode == 0


def test_worker_context_rejects_tampered_files_and_symlink_directory(tmp_path):
    assert materialize(tmp_path).returncode == 0
    p = next(tmp_path.glob("skill-context/*/skills/site-design/SKILL.md"))
    p.write_text("unreviewed instructions")
    assert materialize(tmp_path).returncode == 73
    shutil.rmtree(tmp_path / "skill-context")
    (tmp_path / "skill-context").symlink_to(tmp_path.parent)
    assert materialize(tmp_path).returncode == 73


def test_existing_array_skills_configuration_is_preserved(tmp_path):
    result = materialize(tmp_path, {"skills": ["/first", "https://example.invalid/list"]})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["skills"][:2] == ["/first", "https://example.invalid/list"]


def test_pack_matches_explicit_resource_allowlist():
    pack = snapshot()
    assert set(pack["files"]) == {f"{n}/{r}" for n, m in SKILLS.items() for r in m["resources"]}


def test_worker_transport_is_bounded_and_not_an_all_skills_prompt():
    lines = worker_skill_script_lines()
    assert len("\n".join(lines).encode()) < 65536
    assert "name: gateway-finish-task" not in "\n".join(lines)
