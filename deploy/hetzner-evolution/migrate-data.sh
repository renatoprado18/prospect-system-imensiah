#!/usr/bin/env bash
# Migra dados Postgres do Evolution Railway -> Hetzner.
# Roda LOCAL (na maquina do dev), nao no VPS.
#
# Pre-req:
#   - pg_dump local instalado (brew install postgresql@16)
#   - VPS ja com docker-compose up (postgres rodando)
#   - SSH key configurada pro VPS
#
# Uso:
#   VPS_IP=1.2.3.4 ./migrate-data.sh

set -euo pipefail

VPS_IP="${VPS_IP:?defina VPS_IP=<ip-do-hetzner>}"
RAILWAY_PG="${RAILWAY_PG:-postgresql://postgres:fRQREIzMCNqSTITkYfFtWgjMcMmqoxrH@ballast.proxy.rlwy.net:53325/railway}"

DUMP_FILE="/tmp/evolution_migration_$(date +%Y%m%d_%H%M%S).sql"

echo "==> Dump Railway Postgres -> ${DUMP_FILE}"
pg_dump --no-owner --no-acl --clean --if-exists "$RAILWAY_PG" > "$DUMP_FILE"
echo "    Dump: $(du -h "$DUMP_FILE" | cut -f1)"

echo "==> Upload dump pro VPS"
scp "$DUMP_FILE" "root@${VPS_IP}:/tmp/migration.sql"

echo "==> Restore no Postgres do Hetzner (via container)"
ssh "root@${VPS_IP}" "docker exec -i evolution-postgres psql -U evolution -d evolution < /tmp/migration.sql"

echo "==> Limpando dump no VPS"
ssh "root@${VPS_IP}" "rm -f /tmp/migration.sql"

echo "==> Restart Evolution (pega nova base)"
ssh "root@${VPS_IP}" "cd /opt/evolution && docker compose restart evolution"

echo ""
echo "OK. Dump local mantido em: $DUMP_FILE"
echo "Confira historico: curl -H 'apikey: \$EVOLUTION_API_KEY' https://wa.almeida-prado.com/chat/findChats/rap-whatsapp"
