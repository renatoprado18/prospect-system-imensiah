#!/usr/bin/env bash
# Re-pareia a instância rap-whatsapp e recupera a janela cega — em um comando.
#
# POR QUE EXISTE (15/10/2026). O WhatsApp Business do Renato deu problema e ele
# re-registrou o número no WhatsApp comum. Isso invalida TODOS os dispositivos
# vinculados: a instância caiu com `state=close`/`reason=403` às 18h59 e o INTEL
# ficou 15,8h sem receber nada. Religar exige QR escaneado no aparelho, e o QR
# regenera a cada ~20s — fazer isso à mão, alternando terminal e celular, é como
# se perde a janela. Aqui o Renato escaneia uma vez e o script cuida do resto.
#
# O QUE ELE FAZ, NESTA ORDEM (a ordem importa):
#   1. Confere o estado. Já `open` ⇒ não gera QR nenhum (gerar QR com a sessão
#      de pé é o caminho mais curto para derrubá-la de novo).
#   2. Gera o QR, salva PNG e abre no Preview.
#   3. Espera `open`, re-perguntando a cada 5s.
#   4. SÓ DEPOIS de `open`, dispara o catchup com a janela INTEIRA.
#
# ⚠️ O `hours` é o ponto do script. O cron roda com `hours=2` (default), e a
# queda tem 15,8h — o automático cobriria as 2 horas mais recentes e deixaria 14
# para trás, sem dizer que deixou. Aqui a janela é calculada do último evento
# real da instância, com 2h de folga.
#
# ⚠️ O catchup cobre DM, não grupo: mensagem de grupo é decisão terminal
# (`group_message`) no handler e grava por outro caminho
# (`persist_group_message_realtime` → `group_messages`, UNIQUE em `message_id`).
# Grupo é 83% do tráfego. Se as de grupo não voltarem pelo history sync do
# pareamento, elas não voltam — não há catchup de grupo hoje.
#
# Uso:  ./scripts/wa_repareia.sh
set -uo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
BASE="${EVOLUTION_API_URL%/}"
INST="${EVOLUTION_INSTANCE:-rap-whatsapp}"
KEY="$EVOLUTION_API_KEY"
QR_PNG="/tmp/evolution_qr_${INST}.png"

estado() {
  curl -s --max-time 20 -H "apikey: $KEY" \
    "$BASE/instance/connectionState/$INST" |
    python3 -c 'import sys,json;print((json.load(sys.stdin).get("instance") or {}).get("state",""))' 2>/dev/null
}

echo "── instância: $INST"
E=$(estado); echo "   estado atual: ${E:-'(não respondeu)'}"

if [ "$E" = "open" ]; then
  echo "✅ já conectada — nenhum QR será gerado (gerar QR com a sessão de pé a derruba)."
else
  echo "── gerando QR…"
  curl -s --max-time 40 -H "apikey: $KEY" "$BASE/instance/connect/$INST" \
    > /tmp/evolution_connect.json
  python3 - "$QR_PNG" <<'PY'
import base64, json, sys
with open("/tmp/evolution_connect.json") as fh:
    d = json.load(fh)
b64 = (d.get("base64") or d.get("qrcode", {}).get("base64") or "")
if not b64:
    print("   ⚠️ sem QR no retorno:", json.dumps(d)[:300]); raise SystemExit(1)
open(sys.argv[1], "wb").write(base64.b64decode(b64.split(",", 1)[-1]))
print("   QR salvo em", sys.argv[1])
PY
  [ -f "$QR_PNG" ] && open "$QR_PNG"
  echo
  echo "📲 No celular: WhatsApp › Configurações › Dispositivos conectados › Conectar dispositivo"
  echo "   O QR regenera a cada ~20s. Se expirar, rode o script de novo."
  echo
  echo "── aguardando conexão (até 3 min)…"
  for _ in $(seq 1 36); do
    sleep 5
    E=$(estado)
    printf "\r   estado: %-14s" "${E:-?}"
    [ "$E" = "open" ] && break
  done
  echo
fi

if [ "$E" != "open" ]; then
  echo "🔴 não conectou (estado: ${E:-?}). O catchup NÃO será disparado —"
  echo "   com o cano arrancado ele varreria 200 contatos para achar zero."
  exit 1
fi

echo "✅ conectada."

# Janela a recuperar: do último evento REAL desta instância até agora, +2h de
# folga. Note o `WHERE instance` — sem ele o `MAX` pega o evento do bot e a
# janela sai curta, que é o defeito que custou as 15,8h (ver 8dcfb13).
HORAS=$(DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python3 -c "
import os, psycopg2
c=psycopg2.connect(os.environ['DATABASE_URL'],keepalives=1); cur=c.cursor()
cur.execute('''SELECT ceil(extract(epoch from (now()-max(received_at)))/3600.0)+2
               FROM webhook_audit WHERE instance=%s''', (os.environ.get('EVOLUTION_INSTANCE') or 'rap-whatsapp',))
r=cur.fetchone()[0]; print(int(r) if r else 18)
" 2>/dev/null) || HORAS=18
echo "── catchup da janela inteira: hours=$HORAS (o cron usa 2 e deixaria o resto para trás)"

SECRET=$(grep -m1 '^CRON_SECRET=' .env | cut -d= -f2- | tr -d '[:space:]')
curl -s --max-time 300 -H "Authorization: Bearer $SECRET" \
  "https://intel.almeida-prado.com/api/cron/wa-catchup?hours=$HORAS" |
  python3 -m json.tool 2>/dev/null | head -40

echo
echo "── e o veredito do heartbeat, que deve voltar a 'ok':"
curl -s --max-time 60 -H "Authorization: Bearer $SECRET" \
  "https://intel.almeida-prado.com/api/cron/wa-ingest-heartbeat" | head -c 500
echo
echo
echo "⏭️  Depois: medir o EFEITO, não o 200 —"
echo "    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python3 scripts/verifica_frescor.py"
echo "    As três fontes de WA têm de voltar a 🟢. Grupo pode NÃO voltar: não há catchup de grupo."
