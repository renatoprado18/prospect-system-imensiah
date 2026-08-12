#!/bin/bash
# ============================================================
# INTEL CRM - Development Server
# ============================================================
# Inicia o servidor local com banco PostgreSQL local (rápido)
#
# Uso:
#   ./dev.sh          # Inicia servidor na porta 8000
#   ./dev.sh sync     # Sincroniza banco local com produção primeiro
#   ./dev.sh setup    # Setup inicial (instala dependências)
# ============================================================

set -e
cd "$(dirname "$0")"

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════╗"
echo "║          INTEL CRM - Dev Server           ║"
echo "╚═══════════════════════════════════════════╝"
echo -e "${NC}"

PG_BIN=/usr/local/opt/postgresql@15/bin
PG_DATA=/usr/local/var/postgresql@15

# Sobe o Postgres. Tenta o brew primeiro (é quem gerencia o boot), mas NÃO
# depende dele: `pg_ctl` sobe o mesmo cluster quando o brew está quebrado.
start_postgres() {
    # LC_ALL é obrigatório: sem locale válido o postmaster morre no start com
    # "became multithreaded during startup" e o pg_ctl só diz "stopped waiting".
    # É o mesmo valor que o plist do brew injeta — o fallback não pode subir o
    # cluster num ambiente diferente do caminho normal.
    brew services start postgresql@15 2>/dev/null \
        || LC_ALL=en_US.UTF-8 "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_DATA/server.log" start 2>/dev/null \
        || true

    # Espera aceitar conexão em vez de dormir um número fixo — `sleep 2` tanto
    # sobra quanto falta, e quando falta o erro sai como "banco não existe".
    for _ in $(seq 1 15); do
        "$PG_BIN/pg_isready" -h localhost -p 5432 -q && return 0
        sleep 1
    done
    return 1
}

# Verificar PostgreSQL local
check_postgres() {
    # Pergunta ao POSTGRES, não ao brew. Em 11/08/26 o `brew services list`
    # passou a estourar em Ruby (`undefined method 'stop_timeout'`, Homebrew 6.x)
    # e, com `set -e`, derrubava o dev.sh inteiro — com o banco NO AR o tempo
    # todo. O que decide aqui é "aceita conexão", não o que o brew acha do
    # serviço: o brew é um caminho pra subir, nunca a fonte da verdade.
    if ! "$PG_BIN/pg_isready" -h localhost -p 5432 -q; then
        echo -e "${YELLOW}⚠️  PostgreSQL não está rodando. Iniciando...${NC}"
        if ! start_postgres; then
            echo -e "${RED}❌ PostgreSQL não subiu em 15s.${NC}"
            echo -e "   Tente: ${BLUE}$PG_BIN/pg_ctl -D $PG_DATA -l $PG_DATA/server.log start${NC}"
            echo -e "   Log:   ${BLUE}tail -20 $PG_DATA/server.log${NC}"
            exit 1
        fi
    fi

    # Verificar se banco intel existe
    if ! "$PG_BIN/psql" -lqt | cut -d \| -f 1 | grep -qw intel; then
        echo -e "${YELLOW}⚠️  Banco 'intel' não existe. Criando...${NC}"
        "$PG_BIN/createdb" intel
        echo -e "${YELLOW}⚠️  Banco vazio. Execute: ./dev.sh sync${NC}"
    fi
}

# Setup inicial
setup() {
    echo -e "${BLUE}🔧 Instalando dependências...${NC}"

    # Verificar Homebrew
    if ! command -v brew &> /dev/null; then
        echo -e "${RED}❌ Homebrew não instalado. Instale em https://brew.sh${NC}"
        exit 1
    fi

    # Instalar PostgreSQL se necessário
    if ! brew list postgresql@15 &> /dev/null; then
        echo "Instalando PostgreSQL 15..."
        brew install postgresql@15
    fi

    if ! brew list postgresql@17 &> /dev/null; then
        echo "Instalando PostgreSQL 17 (para pg_dump)..."
        brew install postgresql@17
    fi

    # Iniciar PostgreSQL (mesmo caminho tolerante do check_postgres)
    start_postgres || echo -e "${YELLOW}⚠️  PostgreSQL não subiu — veja $PG_DATA/server.log${NC}"

    # Criar banco
    "$PG_BIN/createdb" intel 2>/dev/null || true

    # Ativar venv e instalar deps Python
    if [ -d ".venv" ]; then
        source .venv/bin/activate
        pip install -r requirements.txt -q
    fi

    echo -e "${GREEN}✅ Setup completo!${NC}"
    echo ""
    echo "Próximos passos:"
    echo "  1. ./dev.sh sync   # Baixar dados de produção"
    echo "  2. ./dev.sh        # Iniciar servidor"
}

# Sincronizar banco
sync_db() {
    echo -e "${BLUE}🔄 Sincronizando banco local com produção...${NC}"
    ./scripts/sync-local-db.sh
}

# Iniciar servidor
start_server() {
    check_postgres

    # Matar processo anterior na porta 8000
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true

    echo -e "${GREEN}🚀 Iniciando servidor em http://localhost:8000${NC}"
    echo -e "${YELLOW}   Usando banco LOCAL (PostgreSQL)${NC}"
    echo ""
    echo -e "   ${BLUE}Ctrl+C${NC} para parar"
    echo ""

    cd app
    # BASE_URL=localhost: catchup/trigger fan-out NAO bate em prod por engano.
    # Sem isso, agentes/testes locais ja causaram 401 fan-out em prod (2026-05-05).
    DB_TARGET=local BASE_URL=http://localhost:8000 ../.venv/bin/uvicorn main:app --reload --port 8000
}

# Main
case "${1:-}" in
    setup)
        setup
        ;;
    sync)
        sync_db
        ;;
    *)
        start_server
        ;;
esac
