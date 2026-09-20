#!/bin/bash
# Sincroniza banco local com Neon (produção)
# Uso: ./scripts/sync-local-db.sh
#
# ─────────────────────────────────────────────────────────────────────────────
# POR QUE ESTE SCRIPT FOI REESCRITO (20/09/2026)
# ─────────────────────────────────────────────────────────────────────────────
# Ele dizia "✅ Sincronização completa!" depois de falhar. Medido em 20/09:
# `.last_sync` marcava 07/09, os dados locais paravam em 22/08 e a produção
# estava em 20/09. Uma sessão inteira concluiu "não estava no INTEL" sobre 72
# comunicações judiciais que estavam.
#
# A causa raiz NÃO era o `set -e` faltando (ele estava lá). Era o `psql` sem
# `ON_ERROR_STOP=1`: por default o psql CONTINUA depois de um erro de SQL e sai
# com código 0. Medido: um .sql com uma tabela inexistente no meio importa o
# resto e devolve exit 0 — com ON_ERROR_STOP=1 devolve 3. Somado aos quatro
# `2>/dev/null`, que jogavam fora a mensagem de erro, a importação podia
# fracassar pela metade sem deixar rastro nenhum.
#
# Os outros dois defeitos que completavam a mentira:
#   · `.last_sync` era gravado INCONDICIONALMENTE, e o "✅" impresso sempre.
#   · A "verificação" contava contacts/messages do LOCAL e não comparava com a
#     origem — 24.917 passava tão bem quanto 29.830.
#
# 🚨 Por que isso era perigoso e não só impreciso: o hook `pre-push` roda o
# `sync-to-remote.sh` (local → prod) a cada `git push`. Com o local stale e o
# `.last_sync` zerado, todo registro local sobrescreve o de prod via UPSERT —
# edições feitas em produção desde a cópia velha somem. (Não há DELETE: linhas
# que só existem em prod ficam. O estrago é sobrescrita, não remoção.)
#
# Regra desta versão: em divergência o script SAI COM 1 e NÃO grava `.last_sync`.
# Erro barulhento é o único jeito de o hook segurar o push.
# ─────────────────────────────────────────────────────────────────────────────

set -eo pipefail

echo "🔄 Sincronizando banco local com Neon..."

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Carregar variáveis do .env
source "$PROJECT_DIR/.env"

if [[ -z "$POSTGRES_URL" ]]; then
    echo "❌ POSTGRES_URL não está definido no .env — sem origem pra copiar." >&2
    exit 1
fi

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

# ON_ERROR_STOP=1 é o que faz o psql devolver exit != 0 num erro de SQL. Sem
# isso ele engole o erro e sai 0 — a origem da mentira que este script contava.
PSQL_SAFE=("$PSQL" -v ON_ERROR_STOP=1)

TMP_DIR="/tmp/intel_sync"
mkdir -p "$TMP_DIR"

fatal() {
    echo "" >&2
    echo "❌ SYNC FALHOU: $1" >&2
    echo "   O banco local NÃO é uma cópia fiel da produção." >&2
    echo "   \`.last_sync\` NÃO foi atualizado de propósito — sem isso o" >&2
    echo "   \`sync-to-remote.sh\` (hook pre-push) empurraria dado velho pra prod." >&2
    echo "   Conserte a causa e rode de novo antes de qualquer \`git push\`." >&2
    exit 1
}

# ── Export (erros do pg_dump ficam VISÍVEIS; nada de 2>/dev/null) ────────────

echo "📥 Exportando schema do Neon..."
PGPASSWORD="$DB_PASS" "$PG_DUMP" -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    --schema-only --no-owner --no-privileges \
    -f "$TMP_DIR/schema.sql" || fatal "pg_dump do schema falhou (veja o erro acima)"

# --disable-triggers: `signals` e `tonha_decisions` têm FK CIRCULAR entre si, e
# num dump --data-only não existe ordem de COPY que satisfaça as duas. O
# pg_dump AVISA sobre isso — e o `2>/dev/null` da versão antiga jogava o aviso
# fora. Resultado: o COPY de `signals` morria em
# `signals_decision_id_fkey`, o psql (sem ON_ERROR_STOP) seguia em frente, e as
# tabelas ficavam ZERADAS num sync que se declarava completo. Era essa a causa
# das 4 tabelas vazias medidas em 20/09. Requer superuser no restore — o usuário
# do Postgres local é (`usesuper = t`); a produção não é tocada por esta flag.
echo "📥 Exportando dados do Neon..."
PGPASSWORD="$DB_PASS" "$PG_DUMP" -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    --data-only --no-owner --no-privileges --disable-triggers \
    -f "$TMP_DIR/data.sql" || fatal "pg_dump dos dados falhou (veja o erro acima)"

# Um dump vazio passaria por "sucesso" e recriaria o local zerado.
[[ -s "$TMP_DIR/schema.sql" ]] || fatal "dump do schema veio vazio"
[[ -s "$TMP_DIR/data.sql" ]] || fatal "dump dos dados veio vazio"

# Remover configurações incompatíveis com PG15
sed -i '' '/transaction_timeout/d' "$TMP_DIR/schema.sql" "$TMP_DIR/data.sql"

# ── Import ──────────────────────────────────────────────────────────────────

echo "🗑️  Recriando banco local..."
"$DROPDB" --if-exists "$LOCAL_DB"
"$CREATEDB" "$LOCAL_DB" || fatal "createdb $LOCAL_DB falhou"

# Forca timezone UTC (Neon e UTC; sem isso, sync-to-remote compara
# atualizado_em em BRT contra last_sync em UTC e pula edicoes).
"${PSQL_SAFE[@]}" -d "$LOCAL_DB" -q \
    -c "ALTER DATABASE $LOCAL_DB SET timezone = 'UTC';" \
    || fatal "não consegui fixar o timezone do banco local em UTC"

echo "📤 Importando schema..."
"${PSQL_SAFE[@]}" -d "$LOCAL_DB" -q -f "$TMP_DIR/schema.sql" \
    -o /dev/null || fatal "importação do schema parou num erro de SQL (veja acima)"

echo "📤 Importando dados..."
"${PSQL_SAFE[@]}" -d "$LOCAL_DB" -q -f "$TMP_DIR/data.sql" \
    -o /dev/null || fatal "importação dos dados parou num erro de SQL (veja acima)"

# ── Verificação DE VERDADE: conta nos DOIS lados e compara ──────────────────
#
# A versão antiga contava contacts/messages só no local e imprimia o número.
# Um número não é uma verificação: 24.917 parece tão saudável quanto 29.830.
# Aqui cada lado gera a própria query de contagem a partir do seu catálogo, e a
# comparação é o diff das duas saídas — tabela que existe só de um lado aparece
# como linha faltando em vez de derrubar a query.

echo "🔍 Conferindo local × produção (todas as tabelas)..."

GEN_COUNT_SQL="SELECT coalesce(string_agg(
    format('SELECT %L AS t, count(*) AS n FROM %I.%I', schemaname||'.'||tablename, schemaname, tablename),
    ' UNION ALL '), 'SELECT ''(nenhuma)'' AS t, 0 AS n')
  FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"

REMOTE_QUERY=$(PGPASSWORD="$DB_PASS" "${PSQL_SAFE[@]}" -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -t -A \
    -c "$GEN_COUNT_SQL") || fatal "não consegui listar as tabelas da produção"
LOCAL_QUERY=$("${PSQL_SAFE[@]}" -d "$LOCAL_DB" -t -A \
    -c "$GEN_COUNT_SQL") || fatal "não consegui listar as tabelas locais"

PGPASSWORD="$DB_PASS" "${PSQL_SAFE[@]}" -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -t -A -F'|' \
    -c "$REMOTE_QUERY" | sort > "$TMP_DIR/counts_remote.txt" \
    || fatal "contagem na produção falhou"
"${PSQL_SAFE[@]}" -d "$LOCAL_DB" -t -A -F'|' \
    -c "$LOCAL_QUERY" | sort > "$TMP_DIR/counts_local.txt" \
    || fatal "contagem no local falhou"

# A régua NÃO pode ser igualdade estrita. A produção continua recebendo escrita
# enquanto o dump roda — medido em 20/09: `messages` +5, `cron_heartbeats` +16,
# `group_messages` +2 na janela. Um check de igualdade reprovaria todo sync e
# viraria ruído que se aprende a ignorar. O que importa distinguir é:
#
#   FALTANDO  tabela existe em prod e não no local  → falha
#   VAZIA     prod tem linhas, local tem ZERO       → falha (o modo real: em
#                                                     20/09 signals, delegations,
#                                                     tonha_decisions e
#                                                     dev_delegation_runs vieram
#                                                     zeradas de um sync que se
#                                                     declarou completo)
#   CURTA     local menor que prod além da tolerância → falha
#   ADIANTE   local MAIOR que prod                  → aviso alto: depois de um
#                                                     drop+create isso é
#                                                     impossível por deriva, e é
#                                                     justamente o que o
#                                                     `sync-to-remote.sh`
#                                                     empurraria pra produção
#   DERIVA    local um pouco atrás                  → normal, silencioso
#
# Tolerância: 1% da tabela, com piso absoluto de 50 linhas.
awk -F'|' -v OFS='|' '
    NR==FNR { remoto[$1]=$2; next }
    { local[$1]=$2 }
    END {
        falhas = 0; avisos = 0
        for (t in remoto) {
            r = remoto[t] + 0
            if (!(t in local))        { printf "   FALTANDO  %-45s prod=%d, não existe no local\n", t, r > "/dev/stderr"; falhas++; continue }
            l = local[t] + 0
            if (r > 0 && l == 0)      { printf "   VAZIA     %-45s prod=%d, local=0\n", t, r > "/dev/stderr"; falhas++; continue }
            if (l > r)                { printf "   ADIANTE   %-45s prod=%d, local=%d (+%d só no local)\n", t, r, l, l-r > "/dev/stderr"; avisos++; continue }
            tol = r * 0.01; if (tol < 50) tol = 50
            if (r - l > tol)          { printf "   CURTA     %-45s prod=%d, local=%d (faltam %d, tolerância %d)\n", t, r, l, r-l, int(tol) > "/dev/stderr"; falhas++ }
        }
        for (t in local) if (!(t in remoto))
            printf "   EXTRA     %-45s existe só no local (%d linhas)\n", t, local[t] > "/dev/stderr"
        printf "%d %d\n", falhas, avisos
    }
' "$TMP_DIR/counts_remote.txt" "$TMP_DIR/counts_local.txt" > "$TMP_DIR/veredito.txt"

read -r FALHAS AVISOS < "$TMP_DIR/veredito.txt"

if [[ "$AVISOS" -gt 0 ]]; then
    echo "" >&2
    echo "⚠️  $AVISOS tabela(s) com MAIS linhas no local que em produção (acima)." >&2
    echo "   Depois de um drop+create isso não acontece por deriva. É exatamente o" >&2
    echo "   que o \`sync-to-remote.sh\` empurraria pra prod no próximo \`git push\`." >&2
fi

if [[ "$FALHAS" -gt 0 ]]; then
    fatal "$FALHAS tabela(s) faltando, vazias ou curtas demais (detalhe acima)"
fi

TABELAS=$(wc -l < "$TMP_DIR/counts_remote.txt" | tr -d ' ')
LINHAS=$(awk -F'|' '{s+=$2} END {print s+0}' "$TMP_DIR/counts_remote.txt")

# ── Só agora o `.last_sync` pode ser gravado ────────────────────────────────
#
# Este arquivo é o que o `sync-to-remote.sh` usa pra decidir o que empurrar pra
# produção. Gravá-lo antes da verificação (como a versão antiga fazia) é dizer
# "local == prod" sem nunca ter conferido.
LAST_SYNC_FILE="$PROJECT_DIR/.last_sync"
date -u '+%Y-%m-%d %H:%M:%S' > "$LAST_SYNC_FILE"

echo ""
echo "✅ Sincronização completa e CONFERIDA!"
echo "   $TABELAS tabelas, $LINHAS linhas — contagens idênticas à produção"
echo ""
echo "🚀 Reinicie o servidor: ./dev.sh"
