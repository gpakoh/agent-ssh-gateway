"""Multi-server management for SSH connections."""

import json
import logging
import threading
import time
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

from app.config import settings

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

    def __init__(self):
        self._servers: dict[str, ServerConfig] = {}
        self._lock = threading.Lock()
        self._load_default_servers()

    def _load_default_servers(self):
        """Load predefined servers from config (SERVER_DEFAULT_CONFIGS env var)."""
        raw = settings.server_default_configs or "{}"
        try:
            configs = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid SERVER_DEFAULT_CONFIGS, Skipping")
            return
        with self._lock:
            for server_id, cfg in configs.items():
                self._servers[server_id] = ServerConfig(
                    id=server_id,
                    name=cfg.get("name", server_id),
                    host=cfg.get("host", ""),
                    port=cfg.get("port", 22),
                    username=cfg.get("username", ""),
                    description=cfg.get("description", ""),
                    tags=cfg.get("tags", []),
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
        with self._lock:
            self._servers[server_id] = server
        return server

    def get_server(self, server_id: str) -> Optional[ServerConfig]:
        """Get server by ID."""
        with self._lock:
            return self._servers.get(server_id)

    def list_servers(self) -> list[ServerConfig]:
        """List all servers."""
        with self._lock:
            return list(self._servers.values())

    def remove_server(self, server_id: str) -> bool:
        """Remove a server."""
        with self._lock:
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
        with self._lock:
            server = self._servers.get(server_id)
            if server:
                server.status = status
                server.last_check = time.time()
                if session_id:
                    server.session_id = session_id

    def get_servers_by_tag(self, tag: str) -> list[ServerConfig]:
        """Get servers by tag."""
        with self._lock:
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
            "last_check": server.last_check or 0,
            "has_session": server.session_id is not None,
        }
