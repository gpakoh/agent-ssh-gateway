#!/bin/bash
# Example: Database + data volume backup script
# Customize for your infrastructure. Keep real backup scripts private.

set -euo pipefail

BACKUP_BASE="${BACKUP_DIR:-/var/backups/service}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DATE="$(date +%Y%m%d%H%M%S)"
BACKUP_PATH="${BACKUP_BASE}/${DATE}"
TARGET_HOST="${TARGET_HOST:-}"
TARGET_USER="${TARGET_USER:-}"
SSH_KEY="${SSH_KEY:-~/.ssh/id_ed25519}"

if [ -z "$TARGET_HOST" ]; then
    echo "TARGET_HOST is required"
    exit 1
fi

mkdir -p "${BACKUP_PATH}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting backup to ${BACKUP_PATH}"

# Example: dump a database
# ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "${SSH_KEY}" "${TARGET_USER}@${TARGET_HOST}" \
#   "docker exec mydb pg_dump -U myuser -d mydb --clean --if-exists" \
#   | gzip > "${BACKUP_PATH}/db.sql.gz"

# Example: copy data volumes
# ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "${SSH_KEY}" "${TARGET_USER}@${TARGET_HOST}" \
#   "tar czf - -C /srv/data ." > "${BACKUP_PATH}/data.tar.gz"

# Metadata
{
    echo "backup_date: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "target_host: ${TARGET_HOST}"
    ls -lh "${BACKUP_PATH}/"
} > "${BACKUP_PATH}/backup-info.yaml"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete: ${BACKUP_PATH}"

# Rotate old backups
find "${BACKUP_BASE}" -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rotation complete"
