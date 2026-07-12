"""Tests for build metadata module."""

import os
import time
from unittest.mock import patch

from app import build_info


class TestBuildSha:
    def test_env_override(self):
        with patch.dict(os.environ, {"BUILD_SHA": "abc123def"}):
            sha = build_info._resolve_build_sha()
        assert sha == "abc123def"

    def test_unknown_when_no_env_and_no_git(self):
        with patch.dict(os.environ, {"BUILD_SHA": ""}, clear=False):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                sha = build_info._resolve_build_sha()
        assert sha == "unknown"


class TestBuildTime:
    def test_env_override(self):
        with patch.dict(os.environ, {"BUILD_TIME": "2026-07-12T12:30:00Z"}):
            bt = build_info._resolve_build_time()
        assert bt == "2026-07-12T12:30:00Z"

    def test_empty_when_no_env(self):
        with patch.dict(os.environ, {"BUILD_TIME": ""}, clear=False):
            bt = build_info._resolve_build_time()
        assert bt == ""


class TestStartedAt:
    def test_initially_none(self):
        build_info._started_at = None
        assert build_info.get_started_at() is None

    def test_set_started_at(self):
        before = time.time()
        build_info.set_started_at()
        after = time.time()
        assert build_info._started_at is not None
        assert before <= build_info._started_at <= after
        build_info._started_at = None  # cleanup


class TestGetBuildMetadata:
    def test_returns_dict_with_expected_keys(self):
        build_info._started_at = 1700000000.0
        meta = build_info.get_build_metadata()
        assert set(meta.keys()) == {"build_sha", "build_time", "started_at"}
        assert isinstance(meta["build_sha"], str)
        assert isinstance(meta["build_time"], str)
        assert meta["started_at"] == "2023-11-14T22:13:20Z"
        build_info._started_at = None  # cleanup

    def test_started_at_empty_when_none(self):
        build_info._started_at = None
        meta = build_info.get_build_metadata()
        assert meta["started_at"] == ""
