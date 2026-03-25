#!/bin/bash
# Pre-merge check script
# Rodar antes de pedir merge para 1ARCH

set -e

echo "============================================"
echo "       PRE-MERGE CHECK - RAP System        "
echo "============================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ERRORS=0

# 1. Check Python syntax
echo "1. Verificando sintaxe Python..."
for f in $(find app -name "*.py" 2>/dev/null); do
    if ! python3 -m py_compile "$f" 2>/dev/null; then
        echo -e "${RED}   ERRO: $f tem erro de sintaxe${NC}"
        ERRORS=$((ERRORS + 1))
    fi
done
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}   OK: Sintaxe Python valida${NC}"
fi

# 2. Run tests
echo ""
echo "2. Rodando testes..."
if python3 -m pytest tests/ -v --tb=short 2>/dev/null; then
    echo -e "${GREEN}   OK: Todos os testes passaram${NC}"
else
    echo -e "${RED}   ERRO: Alguns testes falharam${NC}"
    ERRORS=$((ERRORS + 1))
fi

# 3. Check for uncommitted changes
echo ""
echo "3. Verificando commits pendentes..."
if [ -n "$(git status --porcelain)" ]; then
    echo -e "${YELLOW}   AVISO: Ha mudancas nao commitadas${NC}"
    git status --short
else
    echo -e "${GREEN}   OK: Tudo commitado${NC}"
fi

# 4. Check branch is up to date with main
echo ""
echo "4. Verificando sincronizacao com main..."
git fetch origin main --quiet 2>/dev/null || true
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "0")
if [ "$BEHIND" -gt 0 ]; then
    echo -e "${YELLOW}   AVISO: Branch esta $BEHIND commits atras de main${NC}"
    echo "   Considere: git rebase origin/main"
else
    echo -e "${GREEN}   OK: Branch atualizada com main${NC}"
fi

# 5. Check COORDINATION.md updated
echo ""
echo "5. Verificando COORDINATION.md..."
if git diff --name-only HEAD~1 2>/dev/null | grep -q "COORDINATION.md"; then
    echo -e "${GREEN}   OK: COORDINATION.md foi atualizado${NC}"
else
    echo -e "${YELLOW}   AVISO: COORDINATION.md nao foi atualizado neste commit${NC}"
fi

# Summary
echo ""
echo "============================================"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}RESULTADO: PRONTO PARA REVIEW${NC}"
    echo ""
    echo "Proximo passo:"
    echo "  git push origin $(git branch --show-current)"
    echo "  Atualizar COORDINATION.md com 'PRONTO PARA REVIEW'"
else
    echo -e "${RED}RESULTADO: $ERRORS ERRO(S) ENCONTRADO(S)${NC}"
    echo ""
    echo "Corrija os erros antes de pedir merge."
fi
echo "============================================"

exit $ERRORS
