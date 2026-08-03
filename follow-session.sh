#!/bin/bash
# follow-session.sh — acompanhar no terminal uma sessao Claude Code dirigida do celular.
# Acha a sessao remote-control (bridge) mais recente e mostra como seguir/continuar.
#
# Uso:
#   ./follow-session.sh          # mostra UUID + comandos
#   ./follow-session.sh --tail   # ja segue o transcript ao vivo (raw JSON)
#   ./follow-session.sh --resume # ja abre a sessao no terminal (interativo)

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
PROJ_ROOT="$HOME/.claude/projects"

DIR=$(ls -dt "$PROJ_ROOT"/*bridge-cse* 2>/dev/null | head -1)
if [ -z "$DIR" ]; then
  echo "Nenhuma sessao bridge (do celular) encontrada em $PROJ_ROOT."
  echo "Abra uma sessao do celular primeiro (Code -> prospect-system-imensiah -> Nova sessao)."
  exit 1
fi

JSONL=$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$JSONL" ]; then
  echo "Sessao encontrada ($(basename "$DIR")) mas ainda sem transcript — mande uma mensagem nela primeiro."
  exit 1
fi
UUID=$(basename "$JSONL" .jsonl)

echo "Sessao : $(basename "$DIR")"
echo "UUID   : $UUID"
echo "Arquivo: $JSONL"
echo ""
echo "Seguir ao vivo (raw JSON):  tail -f \"$JSONL\""
echo "Continuar no terminal    :  claude --resume $UUID"

case "${1:-}" in
  --tail)   echo; echo "== seguindo (Ctrl-C sai) =="; exec tail -f "$JSONL";;
  --resume) echo; exec claude --resume "$UUID";;
esac
