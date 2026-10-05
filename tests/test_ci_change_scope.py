"""Hermetic Git/event regression tests for the lightweight CI gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.ci_change_scope import classify

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_change_scope.py"


class TestChangeScope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI scope test")
        self.git("config", "user.email", "ci-scope@example.invalid")
        self.write("README.md", "Initial\n")
        self.write("app/service.py", "value = 1\n")
        self.write("docs/guide.md", "Guide\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args]).decode().strip()

    def write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "test revision")
        return self.git("rev-parse", "HEAD")

    def event(self, before: str, after: str, name: str = "push") -> dict:
        if name == "pull_request":
            return {"pull_request": {"base": {"sha": before}, "head": {"sha": after}}}
        return {"before": before, "after": after}

    def test_docs_push_and_pr_skip_heavy_jobs(self) -> None:
        for path in ("README.md", "TODO.md", "docs/nested/guide.rst"):
            self.write(path, "Updated documentation\n")
        head = self.commit()
        for name in ("push", "pull_request"):
            with self.subTest(event=name):
                scope = classify(self.root, name, self.event(self.base, head, name))
                self.assertFalse(scope.full_ci)
                self.assertEqual(scope.head, head)

    def test_runtime_paths_and_mixed_changes_run_full_ci(self) -> None:
        for path in (
            "app/service.py",
            "tests/test_example.py",
            "pyproject.toml",
            "uv.lock",
            "Dockerfile",
            "compose.yml",
            ".github/workflows/ci.yml",
            ".gitea/workflows/ci.yml",
            "scripts/example.sh",
            "AGENTS.md",
            "docs/AGENTS.md",
            "docs/contract.json",
            "docs/example.py",
            "app/prompt.md",
            "docs/fake.md/runtime.py",
        ):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.base)
                self.git("clean", "-fdq")
                self.write("README.md", "Docs too\n")
                self.write(path, "Changed\n")
                head = self.commit()
                self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def test_code_renamed_to_documentation_runs_full_ci(self) -> None:
        self.git("mv", "app/service.py", "docs/service.md")
        head = self.commit()
        self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def test_documentation_rename_and_deletion_remain_light(self) -> None:
        self.git("mv", "README.md", "docs/renamed.md")
        self.git("rm", "docs/guide.md")
        head = self.commit()
        self.assertFalse(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def test_multiple_commit_push_includes_earlier_code_change(self) -> None:
        self.write("app/service.py", "value = 2\n")
        self.commit()
        self.write("README.md", "Updated\n")
        head = self.commit()
        self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def test_pull_request_uses_merge_base(self) -> None:
        self.git("checkout", "-qb", "feature")
        self.write("README.md", "Feature docs\n")
        head = self.commit()
        self.git("checkout", "-q", "--detach", self.base)
        self.write("app/service.py", "base advanced\n")
        advanced_base = self.commit()
        self.git("checkout", "-q", "--detach", head)
        scope = classify(self.root, "pull_request", self.event(advanced_base, head, "pull_request"))
        self.assertFalse(scope.full_ci)
        self.assertEqual(scope.base, self.base)

    def test_unknown_or_missing_history_never_skips(self) -> None:
        for name, event in (
            ("workflow_dispatch", {}),
            ("push", {}),
            ("push", self.event("0" * 40, self.base)),
            ("push", self.event("a" * 40, self.base)),
            ("pull_request", self.event("a" * 40, self.base, "pull_request")),
            ("push", self.event(self.base, self.base)),
        ):
            with self.subTest(event=name, payload=event):
                self.assertTrue(classify(self.root, name, event).full_ci)

    def test_checkout_mismatch_fails(self) -> None:
        self.write("README.md", "Updated\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "checkout HEAD"):
            classify(self.root, "push", self.event(self.base, self.base))

    def test_symlink_and_executable_docs_require_full_ci(self) -> None:
        (self.root / "docs/link.md").symlink_to("../app/service.py")
        head = self.commit()
        self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)
        self.git("reset", "--hard", self.base)
        (self.root / "README.md").chmod(0o755)
        head = self.commit()
        self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def test_newline_in_filename_does_not_hide_code(self) -> None:
        self.write("app/runtime\nREADME.md", "code\n")
        head = self.commit()
        self.assertTrue(classify(self.root, "push", self.event(self.base, head)).full_ci)

    def run_script(self, event: dict) -> subprocess.CompletedProcess:
        payload = self.root / "event.json"
        output = self.root / "outputs.txt"
        payload.write_text(json.dumps(event), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.root,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GITHUB_EVENT_PATH": str(payload),
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_OUTPUT": str(output),
            },
            timeout=30,
        )

    def test_cli_records_exact_head_and_light_output(self) -> None:
        self.write("README.md", "Updated\n")
        head = self.commit()
        result = self.run_script(self.event(self.base, head))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "outputs.txt").read_text(), "full_ci=false\n")
        self.assertEqual(json.loads(result.stdout)["head"], head)

    def test_docs_whitespace_error_is_not_green(self) -> None:
        self.write("README.md", "Trailing spaces  \n")
        head = self.commit()
        result = self.run_script(self.event(self.base, head))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "outputs.txt").exists())


if __name__ == "__main__":
    unittest.main()
