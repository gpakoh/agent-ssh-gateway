"""Diff generation for file changes."""

import difflib


class DiffGenerator:
    """Generate diffs for file changes."""

    @staticmethod
    def generate_unified_diff(
        old_content: str,
        new_content: str,
        old_path: str = "a/file",
        new_path: str = "b/file",
        context_lines: int = 3,
    ) -> str:
        """Generate unified diff between two contents."""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        
        # Ensure lines end with newline for proper diff
        if old_lines and not old_lines[-1].endswith('\n'):
            old_lines[-1] += '\n'
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
        
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_path,
            tofile=new_path,
            n=context_lines,
        )
        
        return ''.join(diff)

    @staticmethod
    def generate_context_diff(
        old_content: str,
        new_content: str,
        old_path: str = "a/file",
        new_path: str = "b/file",
        context_lines: int = 3,
    ) -> str:
        """Generate context diff between two contents."""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        
        if old_lines and not old_lines[-1].endswith('\n'):
            old_lines[-1] += '\n'
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
        
        diff = difflib.context_diff(
            old_lines,
            new_lines,
            fromfile=old_path,
            tofile=new_path,
            n=context_lines,
        )
        
        return ''.join(diff)

    @staticmethod
    def generate_inline_diff(
        old_content: str,
        new_content: str,
    ) -> list[dict]:
        """Generate inline diff showing line-by-line changes."""
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        
        result = []
        line_num_old = 1
        line_num_new = 1
        
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines
        ).get_opcodes():
            if tag == 'equal':
                for i in range(i1, i2):
                    result.append({
                        "type": "equal",
                        "old_line": line_num_old,
                        "new_line": line_num_new,
                        "content": old_lines[i],
                    })
                    line_num_old += 1
                    line_num_new += 1
            elif tag == 'delete':
                for i in range(i1, i2):
                    result.append({
                        "type": "removed",
                        "old_line": line_num_old,
                        "new_line": None,
                        "content": old_lines[i],
                    })
                    line_num_old += 1
            elif tag == 'insert':
                for j in range(j1, j2):
                    result.append({
                        "type": "added",
                        "old_line": None,
                        "new_line": line_num_new,
                        "content": new_lines[j],
                    })
                    line_num_new += 1
            elif tag == 'replace':
                # Removed lines
                for i in range(i1, i2):
                    result.append({
                        "type": "removed",
                        "old_line": line_num_old,
                        "new_line": None,
                        "content": old_lines[i],
                    })
                    line_num_old += 1
                # Added lines
                for j in range(j1, j2):
                    result.append({
                        "type": "added",
                        "old_line": None,
                        "new_line": line_num_new,
                        "content": new_lines[j],
                    })
                    line_num_new += 1
        
        return result

    @staticmethod
    def count_changes(diff_content: str) -> dict:
        """Count additions and deletions in diff."""
        additions = 0
        deletions = 0
        
        for line in diff_content.splitlines():
            if line.startswith('+') and not line.startswith('+++'):
                additions += 1
            elif line.startswith('-') and not line.startswith('---'):
                deletions += 1
        
        return {
            "additions": additions,
            "deletions": deletions,
            "total_changes": additions + deletions,
        }

    @staticmethod
    def format_diff_for_display(diff_content: str) -> str:
        """Format diff for terminal display with colors."""
        lines = []
        for line in diff_content.splitlines():
            if line.startswith('+') and not line.startswith('+++'):
                lines.append(f"\033[92m{line}\033[0m")  # Green
            elif line.startswith('-') and not line.startswith('---'):
                lines.append(f"\033[91m{line}\033[0m")  # Red
            elif line.startswith('@@'):
                lines.append(f"\033[36m{line}\033[0m")  # Cyan
            else:
                lines.append(line)
        return '\n'.join(lines)
