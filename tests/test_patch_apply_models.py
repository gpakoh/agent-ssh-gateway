"""Tests for patch apply models and unidiff import."""

from app.models import ProjectPatchApplyRequest, ProjectPatchApplyResponse


def test_unidiff_importable():
    import unidiff
    assert hasattr(unidiff, "PatchSet")


def test_patch_apply_request_valid():
    req = ProjectPatchApplyRequest(
        session_id="abc",
        project="myproject",
        patch="--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,4 @@\n line1\n+new\n line2\n line3\n",
        expected_hashes={"file.py": "sha256:abcdef"},
    )
    assert req.session_id == "abc"
    assert req.project == "myproject"
    assert req.strip == 1
    assert req.dry_run is False


def test_patch_apply_request_empty_patch_rejected():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x", project="p", patch="", expected_hashes={}
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_request_empty_project_rejected():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x",
            project="",
            patch="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
            expected_hashes={},
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_request_strip_bounds():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x",
            project="p",
            patch="--- a/f\n+++ b/f\n",
            expected_hashes={},
            strip=-1,
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_response():
    resp = ProjectPatchApplyResponse(
        success=True,
        files_applied=1,
        files_failed=0,
        hunks_applied=3,
        preview=None,
        errors=[],
    )
    assert resp.success is True
    assert resp.files_applied == 1
