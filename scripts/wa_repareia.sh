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

# 🔴 A JANELA SE MEDE AGORA, ANTES DE RELIGAR, e a ORDEM é a proteção
# real. Religar grava eventos (`connection.update`, `qrcode.updated`) e,
# pior, o tráfego novo que volta a entrar também é `messages.upsert`: medida
# depois, a janela some no instante em que o cano volta, sem que uma única
# mensagem tenha sido recuperada. Em 15/10 o script pediu `hours=3` para uma
# queda de 16,5h e cobriu um quinto dela. O filtro de `event_type` (em
# `scripts/_janela_wa.py`) é o cinto secundário; o primário é medir ANTES.
# ⚠️ TRÊS CAMADAS DE SILÊNCIO, e o cálculo NUNCA rodou (descoberto 16/10/26).
# A 1ª versão era `HORAS=$(python3 -c "..." 2>/dev/null) || HORAS=20`, e o SQL
# inline terminava com o literal `messages.upsert` entre apóstrofos encostado
# na tripla-aspa que fechava a string Python. O apóstrofo de fechamento do SQL
# se fundiu com a tripla-aspa, o Python cortou a string ali, e o Postgres
# recebeu aspa não terminada:
#     psycopg2.errors.SyntaxError: unterminated quoted string
# O `2>/dev/null` engolia o traceback, o `|| HORAS=20` entregava um número
# plausível, e a linha impressa ainda dizia "medida ANTES de religar". Em
# 15/10 anunciou 3h; em 16/10, 20h — NENHUMA das duas foi medida, as duas
# eram o default. Default que parece resultado é pior que erro: o erro para a
# frente, o default a deixa seguir com um número inventado.
#
# Conserto em três pontos, um por camada de silêncio:
#   1. o literal vai como PARÂMETRO (%s), nunca inline — não há apóstrofo para
#      colidir com o quoting do Python nem com o do bash;
#   2. o número sai com MARCADOR (JANELA_HORAS=), porque o módulo `database`
#      escreve `[DB] neondb @ ...` em stdout e captura crua misturaria os dois;
#   3. stderr vai para arquivo e é EXIBIDO quando falha. O fallback continua
#      (melhor recuperar demais que travar o religamento), mas se ANUNCIA como
#      fallback, e a mensagem deixa de alegar medição.
JANELA_ERR=$(mktemp)
SAIDA=$(DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python3 \
  scripts/_janela_wa.py 2>"$JANELA_ERR")

HORAS=$(printf '%s' "$SAIDA" | sed -n 's/^JANELA_HORAS=\([0-9]\{1,\}\)$/\1/p' | head -1)
if [ -n "$HORAS" ]; then
  echo "   janela a recuperar: ${HORAS}h — MEDIDA agora (último messages.upsert + 2h de folga)"
else
  HORAS=20
  echo "   🔴 NÃO consegui medir a janela — usando o fallback de ${HORAS}h."
  echo "      Isto NÃO é medição: confira o tamanho real da queda antes de confiar."
  if [ -s "$JANELA_ERR" ]; then
    echo "      motivo:"
    sed 's/^/        /' "$JANELA_ERR" | tail -6
  else
    echo "      (stderr vazio — consulta sem resultado? nenhum evento desta instância?)"
  fi
fi
rm -f "$JANELA_ERR"

if [ "$E" = "open" ]; then
  echo "✅ já conectada — nenhum QR será gerado (gerar QR com a sessão de pé a derruba)."
else
  echo "📲 No celular: WhatsApp › Configurações › Dispositivos conectados › Conectar dispositivo"
  echo
  echo "── gerando QR (RENOVA sozinho a cada 20s, até 3 min)…"
  # ⚠️ O loop de renovação é o ponto desta parte. O QR da Evolution expira em
  # ~20s; gerar UM e esperar 3 min entrega um código morto e o pareamento
  # falha sem dizer por quê -- foi assim que a tentativa manual de 03/06/26
  # queimou várias rodadas. Aqui o PNG é SOBRESCRITO no mesmo caminho: o
  # Preview do macOS recarrega o arquivo quando ele muda, então a janela já
  # aberta passa a mostrar o código novo sem abrir uma segunda.
  ABRIU=0
  for _ in $(seq 1 9); do
    curl -s --max-time 40 -H "apikey: $KEY" "$BASE/instance/connect/$INST" \
      > /tmp/evolution_connect.json 2>/dev/null
    if python3 - "$QR_PNG" <<'PY'
import base64, json, sys
try:
    with open("/tmp/evolution_connect.json") as fh:
        d = json.load(fh)
except Exception as e:
    print("   ⚠️ retorno ilegível:", e); raise SystemExit(1)
b64 = (d.get("base64") or (d.get("qrcode") or {}).get("base64") or "")
if not b64:
    print("   ⚠️ sem QR no retorno:", json.dumps(d)[:200]); raise SystemExit(1)
open(sys.argv[1], "wb").write(base64.b64decode(b64.split(",", 1)[-1]))
raise SystemExit(0)
PY
    then
      [ "$ABRIU" = "0" ] && { open "$QR_PNG"; ABRIU=1; echo "   QR aberto no Preview — escaneie; ele se renova sozinho."; }
    fi
    for _ in $(seq 1 4); do
      sleep 5
      E=$(estado)
      printf "\r   estado: %-14s" "${E:-?}"
      [ "$E" = "open" ] && break
    done
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

echo "── catchup de DM: hours=$HORAS (o cron usa 2) e max_contacts=200 (o cron usa 50)"
SECRET=$(grep -m1 '^CRON_SECRET=' .env | cut -d= -f2- | tr -d '[:space:]')
curl -s --max-time 580 -H "Authorization: Bearer $SECRET" \
  "https://intel.almeida-prado.com/api/cron/wa-catchup?hours=$HORAS&max_contacts=200" |
  python3 -m json.tool 2>/dev/null | head -24

# ⚠️ GRUPO NÃO VEM DO CATCHUP, e sem este bloco 83% do tráfego fica fora.
# Medido em 15/10: o catchup devolveu 0 e o polling de grupo recuperou 141
# mensagens cobrindo a janela hora a hora. O mecanismo é outro — ao religar,
# o Baileys sincroniza o histórico para o banco da EVOLUTION, mas o webhook
# não assina evento de histórico (`MESSAGES_UPSERT, MESSAGES_UPDATE,
# SEND_MESSAGE, CONNECTION_UPDATE, QRCODE_UPDATED, CALL`), então o INTEL
# nunca é avisado; quem acha é o pull por `findMessages` de cada grupo.
# É PAGINADO: uma passada cobre ~9-13 dos 49 grupos, então roda-se o ciclo.
echo
echo "── polling de GRUPO, ciclo completo (o catchup não cobre grupo)"
for i in $(seq 1 8); do
  R=$(curl -s --max-time 580 -H "Authorization: Bearer $SECRET" \
        "https://intel.almeida-prado.com/api/cron/run-group-messages")
  echo "   [$i] $(printf '%s' "$R" | python3 -c 'import sys,json
d=json.load(sys.stdin); r=d.get("result") or {}
print("offset", d.get("offset_used"), "->", d.get("next_offset"),
      "| grupos", r.get("groups_synced"), "| salvas", r.get("messages_saved"))' 2>/dev/null)"
  printf '%s' "$R" | grep -q '"next_offset":0' && [ "$i" -gt 1 ] && break
done

echo
echo "── e o veredito do heartbeat, que deve voltar a 'ok':"
curl -s --max-time 60 -H "Authorization: Bearer $SECRET" \
  "https://intel.almeida-prado.com/api/cron/wa-ingest-heartbeat" | head -c 500
echo
echo
echo "⏭️  Depois: medir o EFEITO, não o 200 —"
echo "    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python3 scripts/verifica_frescor.py"
echo "    As três fontes de WA têm de voltar a 🟢. Grupo pode NÃO voltar: não há catchup de grupo."
