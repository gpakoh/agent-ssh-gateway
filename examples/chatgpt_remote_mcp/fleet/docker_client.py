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
COMPOSE_PATH_TRAVERSAL_RE = re.compile(r"(?:^|/)\.\.(?:/|$)")
ALLOWED_PRUNE_TYPES: set[str] = {"container", "image", "network"}

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

    def _validate_compose_file(self, path: str) -> str:
        if not COMPOSE_FILE_RE.match(path):
            raise ValueError(f"Invalid compose file path: {shlex.quote(path)}")
        if path.startswith("/"):
            raise ValueError(f"Absolute path not allowed: {shlex.quote(path)}")
        if COMPOSE_PATH_TRAVERSAL_RE.search(path):
            raise ValueError(f"Path traversal not allowed: {shlex.quote(path)}")
        return path

    def _validate_prune_type(self, type: str) -> str:
        if type not in ALLOWED_PRUNE_TYPES:
            raise ValueError(
                f"Unsupported prune type '{type}'. Allowed: {sorted(ALLOWED_PRUNE_TYPES)}"
            )
        return type

    def _resolve_compose_file_path(
        self,
        file_path: str | None,
        project_dir: str | None = None,
        allowed_roots: set[str] | None = None,
    ) -> str | None:
        """Resolve a compose file path with safety checks.

        Relative paths are joined under project_dir if given.
        Absolute paths are allowed only if inside allowed_roots.

        Returns the resolved path string, or None if file_path is None.
        """
        if file_path is None:
            return None

        if not COMPOSE_FILE_RE.match(file_path):
            raise ValueError(f"Invalid compose file path: {shlex.quote(file_path)}")

        if COMPOSE_PATH_TRAVERSAL_RE.search(file_path):
            raise ValueError(f"Path traversal not allowed: {shlex.quote(file_path)}")

        if file_path.startswith("/"):
            roots = allowed_roots or set()
            if not roots:
                raise ValueError(
                    f"Absolute path {shlex.quote(file_path)} not allowed: "
                    "no allowed roots configured"
                )
            ok = any(file_path.startswith(r) for r in roots)
            if not ok:
                raise ValueError(
                    f"Absolute path {shlex.quote(file_path)} is outside allowed root(s)"
                )
            return file_path

        if project_dir:
            pdir = Path(project_dir).resolve()
            if not pdir.is_dir():
                raise ValueError(f"Project directory does not exist: {shlex.quote(project_dir)}")
            return str(pdir / file_path)

        return file_path

    async def ps(
        self,
        all: bool = False,
        format: str | None = None,
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
        return await self._run(argv)

    async def images(
        self,
        format: str | None = None,
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
        return await self._run(argv)

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
        return await self._run(argv)

    async def compose_ps(
        self,
        project_dir: str | None = None,
        file_path: str | None = None,
        format: str | None = None,
    ) -> str:
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
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
        return await self._run(argv, timeout=60.0)

    async def compose_services(
        self,
        project_dir: str | None = None,
        file_path: str | None = None,
    ) -> str:
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
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

    def _compose_base_argv(
        self, file_path: str | None = None, project_dir: str | None = None
    ) -> list[str]:
        argv = [DOCKER_BIN, "compose"]
        if file_path:
            argv.extend(["-f", file_path])
        if project_dir:
            argv.extend(["--project-directory", project_dir])
        return argv

    async def compose_up(
        self,
        project_dir: str | None = None,
        file_path: str | None = None,
        services: list[str] | None = None,
        detach: bool = True,
        build: bool = False,
        timeout: int = 120,
    ) -> str:
        """Start services. detach=True by default; set build=True to rebuild."""
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
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
        file_path: str | None = None,
        services: list[str] | None = None,
        timeout: int = 30,
    ) -> str:
        """Restart services in a compose project."""
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
        argv.append("restart")
        if services:
            for s in services:
                self._validate_service_name(s)
            argv.extend(services)
        return await self._run(argv, timeout=float(timeout))

    async def compose_build(
        self,
        project_dir: str | None = None,
        file_path: str | None = None,
        services: list[str] | None = None,
        no_cache: bool = False,
        timeout: int = 300,
    ) -> str:
        """Build (or rebuild) services. no_cache=True to ignore cache."""
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
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
        file_path: str | None = None,
        services: list[str] | None = None,
        tail: int = 100,
        follow: bool = False,
        timestamps: bool = False,
        timeout: int = 30,
    ) -> str:
        """Fetch logs from compose services. tail: 1-1000 lines."""
        resolved = self._resolve_compose_file_path(file_path, project_dir)
        argv = self._compose_base_argv(resolved, project_dir)
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
        file_path: str | None = None,
        remove_orphans: bool = False,
        timeout: int = 30,
    ) -> RunResult:
        argv = self._compose_base_argv(file_path, project_dir)
        argv.append("down")
        if remove_orphans:
            argv.append("--remove-orphans")
        argv.extend(["-t", str(timeout)])
        return await self._run_with_result(argv, timeout=float(timeout) + 10)

    async def prune(self, type: str = "container") -> RunResult:
        self._validate_prune_type(type)
        argv = [DOCKER_BIN, type, "prune", "-f"]
        return await self._run_with_result(argv)
