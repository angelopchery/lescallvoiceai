#!/bin/bash
# Daily pg_dump of the LesCall main database. Keeps the newest 14 dumps.
# Installed in angelo's crontab: 20 3 * * * /home/angelo/lescall/deploy/pg_backup.sh
set -euo pipefail
BACKUP_DIR=/home/angelo/backups
mkdir -p "$BACKUP_DIR"
docker exec lescall-pg pg_dump -U lescall lescall \
  | gzip > "$BACKUP_DIR/lescall-$(date +%F).sql.gz"
ls -1t "$BACKUP_DIR"/lescall-*.sql.gz | tail -n +15 | xargs -r rm -f
