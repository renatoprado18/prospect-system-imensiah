#!/bin/bash
# Sincroniza banco local com Neon (produção)
# Uso: ./scripts/sync-local-db.sh

set -e

echo "🔄 Sincronizando banco local com Neon..."

# Carregar variáveis do .env
source "$(dirname "$0")/../.env"

# Extrair credenciais do POSTGRES_URL
DB_HOST=$(echo $POSTGRES_URL | sed -n 's/.*@\([^/]*\)\/.*/\1/p')
DB_USER=$(echo $POSTGRES_URL | sed -n 's/.*:\/\/\([^:]*\):.*/\1/p')
DB_PASS=$(echo $POSTGRES_URL | sed -n 's/.*:\/\/[^:]*:\([^@]*\)@.*/\1/p')
DB_NAME=$(echo $POSTGRES_URL | sed -n 's/.*\/\([^?]*\).*/\1/p')

LOCAL_DB="intel"
PG_DUMP="/usr/local/opt/postgresql@17/bin/pg_dump"
PSQL="/usr/local/opt/postgresql@15/bin/psql"
CREATEDB="/usr/local/opt/postgresql@15/bin/createdb"
DROPDB="/usr/local/opt/postgresql@15/bin/dropdb"

TMP_DIR="/tmp/intel_sync"
mkdir -p $TMP_DIR

echo "📥 Exportando schema do Neon..."
PGPASSWORD="$DB_PASS" $PG_DUMP -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    --schema-only --no-owner --no-privileges \
    -f "$TMP_DIR/schema.sql" 2>/dev/null

echo "📥 Exportando dados do Neon..."
PGPASSWORD="$DB_PASS" $PG_DUMP -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    --data-only --no-owner --no-privileges \
    -f "$TMP_DIR/data.sql" 2>/dev/null

# Remover configurações incompatíveis com PG15
sed -i '' '/transaction_timeout/d' "$TMP_DIR/schema.sql" "$TMP_DIR/data.sql" 2>/dev/null

echo "🗑️  Recriando banco local..."
$DROPDB $LOCAL_DB 2>/dev/null || true
$CREATEDB $LOCAL_DB

# Forca timezone UTC (Neon e UTC; sem isso, sync-to-remote compara
# atualizado_em em BRT contra last_sync em UTC e pula edicoes).
$PSQL -d $LOCAL_DB -c "ALTER DATABASE $LOCAL_DB SET timezone = 'UTC';" -q 2>/dev/null

echo "📤 Importando schema..."
$PSQL -d $LOCAL_DB -f "$TMP_DIR/schema.sql" -q 2>/dev/null

echo "📤 Importando dados..."
$PSQL -d $LOCAL_DB -f "$TMP_DIR/data.sql" -q 2>/dev/null

# Verificar
CONTACTS=$($PSQL -d $LOCAL_DB -t -c "SELECT COUNT(*) FROM contacts")
MESSAGES=$($PSQL -d $LOCAL_DB -t -c "SELECT COUNT(*) FROM messages")

# Marcar .last_sync como NOW (UTC). Acabamos de copiar prod->local,
# entao local = prod. Sem isso, o proximo push acha que TUDO mudou
# e empurra de volta com risco de sobrescrever edicoes feitas no prod
# enquanto local estava stale.
LAST_SYNC_FILE="$(dirname "$0")/../.last_sync"
date -u '+%Y-%m-%d %H:%M:%S' > "$LAST_SYNC_FILE"

echo ""
echo "✅ Sincronização completa!"
echo "   Contatos: $CONTACTS"
echo "   Mensagens: $MESSAGES"
echo ""
echo "🚀 Reinicie o servidor: lsof -ti:8000 | xargs kill; USE_LOCAL_DB=1 uvicorn main:app --reload"
