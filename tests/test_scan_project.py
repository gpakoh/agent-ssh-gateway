"""Tests for project_scan_destructive — scanning project for destructive patterns."""

import json

from app.workspace.scan_project import _is_binary, scan_project

# ── Binary detection ──────────────────────────────────────────────────────────

class TestIsBinary:
    def test_text_bytes(self):
        assert _is_binary(b"hello world") is False

    def test_binary_with_null(self):
        assert _is_binary(b"hello\x00world") is True

    def test_empty(self):
        assert _is_binary(b"") is False


# ── Scan project ──────────────────────────────────────────────────────────────

class TestScanProject:
    def test_empty_project(self, tmp_path):
        (tmp_path / "file.py").write_text("print('hello')\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path)
        assert result["files_scanned"] == 1
        assert result["total_findings"] == 0

    def test_finds_destructive_pattern(self, tmp_path):
        f = tmp_path / "script.sh"
        f.write_text("rm -rf /\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path)
        assert result["total_findings"] >= 1
        assert "script.sh" in result["findings"]

    def test_skips_excluded_dirs(self, tmp_path):
        (tmp_path / ".git" / "config").parent.mkdir(parents=True)
        (tmp_path / ".git" / "config").write_text("rm -rf /\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path)
        assert ".git/config" not in result.get("findings", {})

    def test_skips_binary(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"rm\x00-rf\x00/\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path)
        assert result["total_findings"] == 0

    def test_respects_max_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("ls\n")
        result = scan_project("test", pattern="*", max_files=3, _root_override=tmp_path)
        assert result["files_scanned"] == 3
        assert result["truncated"] is True

    def test_empty_project_no_files(self, tmp_path):
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path)
        assert result["files_scanned"] == 0
        assert result["total_findings"] == 0

    def test_pattern_filters_files(self, tmp_path):
        (tmp_path / "script.sh").write_text("rm -rf /\n")
        (tmp_path / "notes.txt").write_text("rm -rf /\n")
        result = scan_project("test", pattern="*.txt", max_files=10, _root_override=tmp_path)
        assert result["files_scanned"] == 1
        assert "notes.txt" in result["findings"]

    def test_json_format(self, tmp_path):
        (tmp_path / "script.sh").write_text("rm -rf /\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path, fmt="json")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["total_findings"] >= 1
        assert "script.sh" in parsed["findings"]

    def test_sarif_format(self, tmp_path):
        (tmp_path / "script.sh").write_text("rm -rf /\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path, fmt="sarif")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["version"] == "2.1.0"
        assert len(parsed["runs"]) == 1
        run = parsed["runs"][0]
        assert len(run["results"]) >= 1
        assert run["tool"]["driver"]["name"] == "agent-ssh-gateway scan_project"
        assert run["results"][0]["ruleId"] is not None
        assert run["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"]

    def test_sarif_empty_project(self, tmp_path):
        (tmp_path / "file.py").write_text("print('ok')\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path, fmt="sarif")
        parsed = json.loads(result)
        assert parsed["version"] == "2.1.0"
        assert len(parsed["runs"][0]["results"]) == 0

    def test_sarif_severity_mapping(self, tmp_path):
        (tmp_path / "x.sh").write_text("chmod -R 777 /etc\n")
        result = scan_project("test", pattern="*", max_files=10, _root_override=tmp_path, fmt="sarif")
        parsed = json.loads(result)
        for r in parsed["runs"][0]["results"]:
            assert r["level"] in ("error", "warning", "note")
