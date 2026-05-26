"""Multi-server management for SSH connections."""

import logging
import time
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ServerStatus(Enum):
    """Server connection status."""
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ServerConfig:
    """Server configuration."""
    id: str
    name: str
    host: str
    port: int = 22
    username: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    status: ServerStatus = ServerStatus.UNKNOWN
    last_check: Optional[float] = None
    session_id: Optional[str] = None


class ServerManager:
    """Manage multiple SSH servers."""

    # Predefined Servers — replace with your infrastructure
    DEFAULT_SERVERS = {
        "node-1": {
            "name": "Gateway Node",
            "host": "10.0.0.1",
            "port": 22,
            "username": "root",
            "description": "Primary gateway node",
            "tags": ["gateway"],
        },
        "node-2": {
            "name": "Application Server",
            "host": "10.0.0.2",
            "port": 22,
            "username": "root",
            "description": "Application server",
            "tags": ["app"],
        },
        "node-3": {
            "name": "Database Server",
            "host": "10.0.0.3",
            "port": 22,
            "username": "root",
            "description": "Database server",
            "tags": ["database"],
        },
        "node-4": {
            "name": "Worker Node",
            "host": "10.0.0.4",
            "port": 22,
            "username": "root",
            "description": "Background worker",
            "tags": ["worker"],
        },
    }

    def __init__(self):
        self._servers: dict[str, ServerConfig] = {}
        self._load_default_servers()

    def _load_default_servers(self):
        """Load predefined servers."""
        for server_id, config in self.DEFAULT_SERVERS.items():
            self._servers[server_id] = ServerConfig(
                id=server_id,
                **config
            )

    def add_server(
        self,
        server_id: str,
        name: str,
        host: str,
        port: int = 22,
        username: str = "",
        description: str = "",
        tags: list[str] = None,
    ) -> ServerConfig:
        """Add a new server."""
        server = ServerConfig(
            id=server_id,
            name=name,
            host=host,
            port=port,
            username=username,
            description=description,
            tags=tags or [],
        )
        self._servers[server_id] = server
        return server

    def get_server(self, server_id: str) -> Optional[ServerConfig]:
        """Get server by ID."""
        return self._servers.get(server_id)

    def list_servers(self) -> list[ServerConfig]:
        """List all servers."""
        return list(self._servers.values())

    def remove_server(self, server_id: str) -> bool:
        """Remove a server."""
        if server_id in self._servers:
            del self._servers[server_id]
            return True
        return False

    def update_server_status(
        self,
        server_id: str,
        status: ServerStatus,
        session_id: str = None,
    ):
        """Update server status."""
        server = self._servers.get(server_id)
        if server:
            server.status = status
            server.last_check = time.time()
            if session_id:
                server.session_id = session_id

    def get_servers_by_tag(self, tag: str) -> list[ServerConfig]:
        """Get servers by tag."""
        return [s for s in self._servers.values() if tag in s.tags]

    def to_dict(self, server: ServerConfig) -> dict:
        """Convert server to dictionary."""
        return {
            "id": server.id,
            "name": server.name,
            "host": server.host,
            "port": server.port,
            "username": server.username,
            "description": server.description,
            "tags": server.tags,
            "status": server.status.value,
            "last_check": server.last_check,
            "has_session": server.session_id is not None,
        }
