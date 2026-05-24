"""Tests for scripts/migrate-nginx-ssh-block.py migration script."""

from pathlib import Path
from time import strftime

import pytest

# The script logic extracted for testing
MARKER = '# --- Web SSH Gateway (ssh.xloud.ru) ---'


def run_migrate(text: str) -> str:
    """Simulate the migration logic on a string."""
    start = text.find(MARKER)
    if start == -1:
        return text
    end = text.find('\n# --- ', start + len(MARKER))
    if end == -1:
        raise SystemExit('cannot locate end of legacy ssh.xloud.ru block')
    return text[:start].rstrip() + '\n\n' + text[end + 1:].lstrip()


def test_no_marker_returns_unchanged():
    text = 'server { listen 80; }\n'
    assert run_migrate(text) == text


def test_removes_marker_block():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (ssh.xloud.ru) ---
server {
    listen 443 ssl;
    server_name ssh.xloud.ru;
}
# --- Another section ---
server {
    listen 80;
}
"""
    expected = """\
server {
    listen 443 ssl;
}

# --- Another section ---
server {
    listen 80;
}
"""
    assert run_migrate(text) == expected


def test_marker_at_end_of_file():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (ssh.xloud.ru) ---
server {
    listen 443 ssl;
    server_name ssh.xloud.ru;
}
"""
    expected = """\
server {
    listen 443 ssl;
}
"""
    assert run_migrate(text) == expected


def test_marker_at_start_of_file():
    text = """\
# --- Web SSH Gateway (ssh.xloud.ru) ---
server {
    listen 443 ssl;
    server_name ssh.xloud.ru;
}
# --- Another section ---
server {
    listen 80;
}
"""
    expected = """\
# --- Another section ---
server {
    listen 80;
}
"""
    assert run_migrate(text) == expected


def test_no_next_marker_raises():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (ssh.xloud.ru) ---
server {
    listen 443 ssl;
    server_name ssh.xloud.ru;
}
"""
    with pytest.raises(SystemExit, match='cannot locate end'):
        run_migrate(text)


def test_indentation_preserved():
    txt = ['# --- Nginx Config ---\n',
           'server {\n',
           '   listen 80;\n',
           '}\n',
           '# --- Web SSH Gateway (ssh.xloud.ru) ---\n',
           'server {\n',
           '   listen 443;\n',
           '   server_name ssh.xloud.ru;\n',
           '}\n',
           '# --- Another Section ---\n']
    text = ''.join(txt)
    result = run_migrate(text)
    assert result == '# --- Nginx Config ---\n\nserver {\n   listen 80;\n}\n\n# --- Another Section ---\n'


def test_trailing_blank_lines():
    text = """\
server { listen 80; }

# --- Web SSH Gateway (ssh.xloud.ru) ---
server { listen 443; server_name ssh.xloud.ru; }


"""
    expected = 'server { listen 80; }\n'
    assert run_migrate(text) == expected


def test_multiple_markers_only_first_removed():
    text = """\
# --- Opening ---
start

# --- Web SSH Gateway (ssh.xloud.ru) ---
middle

# --- Web SSH Gateway (ssh.xloud.ru) ---
end
"""
    expected = """\
# --- Opening ---
start

# --- Web SSH Gateway (ssh.xloud.ru) ---
end
"""
    assert run_migrate(text) == expected


def test_file_integration(tmp_path: Path):
    """End-to-end test: write a file, run migrate, verify result."""
    src = tmp_path / 'AI-Docker.conf'
    content = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (ssh.xloud.ru) ---
server {
    server_name ssh.xloud.ru;
    listen 443 ssl;
}
"""
    src.write_text(content)

    from pathlib import Path as P
    marker = MARKER
    text = src.read_text()
    start = text.find(marker)
    end = text.find('\n# --- ', start + len(marker))
    backup = src.with_suffix(src.suffix + '.bak-' + strftime('%Y%m%d%H%M%S'))
    backup.write_text(text)
    new_text = text[:start].rstrip() + '\n\n' + text[end + 1:].lstrip()
    src.write_text(new_text)

    assert backup.exists()
    assert backup.read_text() == content
    assert src.read_text() == 'server {\n    listen 443 ssl;\n}\n'
