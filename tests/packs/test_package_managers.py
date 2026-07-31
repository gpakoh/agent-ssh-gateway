"""Package managers pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestPackageManagersPack:
    def test_package_managers_pack_patterns(self):
        """Package managers pack (P18) covers npm/pip/apt/cargo/gem."""
        r = build_registry()
        cases = {
            "npm publish": "npm-publish",
            "npm publish --dry-run": None,
            "pip uninstall requests": "pip-uninstall",
            "apt purge nginx": "apt-remove",
            "cargo yank my-crate --vers 0.1.0": "cargo-yank",
            "gem push mygem.gem": "gem-push",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            if expected is None:
                assert not names, f"{cmd!r}: expected no match, got {names}"
            else:
                assert expected in names, f"{cmd!r}: expected {expected}, got {names}"
