#!/usr/bin/env bash
# abrir_projeto.sh — cockpit multi-superficie de um projeto INTEL.
#
# INTEL = fonte canonica (estado). claude.ai = workspace. pasta local = oficina.
# Este helper le os PONTEIROS (Drive/WA/claude.ai/local) + o estado vivo do
# INTEL — sem replicar nada. Ver migration 046_project_workspace_pointers.sql.
#
# Uso:
#   abrir_projeto.sh <id|nome>                   cockpit do projeto
#   abrir_projeto.sh <id|nome> --set-folder P    seta pasta local (oficina)
#   abrir_projeto.sh <id|nome> --set-claude U    seta URL do Projeto claude.ai
#   abrir_projeto.sh <id|nome> --path-only       imprime so o local_folder_path
#
# DB_TARGET=prod (Neon INTEL = fonte canonica dos projetos).
set -euo pipefail

ENV_FILE="/Users/rap/prospect-system/.env"
URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d'"' -f2)
[ -z "$URL" ] && { echo "DATABASE_URL nao encontrado em $ENV_FILE" >&2; exit 1; }

Q="${1:-}"
[ -z "$Q" ] && { echo "uso: abrir_projeto.sh <id|nome> [--set-folder P|--set-claude U|--path-only]" >&2; exit 1; }

# --- resolve project id (numerico = id; senao fuzzy por nome, prefere ativo) ---
if [[ "$Q" =~ ^[0-9]+$ ]]; then
  PID="$Q"
else
  ESC=${Q//\'/\'\'}
  PID=$(psql "$URL" -t -A -c "SELECT id FROM projects WHERE nome ILIKE '%$ESC%' AND status <> 'archived' ORDER BY id LIMIT 2;" || true)
  n=$(printf '%s' "$PID" | grep -c . || true)
  if [ "${n:-0}" -eq 0 ]; then
    PID=$(psql "$URL" -t -A -c "SELECT id FROM projects WHERE nome ILIKE '%$ESC%' ORDER BY id LIMIT 2;" || true)
    n=$(printf '%s' "$PID" | grep -c . || true)
  fi
  if [ "${n:-0}" -eq 0 ]; then echo "nenhum projeto casa '$Q'" >&2; exit 2; fi
  if [ "${n:-0}" -gt 1 ]; then
    echo "ambiguo — casa mais de um (use o id):" >&2
    psql "$URL" -c "SELECT id, nome, status FROM projects WHERE nome ILIKE '%$ESC%' ORDER BY id;" >&2
    exit 2
  fi
fi

# --- flags de escrita / leitura pontual ---
case "${2:-}" in
  --path-only)
    psql "$URL" -t -A -c "SELECT COALESCE(local_folder_path,'') FROM projects WHERE id=$PID;"
    exit 0 ;;
  --set-folder)
    P="${3:?falta o caminho}"; ESCP=${P//\'/\'\'}
    psql "$URL" -c "UPDATE projects SET local_folder_path='$ESCP' WHERE id=$PID RETURNING id, local_folder_path;"
    exit 0 ;;
  --set-claude)
    U="${3:?falta a url}"; ESCU=${U//\'/\'\'}
    psql "$URL" -c "UPDATE projects SET claude_project_url='$ESCU' WHERE id=$PID RETURNING id, claude_project_url;"
    exit 0 ;;
esac

# --- cockpit ---
psql "$URL" -t -A <<SQL
\echo '════════════════════════════════════════════════════════'
SELECT '📁 #'||id||'  '||nome||'   ['||status||']' FROM projects WHERE id=$PID;
SELECT '   '||COALESCE(NULLIF(left(descricao,160),''),'(sem descrição)') FROM projects WHERE id=$PID;
\echo ''
\echo '🔗 Ponteiros (referência, não cópia)'
SELECT '   Drive     : '||COALESCE('https://drive.google.com/drive/folders/'||google_drive_folder_id,'— (link ainda não setado)') FROM projects WHERE id=$PID;
SELECT '   claude.ai : '||COALESCE(claude_project_url,'— (setar: --set-claude URL)') FROM projects WHERE id=$PID;
SELECT '   Local     : '||COALESCE(local_folder_path,'— (setar: --set-folder PATH)') FROM projects WHERE id=$PID;
SELECT '   WA grupo  : '||COALESCE(string_agg(group_name, ', '),'—') FROM project_whatsapp_groups WHERE project_id=$PID;
\echo ''
\echo '📋 Estado vivo (INTEL — fonte canônica)'
SELECT '   Tasks pendentes: '||count(*) FROM tasks WHERE project_id=$PID AND status='pending';
SQL

echo "   próximas:"
psql "$URL" -t -A -F' | ' -c "SELECT '     • '||left(titulo,66), COALESCE(to_char(data_vencimento,'DD/MM'),'s/ prazo') FROM tasks WHERE project_id=$PID AND status='pending' ORDER BY data_vencimento NULLS LAST LIMIT 5;"
echo "════════════════════════════════════════════════════════"
