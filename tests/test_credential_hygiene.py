"""Credential hygiene tests — ensure secrets are masked in repr, model_dump, and not stored in session metadata."""

from app.models import ConnectRequest, ConnectServerRequest


def test_connect_request_secrets_hidden_in_repr():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        password="super-secret-password",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
        key_passphrase="secret-passphrase",
    )

    rendered = repr(req)

    assert "super-secret-password" not in rendered
    assert "secret-passphrase" not in rendered
    assert "BEGIN OPENSSH PRIVATE KEY" not in rendered
    assert "**********" not in rendered


def test_connect_request_model_dump_masks_secrets():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        password="super-secret-password",
    )

    dumped = req.model_dump()

    assert "super-secret-password" not in str(dumped)
    assert "**********" in str(dumped["password"])


def test_connect_request_model_dump_json_masks_secrets():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        password="super-secret-password",
    )

    dumped = req.model_dump(mode="json")

    assert dumped["password"] == "**********"
    assert "super-secret-password" not in str(dumped)


def test_connect_server_request_secrets_hidden_in_repr():
    req = ConnectServerRequest(
        password="super-secret-password",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
    )

    rendered = repr(req)

    assert "super-secret-password" not in rendered
    assert "BEGIN OPENSSH PRIVATE KEY" not in rendered
    assert "**********" in rendered


def test_connect_server_request_model_dump_masks_secrets():
    req = ConnectServerRequest(
        password="super-secret-password",
    )

    dumped = req.model_dump()

    assert "super-secret-password" not in str(dumped)
    assert "**********" in str(dumped["password"])


def test_connect_request_auth_method_detection_password():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        password="secret",
    )
    from app.routers.ssh import get_connect_auth_method

    assert get_connect_auth_method(req) == "password"


def test_connect_request_auth_method_detection_private_key():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
    )
    from app.routers.ssh import get_connect_auth_method

    assert get_connect_auth_method(req) == "private_key"


def test_connect_request_auth_method_detection_none():
    req = ConnectRequest(
        host="10.0.0.1",
        port=22,
        username="deploy",
        password="secret",
        private_key="key",
    )
    from app.routers.ssh import get_connect_auth_method

    assert get_connect_auth_method(req) == "private_key"


def test_ssh_session_model_has_no_credential_fields():
    from dataclasses import fields

    from app.ssh_manager import SessionRecord

    field_names = {field.name for field in fields(SessionRecord)}

    assert "password" not in field_names
    assert "private_key" not in field_names
    assert "key_passphrase" not in field_names
    assert "session_id" in field_names
    assert "host" in field_names
    assert "username" in field_names
