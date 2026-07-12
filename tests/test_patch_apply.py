"""Tests for PatchApplier: validation, parsing, hash check, dry_run."""

import hashlib
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_parse_patch_single_file():
    from app.patch_apply import PatchApplier

    patch_text = textwrap.dedent("""\
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -1,3 +1,4 @@
         line1
        +added line
         line2
         line3
    """)
    applier = PatchApplier.__new__(PatchApplier)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 1
    assert files[0]["path"] == "src/foo.py"
    assert len(files[0]["hunks"]) == 1


def test_parse_patch_multiple_files():
    from app.patch_apply import PatchApplier

    patch_text = textwrap.dedent("""\
        --- a/a.py
        +++ b/a.py
        @@ -1 +1 @@
        -old
        +new
        --- a/b.py
        +++ b/b.py
        @@ -1 +1 @@
        -old
        +new
    """)
    applier = PatchApplier.__new__(PatchApplier)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 2


def test_validate_limits_too_many_files():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="20 files"):
        applier._validate_file_count(21)


def test_validate_limits_too_many_hunks():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="100 hunks"):
        applier._validate_hunk_count(101)


def test_validate_limits_patch_too_large():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="1 MiB"):
        applier._validate_patch_size(1_048_577)


def test_compute_file_hash():
    from app.patch_apply import PatchApplier

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello world\n"
    h = applier._compute_sha256(content)
    expected = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert h == expected


def test_check_hash_match():
    from app.patch_apply import PatchApplier, HashMismatchError

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello\n"
    correct_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Should not raise
    applier._check_hash("file.py", content, correct_hash)


def test_check_hash_mismatch():
    from app.patch_apply import PatchApplier, HashMismatchError

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello\n"
    wrong_hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

    with pytest.raises(HashMismatchError, match="file.py"):
        applier._check_hash("file.py", content, wrong_hash)


def test_forbid_binary_operations():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    patch_text = textwrap.dedent("""\
        --- a/old.txt
        +++ b/new.txt
        @@ -1 +1 @@
        -old
        +new
    """)
    files = applier._parse_patch(patch_text, strip=1)
    with pytest.raises(PatchValidationError, match="rename/copy"):
        applier._validate_no_forbidden_ops(files)


def test_forbid_dev_null():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    patch_text = textwrap.dedent("""\
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1 @@
        +content
    """)
    files = applier._parse_patch(patch_text, strip=0)
    with pytest.raises(PatchValidationError, match="/dev/null"):
        applier._validate_no_forbidden_ops(files)


def test_apply_in_memory():
    from app.patch_apply import PatchApplier

    applier = PatchApplier.__new__(PatchApplier)
    original = "line1\nline2\nline3\n"
    patch_text = textwrap.dedent("""\
        --- a/file.py
        +++ b/file.py
        @@ -1,3 +1,4 @@
         line1
        +added
         line2
         line3
    """)
    files = applier._parse_patch(patch_text, strip=1)
    result = applier._apply_in_memory(original, files[0])
    assert "added" in result
    assert "line1" in result
