"""Integration test fixtures for SSH testing."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def sshd_server():
    host_key_path = "/tmp/ssh_test_host_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", host_key_path, "-N", "", "-q"],
        check=True,
    )

    client_key_path = "/tmp/ssh_test_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", client_key_path, "-N", "", "-q"],
        check=True,
    )

    pub_key_path = f"{client_key_path}.pub"
    with open(pub_key_path) as f:
        pub_key = f.read().strip()

    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    auth_keys = ssh_dir / "authorized_keys"
    with open(auth_keys, "a") as f:
        f.write(pub_key + "\n")
    auth_keys.chmod(0o600)

    with open(client_key_path) as f:
        private_key = f.read()

    result = subprocess.run(["pgrep", "sshd"], capture_output=True)
    if result.returncode != 0:
        for cmd in [
            ["service", "ssh", "start"],
            ["service", "sshd", "start"],
            ["/usr/sbin/sshd"],
        ]:
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                break
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

    yield {
        "host": "127.0.0.1",
        "port": 22,
        "username": "root",
        "private_key": private_key,
    }

    for p in [host_key_path, f"{host_key_path}.pub", client_key_path, pub_key_path]:
        try:
            os.remove(p)
        except OSError:
            pass
