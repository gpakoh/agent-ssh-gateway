"""Integration tests for full patch apply flow: parse -> validate -> apply -> rollback."""

import hashlib
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_full_dry_run_flow():
    from app.patch_apply import PatchApplier

    applier = PatchApplier()
    patch_text = textwrap.dedent("""\
        --- a/src/app.py
        +++ b/src/app.py
        @@ -1,2 +1,3 @@
         def hello():
        +    print("hi")
             pass
         """)
    files = applier._parse_patch(patch_text, strip=1)
    applier._validate_file_count(len(files))
    total_hunks = sum(f["hunk_count"] for f in files)
    applier._validate_hunk_count(total_hunks)
    applier._validate_no_forbidden_ops(files)

    original = textwrap.dedent("""\
        def hello():
            pass
        """)
    new_content = applier._apply_in_memory(original, files[0])

    assert 'print("hi")' in new_content
    assert "def hello():" in new_content


def test_hash_check_prevents_stale_apply():
    from app.patch_apply import HashMismatchError, PatchApplier

    applier = PatchApplier()
    content = "original content\n"
    expected = "sha256:" + hashlib.sha256(b"wrong content\n").hexdigest()

    with pytest.raises(HashMismatchError):
        applier._check_hash("file.py", content, expected)


def test_multiple_hunks_apply():
    from app.patch_apply import PatchApplier

    applier = PatchApplier()
    patch_text = textwrap.dedent("""\
        --- a/file.py
        +++ b/file.py
        @@ -1,3 +1,3 @@
         line1
        -old middle
        +new middle
         line3
        @@ -10,3 +10,3 @@
         line10
        -old2
        +new2
         line12
    """)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 1
    assert files[0]["hunk_count"] == 2

    original = textwrap.dedent("""\
        line1
        old middle
        line3
        line4
        line5
        line6
        line7
        line8
        line9
        line10
        old2
        line12
    """)
    new_content = applier._apply_in_memory(original, files[0])
    assert "new middle" in new_content
    assert "new2" in new_content
    assert "old middle" not in new_content
    assert "old2" not in new_content
