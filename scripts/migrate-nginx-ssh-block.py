#!/usr/bin/env python3
"""Remove a legacy nginx server block identified by a marker comment.

The script removes everything from the marker up to the next section
marker (# --- ...), creates a backup, and writes the cleaned file.

Use environment variables to configure:
  NGINX_CONFIG_PATH   Path to nginx config (default: /etc/nginx/sites-available/example.conf)
  NGINX_BLOCK_MARKER  Marker string to identify the block start
"""

import os
import sys
from pathlib import Path
from time import strftime

path = Path(os.environ.get("NGINX_CONFIG_PATH", "/etc/nginx/sites-available/example.conf"))
marker = os.environ.get("NGINX_BLOCK_MARKER", "# --- agent-ssh-gateway ---")

if not path.exists():
    print(f"{path} not found, nothing to migrate")
    sys.exit(0)

text = path.read_text()
start = text.find(marker)

if start == -1:
    print(f'Marker "{marker}" not found in {path}, nothing to migrate')
    sys.exit(0)

end = text.find("\n# --- ", start + len(marker))
if end == -1:
    print("Cannot locate end of legacy block")
    sys.exit(1)

backup = path.with_suffix(path.suffix + ".bak-" + strftime("%Y%m%d%H%M%S"))
backup.write_text(text)
print(f"Backup saved to {backup}")

new_text = text[:start].rstrip() + "\n\n" + text[end + 1 :].lstrip()
path.write_text(new_text)
print(f"Removed old block from {path}")
