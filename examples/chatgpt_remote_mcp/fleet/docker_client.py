"""Read-only Docker subprocess wrapper for fleet MCP adapter.

Each tool builds its own argv list — never accepts a raw command string.
All tools are read-only. Write/destructive commands have no implementation here.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex

DOCKER_BIN = "/usr/bin/docker"
SUBPROCESS_TIMEOUT = 30.0
MAX_OUTPUT_BYTES = 50 * 1024

CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
IMAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/{-]{0,255}$")
SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
COMPOSE_FILE_RE = re.compile(r"^[a-zA-Z0-9_/.-]{1,256}$")
COMPOSE_PATH_TRAVERSAL_RE = re.compile(r"(?:^|/)\.\.(?:/|$)")

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
        self, argv: list[str], timeout: float = SUBPROCESS_TIMEOUT,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Command timed out after {timeout}s: {shlex.join(argv)}"
            ) from None

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"docker exited {proc.returncode}: {err}")

        result = stdout.decode("utf-8", errors="replace")
        if len(result) > MAX_OUTPUT_BYTES:
            result = result[:MAX_OUTPUT_BYTES] + "\n[output truncated]"
        return result

    def _validate_container_name(self, name: str) -> str:
        if not CONTAINER_NAME_RE.match(name):
            raise ValueError(
                f"Invalid container name: {shlex.quote(name)}"
            )
        return name

    def _validate_image_name(self, name: str) -> str:
        if not IMAGE_NAME_RE.match(name):
            raise ValueError(
                f"Invalid image name: {shlex.quote(name)}"
            )
        return name

    def _validate_service_name(self, name: str) -> str:
        if not SERVICE_NAME_RE.match(name):
            raise ValueError(
                f"Invalid service name: {shlex.quote(name)}"
            )
        return name

    def _validate_compose_file(self, path: str) -> str:
        if not COMPOSE_FILE_RE.match(path):
            raise ValueError(
                f"Invalid compose file path: {shlex.quote(path)}"
            )
        if path.startswith("/"):
            raise ValueError(
                f"Absolute path not allowed: {shlex.quote(path)}"
            )
        if COMPOSE_PATH_TRAVERSAL_RE.search(path):
            raise ValueError(
                f"Path traversal not allowed: {shlex.quote(path)}"
            )
        return path

    async def ps(
        self, all: bool = False,
        format: str | None = None,
    ) -> str:
        argv = [DOCKER_BIN, "ps"]
        if all:
            argv.append("--all")
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend([
                "--format",
                "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}",
            ])
        return await self._run(argv)

    async def images(
        self, format: str | None = None,
    ) -> str:
        argv = [DOCKER_BIN, "images"]
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend([
                "--format",
                "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}",
            ])
        return await self._run(argv)

    async def inspect(
        self, name: str, max_lines: int | None = 500,
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
            key_part = s[:m.end() - 1]
            return f"{key_part}={REDACTED}"
        return s

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return bool(_SECRET_DICT_KEY_RE.search(key))

    async def logs(
        self, container: str, tail: int = 200,
    ) -> str:
        self._validate_container_name(container)
        tail = max(1, min(tail, 1000))
        argv = [DOCKER_BIN, "logs", "--tail", str(tail), container]
        return await self._run(argv)

    async def stats(
        self, format: str | None = None,
    ) -> str:
        argv = [DOCKER_BIN, "stats", "--no-stream"]
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend([
                "--format",
                "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}",
            ])
        return await self._run(argv)

    async def compose_ps(
        self, project_dir: str | None = None,
        file_path: str | None = None,
        format: str | None = None,
    ) -> str:
        argv = [DOCKER_BIN, "compose"]
        if file_path:
            self._validate_compose_file(file_path)
            argv.extend(["-f", file_path])
        if project_dir:
            argv.extend(["--project-directory", project_dir])
        argv.append("ps")
        if format:
            argv.extend(["--format", format])
        else:
            argv.extend([
                "--format",
                "table {{.Name}}\t{{.Status}}",
            ])
        return await self._run(argv, timeout=60.0)

    async def compose_services(
        self, project_dir: str | None = None,
        file_path: str | None = None,
    ) -> str:
        argv = [DOCKER_BIN, "compose"]
        if file_path:
            self._validate_compose_file(file_path)
            argv.extend(["-f", file_path])
        if project_dir:
            argv.extend(["--project-directory", project_dir])
        argv.extend(["config", "--services"])
        return await self._run(argv, timeout=60.0)
