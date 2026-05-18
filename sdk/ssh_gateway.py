"""SSH Gateway Python SDK.

Usage:
    from ssh_gateway import SSHGatewayClient
    
    client = SSHGatewayClient("https://ssh.xloud.ru")
    client.auth("agent", "NpvSBhi7ag1X8stB3kFMnCcxM9PKE9R")
    
    # Connect and persist session
    session = client.ssh_connect("192.168.1.103", username="root", password="...")
    
    # Execute command
    result = client.execute("ls -la")
    
    # Batch read files
    files = client.batch_read(["app/main.py", "app/config.py"])
    
    # Edit file
    client.edit_file("app/main.py", [
        {"type": "replace", "old": "def old():", "new": "def new():"}
    ])
    
    # Background job
    job = client.run_background("pytest", timeout=300)
    for log in job.stream_logs():
        print(log)
"""

import json
import time
import requests
import urllib3
from typing import Optional, Iterator
from dataclasses import dataclass

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
    
    def __init__(self, base_url: str = "https://ssh.xloud.ru"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.verify = False
        self._ssh_session: Optional[SSHSession] = None
        self._context_id: Optional[str] = None
        
    def auth(self, username: str, password: str) -> bool:
        """Authenticate with Authelia."""
        r = self.session.post(
            f"{self.base_url}/api/firstfactor",
            json={
                "username": username,
                "password": password,
                "request_method": "GET",
                "request_uri": self.base_url
            }
        )
        return r.status_code == 200
    
    def ssh_connect(self, host: str, username: str, password: str = "", 
                    port: int = 22, private_key: str = "") -> SSHSession:
        """Connect to SSH server with auto-reconnect support."""
        r = self.session.post(
            f"{self.base_url}/api/ssh/connect",
            json={
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "private_key": private_key
            }
        )
        r.raise_for_status()
        data = r.json()
        
        self._ssh_session = SSHSession(
            session_id=data["session_id"],
            host=host,
            username=username,
            port=port
        )
        
        # Start heartbeat thread
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
                        json={"session_id": self._ssh_session.session_id}
                    )
                except:
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
                "timeout": timeout
            }
        )
        r.raise_for_status()
        return r.json()
    
    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        """Read file content."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        # Use raw endpoint for better performance
        r = self.session.get(
            f"{self.base_url}/api/file/raw",
            params={
                "session_id": self._ssh_session.session_id,
                "path": path,
                "offset": offset,
                "limit": limit
            }
        )
        r.raise_for_status()
        return r.text
    
    def batch_read(self, paths: list[str]) -> dict[str, str]:
        """Read multiple files in one request."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        r = self.session.post(
            f"{self.base_url}/api/batch/read",
            json={
                "session_id": self._ssh_session.session_id,
                "paths": paths
            }
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
                "operations": operations
            }
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
                "timeout": timeout
            }
        )
        r.raise_for_status()
        data = r.json()
        
        return BackgroundJob(self, data["job_id"])
    
    def create_context(self, name: str, path: str, branch: str = "",
                      auto_commit: bool = True, auto_validate: bool = True) -> str:
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
                "auto_validate": auto_validate
            }
        )
        r.raise_for_status()
        data = r.json()
        self._context_id = data["context_id"]
        return self._context_id
    
    def context_edit(self, path: str, operations: list[dict], 
                     commit_message: str = "", run_validation: bool = False) -> dict:
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
                "run_validation": run_validation
            }
        )
        r.raise_for_status()
        return r.json()
    
    def upload_file(self, local_path: str, remote_path: str) -> dict:
        """Upload file to remote server (base64)."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        with open(local_path, 'rb') as f:
            content = f.read()
        
        import base64
        encoded = base64.b64encode(content).decode('ascii')
        
        r = self.session.post(
            f"{self.base_url}/api/file/upload",
            params={
                "session_id": self._ssh_session.session_id,
                "path": remote_path,
                "content": encoded
            }
        )
        r.raise_for_status()
        return r.json()
    
    def upload_file_stream(self, local_path: str, remote_path: str) -> dict:
        """Upload file using multipart/form-data for large files."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        with open(local_path, 'rb') as f:
            files = {'file': (local_path.split('/')[-1], f, 'application/octet-stream')}
            r = self.session.post(
                f"{self.base_url}/api/file/upload/stream",
                params={
                    "session_id": self._ssh_session.session_id,
                    "path": remote_path
                },
                files=files
            )
        r.raise_for_status()
        return r.json()
    
    def download_file(self, remote_path: str, local_path: str):
        """Download file from remote server."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        r = self.session.get(
            f"{self.base_url}/api/file/download",
            params={
                "session_id": self._ssh_session.session_id,
                "path": remote_path
            }
        )
        r.raise_for_status()
        
        with open(local_path, 'wb') as f:
            f.write(r.content)
    
    def project_structure(self, path: str, include_git_status: bool = True, max_depth: int = 3) -> dict:
        """Get project structure with metadata."""
        if not self._ssh_session:
            raise RuntimeError("Not connected. Call ssh_connect() first.")
        
        r = self.session.post(
            f"{self.base_url}/api/project/structure",
            json={
                "session_id": self._ssh_session.session_id,
                "path": path,
                "include_git_status": include_git_status,
                "max_depth": max_depth
            }
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
                "commit_message": commit_message
            }
        )
        r.raise_for_status()
        return r.json()
    
    def disconnect(self):
        """Close SSH session."""
        if self._ssh_session:
            try:
                self.session.post(
                    f"{self.base_url}/api/ssh/disconnect",
                    json={"session_id": self._ssh_session.session_id}
                )
            except:
                pass
            self._ssh_session = None


class BackgroundJob:
    """Background job with streaming logs."""
    
    def __init__(self, client: SSHGatewayClient, job_id: str):
        self.client = client
        self.job_id = job_id
    
    def status(self) -> dict:
        """Get job status."""
        r = self.client.session.get(
            f"{self.client.base_url}/api/jobs/{self.job_id}/status"
        )
        r.raise_for_status()
        return r.json()
    
    def result(self) -> dict:
        """Get job result."""
        r = self.client.session.get(
            f"{self.client.base_url}/api/jobs/{self.job_id}/result"
        )
        r.raise_for_status()
        return r.json()
    
    def stream_logs(self) -> Iterator[str]:
        """Stream job logs via SSE."""
        import sseclient
        
        r = self.client.session.get(
            f"{self.client.base_url}/api/jobs/{self.job_id}/stream",
            stream=True
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


# Convenience functions for quick usage
def connect(host: str, username: str, password: str = "", 
            base_url: str = "https://ssh.xloud.ru") -> SSHGatewayClient:
    """Quick connect helper."""
    client = SSHGatewayClient(base_url)
    client.ssh_connect(host, username, password)
    return client
