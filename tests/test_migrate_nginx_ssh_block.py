from pathlib import Path

import pytest

MARKER = '# --- Web SSH Gateway (gateway.example.com) ---'


def run_migrate(text: str) -> str:
    start = text.find(MARKER)
    if start == -1:
        return text
    end = text.find('\n# --- ', start + len(MARKER))
    if end == -1:
        raise SystemExit('cannot locate end of legacy gateway.example.com block')
    before = text[:start].rstrip()
    after = text[end + 1:].lstrip()
    if before:
        return before + '\n\n' + after
    return after


def test_no_marker_returns_unchanged():
    assert run_migrate('server { listen 80; }\n') == 'server { listen 80; }\n'


def test_removes_marker_block():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (gateway.example.com) ---
server {
    listen 443 ssl;
    server_name gateway.example.com;
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


def test_marker_at_end_of_file_raises():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (gateway.example.com) ---
server {
    listen 443 ssl;
    server_name gateway.example.com;
}
"""
    with pytest.raises(SystemExit, match='cannot locate end'):
        run_migrate(text)


def test_marker_at_start():
    text = "# --- Web SSH Gateway (gateway.example.com) ---\nblock\n# --- Next ---\nend\n"
    assert run_migrate(text) == "# --- Next ---\nend\n"


def test_no_next_marker_raises():
    text = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (gateway.example.com) ---
server {
    listen 443 ssl;
    server_name gateway.example.com;
}
"""
    with pytest.raises(SystemExit, match='cannot locate end'):
        run_migrate(text)


def test_indentation_preserved():
    text = '\n'.join([
        '# --- Nginx Config ---',
        'server {',
        '   listen 80;',
        '}',
        '# --- Web SSH Gateway (gateway.example.com) ---',
        'server {',
        '   listen 443;',
        '   server_name gateway.example.com;',
        '}',
        '# --- Another Section ---',
        '',
    ]) + '\n'
    result = run_migrate(text)
    assert '# --- Nginx Config ---' in result
    assert '# --- Another Section ---' in result
    assert '# --- Web SSH Gateway' not in result
    assert result.startswith('# --- Nginx Config ---')


def test_trailing_blank_lines_raises():
    text = 'server { listen 80; }\n\n# --- Web SSH Gateway (gateway.example.com) ---\nblock\n\n\n'
    with pytest.raises(SystemExit, match='cannot locate end'):
        run_migrate(text)


def test_multiple_markers_only_first_removed():
    text = """\
# --- Opening ---
start

# --- Web SSH Gateway (gateway.example.com) ---
middle

# --- Web SSH Gateway (gateway.example.com) ---
end
"""
    result = run_migrate(text)
    assert '# --- Opening ---' in result
    assert '# --- Web SSH Gateway (gateway.example.com) ---' in result  # second instance remains
    assert result.count('# --- Web SSH Gateway') == 1
    assert 'middle' not in result


def test_realistic_file(tmp_path: Path):
    src = tmp_path / 'AI-Docker.conf'
    content = """\
server {
    listen 443 ssl;
}

# --- Web SSH Gateway (gateway.example.com) ---
server {
    server_name gateway.example.com;
    listen 443 ssl;
}
# --- Another section ---
server {
    listen 80;
}
"""
    src.write_text(content)

    from pathlib import Path as P
    from time import strftime
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
    assert '# --- Another section ---' in src.read_text()
    assert '# --- Web SSH Gateway' not in src.read_text()
