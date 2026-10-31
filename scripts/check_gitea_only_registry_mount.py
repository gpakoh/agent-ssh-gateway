"""Read-only predeploy contract for explicit coordination repo bind mounts.

CI uses a fresh Git checkout, but Compose mounts an operator-owned host
projects.yaml over the image's /app/projects.yaml. A healthy image can
therefore serve an obsolete project catalog. Never auto-edit the host file.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_MAX_CONFIG_BYTES = 1_000_000
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RegistryDrift(ValueError):
    """Expected coordination repository binding is not present on the host."""


class _UniqueKeysLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueKeysLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise RegistryDrift("invalid or duplicate registry key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueKeysLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _projects(raw: bytes) -> dict[str, dict[str, Any]]:
    if len(raw) > _MAX_CONFIG_BYTES:
        raise RegistryDrift("registry config exceeds size limit")
    try:
        doc = yaml.load(raw, Loader=_UniqueKeysLoader)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise RegistryDrift("invalid registry YAML") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), dict):
        raise RegistryDrift("registry projects mapping is invalid")
    entries = doc["projects"]
    if any(not isinstance(k, str) or not isinstance(v, dict) for k, v in entries.items()):
        raise RegistryDrift("registry contains malformed project")
    return entries


def compare_gitea_only_bindings(expected: bytes, mounted: bytes) -> list[str]:
    """Return IDs with missing/changed explicit coordination repo contracts; no secrets.

    Compare only the source-controlled Gitea-only contracts. Unrelated host
    project entries may differ independently and must never be overwritten.
    """
    source_projects = _projects(expected)
    host_projects = _projects(mounted)
    source_only = {
        key: cfg for key, cfg in source_projects.items()
        if cfg.get("type") == "coordination-only"
    }
    host_only = {
        key: cfg for key, cfg in host_projects.items()
        if cfg.get("type") == "coordination-only"
    }
    changed: list[str] = []
    for project_id in sorted(source_only.keys() | host_only.keys()):
        want = source_only.get(project_id)
        have = host_only.get(project_id)
        if want is None or have is None:
            changed.append(project_id)
            continue
        repo = want.get("gitea_repo")
        if (
            not isinstance(repo, str)
            or not _REPO.fullmatch(repo)
            or repo.rsplit("/", 1)[-1] != project_id
            or "root" in want
            or "parent" in want
            or have.get("gitea_repo") != repo
            or "root" in have
            or "parent" in have
        ):
            changed.append(project_id)
    # Source projects may also carry an explicit, case-sensitive Gitea repo
    # different from their registry project_id. The host bind mount must
    # preserve that pin; otherwise a green deploy would still route to a
    # nonexistent lower-case repository. Only compare declared pins, not
    # unrelated operator-owned roots, descriptions or tags.
    source_pinned = {
        key: cfg for key, cfg in source_projects.items()
        if cfg.get("type") != "coordination-only" and "gitea_repo" in cfg
    }
    host_pinned = {
        key: cfg for key, cfg in host_projects.items()
        if cfg.get("type") != "coordination-only" and "gitea_repo" in cfg
    }
    for project_id in sorted(source_pinned.keys() | host_pinned.keys()):
        want = source_pinned.get(project_id)
        have = host_pinned.get(project_id)
        if want is None or have is None:
            changed.append(project_id)
            continue
        repo = want.get("gitea_repo")
        if (
            not isinstance(repo, str)
            or not _REPO.fullmatch(repo)
            or repo.split("/", 1)[0] != os.environ.get("MCP_COORD_GITEA_OWNER", "gpakoh").strip()
            or have.get("gitea_repo") != repo
            or have.get("type") != want.get("type")
        ):
            changed.append(project_id)
    return sorted(set(changed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a mounted Gitea-only registry without writing")
    parser.add_argument("--mounted", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source = sys.stdin.buffer.read(_MAX_CONFIG_BYTES + 1)
        with args.mounted.open("rb") as mounted_file:
            mounted = mounted_file.read(_MAX_CONFIG_BYTES + 1)
        drift = compare_gitea_only_bindings(source, mounted)
    except (OSError, RegistryDrift) as exc:
        # Never echo a YAML value or host path from an untrusted file.
        print(f"REGISTRY_CONFIG_DRIFT: invalid or unreadable registry ({type(exc).__name__})", file=sys.stderr)
        return 2
    if drift:
        ids = ",".join(drift[:20])
        print(
            f"REGISTRY_CONFIG_DRIFT: coordination repository bindings mismatch: {ids}; "
            "check PROJECTS_YAML_PATH and reconcile it with source-controlled "
            "projects.yaml using an operator-approved exact-SHA update.",
            file=sys.stderr,
        )
        return 3
    print("Coordination repository registry bind contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
