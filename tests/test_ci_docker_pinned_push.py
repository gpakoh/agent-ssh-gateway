"""Guard immutable release publication against disappearing or swapped Docker tags."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-docker-push-pinned.sh"
IMAGE_ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64
RELEASE_SHA = "c" * 40
REPOSITORY = "registry.example:3005/gpakoh/ssh-gateway-sshd"
SHA_TAG = f"{REPOSITORY}:{RELEASE_SHA}"
LATEST_TAG = f"{REPOSITORY}:latest"


def _run(
    tmp_path: Path,
    *,
    tags: dict[str, str] | None = None,
    image_present: bool = True,
    failed_push: str = "",
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[list[str]]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "docker"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_DOCKER_STATE"])
log_path = Path(os.environ["FAKE_DOCKER_LOG"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
if args[:3] == ["image", "inspect", "--format"]:
    ref = args[4] if len(args) == 5 else ""
    resolved = state["tags"].get(ref)
    if ref == os.environ["FAKE_IMAGE_ID"] and os.environ["FAKE_IMAGE_PRESENT"] == "1":
        resolved = ref
    if resolved is None:
        print("image not found", file=sys.stderr)
        sys.exit(1)
    print(resolved)
    sys.exit(0)
if args[:1] == ["tag"] and len(args) == 3:
    src, dst = args[1:]
    resolved = state["tags"].get(src)
    if src == os.environ["FAKE_IMAGE_ID"] and os.environ["FAKE_IMAGE_PRESENT"] == "1":
        resolved = src
    if resolved is None:
        sys.exit(1)
    state["tags"][dst] = resolved
    event = ["tag", dst, resolved]
elif args[:1] == ["push"] and len(args) == 2:
    ref = args[1]
    resolved = state["tags"].get(ref)
    if resolved is None:
        print("tag does not exist", file=sys.stderr)
        sys.exit(1)
    event = ["push", ref, resolved]
    if os.environ.get("FAKE_FAIL_PUSH_TAG") == ref:
        print("denied: non-transient", file=sys.stderr)
        sys.exit(1)
else:
    print(f"unsupported fake Docker argv: {args!r}", file=sys.stderr)
    sys.exit(9)
with log_path.open("a") as log:
    log.write(json.dumps(event) + "\\n")
state_path.write_text(json.dumps(state))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"tags": tags or {}}), encoding="utf-8")
    log = tmp_path / "docker.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "FAKE_IMAGE_PRESENT": "1" if image_present else "0",
            "FAKE_FAIL_PUSH_TAG": failed_push,
            "CI_DOCKER_PUSH_RETRY_DELAY_SECONDS": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), *(args if args is not None else [IMAGE_ID, REPOSITORY, RELEASE_SHA])],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    after = json.loads(state.read_text(encoding="utf-8"))["tags"]
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return result, after, events


def test_missing_sha_tag_is_restored_only_from_recorded_image_id(tmp_path: Path) -> None:
    result, tags, events = _run(tmp_path, tags={LATEST_TAG: OTHER_ID})
    assert result.returncode == 0, result.stderr
    assert tags == {SHA_TAG: IMAGE_ID, LATEST_TAG: IMAGE_ID}
    assert events == [
        ["tag", SHA_TAG, IMAGE_ID],
        ["push", SHA_TAG, IMAGE_ID],
        ["tag", LATEST_TAG, IMAGE_ID],
        ["push", LATEST_TAG, IMAGE_ID],
    ]


def test_existing_wrong_sha_tag_fails_closed_without_mutation(tmp_path: Path) -> None:
    result, tags, events = _run(tmp_path, tags={SHA_TAG: OTHER_ID})
    assert result.returncode != 0
    assert "different image" in result.stderr
    assert tags == {SHA_TAG: OTHER_ID}
    assert events == []


def test_missing_immutable_image_id_does_not_fall_back_to_latest(tmp_path: Path) -> None:
    result, tags, events = _run(
        tmp_path, tags={LATEST_TAG: OTHER_ID}, image_present=False
    )
    assert result.returncode != 0
    assert "no longer exists" in result.stderr
    assert tags == {LATEST_TAG: OTHER_ID}
    assert events == []


def test_existing_correct_sha_tag_is_not_overwritten(tmp_path: Path) -> None:
    result, tags, events = _run(tmp_path, tags={SHA_TAG: IMAGE_ID})
    assert result.returncode == 0, result.stderr
    assert events == [
        ["push", SHA_TAG, IMAGE_ID],
        ["tag", LATEST_TAG, IMAGE_ID],
        ["push", LATEST_TAG, IMAGE_ID],
    ]
    assert tags[SHA_TAG] == tags[LATEST_TAG] == IMAGE_ID


def test_failed_sha_push_does_not_mutate_or_publish_latest(tmp_path: Path) -> None:
    result, tags, events = _run(
        tmp_path, tags={LATEST_TAG: OTHER_ID}, failed_push=SHA_TAG
    )
    assert result.returncode != 0
    assert tags[LATEST_TAG] == OTHER_ID
    assert events == [["tag", SHA_TAG, IMAGE_ID]]
    assert "non-transient" in result.stderr


def test_invalid_inputs_fail_before_calling_docker(tmp_path: Path) -> None:
    for index, args in enumerate(
        (
            ["bad", REPOSITORY, RELEASE_SHA],
            [IMAGE_ID, REPOSITORY, "not-a-sha"],
            [IMAGE_ID, "invalid-repository", RELEASE_SHA],
        )
    ):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        result, _, events = _run(case, args=args)
        assert result.returncode == 64
        assert events == []


def test_all_ci_release_images_use_pinned_push_after_smoke() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count("bash scripts/ci-docker-push-pinned.sh") == 3
    for image_id, repository in (
        ("CI_SMOKE_GW_IMAGE_ID", "agent-ssh-gateway"),
        ("CI_SMOKE_MCP_IMAGE_ID", "mcp-server"),
        ("CI_SMOKE_SSHD_IMAGE_ID", "ssh-gateway-sshd"),
    ):
        assert (
            f'bash scripts/ci-docker-push-pinned.sh "${image_id}" '
            f'"$REGISTRY/gpakoh/{repository}" "${{{{ github.sha }}}}"'
        ) in workflow
    assert "bash scripts/ci-docker-push-retry.sh \"$REGISTRY/gpakoh/" not in workflow
