"""Central registry for all pattern packs.

Lazy-builds packs on first access (DCG-style PackEntry pattern).
"""

from __future__ import annotations

from app.packs import PackRegistry
from app.packs.backup import build_backup_pack
from app.packs.cloud import build_cloud_pack
from app.packs.database import build_database_pack
from app.packs.dns import build_dns_pack
from app.packs.docker import build_docker_pack
from app.packs.filesystem import build_filesystem_pack
from app.packs.firewall import build_firewall_pack
from app.packs.git_pack import build_git_pack
from app.packs.kubernetes import build_kubernetes_pack
from app.packs.loadbalancer import build_loadbalancer_pack
from app.packs.package_managers import build_package_managers_pack
from app.packs.secrets import build_secrets_pack
from app.packs.system import build_system_pack


def build_registry() -> PackRegistry:
    registry = PackRegistry()
    registry.register(build_backup_pack())
    registry.register(build_docker_pack())
    registry.register(build_filesystem_pack())
    registry.register(build_kubernetes_pack())
    registry.register(build_cloud_pack())
    registry.register(build_database_pack())
    registry.register(build_dns_pack())
    registry.register(build_git_pack())
    registry.register(build_firewall_pack())
    registry.register(build_loadbalancer_pack())
    registry.register(build_package_managers_pack())
    registry.register(build_secrets_pack())
    registry.register(build_system_pack())
    return registry


# Module-level singleton
_REGISTRY: PackRegistry | None = None


def get_registry() -> PackRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY
