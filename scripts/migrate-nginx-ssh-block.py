#!/usr/bin/env python3
"""Remove the legacy ssh.xloud.ru server block from AI-Docker.conf.

The block is identified by the marker comment:
    # --- Web SSH Gateway (ssh.xloud.ru) ---

The script removes everything from that marker up to the next section
marker (# --- ...), creates a backup, and writes the cleaned file.
"""

from pathlib import Path
from time import strftime
import sys

path = Path('/etc/nginx/sites-available/AI-Docker.conf')
marker = '# --- Web SSH Gateway (ssh.xloud.ru) ---'

if not path.exists():
    print(f'{path} not found, nothing to migrate')
    sys.exit(0)

text = path.read_text()
start = text.find(marker)

if start == -1:
    print(f'Marker "{marker}" not found in {path}, nothing to migrate')
    sys.exit(0)

end = text.find('\n# --- ', start + len(marker))
if end == -1:
    print('Cannot locate end of legacy ssh.xloud.ru block')
    sys.exit(1)

backup = path.with_suffix(path.suffix + '.bak-' + strftime('%Y%m%d%H%M%S'))
backup.write_text(text)
print(f'Backup saved to {backup}')

new_text = text[:start].rstrip() + '\n\n' + text[end + 1:].lstrip()
path.write_text(new_text)
print(f'Removed old ssh.xloud.ru block from {path}')
