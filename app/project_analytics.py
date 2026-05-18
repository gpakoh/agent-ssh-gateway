"""Project analytics and metrics."""

import asyncio
from typing import Optional


class ProjectAnalytics:
    """Analyze project metrics."""

    def __init__(self, ssh_manager):
        self._ssh = ssh_manager

    async def analyze_project(
        self,
        session_id: str,
        path: str,
    ) -> dict:
        """Analyze project and return metrics."""
        metrics = {
            "project_path": path,
            "files": {},
            "code": {},
            "git": {},
            "tests": {},
            "dependencies": {},
        }

        # File statistics
        file_stats = await self._get_file_stats(session_id, path)
        metrics["files"] = file_stats

        # Code statistics
        code_stats = await self._get_code_stats(session_id, path)
        metrics["code"] = code_stats

        # Git statistics
        git_stats = await self._get_git_stats(session_id, path)
        metrics["git"] = git_stats

        # Test statistics
        test_stats = await self._get_test_stats(session_id, path)
        metrics["tests"] = test_stats

        # Dependencies
        dep_stats = await self._get_dependency_stats(session_id, path)
        metrics["dependencies"] = dep_stats

        return metrics

    async def _get_file_stats(self, session_id: str, path: str) -> dict:
        """Get file statistics."""
        # Count files by extension
        cmd = f"cd {path} && find . -type f -not -path './venv/*' -not -path './.git/*' -not -path './__pycache__/*' | sed 's/.*\\\\.//' | sort | uniq -c | sort -rn | head -20"
        result = await self._ssh.execute(session_id, cmd, timeout=15)
        
        extensions = {}
        for line in result["stdout"].strip().split("\n"):
            parts = line.strip().split()
            if len(parts) == 2:
                count = int(parts[0])
                ext = parts[1]
                extensions[ext] = count

        # Total files
        total_cmd = f"cd {path} && find . -type f -not -path './venv/*' -not -path './.git/*' -not -path './__pycache__/*' | wc -l"
        total_result = await self._ssh.execute(session_id, total_cmd, timeout=10)
        total_files = int(total_result["stdout"].strip() or 0)

        # Total directories
        dir_cmd = f"cd {path} && find . -type d -not -path './venv/*' -not -path './.git/*' -not -path './__pycache__/*' | wc -l"
        dir_result = await self._ssh.execute(session_id, dir_cmd, timeout=10)
        total_dirs = int(dir_result["stdout"].strip() or 0)

        return {
            "total_files": total_files,
            "total_directories": total_dirs,
            "extensions": extensions,
        }

    async def _get_code_stats(self, session_id: str, path: str) -> dict:
        """Get code statistics."""
        # Lines of code by language
        loc_cmd = f"cd {path} && find . -name '*.py' -not -path './venv/*' -not -path './__pycache__/*' | xargs wc -l 2>/dev/null | tail -1"
        loc_result = await self._ssh.execute(session_id, loc_cmd, timeout=15)
        
        python_loc = 0
        try:
            python_loc = int(loc_result["stdout"].strip().split()[0])
        except (IndexError, ValueError):
            pass

        # Count classes and functions
        class_cmd = f"cd {path} && grep -r '^class ' --include='*.py' . | wc -l"
        class_result = await self._ssh.execute(session_id, class_cmd, timeout=10)
        class_count = int(class_result["stdout"].strip() or 0)

        func_cmd = f"cd {path} && grep -r '^def ' --include='*.py' . | wc -l"
        func_result = await self._ssh.execute(session_id, func_cmd, timeout=10)
        func_count = int(func_result["stdout"].strip() or 0)

        return {
            "python_lines_of_code": python_loc,
            "classes": class_count,
            "functions": func_count,
        }

    async def _get_git_stats(self, session_id: str, path: str) -> dict:
        """Get git statistics."""
        # Check if git repo
        is_git_cmd = f"cd {path} && test -d .git && echo 'yes' || echo 'no'"
        is_git_result = await self._ssh.execute(session_id, is_git_cmd, timeout=5)
        is_git = is_git_result["stdout"].strip() == "yes"

        if not is_git:
            return {"is_git_repo": False}

        # Commits count
        commits_cmd = f"cd {path} && git log --oneline | wc -l"
        commits_result = await self._ssh.execute(session_id, commits_cmd, timeout=10)
        commits = int(commits_result["stdout"].strip() or 0)

        # Branches
        branches_cmd = f"cd {path} && git branch -a | wc -l"
        branches_result = await self._ssh.execute(session_id, branches_cmd, timeout=10)
        branches = int(branches_result["stdout"].strip() or 0)

        # Contributors
        contrib_cmd = f"cd {path} && git log --format='%an' | sort -u | wc -l"
        contrib_result = await self._ssh.execute(session_id, contrib_cmd, timeout=10)
        contributors = int(contrib_result["stdout"].strip() or 0)

        # Last commit date
        last_cmd = f"cd {path} && git log -1 --format='%ar'"
        last_result = await self._ssh.execute(session_id, last_cmd, timeout=10)
        last_commit = last_result["stdout"].strip()

        return {
            "is_git_repo": True,
            "total_commits": commits,
            "branches": branches,
            "contributors": contributors,
            "last_commit": last_commit,
        }

    async def _get_test_stats(self, session_id: str, path: str) -> dict:
        """Get test statistics."""
        # Check for test files
        test_cmd = f"cd {path} && find . -name 'test_*.py' -o -name '*_test.py' | wc -l"
        test_result = await self._ssh.execute(session_id, test_cmd, timeout=10)
        test_files = int(test_result["stdout"].strip() or 0)

        # Run pytest to get test count
        pytest_cmd = f"cd {path} && python -m pytest --collect-only -q 2>/dev/null | tail -1 || echo '0'"
        pytest_result = await self._ssh.execute(session_id, pytest_cmd, timeout=30)
        
        test_count = 0
        try:
            # Parse "X tests collected"
            output = pytest_result["stdout"].strip()
            if "tests collected" in output:
                test_count = int(output.split()[0])
        except (IndexError, ValueError):
            pass

        return {
            "test_files": test_files,
            "total_tests": test_count,
            "has_tests": test_files > 0,
        }

    async def _get_dependency_stats(self, session_id: str, path: str) -> dict:
        """Get dependency statistics."""
        # Parse requirements.txt
        req_cmd = f"cd {path} && test -f requirements.txt && wc -l requirements.txt | awk '{{print $1}}' || echo '0'"
        req_result = await self._ssh.execute(session_id, req_cmd, timeout=5)
        req_count = int(req_result["stdout"].strip() or 0)

        # Parse pyproject.toml
        pyproject_cmd = f"cd {path} && test -f pyproject.toml && echo 'yes' || echo 'no'"
        pyproject_result = await self._ssh.execute(session_id, pyproject_cmd, timeout=5)
        has_pyproject = pyproject_result["stdout"].strip() == "yes"

        # Check for outdated packages (if pip available)
        outdated_cmd = f"cd {path} && pip list --outdated --format=json 2>/dev/null | wc -l || echo '0'"
        outdated_result = await self._ssh.execute(session_id, outdated_cmd, timeout=30)
        try:
            outdated = int(outdated_result["stdout"].strip() or 0)
        except ValueError:
            outdated = 0

        return {
            "requirements_count": req_count,
            "has_pyproject": has_pyproject,
            "outdated_packages": outdated,
        }
