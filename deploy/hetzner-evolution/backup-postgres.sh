#!/bin/bash
# Backup diario do Postgres do Evolution.
# Instalar como /etc/cron.daily/evolution-backup no VPS.
#
# Setup:
#   scp deploy/hetzner-evolution/backup-postgres.sh root@<IP>:/etc/cron.daily/evolution-backup
#   ssh root@<IP> "chmod +x /etc/cron.daily/evolution-backup"

set -euo pipefail

BACKUP_DIR="/var/backups/evolution"
RETENTION_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="${BACKUP_DIR}/evolution_${DATE}.sql.gz"

mkdir -p "$BACKUP_DIR"

# Dump dentro do container Postgres, gzip no host
docker exec evolution-postgres pg_dump -U evolution evolution \
    | gzip > "$DUMP_FILE"

# Verifica tamanho minimo (>1MB) — se nao, alerta no syslog
SIZE=$(stat -c%s "$DUMP_FILE")
if [ "$SIZE" -lt 1048576 ]; then
    logger -t evolution-backup "ERRO: dump de $DUMP_FILE so $SIZE bytes (esperado > 1MB)"
    exit 1
fi

# Rotacao: remove dumps mais antigos que RETENTION_DAYS
find "$BACKUP_DIR" -name "evolution_*.sql.gz" -mtime +${RETENTION_DAYS} -delete

logger -t evolution-backup "OK: $DUMP_FILE ($(du -h "$DUMP_FILE" | cut -f1))"
