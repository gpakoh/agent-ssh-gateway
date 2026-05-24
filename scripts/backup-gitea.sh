#!/bin/bash
# Gitea + PostgreSQL backup script
# Run daily via cron: 0 3 * * * /path/to/scripts/backup-gitea.sh

set -euo pipefail

BACKUP_BASE="${BACKUP_DIR:-/var/backups/gitea}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DATE="$(date +%Y%m%d%H%M%S)"
BACKUP_PATH="${BACKUP_BASE}/${DATE}"
GITEA_HOST="${GITEA_HOST:-192.168.1.103}"
SSH_KEY="${SSH_KEY:-/root/.ssh/id_ed25519}"

mkdir -p "${BACKUP_PATH}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Gitea backup to ${BACKUP_PATH}"

# 1. PostgreSQL dump
echo "  -> Dumping PostgreSQL..."
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i "${SSH_KEY}" "root@${GITEA_HOST}" \
  "docker exec postgres-gitea pg_dump -U gitea -d gitea --clean --if-exists" \
  | gzip > "${BACKUP_PATH}/gitea-db.sql.gz"
echo "  -> DB dump: $(du -h "${BACKUP_PATH}/gitea-db.sql.gz" | cut -f1)"

# 2. Gitea data volume (repos, config, etc.)
echo "  -> Copying Gitea data volume..."
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i "${SSH_KEY}" "root@${GITEA_HOST}" \
  "tar czf - -C /media/1TB/Docker/compose/gitea-stack gitea-data" \
  > "${BACKUP_PATH}/gitea-data.tar.gz"
echo "  -> Data archive: $(du -h "${BACKUP_PATH}/gitea-data.tar.gz" | cut -f1)"

# 3. Compose + config files (lightweight, no compression needed)
echo "  -> Copying compose files..."
scp -o BatchMode=yes -o StrictHostKeyChecking=no -i "${SSH_KEY}" \
  "root@${GITEA_HOST}:/media/1TB/Docker/compose/gitea-stack/docker-compose.yml" \
  "${BACKUP_PATH}/"
scp -o BatchMode=yes -o StrictHostKeyChecking=no -i "${SSH_KEY}" \
  "root@${GITEA_HOST}:/media/1TB/Docker/compose/gitea-stack/config.yaml" \
  "${BACKUP_PATH}/"

# 4. Backup metadata
{
  echo "backup_date: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "gitea_host: ${GITEA_HOST}"
  echo "files:"
  ls -lh "${BACKUP_PATH}/" | tail -n +2 | awk '{print "  - " $9 " (" $5 ")"}'
} > "${BACKUP_PATH}/backup-info.yaml"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete: ${BACKUP_PATH}"

# 5. Rotate old backups
echo "  -> Cleaning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_BASE}" -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rotation complete"
