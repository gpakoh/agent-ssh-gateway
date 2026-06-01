"""Global search and replace across project files."""

import re
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _is_safe_regex(pattern: str) -> None:
    """Check regex for dangerous nested quantifiers (ReDoS)."""
    if re.search(r'\([^)]*[\+\*]\)[\+\*]', pattern):
        raise ValueError("Dangerous regex pattern: nested quantifiers (potential ReDoS)")


@dataclass
class SearchMatch:
    """Single search match."""
    path: str
    line: int
    column: int
    content: str
    context_before: str = ""
    context_after: str = ""


@dataclass
class ReplaceResult:
    """Result of replace operation."""
    path: str
    replacements_count: int
    success: bool
    error: Optional[str] = None


class GlobalSearchReplace:
    """Global search and replace manager."""

    def __init__(self, ssh_manager, file_editor):
        self._ssh = ssh_manager
        self._file_editor = file_editor

    async def search(
        self,
        session_id: str,
        path: str,
        query: str,
        file_pattern: str = "*",
        use_regex: bool = False,
        case_sensitive: bool = True,
        context_lines: int = 2,
    ) -> list[SearchMatch]:
        """Search for pattern across project files."""
        results: list[SearchMatch] = []

        # Build Grep Command
        grep_flags = "-rn"
        if not case_sensitive:
            grep_flags += " -i"
        if use_regex:
            grep_flags += " -E"
        
        # Escape Query For Shell
        escaped_query = query.replace("'", "'\"'\"'")
        
        # Find Matching Files
        if file_pattern != "*":
            find_cmd = f"cd {path} && find . -type f -name '{file_pattern}' -not -path './venv/*' -not -path './.git/*' -not -path './__pycache__/*' | head -100"
            find_result = await self._ssh.execute(session_id, find_cmd, timeout=15)
            files = [f.strip() for f in find_result["stdout"].strip().split("\n") if f.strip()]
            
            if not files:
                return results
            
            # Search In Specific Files
            files_arg = " ".join(files)
            cmd = f"cd {path} && grep {grep_flags} -C {context_lines} '{escaped_query}' {files_arg} 2>/dev/null || true"
        else:
            # Search In All Files
            cmd = f"cd {path} && grep {grep_flags} -C {context_lines} --include='*' '{escaped_query}' . 2>/dev/null || true"
        
        result = await self._ssh.execute(session_id, cmd, timeout=30)
        
        if result["exit_code"] != 0 and not result["stdout"]:
            return results
        
        # Parse Grep Output
        lines = result["stdout"].split("\n")
        
        for line in lines:
            if line.startswith("--"):
                continue
            
            # Match: Filename:line:content
            match = re.match(r'^(.+):(\d+):(.*)$', line)
            if match:
                file_path = match.group(1).lstrip("./")
                line_num = int(match.group(2))
                content = match.group(3)
                
                # Calculate Column
                if use_regex:
                    _is_safe_regex(query)
                    pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
                    match_obj = pattern.search(content)
                    column = match_obj.start() if match_obj else 0
                else:
                    query_lower = query.lower() if not case_sensitive else query
                    content_lower = content.lower() if not case_sensitive else content
                    column = content_lower.find(query_lower)
                    if column == -1:
                        column = 0
                
                results.append(SearchMatch(
                    path=file_path,
                    line=line_num,
                    column=column,
                    content=content.strip(),
                ))
        
        return results

    async def replace(
        self,
        session_id: str,
        path: str,
        search_query: str,
        replace_with: str,
        file_pattern: str = "*",
        use_regex: bool = False,
        case_sensitive: bool = True,
        dry_run: bool = False,
    ) -> list[ReplaceResult]:
        """Replace occurrences across project files."""
        results: list[ReplaceResult] = []

        # First Search To Find Files
        matches = await self.search(
            session_id, path, search_query, file_pattern,
            use_regex, case_sensitive, context_lines=0
        )
        
        # Group By File
        files_to_modify: dict[str, list[SearchMatch]] = {}
        for match in matches:
            if match.path not in files_to_modify:
                files_to_modify[match.path] = []
            files_to_modify[match.path].append(match)
        
        if dry_run:
            # Just Return What Would Be Changed
            for file_path, file_matches in files_to_modify.items():
                results.append(ReplaceResult(
                    path=file_path,
                    replacements_count=len(file_matches),
                    success=True,
                ))
            return results
        
        # Perform Replacements
        for file_path in files_to_modify:
            full_path = f"{path}/{file_path}"
            
            try:
                # Read File
                content = await self._file_editor.read_file(session_id, full_path)
                
                # Perform Replacement
                if use_regex:
                    _is_safe_regex(search_query)
                    flags = 0 if case_sensitive else re.IGNORECASE
                    pattern = re.compile(search_query, flags)
                    new_content, count = pattern.subn(replace_with, content)
                else:
                    if case_sensitive:
                        new_content = content.replace(search_query, replace_with)
                        count = content.count(search_query)
                    else:
                        # Case-insensitive Replacement Is Tricky
                        # Use Regex For This
                        flags = re.IGNORECASE
                        pattern = re.compile(re.escape(search_query), flags)
                        new_content, count = pattern.subn(replace_with, content)
                
                if count > 0:
                    try:
                        await self._file_editor.write_file(session_id, full_path, new_content)
                        results.append(ReplaceResult(
                            path=file_path,
                            replacements_count=count,
                            success=True,
                        ))
                    except Exception as exc:
                        results.append(ReplaceResult(
                            path=file_path,
                            replacements_count=0,
                            success=False,
                            error=str(exc),
                        ))
                else:
                    results.append(ReplaceResult(
                        path=file_path,
                        replacements_count=0,
                        success=True,
                    ))
                    
            except Exception as exc:
                logger.error("Replace failed for %s: %s", file_path, exc)
                results.append(ReplaceResult(
                    path=file_path,
                    replacements_count=0,
                    success=False,
                    error=str(exc),
                ))
        
        return results

    async def count_occurrences(
        self,
        session_id: str,
        path: str,
        query: str,
        file_pattern: str = "*",
        use_regex: bool = False,
        case_sensitive: bool = True,
    ) -> int:
        """Count total occurrences."""
        matches = await self.search(
            session_id, path, query, file_pattern,
            use_regex, case_sensitive, context_lines=0
        )
        return len(matches)
