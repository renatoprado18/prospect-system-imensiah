#!/bin/bash
# Edição semanal do Boletim de Inteligência. É isto que o launchd chama na sexta.
#
# DOIS PASSOS, nesta ordem, e a ordem importa: o dossiê determinista precisa
# existir ANTES do render, porque é dele que sai a profundidade (protagonistas
# com cargo, histórico da frente, a entidade cruzada com o corpus do Renato).
# Se o dossiê falhar, o boletim ainda sai — cada card avisa que degradou pra
# camada 1 em vez de afinar calado.
#
# POR QUE SEXTA DE MANHÃ. A semana chega fechada: o que aconteceu já aconteceu,
# e o que pede ação tem o fim de semana pra ser pensado antes de segunda. Era a
# alternativa ao WhatsApp diário, que morreu em 17/07 sem ninguém sentir falta —
# notícia não interrompe, ela se varre.
#
# PUBLICA por default (marca as manchetes como mostradas, pra não repetirem na
# semana seguinte). `--previa` gera sem marcar, pra conferir à mão.
set -uo pipefail

ROOT="/Users/rap/prospect-system"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$HOME/.cos-agent"
mkdir -p "$LOG_DIR"

PUBLICAR="--publicar"
PAGINA="$HOME/cockpit/boletim.html"
# A prévia sai em ARQUIVO SEPARADO (boletim_previa.html) pra não destruir a
# edição vigente — ver o comentário do OUT em boletim.py.
[[ "${1:-}" == "--previa" ]] && { PUBLICAR=""; PAGINA="$HOME/cockpit/boletim_previa.html"; }

cd "$ROOT" || exit 1
echo "═══ boletim semanal · $(date '+%Y-%m-%d %H:%M:%S') ═══"

# Passo 1 — camada determinista. Lê prod: as duas chaves de propósito do
# protocolo DB_TARGET (reference_db_target_protocol).
echo "→ dossiê determinista (85 artigos, cache em ~/cockpit/.cache_artigos)"
if ! DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 "$PY" "$ROOT/scripts/boletim_dossie.py" --dias 30; then
  echo "⚠️  dossiê falhou — seguindo com o boletim (ele degrada e avisa nos cards)"
fi

# Passo 2 — render. Sem `if`: se o boletim falhar, o exit code tem de subir pro
# launchd, senão a sexta sem boletim passa em silêncio.
echo "→ render"
"$PY" "$ROOT/scripts/boletim.py" --dias 30 $PUBLICAR
CODE=$?

if [[ $CODE -eq 0 ]]; then
  echo "✓ $PAGINA"
  # Abre na tela dele — a entrega é a página aberta, não o caminho no log
  # (feedback_entrega_visual_html_local). `|| true`: sem sessão gráfica o open
  # falha e isso não é motivo pra marcar a edição como perdida.
  open "$PAGINA" 2>/dev/null || true
else
  echo "✗ render falhou (exit $CODE)"
fi
exit $CODE
