#!/bin/bash
# Sincroniza alterações do banco local para Neon (produção)
# Uso: ./scripts/sync-to-remote.sh [--force]
#
# Sincroniza apenas registros modificados desde o último sync
# Use --force para sincronizar tudo

set -e

SCRIPT_DIR="$(dirname "$0")"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LAST_SYNC_FILE="$PROJECT_DIR/.last_sync"

# Carregar variáveis do .env
source "$PROJECT_DIR/.env"

# Extrair credenciais do POSTGRES_URL
DB_HOST=$(echo $POSTGRES_URL | sed -n 's/.*@\([^:/]*\).*/\1/p')
DB_PORT=$(echo $POSTGRES_URL | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')
DB_USER=$(echo $POSTGRES_URL | sed -n 's/.*:\/\/\([^:]*\):.*/\1/p')
DB_PASS=$(echo $POSTGRES_URL | sed -n 's/.*:\/\/[^:]*:\([^@]*\)@.*/\1/p')
DB_NAME=$(echo $POSTGRES_URL | sed -n 's/.*\/\([^?]*\).*/\1/p')

LOCAL_DB="intel"
PSQL_LOCAL="psql -d $LOCAL_DB -t -A"
PSQL_REMOTE="PGPASSWORD=$DB_PASS psql -h $DB_HOST -p ${DB_PORT:-5432} -U $DB_USER -d $DB_NAME -t -A"

# Tabelas para sincronizar (ordem importa para foreign keys)
TABLES=(
    "contacts"
    "projects"
    "project_notes"
    "editorial_posts"
    "hot_takes"
    "calendar_events"
    "tasks"
    "veiculos"
    "veiculo_manutencoes"
    "veiculo_ordens_servico"
)

# Última sincronização
if [[ -f "$LAST_SYNC_FILE" && "$1" != "--force" ]]; then
    LAST_SYNC=$(cat "$LAST_SYNC_FILE")
    echo "📅 Última sincronização: $LAST_SYNC"
else
    LAST_SYNC="1970-01-01 00:00:00"
    echo "🔄 Sincronização completa (--force ou primeira vez)"
fi

CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
TOTAL_SYNCED=0
TMP_DIR="/tmp/intel_sync_up"
mkdir -p "$TMP_DIR"

echo ""
echo "🔄 Sincronizando local → remoto..."
echo ""

for TABLE in "${TABLES[@]}"; do
    # Verificar se tabela existe localmente
    EXISTS=$($PSQL_LOCAL -c "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='$TABLE')" 2>/dev/null || echo "f")

    if [[ "$EXISTS" != "t" ]]; then
        continue
    fi

    # Verificar se tem coluna atualizado_em
    HAS_TIMESTAMP=$($PSQL_LOCAL -c "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='$TABLE' AND column_name='atualizado_em')" 2>/dev/null || echo "f")

    if [[ "$HAS_TIMESTAMP" == "t" ]]; then
        # Contar registros modificados
        COUNT=$($PSQL_LOCAL -c "SELECT COUNT(*) FROM $TABLE WHERE atualizado_em > '$LAST_SYNC'" 2>/dev/null || echo "0")
    else
        # Sem timestamp, conta todos se --force
        if [[ "$1" == "--force" ]]; then
            COUNT=$($PSQL_LOCAL -c "SELECT COUNT(*) FROM $TABLE" 2>/dev/null || echo "0")
        else
            COUNT=0
        fi
    fi

    if [[ "$COUNT" -gt 0 ]]; then
        echo "📤 $TABLE: $COUNT registros"

        # Exportar registros modificados
        if [[ "$HAS_TIMESTAMP" == "t" ]]; then
            $PSQL_LOCAL -c "COPY (SELECT * FROM $TABLE WHERE atualizado_em > '$LAST_SYNC') TO STDOUT WITH CSV HEADER" > "$TMP_DIR/$TABLE.csv" 2>/dev/null
        else
            $PSQL_LOCAL -c "COPY $TABLE TO STDOUT WITH CSV HEADER" > "$TMP_DIR/$TABLE.csv" 2>/dev/null
        fi

        # Importar para remoto com UPSERT via temp table
        if [[ -s "$TMP_DIR/$TABLE.csv" ]]; then
            # Criar tabela temporária e importar
            PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "${DB_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" -q <<EOF 2>/dev/null
-- Criar temp table com mesma estrutura
CREATE TEMP TABLE tmp_$TABLE (LIKE $TABLE INCLUDING ALL);

-- Importar CSV
\copy tmp_$TABLE FROM '$TMP_DIR/$TABLE.csv' WITH CSV HEADER;

-- Upsert: atualiza se existe, insere se não
INSERT INTO $TABLE
SELECT * FROM tmp_$TABLE
ON CONFLICT (id) DO UPDATE SET
$(psql -d intel -t -A -c "SELECT string_agg(column_name || ' = EXCLUDED.' || column_name, ', ') FROM information_schema.columns WHERE table_name='$TABLE' AND column_name != 'id'" 2>/dev/null);

DROP TABLE tmp_$TABLE;
EOF
            TOTAL_SYNCED=$((TOTAL_SYNCED + COUNT))
        fi
    fi
done

# Atualizar timestamp do último sync
echo "$CURRENT_TIME" > "$LAST_SYNC_FILE"

echo ""
echo "✅ Sincronização completa!"
echo "   Total de registros: $TOTAL_SYNCED"
echo "   Timestamp: $CURRENT_TIME"
echo ""
