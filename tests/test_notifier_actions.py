"""Tests for opaque action tokens (Telegram inline buttons)."""

import time

from app.notifier.actions import (
    clear_actions,
    create_action,
    get_action,
    pop_action,
)


def test_create_and_get_action():
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="abc123",
        source_ip="203.0.113.10",
        event_type="session.connect",
        request_id="req-1",
    )
    assert token
    payload = get_action(token)
    assert payload is not None
    assert payload.action_type == "allow_actor"
    assert payload.actor_fingerprint == "abc123"
    assert payload.source_ip == "203.0.113.10"
    assert payload.event_type == "session.connect"
    assert payload.request_id == "req-1"


def test_action_token_is_opaque():
    """Token must not contain the actor fingerprint."""
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="secret-actor-abc123",
        source_ip="10.0.0.1",
        event_type="session.connect",
        request_id="req-2",
    )
    assert "secret-actor" not in token
    assert "abc123" not in token


def test_get_expired_action_returns_none():
    token = create_action(
        action_type="deny_actor",
        actor_fingerprint="x",
        source_ip="10.0.0.1",
        event_type="session.connect",
        request_id="req-3",
        ttl_seconds=0,
    )
    time.sleep(0.01)
    assert get_action(token) is None


def test_pop_action_removes():
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="y",
        source_ip="10.0.0.2",
        event_type="session.connect",
        request_id="req-4",
    )
    payload = pop_action(token)
    assert payload is not None
    assert pop_action(token) is None


def test_get_unknown_token_returns_none():
    assert get_action("nonexistent-token") is None


def test_clear_actions():
    create_action(
        action_type="allow_actor",
        actor_fingerprint="a",
        source_ip="10.0.0.1",
        event_type="session.connect",
        request_id="r1",
    )
    create_action(
        action_type="deny_actor",
        actor_fingerprint="b",
        source_ip="10.0.0.2",
        event_type="session.connect",
        request_id="r2",
    )
    clear_actions()
    assert get_action("any") is None
