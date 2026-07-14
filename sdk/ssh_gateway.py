"""SSH Gateway Python SDK.

Usage:
    from ssh_gateway import SSHGatewayClient

    client = SSHGatewayClient("https://gateway.example.com")
    client.auth("agent", "change-me-agent-token")

    # Connect And Persist Session
    session = client.ssh_connect("192.0.2.10", username="root", password="...")

    # Execute Command
    result = client.execute("ls -la")

    # Batch Read Files
    files = client.batch_read(["app/main.py", "app/config.py"])

    # Edit File
    client.edit_file("app/main.py", [
        {"type": "replace", "old": "def old():", "new": "def new():"}
    ])

    # Background Job
    job = client.run_background("pytest", timeout=300)
    for log in job.stream_logs():
        print(log)
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass

import requests
import urllib3

urllib3.disable_warnings()


@dataclass
class SSHSession:
    """SSH session with auto-reconnect."""

    session_id: str
    host: str
    username: str
    port: int = 22


class SSHGatewayClient:
    """Python SDK for SSH Gateway API."""

    def __init__(self, base_url: str = "https://gateway.example.com"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.verify = False
        self._ssh_session: SSHSession | None = None
        self._context_id: str | None = None

    def auth(self, username: str, password: str) -> bool:
        """Authenticate with Authelia."""
        r = self.session.post(
            f"{self.base_url}/api/firstfactor",
            json={
                "username": username,
                "password": password,
                "request_method": "GET",
                "request_uri": self.base_url,
            },
        )
        return r.status_code == 200

    def ssh_connect(
        self, host: str, username: str, password: str = "", port: int = 22, private_key: str = ""
    ) -> SSHSession:
        """Connect to SSH server with auto-reconnect support."""
        r = self.session.post(
            f"{self.base_url}/api/ssh/connect",
            json={
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "private_key": private_key,
            },
        )
        r.raise_for_status()
        data = r.json()

        self._ssh_session = SSHSession(
            session_id=data["session_id"], host=host, username=username, port=port
        )

        # Start Heartbeat Thread
        self._start_heartbeat()

        return self._ssh_session

    def _start_heartbeat(self):
        """Start background heartbeat to keep session alive."""
        import threading

        def heartbeat_loop():
            while self._ssh_session:
                try:
                    self.session.post(
                        f"{self.base_url}/api/ssh/heartbeat",
                        json={"session_id": self._ssh_session.session_id},
                    )
                except Exception:
                    pass
                time.sleep(30)

        thread = threading.Thread(target=heartbeat_loop, daemon=True)
        thread.start()

    def execute(self, command: str, timeout: int = 120) -> dict:
        """Execute command on remote server."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.post(
            f"{self.base_url}/api/ssh/execute",
            json={
                "session_id": self._ssh_session.session_id,
                "command": command,
                "timeout": timeout,
            },
        )
        r.raise_for_status()
        return r.json()

    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        """Read file content."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        # Use Raw Endpoint For Better Performance
        r = self.session.get(
            f"{self.base_url}/api/file/raw",
            params={
                "session_id": self._ssh_session.session_id,
                "path": path,
                "offset": offset,
                "limit": limit,
            },
        )
        r.raise_for_status()
        return r.text

    def batch_read(self, paths: list[str]) -> dict[str, str]:
        """Read multiple files in one request."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.post(
            f"{self.base_url}/api/batch/read",
            json={"session_id": self._ssh_session.session_id, "paths": paths},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("files", {})

    def edit_file(self, path: str, operations: list[dict]) -> dict:
        """Edit file with operations."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.patch(
            f"{self.base_url}/api/file/edit",
            json={
                "session_id": self._ssh_session.session_id,
                "path": path,
                "operations": operations,
            },
        )
        r.raise_for_status()
        return r.json()

    def run_background(self, command: str, timeout: int = 300) -> "BackgroundJob":
        """Run command in background."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.post(
            f"{self.base_url}/api/jobs/run",
            json={
                "session_id": self._ssh_session.session_id,
                "command": command,
                "timeout": timeout,
            },
        )
        r.raise_for_status()
        data = r.json()

        return BackgroundJob(self, data["job_id"])

    def create_context(
        self,
        name: str,
        path: str,
        branch: str = "",
        auto_commit: bool = True,
        auto_validate: bool = True,
    ) -> str:
        """Create development context."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.post(
            f"{self.base_url}/api/context/create",
            json={
                "session_id": self._ssh_session.session_id,
                "name": name,
                "path": path,
                "branch": branch,
                "auto_commit": auto_commit,
                "auto_validate": auto_validate,
            },
        )
        r.raise_for_status()
        data = r.json()
        self._context_id = data["context_id"]
        return self._context_id

    def context_edit(
        self,
        path: str,
        operations: list[dict],
        commit_message: str = "",
        run_validation: bool = False,
    ) -> dict:
        """Edit file through context with auto-commit."""
        if not self._context_id:
            raise RuntimeError("No context. Call create_context() first.")

        r = self.session.patch(
            f"{self.base_url}/api/context/file/edit",
            json={
                "context_id": self._context_id,
                "path": path,
                "operations": operations,
                "commit_message": commit_message,
                "run_validation": run_validation,
            },
        )
        r.raise_for_status()
        return r.json()

    def upload_file(self, local_path: str, remote_path: str) -> dict:
        """Upload file to remote server (base64 via legacy upload endpoint)."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        with open(local_path, "rb") as f:
            content = f.read()

        import base64

        encoded = base64.b64encode(content).decode("ascii")

        r = self.session.post(
            f"{self.base_url}/api/file/upload",
            params={
                "session_id": self._ssh_session.session_id,
                "path": remote_path,
                "content": encoded,
            },
        )
        r.raise_for_status()
        return r.json()

    # ── Phase C0: Diagnostic helpers ──────────────────────────────

    def auth_check(self) -> dict:
        """Check if current API key is valid.

        Returns:
            dict with keys: valid (bool), auth_mode (str), key_name (str)

        Raises:
            requests.HTTPError: on network or server errors.
        """
        r = self.session.get(f"{self.base_url}/api/auth/check")
        r.raise_for_status()
        return r.json()

    def session_check(self, session_id: str | None = None) -> dict:
        """Check if an SSH session is alive.

        Args:
            session_id: Session to check. Uses current session if None.

        Returns:
            dict with keys: valid (bool), session_id (str), status (str),
            or valid=False with code and hint on failure.

        Raises:
            RuntimeError: if no session_id provided and no current session.
        """
        sid = session_id or (self._ssh_session.session_id if self._ssh_session else None)
        if not sid:
            raise RuntimeError("No session_id provided and no active session.")

        r = self.session.post(
            f"{self.base_url}/api/session/check",
            json={"session_id": sid},
        )
        r.raise_for_status()
        return r.json()

    def upload_file_stream(self, local_path: str, remote_path: str) -> dict:
        """Upload file using multipart/form-data for large files."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        with open(local_path, "rb") as f:
            files = {"file": (local_path.split("/")[-1], f, "application/octet-stream")}
            r = self.session.post(
                f"{self.base_url}/api/file/upload/stream",
                params={"session_id": self._ssh_session.session_id, "path": remote_path},
                files=files,
            )
        r.raise_for_status()
        return r.json()

    def download_file(self, remote_path: str, local_path: str):
        """Download file from remote server."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.get(
            f"{self.base_url}/api/file/download",
            params={"session_id": self._ssh_session.session_id, "path": remote_path},
        )
        r.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(r.content)

    def project_structure(
        self, path: str, include_git_status: bool = True, max_depth: int = 3
    ) -> dict:
        """Get project structure with metadata."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.post(
            f"{self.base_url}/api/project/structure",
            json={
                "session_id": self._ssh_session.session_id,
                "path": path,
                "include_git_status": include_git_status,
                "max_depth": max_depth,
            },
        )
        r.raise_for_status()
        return r.json()

    def batch_edit(self, files: list[dict], commit_message: str = None) -> dict:
        """Edit multiple files in a single request.

        files: [{"path": "...", "operations": [{"type": "...", ...}]}]
        """
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")

        r = self.session.patch(
            f"{self.base_url}/api/batch/edit",
            json={
                "session_id": self._ssh_session.session_id,
                "files": files,
                "commit_message": commit_message,
            },
        )
        r.raise_for_status()
        return r.json()

    def disconnect(self):
        """Close SSH session."""
        if self._ssh_session:
            try:
                self.session.post(
                    f"{self.base_url}/api/ssh/disconnect",
                    json={"session_id": self._ssh_session.session_id},
                )
            except Exception:
                pass
            self._ssh_session = None


class BackgroundJob:
    """Background job with streaming logs."""

    def __init__(self, client: SSHGatewayClient, job_id: str):
        self.client = client
        self.job_id = job_id

    def status(self) -> dict:
        """Get job status."""
        r = self.client.session.get(f"{self.client.base_url}/api/jobs/{self.job_id}/status")
        r.raise_for_status()
        return r.json()

    def result(self) -> dict:
        """Get job result."""
        r = self.client.session.get(f"{self.client.base_url}/api/jobs/{self.job_id}/result")
        r.raise_for_status()
        return r.json()

    def stream_logs(self) -> Iterator[str]:
        """Stream job logs via SSE."""
        import sseclient

        r = self.client.session.get(
            f"{self.client.base_url}/api/jobs/{self.job_id}/stream", stream=True
        )

        client = sseclient.SSEClient(r)
        for event in client.events():
            yield event.data

    def wait(self, poll_interval: int = 5) -> dict:
        """Wait for job completion."""
        while True:
            status = self.status()
            if status["status"] in ("completed", "failed", "cancelled"):
                return self.result()
            time.sleep(poll_interval)


# Convenience Functions For Quick Usage
def connect(
    host: str, username: str, password: str = "", base_url: str = "https://gateway.example.com"
) -> SSHGatewayClient:
    """Quick connect helper."""
    client = SSHGatewayClient(base_url)
    client.ssh_connect(host, username, password)
    return client


class quick:
    """One-shot helpers that connect, perform an operation, and disconnect.

    Usage:
        from sdk.ssh_gateway import quick

        # Run a command
        result = quick.run(
            host="192.168.1.100",
            username="root",
            password="secret",
            command="ls -la",
        )

        # Read a file
        content = quick.read(
            host="192.168.1.100",
            username="root",
            private_key=open("~/.ssh/id_rsa").read(),
            path="/etc/hostname",
        )
    """

    @staticmethod
    def run(
        host: str,
        username: str,
        command: str = "echo hello",
        password: str = "",
        private_key: str = "",
        port: int = 22,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Connect, execute a command, disconnect, return result.

        Always disconnects in finally block, even on error.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        try:
            client.ssh_connect(host, username, password=password, port=port, private_key=private_key)
            return client.execute(command)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    @staticmethod
    def read(
        host: str,
        username: str,
        path: str = "/etc/hostname",
        password: str = "",
        private_key: str = "",
        port: int = 22,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> str:
        """Connect, read a file, disconnect, return content.

        Always disconnects in finally block, even on error.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        try:
            client.ssh_connect(host, username, password=password, port=port, private_key=private_key)
            return client.read_file(path)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
