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
    r"\w*(?:PASSWORD|SECRET|TOKEN)"
    r"|API[_-]?KEY|JWT|BEARER|AUTH|COOKIE|SESSION"
    r"|PRIVATE[_-]?KEY|CREDENTIAL|ACCESS[_-]?KEY"
    r"|REFRESH[_-]?TOKEN|CLIENT[_-]?SECRET|WEBHOOK[_-]?SECRET"
    r")\s*="
)

_SECRET_DICT_KEY_RE = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASS|API[_-]?KEY|JWT|BEARER|AUTH|COOKIE|SESSION|"
    r"PRIVATE[_-]?KEY|CREDENTIAL|ACCESS[_-]?KEY|REFRESH[_-]?TOKEN|CLIENT[_-]?SECRET|"
    r"WEBHOOK[_-]?SECRET|AUTHORIZATION)"
)


class DockerClient:
    """Read-only async subprocess wrapper for /usr/bin/docker."""

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
        raise ValueError(
            f"Project directory {shlex.quote(project_dir)} is outside allowed roots: "
            f"{ALLOWED_PROJECT_ROOTS}"
        )

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
    def _truncate_table_output(output: str, limit: int) -> str:
        """Truncate tabular docker output to *limit* data rows, preserving the header.

        Docker table output always starts with a header line followed by a separator
        line (dashes).  We keep both plus *limit* data lines and append a notice when
        truncated.
        """
        lines = output.splitlines()
        if len(lines) <= 2:
            return output
        header = lines[:2]
        data = lines[2:]
        if len(data) <= limit:
            return output
        truncated = data[:limit]
        total = len(data)
        return "\n".join(header + truncated) + (
            f"\n[showing {limit} of {total} results — use limit or filter to narrow]"
        )

    async def ps(
        self,
        all: bool = False,
        format: str | None = None,
        limit: int = 50,
    ) -> str:
        argv = [DOCKER_BIN, "ps"]
        if all:
            argv.append("--all")
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend(
                [
                    "--format",
                    "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}",
                ]
            )
        result = await self._run(argv)
        return self._truncate_table_output(result, limit)

    async def images(
        self,
        format: str | None = None,
        limit: int = 50,
    ) -> str:
        argv = [DOCKER_BIN, "images"]
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend(
                [
                    "--format",
                    "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}",
                ]
            )
        result = await self._run(argv)
        return self._truncate_table_output(result, limit)

    async def inspect(
        self,
        name: str,
        max_lines: int | None = 500,
    ) -> str:
        self._validate_container_name(name)
        argv = [DOCKER_BIN, "inspect", name]
        result = await self._run(argv)
        result = self._sanitize_inspect_output(result)
        if max_lines:
            lines = result.split("\n")
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                result = "\n".join(lines) + f"\n[output truncated at {max_lines} lines]"
        return result

    def _sanitize_inspect_output(self, raw: str) -> str:
        """Redact secrets from docker inspect JSON output."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        data = self._sanitize_value(data)
        return json.dumps(data, indent=2, ensure_ascii=False)

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
        Handles 'KEY=value' env format.
        """
        m = _SECRET_ENV_KEY_RE.match(s)
        if m:
            key_part = s[: m.end() - 1]
            return f"{key_part}={REDACTED}"
        return s

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return bool(_SECRET_DICT_KEY_RE.search(key))

    async def logs(
        self,
        container: str,
        tail: int = 200,
    ) -> str:
        self._validate_container_name(container)
        tail = max(1, min(tail, 1000))
        argv = [DOCKER_BIN, "logs", "--tail", str(tail), container]
        return await self._run(argv)

    async def stats(
        self,
        format: str | None = None,
        limit: int = 50,
    ) -> str:
        argv = [DOCKER_BIN, "stats", "--no-stream"]
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend(
                [
                    "--format",
                    "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}",
                ]
            )
        result = await self._run(argv)
        return self._truncate_table_output(result, limit)

    async def compose_ps(
        self,
        project_dir: str | None = None,
        format: str | None = None,
        limit: int = 50,
    ) -> str:
        self._validate_project_dir(project_dir)
        argv = self._compose_base_argv(project_dir)
        argv.append("ps")
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend(
                [
                    "--format",
                    "table {{.Name}}\t{{.Status}}",
                ]
            )
        result = await self._run(argv, timeout=60.0)
        return self._truncate_table_output(result, limit)

    async def compose_services(
        self,
        project_dir: str | None = None,
    ) -> str:
        self._validate_project_dir(project_dir)
        argv = self._compose_base_argv(project_dir)
        argv.extend(["config", "--services"])
        return await self._run(argv, timeout=60.0)

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
    ) -> str:
        """Fetch logs from compose services. tail: 1-1000 lines."""
        self._validate_project_dir(project_dir)
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
        return await self._run(argv, timeout=float(timeout))

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
