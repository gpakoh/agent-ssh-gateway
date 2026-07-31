from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity
from app.packs import Pack

GIT_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="git-push-force",
        regex=r"git\b.*?\bpush(?:[^\n;]*\s(?:--force(?:=\S*)?|--force-with-lease(?:=\S*)?|-f)(?=\s|$)|(?:\s+\S+)*\s+(?:\$?[\"']|\\)*\+\S+)",
        reason="Force push rewrites remote history",
        severity=Severity.CRITICAL,
        description="All forms of force push (--force, --force-with-lease, +refspec) "
        "rewrite remote history and may cause data loss for collaborators.",
        suggestions=(
            PatternSuggestion(command="git push --force-with-lease", description="Safer force push — respects remote changes"),
            PatternSuggestion(command="git push {remote} {branch}", description="Normal push without force"),
        ),
    ),
DestructivePattern(
        name="git-push-mirror",
        regex=r"git\b.*?\bpush\b[^\n;]*(?:^|\s)--mirror(?:=\S*)?(?=\s|$)",
        reason="git push --mirror force-updates and deletes remote refs",
        severity=Severity.CRITICAL,
        description="Mirror push deletes remote refs absent locally. Extremely destructive.",
        suggestions=(
            PatternSuggestion(command="git remote -v", description="List remotes first"),
            PatternSuggestion(command="git push --all {remote}", description="Push all branches without mirror semantics"),
        ),
    ),
DestructivePattern(
        name="git-push-dynamic-arg",
        regex=r"git\b.*?\bpush\b[^\n;]*(?:\\|\$|`|\*|\?|\{|\}|\[)",
        reason="Shell-expanded push argument cannot be verified as non-forcing",
        severity=Severity.HIGH,
        description="Variables, globs, or escaped args in git push may expand to destructive refspecs.",
        suggestions=(
            PatternSuggestion(command="echo {arg}", description="Expand and review argument first"),
            PatternSuggestion(command="git push {remote} {branch}", description="Use an explicit refspec"),
        ),
    ),
DestructivePattern(
        name="git-rebase",
        regex=r"git\b.*?\brebase\b",
        reason="git rebase rewrites commit history",
        severity=Severity.HIGH,
        description="Rebase rewrites commits. Force push needed afterward. Conflicts may lose changes.",
        suggestions=(
            PatternSuggestion(command="git merge {branch}", description="Merge instead of rebase — preserves history"),
            PatternSuggestion(command="git rebase --interactive HEAD~{n}", description="Interactive rebase with control"),
        ),
    ),
DestructivePattern(
        name="git-commit-amend",
        regex=r"git\b.*?\bcommit\s+.*--amend",
        reason="git commit --amend rewrites the last commit",
        severity=Severity.HIGH,
        description="Amending a pushed commit rewrites history. Previous commit is lost.",
        suggestions=(
            PatternSuggestion(command="git commit --amend --no-edit", description="Amend without changing the message"),
            PatternSuggestion(command="git log -1", description="Review last commit before amending"),
        ),
    ),
DestructivePattern(
        name="git-filter-branch",
        regex=r"git\b.*?\bfilter-branch\b",
        reason="git filter-branch rewrites entire repository history",
        severity=Severity.CRITICAL,
        description="Rewrites ALL commits. Extremely dangerous. Use filter-repo instead.",
        suggestions=(
            PatternSuggestion(command="git filter-repo --path {path}", description="Use filter-repo with path scope"),
            PatternSuggestion(command="git clone {repo} /tmp/backup", description="Clone a backup before rewriting history"),
        ),
    ),
DestructivePattern(
        name="git-filter-repo",
        regex=r"git\b.*?\bfilter-repo\b",
        reason="git filter-repo rewrites repository history",
        severity=Severity.CRITICAL,
        description="Modern replacement for filter-branch. Still rewrites all history.",
        suggestions=(
            PatternSuggestion(command="git filter-repo --dry-run", description="Preview changes first"),
            PatternSuggestion(command="git clone {repo} /tmp/backup", description="Clone a backup before rewriting history"),
        ),
    ),
DestructivePattern(
        name="git-cherry-pick",
        regex=r"git\b.*?\bcherry-pick\b",
        reason="git cherry-pick can introduce duplicate commits",
        severity=Severity.MEDIUM,
        description="Cherry-pick creates duplicate commits. Can cause merge conflicts later.",
        suggestions=(
            PatternSuggestion(command="git log --oneline -10", description="Review recent commits before picking"),
            PatternSuggestion(command="git merge {branch}", description="Merge instead of cherry-pick"),
        ),
    ),
DestructivePattern(
        name="git-reflog-expire",
        regex=r"git\b.*?\breflog\s+expire",
        reason="git reflog expire removes recovery entries",
        severity=Severity.HIGH,
        description="Reflog is the last resort for recovery. Expiring entries may lose data permanently.",
        suggestions=(
            PatternSuggestion(command="git reflog show", description="Review reflog before expiring"),
            PatternSuggestion(command="git reflog expire --expire=90.days --all", description="Use a longer expiration time"),
        ),
    ),
DestructivePattern(
        name="git-gc-aggressive",
        regex=r"git\b.*?\bgc\s+.*--(?:aggressive|prune)",
        reason="git gc with aggressive/prune removes recoverable objects",
        severity=Severity.HIGH,
        description="Prunes loose objects. Reflog entries and stashed changes may be lost.",
        suggestions=(
            PatternSuggestion(command="git count-objects -vH", description="Check repo size before gc"),
            PatternSuggestion(command="git gc --auto", description="Run auto gc instead"),
        ),
    ),
DestructivePattern(
        name="git-worktree-remove",
        regex=r"git\b.*?\bworktree\s+remove",
        reason="git worktree remove deletes a linked working tree",
        severity=Severity.HIGH,
        description="Uncommitted changes in the worktree are lost.",
        suggestions=(
            PatternSuggestion(command="git worktree list", description="List all worktrees first"),
            PatternSuggestion(command="git stash -u && git worktree remove {name}", description="Stash changes before removing"),
        ),
    ),
DestructivePattern(
        name="git-submodule-deinit",
        regex=r"git\b.*?\bsubmodule\s+deinit",
        reason="git submodule deinit removes submodule configuration",
        severity=Severity.MEDIUM,
        description="Submodule working tree is removed. Clone again to restore.",
        suggestions=(
            PatternSuggestion(command="git submodule status", description="Check submodule status first"),
            PatternSuggestion(command="git submodule deinit -f {name}", description="Force deinit if needed"),
        ),
    ),
DestructivePattern(
        name="git-add-all-dot",
        regex=r"git\b.*?\badd\s+['\"]?\.['\"]?(?:\s|$)",
        reason="git add . stages everything including secrets, .env, build artifacts",
        severity=Severity.MEDIUM,
        description="All changes in the repo are staged. Secrets or build artifacts may be committed.",
        suggestions=(
            PatternSuggestion(command="git status", description="Review changes before staging"),
            PatternSuggestion(command="git add {specific-file}", description="Stage specific files only"),
        ),
    ),
DestructivePattern(
        name="git-add-all-flag",
        regex=r"git\b.*?\badd\s+(?:-A|--all)\b",
        reason="git add -A/--all stages all changes including secrets",
        severity=Severity.MEDIUM,
        description="Tracks new and modified files. Secrets may be unintentionally staged.",
        suggestions=(
            PatternSuggestion(command="git status", description="Review changes before staging"),
            PatternSuggestion(command="git add -p", description="Interactive staging to review each change"),
        ),
    ),
DestructivePattern(
        name="git-push-to-master",
        regex=r"git\s+(?:\S+\s+)*push\s+(?:.*[\s:/])?\+?master(?:\s|$)",
        reason="Direct push to master/main branch is blocked",
        severity=Severity.MEDIUM,
        description="Push to default branch may bypass review. Use a feature branch and PR.",
        suggestions=(
            PatternSuggestion(command="git checkout -b feature/{name}", description="Create a feature branch"),
            PatternSuggestion(command="gh pr create", description="Create a PR instead of direct push"),
        ),
    ),
DestructivePattern(
        name="git-push-to-main",
        regex=r"git\s+(?:\S+\s+)*push\s+(?:.*[\s:/])?\+?main(?:\s|$)",
        reason="Direct push to main branch is blocked",
        severity=Severity.MEDIUM,
        description="Push to default branch may bypass review. Use a feature branch and PR.",
        suggestions=(
            PatternSuggestion(command="git checkout -b feature/{name}", description="Create a feature branch"),
            PatternSuggestion(command="gh pr create", description="Create a PR instead of direct push"),
        ),
    ),
)

def build_git_pack() -> Pack:
    return Pack(id="git", name="Git patterns",
        destructive_patterns=GIT_PATTERNS,
        keywords=("git",),
    )
