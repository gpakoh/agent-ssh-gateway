from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

# Cache cleanup detection follows the Docker/Buildx command path, not an
# arbitrary later occurrence of "builder prune" in flag values or other
# shell commands. This is a bounded informational pattern, not a shell parser.
# Known options start with a dash; possessive repetition avoids exponential
# backtracking on repeated short options with attached values (Python 3.11+).
# A shell continuation removes the escaped newline, not a real argument
# separator. Require horizontal whitespace and never cross a bare newline.
_ARG_SPACE = r"(?:\\\n)*+[ \t]++(?:[ \t]|\\\n)*+"
_OPTION_VALUE = r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:[^\s;&|'\"`\\]|\\[^\r\n])++)"
_DOCKER_PREFIX = (
    r"(?<![\w-])docker(?:\.exe)?\b"
    + r"(?:" + _ARG_SPACE + r"(?:--(?:config|context|host|log-level|tlscacert|tlscert|tlskey)"
    + r"(?:=|" + _ARG_SPACE + r")" + _OPTION_VALUE
    + r"|-[Hcl](?:=|" + _ARG_SPACE + r")?" + _OPTION_VALUE
    + r"|--(?:debug|tls|tlsverify)(?:=(?:true|false))?|-D))*+" + _ARG_SPACE
)
_BUILDX_OPTIONS = (
    r"(?:" + _ARG_SPACE + r"(?:--builder(?:=|" + _ARG_SPACE + r")" + _OPTION_VALUE
    + r"|--debug(?:=(?:true|false))?|-D))*+" + _ARG_SPACE
)


DOCKER_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(name="system-prune",
        regex=r"docker\b.*?\bsystem\s+prune",
        reason="docker system prune removes ALL unused containers, networks, images, and build cache",
        severity=Severity.HIGH,
        description="docker system prune is Docker's most aggressive cleanup command.",
        suggestions=(PatternSuggestion("docker system df -v", "Preview without deleting", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="volume-prune",
        regex=r"docker\b.*?\bvolume\s+prune",
        reason="docker volume prune permanently deletes ALL unused volumes and their data",
        severity=Severity.HIGH,
        description="Deletes all volumes not attached to a running container.",
        suggestions=(PatternSuggestion("docker volume ls", "List all volumes first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="network-prune",
        regex=r"docker\b.*?\bnetwork\s+prune",
        reason="docker network prune removes ALL unused networks",
        severity=Severity.HIGH,
        description="Removes all user-defined networks not used by containers.",
        suggestions=(PatternSuggestion("docker network ls", "List networks before pruning", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="image-prune",
        regex=r"docker\b.*?\bimage\s+prune",
        reason="docker image prune removes unused (dangling) images",
        severity=Severity.MEDIUM,
        description="Removes dangling images. With -a flag, removes ALL unused images.",
        suggestions=(PatternSuggestion("docker images -f dangling=true", "List dangling images first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="container-prune",
        regex=r"docker\b.*?\bcontainer\s+prune",
        reason="docker container prune removes ALL stopped containers",
        severity=Severity.MEDIUM,
        description="Container logs and filesystem layers are lost.",
        suggestions=(PatternSuggestion("docker ps -a -f status=exited", "List stopped containers first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="rm-force",
        # The short-flag alternative requires proper token boundaries
        # (lookbehind before the dash, \b after the run) so it matches a
        # real -f/-rf/-fr-style flag token and never a substring inside a
        # long option name like --format or --filter (both happen to
        # contain "-f" as a substring of their own name).
        regex=r"docker\b.*?\brm\s+.*(?:(?<![\w-])-[a-zA-Z0-9]*f[a-zA-Z0-9]*\b|--force(?!=false)\b)",
        reason="docker rm -f forcibly removes containers without graceful shutdown",
        severity=Severity.HIGH,
        description="Sends SIGKILL instead of SIGTERM.",
        suggestions=(PatternSuggestion("docker stop <c> && docker rm <c>", "Graceful shutdown", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="rmi-force",
        regex=r"docker\b.*?\brmi\s+.*(?:(?<![\w-])-[a-zA-Z0-9]*f[a-zA-Z0-9]*\b|--force(?!=false)\b)",
        reason="docker rmi -f forcibly removes images even if in use by containers",
        severity=Severity.HIGH,
        description="Forces image removal, potentially breaking running containers.",
        suggestions=(PatternSuggestion("docker rmi <image>", "Remove without force", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="volume-rm",
        regex=r"docker\b.*?\bvolume\s+rm",
        reason="docker volume rm permanently deletes volumes and their data",
        severity=Severity.HIGH,
        description="Database files, uploads, and configuration volume data lost forever.",
        suggestions=(PatternSuggestion("docker volume inspect <volume>", "Inspect volume metadata first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="stop-all",
        regex=r"docker\b.*?\b(?:stop|kill)\s+\$\(",
        reason="Stopping/killing all containers can disrupt running services",
        severity=Severity.HIGH,
        description="Stops/kills ALL running containers via command substitution.",
        suggestions=(PatternSuggestion("docker stop <container>", "Stop specific containers", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="builder-prune",
        regex=_DOCKER_PREFIX + r"builder" + _ARG_SPACE + r"prune(?![\w-])",
        reason="docker builder prune removes ALL build cache — rebuilds will be slow",
        severity=Severity.MEDIUM,
        description="Removes the entire build cache layer. All images must be rebuilt from scratch.",
        suggestions=(PatternSuggestion("docker builder ls", "List builders first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="buildx-prune",
        regex=_DOCKER_PREFIX + r"buildx" + _BUILDX_OPTIONS + r"prune(?![\w-])",
        reason="docker buildx prune removes build cache from the selected builder",
        severity=Severity.MEDIUM,
        description="May discard shared cache and make subsequent CI builds slower.",
        suggestions=(PatternSuggestion("docker buildx du", "Preview cache on the same builder and Docker context", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="network-rm",
        regex=r"docker\b.*?\bnetwork\s+rm\b",
        reason="docker network rm can break connectivity between running containers",
        severity=Severity.HIGH,
        description="Removing a network currently in use by containers will disconnect them.",
        suggestions=(PatternSuggestion("docker network inspect <name>", "Check which containers use it", kind=SuggestionKind.PREVIEW_FIRST),)),
)

COMPOSE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(name="down-volumes",
        regex=r"(?:docker-compose|docker\s+compose)\s+down\s+.*(?:-v\b|--volumes)",
        reason="docker-compose down -v removes volumes and their data permanently",
        severity=Severity.CRITICAL,
        description="The -v flag removes named volumes declared in Compose.",
        suggestions=(PatternSuggestion("docker-compose down", "Stops without touching volumes", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="down-rmi-all",
        regex=r"(?:docker-compose|docker\s+compose)\s+down\s+.*--rmi\s+all",
        reason="docker-compose down --rmi all removes all images used by services",
        severity=Severity.HIGH,
        description="All images must be re-pulled after this.",
        suggestions=(PatternSuggestion("docker-compose down", "Preserves images", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="rm-volumes",
        regex=r"(?:docker-compose|docker\s+compose)\s+rm\s+.*(?:-v\b|--volumes)",
        reason="docker-compose rm -v removes volumes attached to containers",
        severity=Severity.HIGH,
        description="Anonymous volumes attached to containers are lost.",
        suggestions=(PatternSuggestion("docker-compose rm", "Preserves volumes", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="compose-rm-force",
        regex=r"(?:docker-compose|docker\s+compose)\s+rm\s+.*(?:-f\b|--force(?!=false)\b)",
        reason="docker-compose rm -f forcibly removes containers without confirmation",
        severity=Severity.MEDIUM,
        description="Running containers are stopped abruptly (SIGKILL).",
        suggestions=(PatternSuggestion("docker-compose stop", "Graceful shutdown first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="compose-kill",
        regex=r"(?:docker-compose|docker\s+compose)\s+kill\b",
        reason="docker compose kill sends SIGKILL to all service containers",
        severity=Severity.MEDIUM,
        description="All containers are killed without graceful shutdown.",
        suggestions=(PatternSuggestion("docker-compose stop", "Graceful stop (SIGTERM) instead", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
)


def build_docker_pack() -> Pack:
    return Pack(id="docker", name="Docker patterns",
        destructive_patterns=DOCKER_PATTERNS + COMPOSE_PATTERNS,
        keywords=("docker", "docker-compose", "compose"),
    )
