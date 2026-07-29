from __future__ import annotations

import re

from app.command_policy import (
    DestructiveMatch,
    DestructivePattern,
)


class Pack:
    """A group of destructive (and optional safe) patterns (DCG-style).

    Each pack represents a domain: docker, filesystem, kubernetes, etc.
    Patterns are precompiled on construction.
    """

    def __init__(
        self,
        id: str,
        name: str,
        destructive_patterns: tuple[DestructivePattern, ...] = (),
        keywords: tuple[str, ...] = (),
    ) -> None:
        self.id = id
        self.name = name
        self.keywords = keywords
        self.destructive_patterns = destructive_patterns
        # Precompile regexes — stored as list of (pattern, DestructivePattern) pairs
        # to handle duplicate pattern names within a single pack.
        self._compiled: list[tuple[re.Pattern, DestructivePattern]] = [
            (re.compile(dp.regex, re.IGNORECASE | re.DOTALL), dp)
            for dp in destructive_patterns
        ]

    def matches_keywords(self, command: str) -> bool:
        """Quick-reject via keyword check.

        Returns True if command contains ANY keyword. Empty keywords
        means "always check" (no quick-reject).
        """
        if not self.keywords:
            return True
        cmd_lower = command.lower()
        return any(kw.lower() in cmd_lower for kw in self.keywords)

    def check(self, command: str) -> list[DestructiveMatch]:
        """Evaluate a command against this pack's destructive patterns.

        Returns matches from destructive patterns only.
        """
        results: list[DestructiveMatch] = []
        if not self.matches_keywords(command):
            return results
        for compiled, dp in self._compiled:
            if compiled.search(command):
                results.append(DestructiveMatch(
                    pattern_name=dp.name,
                    reason=dp.reason,
                    severity=dp.severity,
                    suggestion=dp.suggestions[0].command if dp.suggestions else None,
                ))
        return results


# ── Registry ────────────────────────────────────────────────────────────────

class PackRegistry:
    """Registry of all packs with lazy loading.

    Inspired by DCG's PackEntry + OnceLock approach.
    """

    def __init__(self) -> None:
        self._packs: dict[str, Pack] = {}
        self._all_keywords: tuple[str, ...] = ()

    def register(self, pack: Pack) -> None:
        self._packs[pack.id] = pack
        self._rebuild_keyword_index()

    def _rebuild_keyword_index(self) -> None:
        keywords: set[str] = set()
        for p in self._packs.values():
            keywords.update(p.keywords)
        self._all_keywords = tuple(sorted(keywords))

    def get(self, pack_id: str) -> Pack | None:
        return self._packs.get(pack_id)

    @property
    def all_packs(self) -> tuple[Pack, ...]:
        return tuple(self._packs.values())

    def evaluate(self, command: str) -> list[DestructiveMatch]:
        """Evaluate command against ALL registered packs.

        Returns aggregated destructive matches.
        """
        if not self._passes_quick_reject(command):
            return []
        results: list[DestructiveMatch] = []
        for pack in self._packs.values():
            results.extend(pack.check(command))
        return results

    def evaluate_pack(self, pack_id: str, command: str) -> list[DestructiveMatch]:
        """Evaluate command against a single pack."""
        pack = self._packs.get(pack_id)
        if pack is None:
            return []
        return pack.check(command)

    def _passes_quick_reject(self, command: str) -> bool:
        """Return True if command might match any pack.

        False means no pack's keywords match (reject all packs).
        """
        if not self._all_keywords:
            return True
        cmd_lower = command.lower()
        return any(kw.lower() in cmd_lower for kw in self._all_keywords)

    @property
    def pack_count(self) -> int:
        return len(self._packs)

    @property
    def pattern_count(self) -> int:
        return sum(len(p.destructive_patterns) for p in self._packs.values())

    @property
    def keyword_count(self) -> int:
        return len(self._all_keywords)
