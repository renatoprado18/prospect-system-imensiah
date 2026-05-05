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

# Verificar PostgreSQL local
check_postgres() {
    if ! brew services list | grep -q "postgresql@15.*started"; then
        echo -e "${YELLOW}⚠️  PostgreSQL não está rodando. Iniciando...${NC}"
        brew services start postgresql@15
        sleep 2
    fi

    # Verificar se banco intel existe
    if ! /usr/local/opt/postgresql@15/bin/psql -lqt | cut -d \| -f 1 | grep -qw intel; then
        echo -e "${YELLOW}⚠️  Banco 'intel' não existe. Criando...${NC}"
        /usr/local/opt/postgresql@15/bin/createdb intel
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

    # Iniciar PostgreSQL
    brew services start postgresql@15
    sleep 2

    # Criar banco
    /usr/local/opt/postgresql@15/bin/createdb intel 2>/dev/null || true

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
    USE_LOCAL_DB=1 BASE_URL=http://localhost:8000 ../.venv/bin/uvicorn main:app --reload --port 8000
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
