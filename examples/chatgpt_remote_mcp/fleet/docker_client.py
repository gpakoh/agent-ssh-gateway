"""Read-only Docker subprocess wrapper for fleet MCP adapter.

Each tool builds its own argv list — never accepts a raw command string.
All tools are read-only. Write/destructive commands have no implementation here.
"""

from __future__ import annotations

import asyncio
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
        if max_lines:
            lines = result.split("\n")
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                result = "\n".join(lines) + f"\n[output truncated at {max_lines} lines]"
        return result

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
