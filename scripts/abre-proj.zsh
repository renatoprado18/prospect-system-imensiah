# abre-proj — abre um projeto INTEL em qualquer superfície.
# Mostra o cockpit (ponteiros + estado vivo) e, se houver pasta local setada,
# faz cd nela. Uso: abre-proj <id|nome>   ·   abre-proj vallen
#
# Instalar: adicione ao ~/.zshrc:
#   source /Users/rap/prospect-system/scripts/abre-proj.zsh
# (ou rode `source .../abre-proj.zsh` na sessão atual pra testar)

abre-proj() {
  local script="/Users/rap/prospect-system/scripts/abrir_projeto.sh"
  [ -z "$1" ] && { echo "uso: abre-proj <id|nome>"; return 1; }
  bash "$script" "$1" || return $?
  # se o projeto tem pasta local, entra nela
  local p
  p=$(bash "$script" "$1" --path-only 2>/dev/null)
  if [ -n "$p" ]; then
    if [ -d "$p" ]; then
      echo "→ cd $p"
      cd "$p"
    else
      echo "→ pasta setada mas não existe ($p) — crie com: mkdir -p \"$p\""
    fi
  fi
}
