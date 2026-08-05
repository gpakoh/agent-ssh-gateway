from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

# Anchors "rm" to an actual command-start position: start of string, or right
# after a shell separator (&&, ;, |, newline — a single-char class also
# matches one half of "&&"/"||", which is enough since the anchor only needs
# *a* valid separator position immediately before the optional sudo/path
# prefix and "rm"). Without this, a bare `\brm\b` matches the "rm" *argument*
# of an unrelated command like "docker rm --force x" (docker's own rm
# subcommand, not the filesystem rm binary), or "rm" appearing inside a
# quoted string like echo 'rm -rf /' — neither of which actually invokes rm.
_RM_ANCHOR = r"(?:^|[;&|]|\n)\s*(?:sudo\s+)?(?:[\w./-]*/)?rm\b"

# Recursive/force flags detected as independent lookaheads (rather than a
# single sequential match) so -r and -f are caught whether combined in one
# token (-rf, -fr, -vrf) or given as separate arguments (-r -f, --force -r).
_RECURSIVE_LOOKAHEAD = r"(?=.*?(?:(?<![\w-])-[a-zA-Z0-9]*r[a-zA-Z0-9]*(?![\w-])|--recursive\b))"
_FORCE_LOOKAHEAD = r"(?=.*?(?:(?<![\w-])-[a-zA-Z0-9]*f[a-zA-Z0-9]*(?![\w-])|--force\b))"

FILESYSTEM_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(name="rm-rf-root",
        regex=_RM_ANCHOR + _RECURSIVE_LOOKAHEAD + _FORCE_LOOKAHEAD
        + r'.*?\s+["\']?(?:/|/\*)["\']?(?:\s|&&|\|\||;|\||$|#)',
        reason="rm -rf targeting root filesystem (/) will DESTROY THE OPERATING SYSTEM",
        severity=Severity.CRITICAL,
        description="Recursive force-delete on / wipes the entire filesystem.",
        suggestions=(PatternSuggestion("rm -rf /path/to/specific/dir", "Target specific directories", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="rm-rf-sensitive",
        regex=_RM_ANCHOR + _RECURSIVE_LOOKAHEAD + _FORCE_LOOKAHEAD
        + r'.*?\s+/'
        r"(?:etc(?=[ /\t]|$)|var(?=[ /\t]|$)|boot(?=[ /\t]|$)|dev(?=[ /\t]|$)"
        r"|proc(?=[ /\t]|$)|sys(?=[ /\t]|$)|usr(?=[ /\t]|$)"
        r"|lib(?=[ /\t]|$)|bin(?=[ /\t]|$)|sbin(?=[ /\t]|$)"
        r"|opt(?=[ /\t]|$)|root(?=[ /\t]|$))",
        reason="rm -rf targeting a system-critical directory will BREAK the OS",
        severity=Severity.CRITICAL,
        description="System directories like /etc, /var, /usr contain critical files.",
        suggestions=(PatternSuggestion("rm -rf /tmp/specific/subdir", "Use /tmp for temp deletions", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="rm-rf",
        regex=_RM_ANCHOR + _RECURSIVE_LOOKAHEAD + _FORCE_LOOKAHEAD,
        reason="rm -rf is destructive — recursively forces deletion without confirmation",
        severity=Severity.HIGH,
        description="Recursive force-delete is the most dangerous filesystem command.",
        suggestions=(PatternSuggestion("rm -ri {path}", "Interactive mode", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="rm-recursive",
        regex=_RM_ANCHOR + r".*\s+(?:-(?:[a-zA-Z]*[rR][a-zA-Z]*)|--recursive)(?:\s|$)",
        reason="Recursive rm can silently remove an entire directory tree",
        severity=Severity.MEDIUM,
        description="Removes entire directory trees without individual file prompts.",
        suggestions=(PatternSuggestion("rm -ri {path}", "Interactive confirmation", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
    DestructivePattern(name="find-delete",
        regex=r"\bfind\b.*\s+-delete\b",
        reason="find -delete recursively removes files matching the search",
        severity=Severity.HIGH,
        description="find -delete silently removes every matched file.",
        suggestions=(PatternSuggestion("find /tmp -type f | head -20", "Preview before deleting", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="find-exec-rm",
        regex=r"\bfind\b.*\s+-exec\b.*\brm\b",
        reason="find -exec rm runs rm on every matched file — bulk deletion",
        severity=Severity.HIGH,
        description="find combined with -exec rm deletes every file matched.",
        suggestions=(PatternSuggestion("find /tmp -type f | head -20", "Preview first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="dd-block-device",
        regex=r"\bdd\b.*\bof=\s*/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|mmcblk\d+|loop\d+|dm-\d+|md\d+)",
        reason="dd writing directly to a block device will DESTROY filesystem and data",
        severity=Severity.CRITICAL,
        description="dd of=/dev/sdX overwrites the raw block device.",
        suggestions=(PatternSuggestion("lsblk", "List block devices first", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="mkfs-destructive",
        regex=r"\bmkfs\b",
        reason="mkfs formats a filesystem, ERASING ALL DATA on the target device",
        severity=Severity.CRITICAL,
        description="mkfs creates a new filesystem, destroying all existing data.",
        suggestions=(PatternSuggestion("lsblk -f", "Check existing filesystems", kind=SuggestionKind.PREVIEW_FIRST),)),
    DestructivePattern(name="shred-destructive",
        regex=r"\bshred\b.*(?:-[a-zA-Z0-9]*u\b|--remove)",
        reason="shred overwrites a file to hide its contents, then optionally deletes it",
        severity=Severity.HIGH,
        description="shred securely deletes files by overwriting with random data.",
        suggestions=(PatternSuggestion("rm -P {file}", "Use rm -P for secure deletion", kind=SuggestionKind.SAFER_ALTERNATIVE),)),
)


def build_filesystem_pack() -> Pack:
    return Pack(id="filesystem", name="Filesystem patterns",
        destructive_patterns=FILESYSTEM_PATTERNS,
        keywords=("rm", "find", "dd", "mkfs", "shred"),
    )
