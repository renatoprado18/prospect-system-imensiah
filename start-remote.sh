#!/bin/bash
# start-remote.sh — sobe o servidor Claude Code "remote control" (modo worktree)
# dentro de um tmux, pra o Renato abrir sessoes /cos e /dev do celular.
# Idempotente: se ja estiver rodando, nao duplica.
#
# Uso manual:  ./start-remote.sh
# Auto-start:  via LaunchAgent ~/Library/LaunchAgents/com.rap.prospect-remote.plist

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

SESSION="prospect"
REPO="/Users/rap/prospect-system"
NAME="prospect-system"

# --- higiene de worktrees (SEGURA) ---
# prune: remove so referencias orfas (nunca apaga arquivos/trabalho).
# hygiene --apply: remove so worktrees descartaveis (mergeados no main + limpos + destravados);
# NUNCA toca em worktrees com trabalho nao-mergeado, sujos, ou de sessao ativa (travada).
git -C "$REPO" worktree prune 2>/dev/null || true
[ -x "$REPO/worktree-hygiene.sh" ] && "$REPO/worktree-hygiene.sh" --apply >> /tmp/prospect-hygiene.log 2>&1 || true

# ja ativo? nada a fazer.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[start-remote] tmux '$SESSION' ja ativo — ok."
  exit 0
fi

tmux new-session -d -s "$SESSION" -c "$REPO" \
  "claude remote-control --spawn=worktree --capacity 4 --name $NAME 2>&1 | tee /tmp/prospect-remote.log"

echo "[start-remote] servidor iniciado no tmux '$SESSION' (repo: $REPO)."
