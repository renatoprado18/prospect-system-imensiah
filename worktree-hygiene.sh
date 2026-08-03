#!/bin/bash
# worktree-hygiene.sh — higiene SEGURA dos worktrees do Claude Code (.claude/worktrees).
#
# Remove APENAS worktrees descartaveis:
#   - HEAD ja contido no main (nenhum commit se perde), E
#   - sem alteracoes nao-commitadas (working tree limpo), E
#   - nao travado (locked).
# NUNCA toca: o repo principal (main), worktrees sujos, com branch nao-mergeado, ou travados.
#
# Uso:
#   ./worktree-hygiene.sh           # DRY-RUN — so relata (padrao)
#   ./worktree-hygiene.sh --apply   # remove os SEGUROS

set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
cd /Users/rap/prospect-system || exit 1

APPLY="${1:-}"
MAIN="main"

# 1) limpeza segura de referencias orfas (nunca apaga arquivos)
git worktree prune -v 2>/dev/null || true

SAFE=(); KEEP=()

# 2) avalia cada worktree
while IFS= read -r line; do
  case "$line" in
    worktree\ *) WT="${line#worktree }";;
    HEAD\ *)     HEAD="${line#HEAD }";;
    branch\ *)   BR="${line#branch refs/heads/}";;
    locked*)     LOCKED=1;;
    "")  # fim do bloco -> avalia
      if [ "$WT" != "/Users/rap/prospect-system" ]; then
        reason=""
        [ "${LOCKED:-0}" = "1" ] && reason="travado"
        if [ -z "$reason" ] && [ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" ]; then
          reason="alteracoes nao-commitadas"
        fi
        if [ -z "$reason" ] && ! git merge-base --is-ancestor "$HEAD" "$MAIN" 2>/dev/null; then
          reason="branch nao-mergeado (${BR:-detached}) — tem commits fora do main"
        fi
        if [ -z "$reason" ]; then
          SAFE+=("$WT")
        else
          KEEP+=("$WT | ${BR:-detached} | $reason")
        fi
      fi
      WT=""; HEAD=""; BR=""; LOCKED=0;;
  esac
done < <(git worktree list --porcelain; echo "")

echo "=== KEEP (preservados — tem trabalho ou estao ativos): ${#KEEP[@]} ==="
for k in "${KEEP[@]}"; do echo "  · $k"; done
echo ""
echo "=== SAFE (descartaveis: mergeados + limpos): ${#SAFE[@]} ==="
for s in "${SAFE[@]}"; do echo "  · $s"; done

if [ "$APPLY" = "--apply" ]; then
  echo ""; echo "=== REMOVENDO os SAFE ==="
  for s in "${SAFE[@]}"; do
    git worktree remove "$s" 2>&1 && echo "  removido: $s" || echo "  FALHOU: $s"
  done
  git worktree prune 2>/dev/null || true
else
  echo ""; echo "(dry-run — nada removido. Rode com --apply para remover os SAFE.)"
fi
