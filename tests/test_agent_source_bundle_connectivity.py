"""Behavioral regression for the trusted source-bundle connectivity gate.

The trusted agent source-bundle producer (scripts/deploy-from-registry.sh::
publish_agent_source_bundle) used to validate a produced bundle ONLY with
`git bundle list-heads` -- which proves the advertised HEAD object exists but
tells us nothing about whether the commit graph an agent will clone is
connected. A shallow deploy checkout (actions/checkout without fetch-depth: 0)
can still bundle a tip whose merge parents are missing; the resulting bundle
passes list-heads but fails `git fsck --connectivity-only` on clones. That is
the real-world failure signature these tests reproduce.

These tests drive the ACTUAL verification function shipped in the deploy
script (verify_source_bundle_connected) against real Git object graphs:

  1. Construct a genuine merge history, shallow-clone it, bundle the shallow
     clone's tip -- the old failure signature (advertised HEAD present,
     broken graph) -- and prove the corrected publication logic REJECTS it.
  2. Bundle a complete merge history with the same `HEAD` ref shape used by
     production and prove the corrected logic ACCEPTS it and the reconstructed
     bundle passes `git fsck --connectivity-only`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-from-registry.sh"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _build_merge_history_repo(path: Path) -> str:
    """Create a repo whose tip is a real merge commit with two parents."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Connectivity Tests")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "checkout", "-qb", "feature")
    (path / "b.txt").write_text("b\n", encoding="utf-8")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-q", "-m", "feature commit")
    feature_head = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "master")
    (path / "c.txt").write_text("c\n", encoding="utf-8")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "master commit")
    master_head = _git(path, "rev-parse", "HEAD")
    _git(path, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge_head = _git(path, "rev-parse", "HEAD")
    assert merge_head != master_head and merge_head != feature_head
    return merge_head


def _shallow_clone(source: Path, destination: Path, depth: int = 1) -> None:
    subprocess.run(
        ["git", "clone", "-q", "--depth", str(depth), f"file://{source}", str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


def _bundle(repo: Path, bundle_path: Path, ref: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "bundle", "create", str(bundle_path), ref],
        check=True,
        capture_output=True,
        text=True,
    )


def _bundle_advertised_head(bundle_path: Path) -> str:
    lines = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return lines[0]


def _verify_with_deploy_logic(bundle_path: Path, expected_head: str) -> int:
    """Run the deploy script's own connectivity verifier against a bundle."""
    harness = f"""
set -euo pipefail
SCRIPT={DEPLOY_SCRIPT.as_posix()!r}
source <(awk '/^verify_source_bundle_connected\\(\\)/{{flag=1}} flag{{print}} /^}}/{{if(flag){{flag=0}}}}' "$SCRIPT")
log() {{ : ; }}
verify_source_bundle_connected {_sh(bundle_path)} {_sh(expected_head)}
"""
    proc = subprocess.run(
        ["bash", "-c", harness],
        text=True,
        capture_output=True,
    )
    return proc.returncode


def _sh(value: object) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def test_old_shallow_list_heads_only_signature_is_insufficient(tmp_path):
    """A shallow merge-tip bundle passes the old list-heads acceptance signal."""
    origin = tmp_path / "origin"
    merge_head = _build_merge_history_repo(origin)

    shallow = tmp_path / "shallow"
    _shallow_clone(origin, shallow, depth=1)
    assert _git(shallow, "rev-parse", "HEAD") == merge_head

    shallow_bundle = tmp_path / "shallow.bundle"
    _bundle(shallow, shallow_bundle, "HEAD")
    advertised = _bundle_advertised_head(shallow_bundle)
    assert advertised == merge_head, (
        "shallow bundle must still advertise the tip (the old list-heads gate "
        "would have accepted it)"
    )

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(shallow_bundle), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    fsck = subprocess.run(
        ["git", "-C", str(clone), "fsck", "--connectivity-only"],
        capture_output=True,
        text=True,
    )
    assert fsck.returncode != 0, (
        "the shallow bundle's clone must fail git fsck --connectivity-only; "
        "if it passes, this regression no longer reproduces the real defect"
    )
    output = fsck.stdout + fsck.stderr
    assert "missing commit" in output or "broken link" in output


def test_publisher_rejects_shallow_incomplete_bundle(tmp_path):
    """The corrected publication logic rejects the old false-pass bundle."""
    origin = tmp_path / "origin2"
    merge_head = _build_merge_history_repo(origin)

    shallow = tmp_path / "shallow2"
    _shallow_clone(origin, shallow, depth=1)
    shallow_bundle = tmp_path / "shallow2.bundle"
    _bundle(shallow, shallow_bundle, "HEAD")
    assert _bundle_advertised_head(shallow_bundle) == merge_head

    assert _verify_with_deploy_logic(shallow_bundle, merge_head) != 0


def test_complete_merge_bundle_reconstructs_and_passes_connectivity(tmp_path):
    """A complete production-shaped HEAD bundle reconstructs and is accepted."""
    origin = tmp_path / "origin3"
    merge_head = _build_merge_history_repo(origin)

    complete_bundle = tmp_path / "complete.bundle"
    _bundle(origin, complete_bundle, "HEAD")
    assert _bundle_advertised_head(complete_bundle) == merge_head

    assert _verify_with_deploy_logic(complete_bundle, merge_head) == 0


def test_verify_rejects_wrong_advertised_head(tmp_path):
    """A connected bundle for a different SHA is still rejected."""
    origin = tmp_path / "origin4"
    _build_merge_history_repo(origin)
    complete_bundle = tmp_path / "complete4.bundle"
    _bundle(origin, complete_bundle, "HEAD")

    assert _verify_with_deploy_logic(complete_bundle, "0" * 40) != 0
