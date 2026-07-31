from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

PACKAGE_MANAGER_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="npm-publish",
        regex=r"\bnpm\b.*?\bpublish\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="npm publish releases a package publicly",
        severity=Severity.HIGH,
        description="npm publish releases a package to the public registry. Irreversible "
        "— even after unpublish, the version remains cached.",
        suggestions=(
            PatternSuggestion(command="npm publish --dry-run", description="Dry-run publish first to verify contents", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="npm pack", description="Inspect the tarball before publishing", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="yarn-publish",
        regex=r"\byarn\b.*?\bpublish\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="yarn publish releases a package publicly",
        severity=Severity.HIGH,
        description="yarn publish releases a package to the registry. Verify package.json "
        "and contents before publishing.",
        suggestions=(
            PatternSuggestion(command="yarn publish --dry-run", description="Dry-run publish first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="yarn pack", description="Inspect the tarball before publishing", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="pnpm-publish",
        regex=r"\bpnpm\b.*?\bpublish\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="pnpm publish releases a package publicly",
        severity=Severity.HIGH,
        description="pnpm publish releases a package to the registry.",
        suggestions=(
            PatternSuggestion(command="pnpm publish --dry-run", description="Dry-run publish first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pnpm pack", description="Inspect the tarball before publishing", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="npm-unpublish",
        regex=r"\bnpm\b.*?\bunpublish(?=\s|$)",
        reason="npm unpublish removes a published package",
        severity=Severity.CRITICAL,
        description="npm unpublish removes a published package. Can break dependent "
        "projects. New packages may require support contact for unpublish.",
        suggestions=(
            PatternSuggestion(command="npm view {package} versions", description="Check who depends on the package first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="npm deprecate {package}@{version} 'reason'", description="Deprecate instead of unpublish — safer for dependents", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pip-uninstall",
        regex=r"\bpip(?:3)?\b.*?\buninstall(?=\s|$)",
        reason="pip uninstall removes installed packages",
        severity=Severity.MEDIUM,
        description="pip uninstall removes installed packages. Verify dependencies before "
        "removing — other packages may depend on them.",
        suggestions=(
            PatternSuggestion(command="pip show {package}", description="Check package details and dependencies first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pip freeze > requirements.txt", description="Backup the environment before uninstalling", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="pip-install-url",
        regex=r"\bpip\b.*?\binstall\s+.*(?:https?://|git\+)",
        reason="pip install from URL can install unvetted code",
        severity=Severity.HIGH,
        description="pip install from URL installs code that bypasses PyPI review. "
        "Verify the source and checksum first.",
        suggestions=(
            PatternSuggestion(command="pip download {package} --no-deps -d /tmp", description="Download and inspect before installing", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pip install {package}=={version}", description="Install a pinned version from PyPI instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="apt-remove",
        regex=r"\bapt(?:-get)?\b.*?\b(?:remove|purge|autoremove)(?=\s|$)",
        reason="apt remove/purge removes packages",
        severity=Severity.HIGH,
        description="apt remove/purge removes system packages. Removing critical packages "
        "(kernel, libc, openssl) can break the system.",
        suggestions=(
            PatternSuggestion(command="apt list --installed | grep -i {package}", description="Check if the package is critical first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="apt install {package}=", description="Pin the version to avoid dependency breakage", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="yum-remove",
        regex=r"\b(?:yum|dnf)\b.*?\b(?:remove|erase|autoremove)(?=\s|$)",
        reason="yum/dnf remove removes packages",
        severity=Severity.HIGH,
        description="yum/dnf remove removes system packages. Verify no critical packages "
        "are affected.",
        suggestions=(
            PatternSuggestion(command="yum deplist {package}", description="Check dependents before removal", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="yum install {package} --setopt=keepcache=1", description="Keep cache for rollback if needed", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="cargo-publish",
        regex=r"\bcargo\b.*?\bpublish\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="cargo publish releases a crate to crates.io",
        severity=Severity.HIGH,
        description="cargo publish releases a crate to crates.io. Versions are immutable "
        "once published.",
        suggestions=(
            PatternSuggestion(command="cargo publish --dry-run", description="Dry-run publish first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="cargo package --list", description="Verify packaged files before publishing", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="cargo-yank",
        regex=r"\bcargo\b.*?\byank(?=\s|$)",
        reason="cargo yank marks a version as unavailable",
        severity=Severity.HIGH,
        description="cargo yank marks a version as unavailable. Can break dependent "
        "projects that reference the yanked version.",
        suggestions=(
            PatternSuggestion(command="cargo search {crate}", description="Check the crate's dependents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="cargo yank --undo {crate}@{version}", description="Un-yank restores availability if needed", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gem-push",
        regex=r"\bgem\b.*?\bpush\b",
        reason="gem push releases a gem to rubygems.org",
        severity=Severity.HIGH,
        description="gem push releases a gem to rubygems.org. Verify before publishing — "
        "versions cannot be fully removed.",
        suggestions=(
            PatternSuggestion(command="gem build {gem}.gemspec", description="Build the gem and inspect contents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gem yank {gem} -v {version}", description="Yank reverses a publish if it was a mistake", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="brew-uninstall",
        regex=r"\bbrew\b.*?\b(?:uninstall|remove)(?=\s|$)",
        reason="brew uninstall removes packages",
        severity=Severity.MEDIUM,
        description="brew uninstall removes packages. Verify no dependent packages are "
        "affected.",
        suggestions=(
            PatternSuggestion(command="brew uses --installed {formula}", description="Check which installed formulae depend on it", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="brew leaves | grep {formula}", description="Confirm it's a leaf formula (not depended on)", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="poetry-publish",
        regex=r"\bpoetry\b.*?\bpublish\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="poetry publish releases a package",
        severity=Severity.HIGH,
        description="poetry publish releases a package to the registry. Use --dry-run "
        "first.",
        suggestions=(
            PatternSuggestion(command="poetry publish --dry-run", description="Dry-run publish first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="poetry build", description="Build the distribution and inspect it first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="poetry-remove",
        regex=r"\bpoetry\b.*?\bremove(?=\s|$)",
        reason="poetry remove uninstalls a dependency",
        severity=Severity.MEDIUM,
        description="poetry remove uninstalls a dependency. Verify no other packages "
        "require it.",
        suggestions=(
            PatternSuggestion(command="poetry show --tree | grep {package}", description="Check the dependency tree first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="poetry add {package}@{version}", description="Re-add the pinned version if needed", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="maven-deploy",
        regex=r"\b(?:mvn|mvnw)\b.*?\bdeploy\b",
        reason="mvn deploy publishes artifacts to a remote repository",
        severity=Severity.HIGH,
        description="mvn deploy publishes artifacts to a remote repository. Verify the "
        "target repository is correct — snapshots may overwrite.",
        suggestions=(
            PatternSuggestion(command="mvn help:effective-settings", description="Check which repository the deploy targets", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mvn verify", description="Run full verification before deploying", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="maven-release-perform",
        regex=r"\b(?:mvn|mvnw)\s+.*release:perform\b",
        reason="mvn release:perform publishes a release",
        severity=Severity.HIGH,
        description="mvn release:perform publishes a release. Verify version and "
        "repository before running.",
        suggestions=(
            PatternSuggestion(command="mvn release:prepare --dryRun=true", description="Dry-run the release preparation first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mvn release:rollback", description="Rollback a failed release", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gradle-publish",
        regex=r"\b(?:gradle|gradlew)\s+.*\bpublish\b",
        reason="gradle publish uploads artifacts",
        severity=Severity.HIGH,
        description="gradle publish uploads artifacts to a repository. Verify the target "
        "repository before publishing.",
        suggestions=(
            PatternSuggestion(command="gradle publishToMavenLocal", description="Publish locally to verify first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gradle --dry-run publish", description="Dry-run the publish task", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)


def build_package_managers_pack() -> Pack:
    return Pack(
        id="package_managers",
        name="Package Managers",
        destructive_patterns=PACKAGE_MANAGER_PATTERNS,
        keywords=("npm", "yarn", "pnpm", "pip", "apt", "yum", "dnf", "cargo", "gem", "brew", "poetry", "mvn", "mvnw", "gradle", "gradlew", "publish"),
    )
