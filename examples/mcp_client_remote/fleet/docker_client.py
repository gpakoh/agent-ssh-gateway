"""Docker subprocess wrapper for fleet MCP adapter.

Each tool builds its own argv list — never accepts a raw command string.
Read-only tools are in ps/images/inspect/logs/stats/compose_ps/compose_services.
Write tools added in Session 160: start/stop/restart/compose_up/restart/build/logs.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast

DOCKER_BIN = "/usr/bin/docker"
SUBPROCESS_TIMEOUT = 30.0
MAX_OUTPUT_BYTES = 50 * 1024


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int


CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
IMAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/{-]{0,255}$")
SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
COMPOSE_FILE_RE = re.compile(r"^[a-zA-Z0-9_/.-]{1,256}$")
ALLOWED_PRUNE_TYPES: set[str] = {"container", "image", "network"}
ALLOWED_ADMIN_PRUNE_TYPES: set[str] = {"volume", "system"}
ALLOWED_PRUNE_TYPES_ALL: set[str] = ALLOWED_PRUNE_TYPES | ALLOWED_ADMIN_PRUNE_TYPES

IMAGE_TAG_RE = re.compile(r"^[a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+$")
IMAGE_REF_RE = re.compile(r"^[a-zA-Z0-9._/-]+(:[a-zA-Z0-9._-]+)?$")
VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

EXEC_ARGV_DENYLIST: set[str] = {
    "env",
    "printenv",
    "/proc/self/environ",
    "/proc/1/environ",
    "/etc/shadow",
    "/etc/gshadow",
    "/root/.ssh",
    "/.ssh/id_",
}

SHELL_CMDS: set[str] = {"sh", "bash", "ash", "zsh"}

REDACTED = "<redacted>"

_SECRET_ENV_KEY_RE = re.compile(
    r"(?i)^\s*("
    r"\w*(?:PASSWORD|PASS|SECRET|TOKEN|KEY|CREDENTIAL)"
    r"|JWT|BEARER|AUTH|COOKIE|SESSION"
    r"|REFRESH[_-]?TOKEN|CLIENT[_-]?SECRET|WEBHOOK[_-]?SECRET"
    r"|DSN|CONNECTION[_-]?STRING|\w*(?:_URL|_URI|_DSN)"
    r")\s*="
)

_SECRET_DICT_KEY_RE = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASS|KEY|JWT|BEARER|AUTH|COOKIE|SESSION|"
    r"CREDENTIAL|REFRESH[_-]?TOKEN|CLIENT[_-]?SECRET|"
    r"WEBHOOK[_-]?SECRET|AUTHORIZATION|DSN|CONNECTION[_-]?STRING|_URL$|_URI$)"
)

# Matches a DSN-style embedded credential (scheme://user:PASSWORD@host) so the
# password is redacted even when it appears inside a value whose own key name
# doesn't look secret-ish (e.g. DATABASE_URL, REDIS_URL).
_DSN_CREDENTIAL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)")

# Values that carry host/CI topology but are not filesystem paths: URLs to
# CI/registry services, email addresses, and commit/revision SHAs embedded in
# image labels (org.opencontainers.image.*) leak infrastructure otherwise.
_LABEL_URL_VALUE_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_LABEL_EMAIL_VALUE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LABEL_SHA_VALUE_RE = re.compile(r"^([0-9a-f]{40}|sha256:[0-9a-f]{64})$", re.IGNORECASE)

# Strict allowlist for `docker inspect` output. Everything not listed here is
# dropped, so host topology (GraphDriver paths, ResolvConfPath/HostsPath/
# LogPath, PID, internal IPs/MACs, network/endpoint IDs, compose working
# dirs) can never leak through a field the sanitizer simply forgot about —
# a blacklist approach has to enumerate every leaky field, an allowlist
# only has to enumerate what an agent legitimately needs.
_INSPECT_TOP_LEVEL_ALLOW: frozenset[str] = frozenset(
    {
        "Id",
        "Name",
        "Created",
        "Image",
        "RestartCount",
        "Driver",
        "Platform",
        "State",
        "Config",
        "HostConfig",
        "Mounts",
        "NetworkSettings",
    }
)

_INSPECT_STATE_ALLOW: frozenset[str] = frozenset(
    {
        "Status",
        "Running",
        "ExitCode",
        "Error",
        "StartedAt",
        "FinishedAt",
        "Health",
    }
)
_INSPECT_HEALTH_ALLOW: frozenset[str] = frozenset({"Status", "FailingStreak"})

_INSPECT_CONFIG_ALLOW: frozenset[str] = frozenset(
    {
        "Image",
        "Cmd",
        "Entrypoint",
        "WorkingDir",
        "ExposedPorts",
        "User",
        "Tty",
        "OpenStdin",
        "StopSignal",
        "StopTimeout",
        "Labels",
    }
)

_INSPECT_HOSTCONFIG_ALLOW: frozenset[str] = frozenset(
    {
        "RestartPolicy",
        "NetworkMode",
        "PortBindings",
        "PublishAllPorts",
        "Privileged",
        "ReadonlyRootfs",
        "AutoRemove",
        "Init",
        "Runtime",
        "UsernsMode",
        "Memory",
        "NanoCpus",
        "CpuShares",
        "CpuQuota",
        "CpusetCpus",
        "MemorySwap",
        "MemoryReservation",
        "LogConfig",
        "Binds",
        "CapAdd",
        "CapDrop",
        "Devices",
    }
)

_INSPECT_MOUNT_ALLOW: frozenset[str] = frozenset(
    {
        "Type",
        "Name",
        "Destination",
        "RW",
        "Mode",
        "Propagation",
        "Source",
    }
)

# NetworkSettings.Networks carries NetworkID/EndpointID/IPAddress/MacAddress/
# Gateway per network — host topology. Only the published-port map survives.
_INSPECT_NETWORK_ALLOW: frozenset[str] = frozenset({"Ports"})


class DockerClient:
    """Read-only async subprocess wrapper for /usr/bin/docker."""

    def __init__(self) -> None:
        # Set by the row-list methods after _truncate_rows: True when the
        # returned rows were cut short of the full set. The tool wrappers
        # read it back into meta.truncated.
        self.last_truncated: bool = False
        # Set by the sanitizing row methods (ps/compose_ps): True when at
        # least one returned row actually had a host path redacted. The
        # tool wrappers read it back into meta.redacted.
        self.last_redacted: bool = False

    async def _run(
        self,
        argv: list[str],
        timeout: float = SUBPROCESS_TIMEOUT,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Command timed out after {timeout}s: {shlex.join(argv)}") from None

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"docker exited {proc.returncode}: {err}")

        result = stdout.decode("utf-8", errors="replace")
        if len(result) > MAX_OUTPUT_BYTES:
            result = result[:MAX_OUTPUT_BYTES] + "\n[output truncated]"
        return result

    async def _run_with_result(
        self,
        argv: list[str],
        timeout: float = SUBPROCESS_TIMEOUT,
    ) -> RunResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
            )

        exit_code = proc.returncode or 0
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if len(out) > MAX_OUTPUT_BYTES:
            out = out[:MAX_OUTPUT_BYTES] + "\n[output truncated]"
        return RunResult(stdout=out, stderr=err, exit_code=exit_code)

    def _validate_container_name(self, name: str) -> str:
        if not CONTAINER_NAME_RE.match(name):
            raise ValueError(f"Invalid container name: {shlex.quote(name)}")
        return name

    def _validate_image_name(self, name: str) -> str:
        if not IMAGE_NAME_RE.match(name):
            raise ValueError(f"Invalid image name: {shlex.quote(name)}")
        return name

    def _validate_service_name(self, name: str) -> str:
        if not SERVICE_NAME_RE.match(name):
            raise ValueError(f"Invalid service name: {shlex.quote(name)}")
        return name

    def _validate_project_dir(self, project_dir: str | None) -> None:
        """Validate that project_dir exists and is under an allowed root."""
        if project_dir is None:
            return
        resolved = Path(project_dir).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Project directory does not exist: {shlex.quote(project_dir)}")
        from examples.mcp_server.config import ALLOWED_PROJECT_ROOTS

        for root in ALLOWED_PROJECT_ROOTS:
            try:
                resolved.relative_to(Path(root).resolve())
                return
            except ValueError:
                continue
        # Deliberately does not list ALLOWED_PROJECT_ROOTS -- confirmed
        # live that doing so turned a routine validation error (e.g.
        # docker_compose_ps(project_dir="/tmp")) into a topology oracle,
        # handing any caller the real host directories (/media/1TB/Python/,
        # /var/www/) without needing any other access at all.
        raise ValueError(f"Project directory {shlex.quote(project_dir)} is outside allowed roots")

    def _validate_prune_type(self, type: str, admin_scope: bool = False) -> str:
        allowed = ALLOWED_PRUNE_TYPES_ALL if admin_scope else ALLOWED_PRUNE_TYPES
        if type not in allowed:
            raise ValueError(f"Unsupported prune type '{type}'. Allowed: {sorted(allowed)}")
        return type

    def _validate_image_tag(self, name: str) -> str:
        if not IMAGE_TAG_RE.match(name):
            raise ValueError(f"Invalid image reference (tag required): {shlex.quote(name)}")
        return name

    def _validate_image_ref(self, name: str) -> str:
        if not IMAGE_REF_RE.match(name):
            raise ValueError(f"Invalid image reference: {shlex.quote(name)}")
        return name

    def _validate_volume_name(self, name: str) -> str:
        if not VOLUME_NAME_RE.match(name):
            raise ValueError(f"Invalid volume name: {shlex.quote(name)}")
        return name

    def _validate_exec_argv(self, argv: list[str]) -> None:
        if not isinstance(argv, list) or not argv:
            raise ValueError("command must be a non-empty array of strings")
        for el in argv:
            if not isinstance(el, str) or not el:
                raise ValueError("each argv element must be a non-empty string")
            if not el.isprintable() or not el.isascii():
                raise ValueError(f"non-printable/non-ASCII argv element: {shlex.quote(el)}")
            # denylist check (case-sensitive exact or substring)
            for blocked in EXEC_ARGV_DENYLIST:
                if blocked in el:
                    raise ValueError(
                        f"argv element contains blocked pattern: {shlex.quote(blocked)}"
                    )
        # shell launcher check
        if len(argv) >= 2 and argv[0] in SHELL_CMDS and argv[1] == "-c":
            raise ValueError(f"shell launcher blocked: {shlex.quote(argv[0])} -c")

    @staticmethod
    def _parse_json_lines(output: str) -> list[dict]:
        """Parse `docker ... --format json` output: one JSON object per
        line (JSON Lines), not a single JSON array. A line that fails to
        parse is skipped rather than raising -- a single malformed row
        (e.g. a warning line docker sometimes interleaves on stdout)
        should not blow up the whole listing."""
        rows: list[dict] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    @staticmethod
    def _truncate_rows(rows: list[dict], limit: int) -> tuple[list[dict], int]:
        """Truncate a structured row list to *limit* entries.

        Returns (truncated_rows, total_count) so the caller can report
        both how many rows are returned and how many exist in total.
        """
        total = len(rows)
        if limit <= 0:
            return [], total
        return rows[:limit], total

    async def ps(
        self,
        all: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """List containers as structured rows (docker's native --format json,
        one object per line). Arbitrary Go-template output is deliberately
        not accepted: a raw template like ``{{.Labels}}`` would return
        unredacted host paths and bypass row sanitization entirely. Host
        paths inside Labels/Mounts are redacted; when *limit* cuts the list
        short, `self.last_truncated` is set so the wrapper can report it."""
        argv = [DOCKER_BIN, "ps"]
        if all:
            argv.append("--all")
        argv.extend(["--format", "json"])
        result = await self._run(argv)
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = total > len(rows)
        sanitized = [self._sanitize_ps_row(r) for r in rows]
        self.last_redacted = sanitized != rows
        return sanitized

    async def images(
        self,
        limit: int = 50,
    ) -> list[dict]:
        argv = [DOCKER_BIN, "images"]
        argv.extend(["--format", "json"])
        result = await self._run(argv)
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = total > len(rows)
        return rows

    async def inspect(
        self,
        name: str,
        max_lines: int | None = 500,
    ) -> list[dict] | dict:
        """Inspect a container, returning the sanitized structure directly
        (docker inspect already prints JSON; this used to re-serialize it
        back to a string for no reason). `max_lines` is now interpreted as
        a cap on the number of top-level entries (docker inspect returns a
        list even for a single name) rather than text lines, since there
        is no longer a string to truncate."""
        self._validate_container_name(name)
        argv = [DOCKER_BIN, "inspect", name]
        result = await self._run(argv)
        data = self._sanitize_inspect_output(result)
        self.last_truncated = bool(
            max_lines and isinstance(data, list) and len(data) > max_lines
        )
        if max_lines and isinstance(data, list) and len(data) > max_lines:
            return data[:max_lines]
        return data

    def _sanitize_inspect_output(self, raw: str) -> list | dict:
        """Parse docker inspect JSON and reduce it to a strict allowlist.

        The raw structure carries host topology the sanitizer has no
        business exposing: GraphDriver overlay paths, ResolvConfPath/
        HostnamePath/HostsPath/LogPath, State.Pid, NetworkSettings
        IPs/MACs/NetworkIDs/EndpointIDs and compose working-dir labels.
        Rather than blacklisting each known leaky field, every top-level
        and nested section keeps only the explicitly allowed keys and
        drops the rest. Remaining values still pass through the generic
        key-level secret redaction as defense-in-depth.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        data = self._apply_inspect_allowlist(data)
        # _sanitize_value(x: object) -> object is intentionally generic for
        # its own recursion (it walks into arbitrary nested values), but at
        # this top level `data` is always the list/dict json.loads() just
        # produced, and _sanitize_value preserves dict/list-ness for
        # dict/list inputs -- cast narrows what mypy can't infer structurally.
        return cast("list | dict", self._sanitize_value(data))

    @staticmethod
    def _subset(value: object, allowed: frozenset[str]) -> dict[str, object]:
        """Keep only *allowed* keys of a dict; non-dicts come back empty."""
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if k in allowed}

    @staticmethod
    def _redact_path_labels(labels: dict[str, object]) -> dict[str, object]:
        """Redact label values that are absolute host paths.

        Compose stamps labels like com.docker.compose.project.config_files
        and com.docker.compose.project.working_dir with absolute host
        paths; keep the label (its name is useful) but hide the path.
        """
        return {
            k: (REDACTED if isinstance(v, str) and v.startswith("/") else v)
            for k, v in labels.items()
        }

    def _allowlist_entry(self, entry: dict) -> dict:
        """Reduce one docker inspect container entry to the allowlist."""
        out: dict = {}
        for key in _INSPECT_TOP_LEVEL_ALLOW:
            if key not in entry:
                continue
            value = entry[key]
            if key == "State":
                state = self._subset(value, _INSPECT_STATE_ALLOW)
                health = state.get("Health")
                if isinstance(health, dict):
                    state["Health"] = self._subset(health, _INSPECT_HEALTH_ALLOW)
                out["State"] = state
            elif key == "Config":
                config = self._subset(value, _INSPECT_CONFIG_ALLOW)
                labels = config.get("Labels")
                if isinstance(labels, dict):
                    config["Labels"] = self._redact_path_labels(labels)
                out["Config"] = config
            elif key == "HostConfig":
                host_config = self._subset(value, _INSPECT_HOSTCONFIG_ALLOW)
                if "Binds" in host_config:
                    host_config["Binds"] = [REDACTED]
                out["HostConfig"] = host_config
            elif key == "Mounts":
                mounts = value if isinstance(value, list) else []
                out["Mounts"] = [self._allowlist_mount(m) for m in mounts if isinstance(m, dict)]
            elif key == "NetworkSettings":
                out["NetworkSettings"] = self._subset(value, _INSPECT_NETWORK_ALLOW)
            else:
                out[key] = value
        return out

    def _allowlist_mount(self, mount: dict) -> dict:
        """Keep mount metadata but redact the host-side Source path."""
        out = self._subset(mount, _INSPECT_MOUNT_ALLOW)
        if "Source" in out:
            out["Source"] = REDACTED
        return out

    def _apply_inspect_allowlist(self, data: object) -> object:
        """Apply the strict allowlist to a full inspect result (list or single dict)."""
        entries = data if isinstance(data, list) else [data]
        cleaned = [self._allowlist_entry(e) for e in entries if isinstance(e, dict)]
        if isinstance(data, list):
            return cleaned
        return cleaned[0] if cleaned else {}

    @staticmethod
    def _sanitize_labels_string(labels: str) -> str:
        """Redact absolute-path values inside a docker ps Labels string.

        `docker ps --format json` renders Labels as one comma-separated
        "k=v" string; compose values like project.config_files=/media/...
        would otherwise leak host paths. A single value can itself contain
        commas (multiple compose files, e.g.
        config_files=/a.yml,/b.yml) — the continuation chunks carry no
        '=' and must be redacted together with the leading path, not
        passed through as-is.
        """
        parts: list[str] = []
        prev_was_path = False
        for chunk in labels.split(","):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                if (
                    value.startswith("/")
                    or _LABEL_URL_VALUE_RE.match(value)
                    or _LABEL_EMAIL_VALUE_RE.match(value)
                    or _LABEL_SHA_VALUE_RE.match(value)
                ):
                    parts.append(f"{key}={REDACTED}")
                    prev_was_path = True
                else:
                    parts.append(chunk)
                    prev_was_path = False
            elif prev_was_path:
                parts.append(REDACTED)
            else:
                parts.append(chunk)
        return ",".join(parts)

    @staticmethod
    def _sanitize_ps_row(row: dict) -> dict:
        """Reduce one `docker ps --format json` row to safe fields.

        Drops nothing structural but redacts host paths that ride along in
        the Labels string (compose config_files/working_dir) and bind-mount
        sources in the Mounts string — the same topology leak the inspect
        allowlist kills structurally.
        """
        out = dict(row)
        labels = out.get("Labels")
        if isinstance(labels, str) and labels:
            out["Labels"] = DockerClient._sanitize_labels_string(labels)
        mounts = out.get("Mounts")
        if isinstance(mounts, str) and mounts:
            # `docker ps --format json` renders Mounts as one comma-
            # separated list mixing named volumes (no leading "/") and
            # bind-mount host paths (leading "/") in arbitrary order --
            # checking mounts.startswith("/") on the whole string only
            # caught the case where a path happened to sort first.
            # Confirmed live on this exact host: "web-ssh-gateway_...,
            # /deploy/web-ssh-gateway,..." left the real host path
            # unredacted because a named volume came first. Redact each
            # path-looking chunk individually instead.
            out["Mounts"] = ",".join(
                REDACTED if chunk.startswith("/") else chunk
                for chunk in mounts.split(",")
            )
        return out

    def _sanitize_value(self, value: object) -> object:
        """Recursively sanitize a JSON value, redacting secrets."""
        if isinstance(value, str):
            return self._sanitize_string(value)
        if isinstance(value, dict):
            return {
                k: REDACTED if self._is_sensitive_key(k) else self._sanitize_value(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        return value

    def _sanitize_string(self, s: str) -> str:
        """Redact secret-like values in a string.

        Handles 'KEY=value' env format (whole value redacted when the key
        name looks secret-ish), and separately redacts any DSN-style
        embedded credential (scheme://user:PASSWORD@host) regardless of
        whether the surrounding key name matched — e.g. a DATABASE_URL or
        REDIS_URL value that embeds a real password.
        """
        m = _SECRET_ENV_KEY_RE.match(s)
        if m:
            key_part = s[: m.end() - 1]
            return f"{key_part}={REDACTED}"
        return _DSN_CREDENTIAL_RE.sub(rf"\1{REDACTED}\3", s)

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return bool(_SECRET_DICT_KEY_RE.search(key))

    async def logs(
        self,
        container: str,
        tail: int = 200,
    ) -> dict:
        """Fetch recent log lines as a structured {"lines": [...], "count": N}
        instead of one giant text blob -- log text itself has no schema, but
        splitting it into an array at least gives a caller a stable shape to
        iterate instead of a string to further parse."""
        self._validate_container_name(container)
        tail = max(1, min(tail, 1000))
        argv = [DOCKER_BIN, "logs", "--tail", str(tail), container]
        result = await self._run(argv)
        # Container log lines are one of the most likely places for an
        # application to have accidentally printed a real secret (an
        # Authorization header, a DSN with an embedded password, ...) --
        # reuse the same DSN/KEY=value redaction docker_inspect already
        # applies, rather than returning raw log text unredacted.
        lines = [self._sanitize_string(line) for line in result.splitlines()]
        return {"lines": lines, "count": len(lines)}

    async def stats(
        self,
        limit: int = 50,
    ) -> list[dict]:
        argv = [DOCKER_BIN, "stats", "--no-stream"]
        # --no-stream still has to sample live cgroup counters for every
        # running container once before returning — on a host running
        # dozens of containers this can occasionally run past the default
        # 30s SUBPROCESS_TIMEOUT under load. Same 60s budget as compose_ps,
        # the other "enumerate everything" read.
        argv.extend(["--format", "json"])
        result = await self._run(argv, timeout=60.0)
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = total > len(rows)
        return rows

    async def compose_ps(
        self,
        project_dir: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        self._validate_project_dir(project_dir)
        argv = self._compose_base_argv(project_dir)
        argv.append("ps")
        argv.extend(["--format", "json"])
        result = await self._run(argv, timeout=60.0)
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = total > len(rows)
        sanitized = [self._sanitize_ps_row(r) for r in rows]
        self.last_redacted = sanitized != rows
        return sanitized

    async def compose_services(
        self,
        project_dir: str | None = None,
    ) -> dict:
        self._validate_project_dir(project_dir)
        argv = self._compose_base_argv(project_dir)
        argv.extend(["config", "--services"])
        result = await self._run(argv, timeout=60.0)
        services = [line.strip() for line in result.splitlines() if line.strip()]
        return {"services": services, "count": len(services)}

    # ── Write operations (Session 160) ──────────────────────────────

    async def start(self, container: str, timeout: int | None = None) -> str:
        """Start a stopped container."""
        self._validate_container_name(container)
        argv = [DOCKER_BIN, "start", container]
        return await self._run(argv, timeout=float(timeout or SUBPROCESS_TIMEOUT))

    @staticmethod
    def _stop_argv(container: str, timeout: int = 10) -> list[str]:
        """Build argv for docker stop (exposed for testing)."""
        timeout = max(1, min(timeout, 120))
        return [DOCKER_BIN, "stop", "--time", str(timeout), container]

    async def stop(self, container: str, timeout: int = 10) -> str:
        """Stop a running container. timeout: sec before force kill (1-120)."""
        self._validate_container_name(container)
        return await self._run(self._stop_argv(container, timeout))

    @staticmethod
    def _restart_argv(container: str, timeout: int = 10) -> list[str]:
        """Build argv for docker restart (exposed for testing)."""
        timeout = max(1, min(timeout, 120))
        return [DOCKER_BIN, "restart", "--time", str(timeout), container]

    async def restart(self, container: str, timeout: int = 10) -> str:
        """Restart a container. timeout: sec before force kill (1-120)."""
        self._validate_container_name(container)
        return await self._run(self._restart_argv(container, timeout))

    # ── Compose write operations (Session 160) ─────────────────────

    def _compose_base_argv(self, project_dir: str | None = None) -> list[str]:
        argv = [DOCKER_BIN, "compose"]
        if project_dir:
            argv.extend(["--project-directory", project_dir])
        return argv

    async def compose_up(
        self,
        project_dir: str | None = None,
        services: list[str] | None = None,
        detach: bool = True,
        build: bool = False,
        timeout: int = 120,
    ) -> str:
        """Start services. detach=True by default; set build=True to rebuild."""
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 900))
        argv = self._compose_base_argv(project_dir)
        argv.append("up")
        if detach:
            argv.append("--detach")
        if build:
            argv.append("--build")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        return await self._run(argv, timeout=float(timeout))

    async def compose_restart(
        self,
        project_dir: str | None = None,
        services: list[str] | None = None,
        timeout: int = 30,
    ) -> str:
        """Restart services in a compose project."""
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 300))
        argv = self._compose_base_argv(project_dir)
        argv.append("restart")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        return await self._run(argv, timeout=float(timeout))

    async def compose_build(
        self,
        project_dir: str | None = None,
        services: list[str] | None = None,
        no_cache: bool = False,
        timeout: int = 300,
    ) -> str:
        """Build (or rebuild) services. no_cache=True to ignore cache."""
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 1800))
        argv = self._compose_base_argv(project_dir)
        argv.append("build")
        if no_cache:
            argv.append("--no-cache")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        return await self._run(argv, timeout=float(timeout))

    async def compose_logs(
        self,
        project_dir: str | None = None,
        services: list[str] | None = None,
        tail: int = 100,
        follow: bool = False,
        timestamps: bool = False,
        timeout: int = 30,
    ) -> dict:
        """Fetch logs from compose services. tail: 1-1000 lines."""
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 300))
        argv = self._compose_base_argv(project_dir)
        argv.append("logs")
        tail = max(1, min(tail, 1000))
        argv.extend(["--tail", str(tail)])
        if follow:
            argv.append("--follow")
        if timestamps:
            argv.append("--timestamps")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        result = await self._run(argv, timeout=float(timeout))
        lines = [self._sanitize_string(line) for line in result.splitlines()]
        return {"lines": lines, "count": len(lines)}

    async def rm(self, container: str, force: bool = False) -> RunResult:
        self._validate_container_name(container)
        argv = [DOCKER_BIN, "rm"]
        if force:
            argv.append("-f")
        argv.append(container)
        return await self._run_with_result(argv)

    async def compose_down(
        self,
        project_dir: str | None = None,
        remove_orphans: bool = False,
        timeout: int = 30,
        volumes: bool = False,
    ) -> RunResult:
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 300))
        argv = self._compose_base_argv(project_dir)
        argv.append("down")
        if remove_orphans:
            argv.append("--remove-orphans")
        if volumes:
            argv.append("--volumes")
        argv.extend(["-t", str(timeout)])
        return await self._run_with_result(argv, timeout=float(timeout) + 10)

    async def exec(
        self,
        container: str,
        command: list[str],
        timeout: int = 30,
    ) -> RunResult:
        self._validate_container_name(container)
        self._validate_exec_argv(command)
        timeout = max(1, min(timeout, 300))
        argv = [DOCKER_BIN, "exec", container] + command
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run(
        self,
        image: str,
        command: list[str],
        container_name: str | None = None,
        timeout: int = 60,
    ) -> RunResult:
        self._validate_image_tag(image)
        timeout = max(1, min(timeout, 600))
        argv = [DOCKER_BIN, "run", "--rm"]
        if container_name:
            self._validate_container_name(container_name)
            argv.extend(["--name", container_name])
        argv.append(image)
        argv.extend(command)
        return await self._run_with_result(argv, timeout=float(timeout))

    async def rmi(self, images: list[str]) -> RunResult:
        if not images or len(images) > 5:
            raise ValueError("rmi accepts 1-5 images")
        for img in images:
            self._validate_image_ref(img)
        argv = [DOCKER_BIN, "rmi"] + images
        return await self._run_with_result(argv)

    async def volume_rm(self, volumes: list[str]) -> RunResult:
        if not volumes or len(volumes) > 5:
            raise ValueError("volume_rm accepts 1-5 volumes")
        for vol in volumes:
            self._validate_volume_name(vol)
        argv = [DOCKER_BIN, "volume", "rm"] + volumes
        return await self._run_with_result(argv)

    async def prune(self, type: str = "container") -> RunResult:
        self._validate_prune_type(type)
        argv = [DOCKER_BIN, type, "prune", "-f"]
        return await self._run_with_result(argv)
