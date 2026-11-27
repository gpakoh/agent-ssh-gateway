"""Run the CI sidecar lifecycle against a stateful fake Docker CLI."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci-selenium-sidecar.sh"
OLD = "a" * 64
NEW = "b" * 64
IMAGE = "selenium@sha256:" + "c" * 64

FAKE = r'''import json
import os
import sys
import time
from pathlib import Path

try:
    os.fstat(9)
except OSError:
    pass
else:
    raise AssertionError('sidecar lock descriptor leaked into Docker CLI')
state_path = Path(os.environ['FAKE_STATE'])
s = json.loads(state_path.read_text())
a = sys.argv[1:]
s['calls'].append(a)
state_path.write_text(json.dumps(s))
if a[0] == 'info':
    print(s['engine'])
elif a[0] == 'inspect':
    pass
elif a[:2] == ['rm', '-f']:
    assert a[2] in ('a'*64, 'b'*64)
    receipt = Path(os.environ['FAKE_RECEIPT']).read_text().strip().split('|')
    assert receipt[0] == 'removing' and receipt[2] == a[2]
    if s['remove_effect']:
        s['containers'].remove(a[2])
    if s.get('drift_after_rm'):
        s['engine'] = 'different-engine'
    state_path.write_text(json.dumps(s))
    sys.exit(s['rm_rc'])
elif a[:2] == ['container', 'ls']:
    assert '--no-trunc' in a and '-a' in a
    if s['list_rc']:
        sys.exit(s['list_rc'])
    if s.get('list_output') is not None:
        print(s['list_output'])
    else:
        target = a[a.index('--filter')+1].removeprefix('id=')
        if target in s['containers']:
            print(target)
elif a[0] == 'run':
    name = a[a.index('--name')+1]
    assert Path(os.environ['FAKE_RECEIPT']).read_text().strip().split('|') == ['starting', s['engine'], '-', name]
    assert '--memory=3g' in a and '--cpus=2' in a and '--shm-size=2g' in a
    assert not s['containers']
    if s['run_effect']:
        s['containers'].append('b'*64)
    state_path.write_text(json.dumps(s))
    if s.get('run_hangs'):
        time.sleep(2)
    if s['run_rc']:
        sys.exit(s['run_rc'])
    print(s.get('run_output', 'b'*64))
else:
    raise AssertionError(a)
'''


class Fixture:
    def __init__(self, path: Path, *, existing: bool = True):
        self.path = path
        self.state = path / "state.json"
        self.prefix = f"ci-selenium-123-2-{socket.gethostname()}"
        self.receipt = path / self.prefix / "state"
        if existing:
            self.receipt.parent.mkdir(mode=0o700)
            self.receipt.write_text(f"running|engine-1|{OLD}|{self.prefix}-initial\n")
        bin_dir = path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f"#!{sys.executable} -S\n" + FAKE)
        docker.chmod(0o755)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("E2E_SELENIUM_")
        }
        self.env.update(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            TMPDIR=str(path), FAKE_STATE=str(self.state), FAKE_RECEIPT=str(self.receipt),
            GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
        )
        if existing:
            self.env.update(E2E_SELENIUM_CONTAINER=OLD)
        self.state.write_text(json.dumps({
            "engine": "engine-1", "containers": [OLD] if existing else [],
            "calls": [], "remove_effect": True, "rm_rc": 0,
            "list_rc": 0, "run_effect": True, "run_rc": 0,
        }))

    def read(self):
        return json.loads(self.state.read_text())

    def change(self, **changes):
        self.state.write_text(json.dumps(self.read() | changes))

    def step(self, mode):
        result = subprocess.run(
            ["bash", str(HELPER), mode, IMAGE], env=self.env,
            capture_output=True, text=True, timeout=15, check=False,
        )
        # The runner's environment may not be updated after a failed step.
        # The private receipt, not the environment, is authoritative.
        if result.returncode == 0 and mode in {"start", "replace"}:
            self.env["E2E_SELENIUM_CONTAINER"] = result.stdout.strip()
        return result

    def count(self, command):
        return sum(call[:len(command)] == command for call in self.read()["calls"])


@pytest.mark.parametrize("rm_rc", [0, 124])
def test_replace_only_after_absence(tmp_path, rm_rc):
    f = Fixture(tmp_path)
    f.change(rm_rc=rm_rc)
    result = f.step("replace")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == NEW
    assert f.env["E2E_SELENIUM_CONTAINER"] == NEW
    assert f.receipt.read_text().split("|")[0:3] == ["running", "engine-1", NEW]
    assert f.read()["containers"] == [NEW]
    assert f.count(["rm", "-f"]) == 1 and f.count(["run"]) == 1
    assert f.step("finalize").returncode == 0
    assert f.read()["containers"] == []


@pytest.mark.parametrize("list_rc,list_output,remove_effect", [
    (0, None, False), (124, None, True), (1, None, True),
    (0, OLD, True), (0, NEW, True), (0, OLD + "\n" + NEW, True),
])
def test_uncertain_removal_never_launches_or_repeats(tmp_path, list_rc, list_output, remove_effect):
    f = Fixture(tmp_path)
    f.change(rm_rc=124, list_rc=list_rc, list_output=list_output, remove_effect=remove_effect)
    assert f.step("replace").returncode != 0
    assert f.env["E2E_SELENIUM_CONTAINER"] == OLD
    assert f.receipt.read_text().split("|")[0:3] == ["removing", "engine-1", OLD]
    assert f.count(["run"]) == 0
    assert f.step("finalize").returncode != 0
    assert f.count(["rm", "-f"]) == 1


def test_late_absence_only_reconciles(tmp_path):
    f = Fixture(tmp_path)
    f.change(remove_effect=False, rm_rc=124)
    assert f.step("replace").returncode != 0
    f.change(containers=[])
    assert f.step("finalize").returncode == 0
    assert f.count(["rm", "-f"]) == 1 and f.count(["run"]) == 0


@pytest.mark.parametrize("container_id", ["", "a"*12, "a"*64 + "\n" + NEW, "x"*64])
def test_invalid_id_cannot_remove_or_replace(tmp_path, container_id):
    f = Fixture(tmp_path)
    f.env["E2E_SELENIUM_CONTAINER"] = container_id
    assert f.step("replace").returncode != 0
    assert f.count(["rm", "-f"]) == 0 and f.count(["run"]) == 0


@pytest.mark.parametrize("mode", ["start", "replace"])
@pytest.mark.parametrize("run_rc,run_output", [(124, NEW), (0, "short")])
def test_ambiguous_creation_never_replays_or_erases_evidence(tmp_path, mode, run_rc, run_output):
    f = Fixture(tmp_path, existing=mode == "replace")
    f.change(run_rc=run_rc, run_output=run_output)
    assert f.step(mode).returncode != 0
    assert f.receipt.read_text().startswith("starting|engine-1|-|")
    assert f.receipt.read_text().strip().endswith("initial" if mode == "start" else "replacement")
    assert f.step("finalize").returncode != 0
    assert f.step(mode).returncode != 0
    assert f.count(["run"]) == 1
    assert f.read()["containers"] == [NEW]


@pytest.mark.parametrize("drift_after_rm", [False, True])
def test_engine_change_blocks_cleanup_or_replacement(tmp_path, drift_after_rm):
    f = Fixture(tmp_path)
    f.change(drift_after_rm=drift_after_rm, engine="engine-1" if drift_after_rm else "other")
    assert f.step("replace").returncode != 0
    assert f.count(["run"]) == 0
    assert f.count(["rm", "-f"]) == int(drift_after_rm)


def test_normal_initial_start_and_finalization(tmp_path):
    f = Fixture(tmp_path, existing=False)
    assert f.step("start").returncode == 0
    assert f.receipt.read_text().split("|")[0:3] == ["running", "engine-1", NEW]
    assert f.step("finalize").returncode == 0
    assert f.read()["containers"] == []


def test_finalization_reports_unknown_removal(tmp_path):
    f = Fixture(tmp_path)
    f.change(remove_effect=False, rm_rc=124)
    assert f.step("finalize").returncode != 0
    assert f.step("finalize").returncode != 0
    assert f.count(["rm", "-f"]) == 1


def test_workflow_finalizer_propagates_unproven_cleanup(tmp_path):
    f = Fixture(tmp_path)
    f.change(remove_effect=False, rm_rc=124)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = re.search(
        r"^      - name: Stop Selenium Chromium sidecar\n(.*?)(?=^      - name:|^  [a-zA-Z][\w-]*:|\Z)",
        workflow, re.MULTILINE | re.DOTALL,
    )
    assert step is not None
    run = step[1].split("        run: ", 1)[1]
    script = textwrap.dedent(run[2:]) if run.startswith("|\n") else run.strip()
    result = subprocess.run(
        ["bash", "-c", script], cwd=ROOT, env=f.env, text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode != 0, "workflow must not report successful cleanup"
    assert f.receipt.read_text().startswith("removing|")
    assert f.read()["containers"] == [OLD]
    assert f.count(["rm", "-f"]) == 1


@pytest.mark.parametrize("content", [
    "", "running|engine-1|short|untrusted\n",
    "running|engine-1|" + OLD + "|another-job-initial\n",
    "running|engine-1|" + OLD + "|untrusted\nextra\n",
])
def test_invalid_receipt_never_dispatches(tmp_path, content):
    f = Fixture(tmp_path)
    f.receipt.write_text(content)
    assert f.step("finalize").returncode != 0
    assert f.read()["calls"] == []


def test_receipt_lock_blocks_second_operation(tmp_path):
    f = Fixture(tmp_path)
    fd = os.open(f.receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert f.step("replace").returncode != 0
        assert f.step("finalize").returncode != 0
        assert f.read()["calls"] == []
    finally:
        os.close(fd)
    assert f.step("finalize").returncode == 0
    assert f.count(["rm", "-f"]) == 1


def test_symlink_receipt_is_refused(tmp_path):
    f = Fixture(tmp_path)
    other = tmp_path / "other-receipt"
    f.receipt.rename(other)
    f.receipt.symlink_to(other)
    assert f.step("finalize").returncode != 0
    assert f.read()["calls"] == []


@pytest.mark.parametrize("tracked_id", ["", OLD])
def test_finalize_without_receipt_cannot_prove_absence(tmp_path, tracked_id):
    f = Fixture(tmp_path, existing=False)
    f.env["E2E_SELENIUM_CONTAINER"] = tracked_id
    assert f.step("finalize").returncode != 0
    assert f.read()["calls"] == []


def test_failed_start_lost_environment_and_changed_tmpdir(tmp_path):
    f = Fixture(tmp_path, existing=False)
    f.change(run_rc=124)
    assert f.step("start").returncode != 0
    assert f.read()["containers"] == [NEW]
    assert f.receipt.read_text().startswith("starting|")
    assert "E2E_SELENIUM_CONTAINER" not in f.env
    other_tmp = tmp_path / "other-tmp"
    other_tmp.mkdir()
    f.env["TMPDIR"] = str(other_tmp)
    assert f.step("finalize").returncode != 0
    assert f.count(["run"]) == 1 and f.count(["rm", "-f"]) == 0


@pytest.mark.parametrize("run_rc", [0, 124])
def test_workflow_start_publishes_environment_only_on_success(tmp_path, run_rc):
    f = Fixture(tmp_path, existing=False)
    f.change(run_rc=run_rc)
    env_file = tmp_path / "github-env"
    f.env["GITHUB_ENV"] = str(env_file)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = re.search(
        r"^      - name: Start pinned Selenium Chromium sidecar\n.*?^        run: \|\n"
        r"((?:^          [^\n]*\n)+)",
        workflow, re.MULTILINE | re.DOTALL,
    )
    assert step is not None
    result = subprocess.run(
        ["bash", "-c", textwrap.dedent(step[1])], cwd=ROOT, env=f.env,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert f.count(["run"]) == 1
    if run_rc:
        assert result.returncode != 0 and not env_file.exists()
        assert f.receipt.read_text().startswith("starting|")
    else:
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        assert values["E2E_SELENIUM_CONTAINER"] == NEW
        assert re.fullmatch(r"ghcr\.io/seleniumhq/standalone-chromium:[^@]+@sha256:[0-9a-f]{64}", values["E2E_SELENIUM_IMAGE"])
        assert values["SELENIUM_REMOTE_URL"] == "http://127.0.0.1:4444/wd/hub"
        assert f.receipt.read_text().startswith("running|")


@pytest.mark.parametrize("remove_effect", [True, False])
def test_workflow_replacement_only_updates_environment_after_proven_absence(tmp_path, remove_effect):
    f = Fixture(tmp_path)
    f.change(remove_effect=remove_effect, rm_rc=124)
    env_file = tmp_path / "github-env"
    f.env.update(GITHUB_ENV=str(env_file), E2E_SELENIUM_IMAGE=IMAGE)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = re.search(
        r"^              # Do not create a sibling[^\n]*\n"
        r"(?:^              [^\n]*\n){3}", workflow, re.MULTILINE,
    )
    assert step is not None
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + textwrap.dedent(step[0])],
        cwd=ROOT, env=f.env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert f.count(["rm", "-f"]) == 1
    if remove_effect:
        assert result.returncode == 0, result.stderr
        assert env_file.read_text() == f"E2E_SELENIUM_CONTAINER={NEW}\n"
        assert f.count(["run"]) == 1
    else:
        assert result.returncode != 0 and not env_file.exists()
        assert f.count(["run"]) == 0
        assert f.receipt.read_text().startswith("removing|")


@pytest.mark.parametrize("mode", ["start", "replace"])
@pytest.mark.parametrize("run_effect", [True, False])
def test_hanging_creation_preserves_uncertainty_and_never_replays(tmp_path, mode, run_effect):
    f = Fixture(tmp_path, existing=mode == "replace")
    f.change(run_hangs=True, run_effect=run_effect)
    # Use the platform's real timeout against a genuinely sleeping child.
    # Integer seconds and short options work with GNU and BusyBox timeout.
    real_timeout = shutil.which("timeout")
    assert real_timeout is not None
    f.env["REAL_TIMEOUT"] = real_timeout
    timeout = tmp_path / "bin" / "timeout"
    timeout.write_text(
        '#!/bin/bash\n'
        'if [[ "$4" == docker && "$5" == run ]]; then\n'
        '  [[ "$1" == -k && "$2" == 5 && "$3" == 180 ]] || exit 99\n'
        '  shift 3\n'
        '  exec "$REAL_TIMEOUT" -k 1 1 "$@"\n'
        'fi\n'
        'exec "$REAL_TIMEOUT" "$@"\n'
    )
    timeout.chmod(0o755)
    result = f.step(mode)
    assert result.returncode != 0, result.stdout
    assert "creation outcome uncertain" in result.stderr
    assert f.receipt.read_text().startswith("starting|engine-1|-|")
    assert f.read()["containers"] == ([NEW] if run_effect else [])
    assert f.step("finalize").returncode != 0
    assert f.step(mode).returncode != 0
    assert f.count(["run"]) == 1
    assert f.count(["rm", "-f"]) == int(mode == "replace")
