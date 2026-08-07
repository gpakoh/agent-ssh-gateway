"""Tests for github list response normalization."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.shared import list_pagination_meta, normalize_list_response


def test_bare_list_wrapped():
    result = normalize_list_response([{"name": "main"}, {"name": "dev"}])
    assert result == {"items": [{"name": "main"}, {"name": "dev"}], "count": 2}


def test_empty_list():
    result = normalize_list_response([])
    assert result == {"items": [], "count": 0}


def test_dict_with_items_preserved():
    result = normalize_list_response({"items": [1, 2, 3], "count": 3})
    assert result == {"items": [1, 2, 3], "count": 3}


def test_dict_with_items_missing_count():
    result = normalize_list_response({"items": ["a", "b"]})
    assert result == {"items": ["a", "b"], "count": 2}


def test_plain_dict_preserved():
    result = normalize_list_response({"key": "value"})
    assert result == {"key": "value"}


def test_plain_dict_with_meta():
    result = normalize_list_response({"key": "value"}, meta={"repo": "owner/name"})
    assert result == {"key": "value", "repo": "owner/name"}


def test_unexpected_type():
    result = normalize_list_response("not a list or dict")
    assert result == {"items": [], "count": 0, "error": "unexpected response type"}


def test_int_value():
    result = normalize_list_response(42)
    assert result["count"] == 0
    assert result["error"] == "unexpected response type"


def test_none_value():
    result = normalize_list_response(None)
    assert result["count"] == 0


def test_list_with_meta():
    result = normalize_list_response(
        [{"id": 1}],
        meta={"repo": "gpakoh/agent-ssh-gateway", "page": 1},
    )
    assert result == {
        "items": [{"id": 1}],
        "count": 1,
        "repo": "gpakoh/agent-ssh-gateway",
        "page": 1,
    }


def test_dict_with_items_and_meta():
    result = normalize_list_response(
        {"items": ["x"], "count": 1},
        meta={"per_page": 30},
    )
    assert result == {"items": ["x"], "count": 1, "per_page": 30}


def test_pagination_meta_full_page_truncated():
    meta = list_pagination_meta(count=30, per_page=30)
    assert meta == {"page": 1, "per_page": 30, "truncated": True}


def test_pagination_meta_partial_page_not_truncated():
    meta = list_pagination_meta(count=3, per_page=30)
    assert meta == {"page": 1, "per_page": 30, "truncated": False}


def test_pagination_meta_empty_page():
    meta = list_pagination_meta(count=0, per_page=30)
    assert meta == {"page": 1, "per_page": 30, "truncated": False}
