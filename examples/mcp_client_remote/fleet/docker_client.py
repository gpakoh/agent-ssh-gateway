"""Docker subprocess wrapper for fleet MCP adapter.

Each tool builds its own argv list — never accepts a raw command string.
Read-only tools are in ps/images/inspect/logs/stats/compose_ps/compose_services.
Write tools added in Session 160: start/stop/restart/compose_up/restart/build/logs.
"""

from __future__ import annotations

import asyncio
import json
import os
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
NETWORK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

EXEC_ARGV_COMMAND_DENYLIST: set[str] = {"env", "printenv"}
EXEC_ARGV_PATTERN_DENYLIST: set[str] = {
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

_DOCKER_PS_HEALTH_RE = re.compile(
    r"\((healthy|unhealthy|health: starting|starting)\)$",
    re.IGNORECASE,
)

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
        # `_run()` applies a fixed output cap after subprocess completion.
        # Structured list methods must carry that truncation into
        # `last_truncated`; otherwise a partial JSONL inventory can be
        # misreported as complete when the row limit itself was not reached.
        self.last_output_truncated: bool = False

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
        self.last_output_truncated = len(result) > MAX_OUTPUT_BYTES
        if self.last_output_truncated:
            result = result[:MAX_OUTPUT_BYTES] + "\n[output truncated]"
        return result

    async def _run_with_result(
        self,
        argv: list[str],
        timeout: float = SUBPROCESS_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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
        # handing any caller the real host directories without needing
        # any other access at all.
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

    def _validate_network_name(self, name: str) -> str:
        if not NETWORK_NAME_RE.match(name):
            raise ValueError(f"Invalid network name: {shlex.quote(name)}")
        return name

    def _validate_exec_argv(self, argv: list[str]) -> None:
        if not isinstance(argv, list) or not argv:
            raise ValueError("command must be a non-empty array of strings")
        for index, el in enumerate(argv):
            if not isinstance(el, str) or not el:
                raise ValueError("each argv element must be a non-empty string")
            if not el.isprintable() or not el.isascii():
                raise ValueError(f"non-printable/non-ASCII argv element: {shlex.quote(el)}")
            # Deny the executable name by exact basename, but keep sensitive
            # path patterns as substring checks across every argv element.
            # This avoids blocking safe data arguments such as branch names
            # containing "env" while still refusing real env-dump commands
            # and known sensitive paths.
            basename = el.rsplit("/", 1)[-1]
            if index == 0 and basename in EXEC_ARGV_COMMAND_DENYLIST:
                raise ValueError(
                    f"argv element contains blocked pattern: {shlex.quote(basename)}"
                )
            for blocked in EXEC_ARGV_PATTERN_DENYLIST:
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
        output_truncated = self.last_output_truncated
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = output_truncated or total > len(rows)
        redacted = [self._redact_ps_row(r) for r in rows]
        self.last_redacted = redacted != rows
        return [self._annotate_ps_health(r) for r in redacted]

    async def images(
        self,
        limit: int = 50,
    ) -> list[dict]:
        argv = [DOCKER_BIN, "images"]
        argv.extend(["--format", "json"])
        result = await self._run(argv)
        output_truncated = self.last_output_truncated
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = output_truncated or total > len(rows)
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
    def _annotate_ps_health(row: dict) -> dict:
        """Add a derived health flag to a sanitized docker-ps row.

        Docker's ps output only appends ``(healthy)`` / ``(unhealthy)`` when a
        container defines a Docker HEALTHCHECK. Long-running infrastructure
        containers such as Gitea may legitimately be ``State=running`` with no
        Docker health flag at all; expose that distinction instead of making
        callers parse the human ``Status`` string or treating the row as
        unhealthy.

        Rows in older/unit fixtures may intentionally omit ``State``; without
        it there is not enough evidence to derive runtime health, so preserve
        the original row shape.
        """
        if "State" not in row:
            return row

        state = str(row.get("State") or "").strip().lower()
        status = str(row.get("Status") or "")
        docker_health = "not_configured"
        match = _DOCKER_PS_HEALTH_RE.search(status)
        if match:
            raw = match.group(1).lower()
            docker_health = "starting" if "starting" in raw else raw

        row["DockerHealth"] = docker_health
        if docker_health == "healthy":
            row["Healthy"] = True
            row["HealthSource"] = "docker_health"
        elif docker_health in {"unhealthy", "starting"}:
            row["Healthy"] = False
            row["HealthSource"] = "docker_health"
        elif state == "running":
            row["Healthy"] = True
            row["HealthSource"] = "running_no_docker_health_flag"
        else:
            row["Healthy"] = False
            row["HealthSource"] = "container_state"
        return row

    @staticmethod
    def _redact_ps_row(row: dict) -> dict:
        """Redact host topology in one `docker ps --format json` row."""
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

    @staticmethod
    def _sanitize_ps_row(row: dict) -> dict:
        """Reduce one `docker ps --format json` row to safe fields.

        Drops nothing structural but redacts host paths that ride along in
        the Labels string (compose config_files/working_dir) and bind-mount
        sources in the Mounts string — the same topology leak the inspect
        allowlist kills structurally. When Docker supplies container State,
        annotate the row with derived health metadata.
        """
        return DockerClient._annotate_ps_health(DockerClient._redact_ps_row(row))

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
        output_truncated = self.last_output_truncated
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = output_truncated or total > len(rows)
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
        output_truncated = self.last_output_truncated
        rows, total = self._truncate_rows(self._parse_json_lines(result), limit)
        self.last_truncated = output_truncated or total > len(rows)
        redacted = [self._redact_ps_row(r) for r in rows]
        self.last_redacted = redacted != rows
        return [self._annotate_ps_health(r) for r in redacted]

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
        runner_image = os.environ.get("MCP_COMPOSE_RUNNER_IMAGE", "").strip()
        registry_auth_volume = os.environ.get("MCP_COMPOSE_DOCKER_CONFIG_VOLUME", "").strip()
        argv = [DOCKER_BIN, "compose"]
        if runner_image and project_dir is not None:
            # Operator-controlled, immutable helper. Only the validated project
            # is mounted, read-only, at its host path so Compose bind mounts and
            # labels retain their real deployment identity.
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}",
                runner_image,
            ):
                raise ValueError("MCP_COMPOSE_RUNNER_IMAGE must be a digest-pinned image")
            self._validate_project_dir(project_dir)
            resolved = str(Path(project_dir).resolve())
            if any(character in resolved for character in (",", "\n", "\r", ":")):
                raise ValueError("project directory is not safe for a Compose runner mount")
            argv = [
                DOCKER_BIN, "run", "--rm", "--network", "none",
                "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "--user", "0:0",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                # Keep mutable Buildx state out of read-only registry auth/rootfs.
                "--env", "BUILDX_CONFIG=/tmp/mcp-buildx",
                "--mount", f"type=bind,src={resolved},dst={resolved},readonly",
                "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            ]
            if registry_auth_volume:
                self._validate_volume_name(registry_auth_volume)
                argv.extend(
                    [
                        "--mount",
                        f"type=volume,src={registry_auth_volume},dst=/run/mcp-docker-config,readonly",
                        "--env",
                        "DOCKER_CONFIG=/run/mcp-docker-config",
                    ]
                )
            argv.extend(["--entrypoint", "/usr/bin/docker", runner_image, "compose"])
            project_dir = resolved
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
        if follow:
            # This is a request/response tool call, not a stream: _run()
            # awaits the subprocess's exit via proc.communicate(), but
            # `docker compose logs --follow` never exits on its own. Every
            # follow=True call would silently hang for the full `timeout`
            # (up to 300s) and then fail with a misleading "Command timed
            # out" RuntimeError instead of ever returning. Fail fast with a
            # clear error instead of burning up to 5 minutes to discover
            # the same thing.
            raise ValueError(
                "compose_logs(follow=True) is not supported -- this is a "
                "request/response tool call with no way to stream output; "
                "call again with follow=False (or omitted) to fetch the "
                "current tail instead."
            )
        self._validate_project_dir(project_dir)
        timeout = max(1, min(timeout, 300))
        argv = self._compose_base_argv(project_dir)
        argv.append("logs")
        tail = max(1, min(tail, 1000))
        argv.extend(["--tail", str(tail)])
        if timestamps:
            argv.append("--timestamps")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        result = await self._run(argv, timeout=float(timeout))
        lines = [self._sanitize_string(line) for line in result.splitlines()]
        return {"lines": lines, "count": len(lines)}

    async def image_id(self, image: str) -> str:
        """Resolve one already-local image reference to its immutable image ID."""
        if not isinstance(image, str) or not image or not image.isascii():
            raise ValueError("image must be a non-empty ASCII reference")
        result = (
            await self._run(
                [DOCKER_BIN, "image", "inspect", "--format", "{{.Id}}", image],
                timeout=60.0,
            )
        ).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
            raise RuntimeError("docker returned an invalid image ID")
        return result

    async def container_image_id(self, container: str) -> str:
        """Resolve a running/stopped container to the exact image ID it uses."""
        self._validate_container_name(container)
        result = (
            await self._run(
                [DOCKER_BIN, "inspect", "--format", "{{.Image}}", container],
                timeout=30.0,
            )
        ).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
            raise RuntimeError("docker returned an invalid container image ID")
        return result

    async def volume_metadata(self, volume: str) -> dict:
        """Return raw metadata for one explicitly named Docker volume.

        This is an internal planning primitive. Public tools must sanitize or
        reduce the returned mountpoint before exposing it to callers.
        """
        self._validate_volume_name(volume)
        raw = await self._run(
            [DOCKER_BIN, "volume", "inspect", "--format", "{{json .}}", volume],
            timeout=30.0,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("docker returned invalid volume metadata") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("docker returned invalid volume metadata")
        return payload

    async def network_metadata(self, network: str) -> dict:
        """Return raw metadata for one explicitly named Docker network."""
        self._validate_network_name(network)
        raw = await self._run(
            [DOCKER_BIN, "network", "inspect", "--format", "{{json .}}", network],
            timeout=30.0,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("docker returned invalid network metadata") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("docker returned invalid network metadata")
        return payload

    async def container_network_snapshot(self, container: str) -> dict[str, str]:
        """Return internal network-name -> IP evidence for one fixed container.

        Public ``inspect`` deliberately strips container network topology. Guarded
        deploy verification sometimes needs that topology internally, so this
        primitive queries only ``NetworkSettings.Networks`` and reduces each
        entry to its IP address. It is not registered as an MCP tool.
        """
        self._validate_container_name(container)
        raw = await self._run(
            [
                DOCKER_BIN,
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                container,
            ],
            timeout=30.0,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("docker returned invalid container network metadata") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("docker returned invalid container network metadata")
        result: dict[str, str] = {}
        for name, metadata in payload.items():
            if not isinstance(name, str) or not isinstance(metadata, dict):
                raise RuntimeError("docker returned invalid container network metadata")
            address = metadata.get("IPAddress")
            if not isinstance(address, str):
                raise RuntimeError("docker returned invalid container network IP evidence")
            result[name] = address
        return result

    async def image_revision(self, image: str) -> str:
        """Return the exact OCI source revision label for a local image."""
        if not isinstance(image, str) or not image or not image.isascii():
            raise ValueError("image must be a non-empty ASCII reference")
        revision = (
            await self._run(
                [
                    DOCKER_BIN,
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                    image,
                ],
                timeout=30.0,
            )
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("docker image OCI revision label is missing or invalid")
        return revision

    async def container_exists(self, container: str) -> bool:
        """Return False only for a proven Docker 'no such container/object' result."""
        self._validate_container_name(container)
        run = await self._run_with_result([DOCKER_BIN, "inspect", container], timeout=30.0)
        if run.exit_code == 0:
            return True
        lowered = (run.stderr or "").lower()
        if "no such container" in lowered or "no such object" in lowered:
            return False
        raise RuntimeError("docker container existence check failed")

    @staticmethod
    def _validate_deploy_mount_path(path: str, label: str) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"{label} must be an absolute path")
        if any(ch in path for ch in ("\x00", "\n", "\r", ",")):
            raise ValueError(f"{label} contains an unsafe character")
        return path

    async def run_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        infra_root: str,
        script_path: str,
        state_file: str,
        image_env: str,
        image_ref: str,
        profile_volume: str,
        profile_mountpoint: str,
        timeout: int = 420,
    ) -> RunResult:
        """Run one already-authorized deploy contract in a constrained helper.

        No arbitrary command is accepted. The argv is fixed here; policy and
        source/script identity are validated by the MCP adapter immediately
        before this method is called.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        if image_env not in {"GPT_BRIDGE_TARGET_IMAGE", "GPT_BRIDGE_RELEASE_IMAGE"}:
            raise ValueError("unsupported deploy image environment key")
        if not isinstance(image_ref, str) or not image_ref or not image_ref.isascii():
            raise ValueError("image_ref must be a non-empty ASCII image reference")
        self._validate_volume_name(profile_volume)
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        infra_root = self._validate_deploy_mount_path(infra_root, "infra_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        state_file = self._validate_deploy_mount_path(state_file, "state_file")
        profile_mountpoint = self._validate_deploy_mount_path(
            profile_mountpoint, "profile_mountpoint"
        )
        timeout = max(30, min(timeout, 600))

        mount_candidate = f"type=bind,src={candidate_root},dst={candidate_root},readonly"
        mount_infra = f"type=bind,src={infra_root},dst={infra_root}"
        mount_socket = "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"
        mount_profile = (
            f"type=volume,src={profile_volume},dst={profile_mountpoint}"
        )
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            mount_socket,
            "--mount",
            mount_candidate,
            "--mount",
            mount_infra,
            "--mount",
            mount_profile,
            "--env",
            f"INFRA_ROOT={infra_root}",
            "--env",
            f"{image_env}={image_ref}",
            "--env",
            "HOME=/tmp",
            helper_image_id,
            "python3",
            "/app/scripts/deploy_contract_runner.py",
            "--script",
            script_path,
            "--infra-root",
            infra_root,
            "--state-file",
            state_file,
            "--image-env",
            image_env,
            "--image-ref",
            image_ref,
            "--timeout",
            str(max(30, timeout - 30)),
        ]
        return await self._run_with_result(argv, timeout=float(timeout))

    async def read_site_audit_deploy_state(
        self,
        *,
        helper_image_id: str,
        state_volume: str,
    ) -> RunResult:
        """Read the fixed Site Audit durable deploy state without host access.

        The helper gets only the named deploy-state volume, mounted read-only.
        It has no Docker socket, no network, and accepts no caller-controlled
        path or command.  The MCP adapter performs schema and live-state
        verification before exposing any evidence to callers.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        self._validate_volume_name(state_volume)

        state_root = "/var/lib/site-audit-deploy-state"
        reader = (
            "import json, stat; "
            "from pathlib import Path; "
            f"root=Path({state_root!r}); "
            "state_path=root/'deploy.json'; pending_path=root/'deploy.json.pending'; "
            "payload={'version':1,'state_exists':False,'pending_exists':pending_path.exists(),'state':{}}; "
            "info=state_path.lstat() if state_path.exists() else None; "
            "safe=info is not None and stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode); "
            "payload['state_exists']=bool(safe); "
            "payload['state']=json.loads(state_path.read_text(encoding='utf-8')) if safe else {}; "
            "print(json.dumps(payload, sort_keys=True))"
        )
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--mount",
            f"type=volume,src={state_volume},dst={state_root},readonly",
            helper_image_id,
            "python3",
            "-c",
            reader,
        ]
        return await self._run_with_result(argv, timeout=30.0)

    async def run_site_audit_crm_provisioner(
        self,
        *,
        helper_image_id: str,
        operator_root: str,
        mode: str,
        expected_env_sha256: str = "",
        timeout: int = 90,
    ) -> RunResult:
        """Run the fixed Site Audit CRM identity/env provisioner.

        The helper gets no Docker socket and can reach only the fixed Site Audit
        control network.  Inspect mode mounts the operator project read-only;
        provision mode makes the same mount writable solely so the fixed helper
        can atomically replace ``/operator/.env`` after its exact SHA lease is
        revalidated.  No caller-controlled endpoint, identity or secret enters
        this surface.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        if mode not in {"inspect", "provision"}:
            raise ValueError("mode must be inspect or provision")
        if mode == "provision":
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_env_sha256):
                raise ValueError("expected_env_sha256 must be sha256:64hex in provision mode")
        elif expected_env_sha256:
            raise ValueError("expected_env_sha256 is only valid in provision mode")
        timeout = max(30, min(timeout, 180))

        mount_mode = "" if mode == "provision" else ",readonly"
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "site-audit-control",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
        ]
        if mode == "provision":
            argv.extend(["--cap-add", "DAC_OVERRIDE"])
        argv.extend(
            [
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--mount",
                f"type=bind,src={operator_root},dst=/operator{mount_mode}",
                helper_image_id,
                "python3",
                "/app/scripts/site_audit_crm_provisioner.py",
                "--mode",
                mode,
            ]
        )
        if mode == "provision":
            argv.extend(["--expected-env-sha256", expected_env_sha256])
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_site_audit_lkg_forensics(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        script_path: str,
        state_volume: str,
        historical_revision: str,
        archive: bool,
        timeout: int = 180,
    ) -> RunResult:
        """Run only the source-pinned forensic Site Audit LKG helper.

        The source and fixed volume are bound by the ADMIN Gateway adapter,
        not caller-supplied host paths.  This helper does not run Compose and
        may write only an append-only forensic archive, never deploy.json.
        The Docker socket is needed for *container inspect* and is therefore
        entrusted only to the exact Git-blob/SHA256 pinned script.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be a verified immutable image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        if script_path != candidate_root.rstrip("/") + "/scripts/prepare_site_audit_lkg_recovery.py":
            raise ValueError("forensic helper script is outside the pinned candidate")
        if not re.fullmatch(r"[0-9a-f]{40}", historical_revision):
            raise ValueError("historical_revision must be an exact lowercase 40-hex SHA")
        self._validate_volume_name(state_volume)
        timeout = max(120, min(timeout, 900))
        state_root = "/var/lib/site-audit-deploy-state"
        volume_mount = f"type=volume,src={state_volume},dst={state_root}"
        if not archive:
            volume_mount += ",readonly"
        argv = [
            DOCKER_BIN, "run", "--rm", "--network", "none",
            "--user", "0:0", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly",
            "--mount", f"type=bind,src={candidate_root},dst={candidate_root},readonly",
            "--mount", volume_mount,
            helper_image_id, "python3", script_path,
            "--state-file", f"{state_root}/deploy.json",
            "--expected-revision", historical_revision,
            "--stability-seconds", "10",
        ]
        if archive:
            argv.append("--archive")
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_site_audit_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        operator_root: str,
        script_path: str,
        env_file: str,
        state_volume: str,
        image_namespace: str,
        source_revision: str,
        timeout: int = 600,
        recover: bool = False,
    ) -> RunResult:
        """Run the dedicated Site Audit repo-owned deploy transaction helper.

        The adapter binds the candidate/source/script/image identities before
        this method is reached.  No arbitrary command or Compose argv enters
        this surface; rollout logic remains entirely in deploy-site-audit.sh.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        env_file = self._validate_deploy_mount_path(env_file, "env_file")
        self._validate_volume_name(state_volume)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", image_namespace):
            raise ValueError("image_namespace is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
        timeout = max(120, min(timeout, 900))

        state_root = "/var/lib/site-audit-deploy-state"
        state_file = f"{state_root}/deploy.json"
        mount_socket = "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"
        mount_candidate = f"type=bind,src={candidate_root},dst={candidate_root},readonly"
        mount_operator = f"type=bind,src={operator_root},dst={operator_root},readonly"
        mount_state = f"type=volume,src={state_volume},dst={state_root}"
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            mount_socket,
            "--mount",
            mount_candidate,
            "--mount",
            mount_operator,
            "--mount",
            mount_state,
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            f"GIT_CONFIG_VALUE_0={candidate_root}",
            helper_image_id,
            "python3",
            "/app/scripts/site_audit_deploy_contract_runner.py",
            "--script",
            script_path,
            "--source-root",
            candidate_root,
            "--env-file",
            env_file,
            "--state-file",
            state_file,
            "--image-namespace",
            image_namespace,
            "--source-revision",
            source_revision,
            "--timeout",
            str(max(60, timeout - 30)),
        ]
        if recover:
            argv.append("--recover")
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_xloud_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        operator_root: str,
        script_path: str,
        env_file: str,
        state_volume: str,
        site_image: str,
        intake_image: str,
        site_image_id: str,
        intake_image_id: str,
        source_revision: str,
        secret_fingerprint: str,
        mode: str,
        stability_seconds: int = 90,
        timeout: int = 720,
    ) -> RunResult:
        """Run the fixed XLOUD rollout helper without exposing its audit secret.

        The operator env is mounted read-only and parsed only inside the helper;
        secret plaintext never appears in Docker argv/environment or the result.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        env_file = self._validate_deploy_mount_path(env_file, "env_file")
        self._validate_volume_name(state_volume)
        for label, image in (("site_image", site_image), ("intake_image", intake_image)):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", image):
                raise ValueError(f"{label} is invalid")
        for label, image_id in (("site_image_id", site_image_id), ("intake_image_id", intake_image_id)):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ValueError(f"{label} must be an exact sha256 image ID")
        if site_image_id == intake_image_id:
            raise ValueError("site and intake image IDs must differ")
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", secret_fingerprint):
            raise ValueError("secret_fingerprint must be sha256:64hex")
        if mode not in {"preflight", "deploy", "recover"}:
            raise ValueError("mode must be preflight, deploy or recover")
        if stability_seconds < 90 or stability_seconds > 900:
            raise ValueError("stability_seconds must be between 90 and 900")
        timeout = max(120, min(timeout, 900))

        state_root = "/var/lib/xloud-deploy-state"
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={candidate_root},dst={candidate_root},readonly",
            "--mount",
            f"type=bind,src={operator_root},dst={operator_root},readonly",
            "--mount",
            f"type=volume,src={state_volume},dst={state_root}",
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            f"GIT_CONFIG_VALUE_0={candidate_root}",
            helper_image_id,
            "python3",
            "/app/scripts/xloud_deploy_contract_runner.py",
            "--mode",
            mode,
            "--script",
            script_path,
            "--source-root",
            candidate_root,
            "--operator-root",
            operator_root,
            "--env-file",
            env_file,
            "--state-dir",
            state_root,
            "--site-image",
            site_image,
            "--intake-image",
            intake_image,
            "--site-image-id",
            site_image_id,
            "--intake-image-id",
            intake_image_id,
            "--source-revision",
            source_revision,
            "--secret-fingerprint",
            secret_fingerprint,
            "--stability-seconds",
            str(stability_seconds),
            "--timeout",
            str(max(60, timeout - 30)),
        ]
        return await self._run_with_result(argv, timeout=float(timeout))

    async def read_telegram_storage_deploy_state(
        self,
        *,
        helper_image_id: str,
        state_volume: str,
    ) -> RunResult:
        """Read the fixed Telegram Storage deploy-state file from one named volume."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        self._validate_volume_name(state_volume)

        state_root = "/var/lib/telegram-storage-deploy-state"
        reader = (
            "import json, stat; "
            "from pathlib import Path; "
            f"root=Path({state_root!r}); "
            "path=root/'telegram-storage-service-deploy.json'; "
            "payload={'version':1,'state_exists':False,'state':{}}; "
            "info=path.lstat() if path.exists() else None; "
            "safe=info is not None and stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode); "
            "payload['state_exists']=bool(safe); "
            "raw=json.loads(path.read_text(encoding='utf-8')) if safe else {}; "
            "payload['state']={k:raw.get(k) for k in ('telegram_storage_service_image','deployed_at') if k in raw}; "
            "print(json.dumps(payload, sort_keys=True))"
        )
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--mount",
            f"type=volume,src={state_volume},dst={state_root},readonly",
            helper_image_id,
            "python3",
            "-c",
            reader,
        ]
        return await self._run_with_result(argv, timeout=30.0)

    async def run_telegram_storage_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        operator_root: str,
        script_path: str,
        env_file: str,
        state_volume: str,
        target_image: str,
        bootstrap_image: str | None = None,
        bootstrap_deployed_at: str | None = None,
        timeout: int = 300,
    ) -> RunResult:
        """Run the fixed Telegram Storage repo-owned deploy helper."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        env_file = self._validate_deploy_mount_path(env_file, "env_file")
        self._validate_volume_name(state_volume)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", target_image):
            raise ValueError("target_image must be an immutable digest reference")
        if (bootstrap_image is None) != (bootstrap_deployed_at is None):
            raise ValueError("Telegram Storage rollback bootstrap requires image and deployed_at together")
        if bootstrap_image is not None:
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", bootstrap_image
            ):
                raise ValueError("bootstrap_image must be an immutable digest reference")
            if (
                not isinstance(bootstrap_deployed_at, str)
                or not bootstrap_deployed_at.strip()
                or any(ch in bootstrap_deployed_at for ch in ("\x00", "\n", "\r"))
            ):
                raise ValueError("bootstrap_deployed_at must be one non-empty line")
        timeout = max(60, min(timeout, 600))

        state_root = "/var/lib/telegram-storage-deploy-state"
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={candidate_root},dst={candidate_root},readonly",
            "--mount",
            f"type=bind,src={operator_root},dst={operator_root},readonly",
            "--mount",
            f"type=volume,src={state_volume},dst={state_root}",
            helper_image_id,
            "python3",
            "/app/scripts/telegram_storage_deploy_contract_runner.py",
            "--script",
            script_path,
            "--source-root",
            candidate_root,
            "--env-file",
            env_file,
            "--state-dir",
            state_root,
            "--target-image",
            target_image,
            "--timeout",
            str(max(30, timeout - 30)),
        ]
        if bootstrap_image is not None and bootstrap_deployed_at is not None:
            argv.extend(
                [
                    "--bootstrap-image",
                    bootstrap_image,
                    "--bootstrap-deployed-at",
                    bootstrap_deployed_at,
                ]
            )
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_js_chat_engine_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        operator_root: str,
        script_path: str,
        env_file: str,
        state_dir: str,
        core_databases_root: str,
        target_image: str,
        release_revision: str,
        timeout: int = 480,
    ) -> RunResult:
        """Run the fixed JS Chat Engine repo-owned deploy helper."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        env_file = self._validate_deploy_mount_path(env_file, "env_file")
        state_dir = self._validate_deploy_mount_path(state_dir, "state_dir")
        core_databases_root = self._validate_deploy_mount_path(
            core_databases_root, "core_databases_root"
        )
        if Path(state_dir) != Path(operator_root) / ".state" or ":" in state_dir:
            raise ValueError("state_dir must be the exact operator .state directory")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", target_image
        ):
            raise ValueError("target_image must be an immutable digest reference")
        if not re.fullmatch(r"[0-9a-f]{40}", release_revision):
            raise ValueError("release_revision must be an exact lowercase 40-hex SHA")
        timeout = max(120, min(timeout, 600))

        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={candidate_root},dst={candidate_root},readonly",
            "--mount",
            f"type=bind,src={operator_root},dst={operator_root},readonly",
            "--volume",
            f"{state_dir}:{state_dir}:rw",
            "--mount",
            f"type=bind,src={core_databases_root},dst={core_databases_root}",
            helper_image_id,
            "python3",
            "/app/scripts/js_chat_engine_deploy_contract_runner.py",
            "--script",
            script_path,
            "--source-root",
            candidate_root,
            "--operator-root",
            operator_root,
            "--env-file",
            env_file,
            "--state-dir",
            state_dir,
            "--core-databases-root",
            core_databases_root,
            "--target-image",
            target_image,
            "--release-revision",
            release_revision,
            "--timeout",
            str(max(90, timeout - 30)),
        ]
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_content_harvester_deploy_contract_helper(
        self,
        *,
        helper_image_id: str,
        candidate_root: str,
        operator_root: str,
        script_path: str,
        env_file: str,
        state_volume: str,
        image_namespace: str,
        source_revision: str,
        artifact_volume: str,
        runtime_network: str,
        timeout: int = 600,
        recover: bool = False,
    ) -> RunResult:
        """Run the fixed Content Harvester Telegram-canary transaction helper."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image_id):
            raise ValueError("helper_image_id must be an immutable sha256 image ID")
        candidate_root = self._validate_deploy_mount_path(candidate_root, "candidate_root")
        operator_root = self._validate_deploy_mount_path(operator_root, "operator_root")
        script_path = self._validate_deploy_mount_path(script_path, "script_path")
        env_file = self._validate_deploy_mount_path(env_file, "env_file")
        self._validate_volume_name(state_volume)
        self._validate_volume_name(artifact_volume)
        self._validate_network_name(runtime_network)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", image_namespace):
            raise ValueError("image_namespace is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
        timeout = max(120, min(timeout, 900))

        state_root = "/var/lib/content-harvester-deploy-state"
        state_file = f"{state_root}/deploy.json"
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={candidate_root},dst={candidate_root},readonly",
            "--mount",
            f"type=bind,src={operator_root},dst={operator_root},readonly",
            "--mount",
            f"type=volume,src={state_volume},dst={state_root}",
            helper_image_id,
            "python3",
            "/app/scripts/content_harvester_deploy_contract_runner.py",
            "--script",
            script_path,
            "--source-root",
            candidate_root,
            "--env-file",
            env_file,
            "--state-file",
            state_file,
            "--image-namespace",
            image_namespace,
            "--source-revision",
            source_revision,
            "--artifact-volume",
            artifact_volume,
            "--runtime-network",
            runtime_network,
            "--timeout",
            str(max(60, timeout - 30)),
        ]
        if recover:
            argv.append("--recover")
        return await self._run_with_result(argv, timeout=float(timeout))

    async def run_content_harvester_migration(
        self,
        *,
        image: str,
        runtime_network: str,
        database_url: str,
        mode: str,
        timeout: int = 300,
    ) -> RunResult:
        """Run the fixed Harvester migration CLI with database-only credentials.

        The database URL is inherited by the Docker CLI process and forwarded
        with ``--env HARVESTER_DATABASE_URL`` so its value never appears in
        process argv. No Telegram/API credentials are passed to the migration
        container.
        """
        self._validate_image_name(image)
        if "@" in image or ":" not in image.rsplit("/", 1)[-1]:
            raise ValueError("migration image must be a tagged image reference")
        self._validate_network_name(runtime_network)
        if mode not in {"check", "apply"}:
            raise ValueError("migration mode must be check or apply")
        if (
            not isinstance(database_url, str)
            or not database_url.strip()
            or any(ch in database_url for ch in ("\x00", "\n", "\r"))
        ):
            raise ValueError("database_url must be one non-empty line")
        timeout = max(30, min(timeout, 600))
        argv = [
            DOCKER_BIN,
            "run",
            "--rm",
            "--network",
            runtime_network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--env",
            "HARVESTER_DATABASE_URL",
            "--entrypoint",
            "content-harvester-migrate",
            image,
            mode,
        ]
        process_env = {**os.environ, "HARVESTER_DATABASE_URL": database_url.strip()}
        return await self._run_with_result(argv, timeout=float(timeout), env=process_env)

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
