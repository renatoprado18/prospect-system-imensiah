"""Heartbeat da ingestao do WhatsApp: avisa quando o cano para de entregar.

POR QUE EXISTE (03-04/08/2026). O webhook do WhatsApp ficou 3h sem entregar nada
-- `a7654f4` fechou `/api/webhooks/whatsapp` com auth achando que a rota nao
tinha consumidor, e a Evolution (que fica fora do repo) passou a levar 401.
Perderam-se 93 mensagens, entre elas o endereco de uma reuniao presencial. Nada
avisou: descobriu-se por acaso, ao investigar outro sintoma. A CoS chegou a
marcar compromisso em cima de um horario que ja estava tomado.

O QUE NAO FAZER. "Alertar se nao chega mensagem ha 20min" parece a resposta e e
uma fabrica de falso positivo: no dia medido, 06h UTC teve ZERO eventos e estava
tudo certo -- gente dorme. Um alerta que grita toda madrugada e desligado na
primeira semana, e ai nao protege mais nada.

O SINAL CERTO e comparativo, nao absoluto: **a Evolution tem mensagem que o INTEL
nao tem?** Silencio dos dois lados = mundo quieto, nada a dizer. Silencio de um
lado so = o cano quebrou. Isso vale a qualquer hora e nao depende de adivinhar
o ritmo do Renato.

Custo: 1 chamada a Evolution por execucao, e SO quando o banco ja esta quieto ha
mais que o limiar -- no caminho normal (mensagem entrou agora) nem chega a
perguntar.

🔴 O QUE O COMPARATIVO NAO COBRE, e custou 15,8h em 14-15/10/2026. O WhatsApp
Business do Renato deu problema no aparelho e ele re-registrou o numero no
WhatsApp comum. Isso invalida TODOS os dispositivos vinculados de uma vez: a
instancia `rap-whatsapp` caiu as 18h59 com `state=close` / `reason=403`. Este
heartbeat rodou **96 vezes** durante a queda e devolveu `alerta: false,
status: mundo_quieto` em todas. Nao foi azar -- foi consequencia de dois
defeitos que se somaram, e o segundo INVERTEU o sinal:

1. `_ultimo_evento_no_banco` lia `MAX(received_at)` de `webhook_audit` **sem
   filtrar instancia**. A instancia `intel-bot-v2` (outro numero, o do bot)
   estava viva e gravou evento as 07h00 -- entao o silencio de 15,8h apareceu
   como 3,8h. Uma instancia viva atestando o frescor de uma morta e a classe de
   [[feedback_medir_o_consumidor_certo]].
2. Pior que encurtar: esse `ultimo` contaminado ficou **mais novo** que a ultima
   mensagem que a Evolution conhecia (14/10 19h00), logo `atraso_seg` deu
   **negativo** e nunca passou da tolerancia. O veredito "mundo_quieto" nao foi
   uma leitura infeliz -- era o unico resultado aritmeticamente possivel.

E o comparativo, por desenho, so enxerga webhook caido com a Evolution VIVA.
Quando a propria instancia cai, a Evolution tambem para de receber: os dois
lados ficam igualmente vazios, nao existe gap para achar, e o cenario mais grave
le-se como o mais tranquilo ([[feedback_guarda_abstencao_vira_fabrica]]).

Por isso, antes de comparar, pergunta-se o que distingue os dois casos com UMA
chamada: **a instancia esta conectada?** `state=close` e determinista, nao
precisa de comparacao e nao da falso positivo -- instancia desconectada e
sempre problema, as 3h ou as 15h, e nao se resolve sozinha (exige QR novo).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import timedelta
from typing import Dict

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)

# Quanto tempo de silencio no banco autoriza a PERGUNTAR pra Evolution. Nao e o
# limiar do alerta -- e so o gatilho da verificacao. O alerta depende da
# resposta dela.
SILENCIO_MIN = int((os.getenv("WA_HEARTBEAT_SILENCIO_MIN") or "20").strip())

# Folga entre o ultimo evento do INTEL e a mensagem na Evolution. Sem isso, a
# mensagem que esta em transito neste exato segundo viraria alerta.
TOLERANCIA_SEG = int((os.getenv("WA_HEARTBEAT_TOLERANCIA_SEG") or "120").strip())

INSTANCIA = (os.getenv("EVOLUTION_INSTANCE") or "rap-whatsapp").strip()

# Silencio que autoriza alertar quando a instancia esta `connecting`. Separado
# do limiar geral de proposito: `connecting` e transitorio (religamento normal)
# e gritar nele seria a fabrica de falso positivo que este modulo existe pra
# evitar. Ja `close` nao espera limiar nenhum -- e terminal.
CONNECTING_MIN = int((os.getenv("WA_HEARTBEAT_CONNECTING_MIN") or "60").strip())


def _ultimo_evento_no_banco():
    """Ultimo evento DESTA instancia -- o filtro nao e detalhe, e o veredito.

    Sem `WHERE instance`, o `MAX` pega o evento mais novo de QUALQUER instancia:
    em 15/10/26 o `intel-bot-v2` encurtou um silencio de 15,8h para 3,8h e, ao
    deixar o `ultimo` mais novo que a fonte, zerou o alarme. Ver o cabecalho.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(received_at) AS ultimo FROM webhook_audit WHERE instance = %s",
            (INSTANCIA,),
        )
        row = cur.fetchone()
        return row["ultimo"] if row else None


def _estado_da_conexao() -> str:
    """`open` | `connecting` | `close` | "" quando nao se sabe.

    String vazia e "nao sei" (rede, chave, Evolution fora) e NUNCA vira alerta
    por si -- quem chama cai no comparativo, que era o comportamento antigo.
    Instabilidade da Evolution nao pode virar alarme de ingestao parada.
    """
    base = (os.getenv("EVOLUTION_API_URL") or "").strip().strip('"').rstrip("/")
    key = (os.getenv("EVOLUTION_API_KEY") or "").strip().strip('"')
    if not base or not key:
        return ""
    req = urllib.request.Request(
        f"{base}/instance/connectionState/{INSTANCIA}",
        headers={"apikey": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"wa-heartbeat: connectionState nao respondeu ({e})")
        return ""
    instancia = payload.get("instance")
    if not isinstance(instancia, dict):
        return ""
    return (instancia.get("state") or "").strip()


def _ultima_mensagem_na_evolution() -> int:
    """Epoch da mensagem mais recente que a Evolution conhece (0 se nao souber).

    Uma pagina de 20 basta: so interessa o topo. Erro de rede devolve 0, que o
    chamador le como "nao sei" -- e nao-saber NUNCA vira alerta, senao a
    instabilidade da Evolution viraria alarme de ingestao parada.
    """
    base = (os.getenv("EVOLUTION_API_URL") or "").strip().strip('"').rstrip("/")
    key = (os.getenv("EVOLUTION_API_KEY") or "").strip().strip('"')
    if not base or not key:
        return 0
    req = urllib.request.Request(
        f"{base}/chat/findMessages/{INSTANCIA}",
        data=json.dumps({"page": 1, "offset": 20}).encode(),
        method="POST",
        headers={"apikey": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"wa-heartbeat: Evolution nao respondeu ({e}) — sem veredito")
        return 0
    registros = payload.get("messages", {}).get("records", []) or []
    epochs = []
    for r in registros:
        try:
            epochs.append(int(r.get("messageTimestamp") or 0))
        except (TypeError, ValueError):
            continue
    return max(epochs) if epochs else 0


def checar_ingestao() -> Dict:
    """Veredito da ingestao. Nao notifica -- quem chama decide o que fazer."""
    agora = now_utc()
    ultimo = _ultimo_evento_no_banco()

    if ultimo is None:
        return {"status": "sem_dados", "alerta": False,
                "detalhe": "webhook_audit vazio"}

    if ultimo.tzinfo is None:
        from services.tz import UTC
        ultimo = ultimo.replace(tzinfo=UTC)

    quieto_min = (agora - ultimo).total_seconds() / 60.0

    if quieto_min < SILENCIO_MIN:
        return {"status": "ok", "alerta": False,
                "quieto_min": round(quieto_min, 1),
                "ultimo_evento": ultimo.isoformat()}

    # Banco quieto. ANTES de comparar, a pergunta que o comparativo nao sabe
    # fazer: o cano esta LIGADO? Se a instancia caiu, a Evolution tambem parou
    # de receber, os dois lados ficam vazios e o comparativo diz "mundo quieto"
    # justamente no pior caso (14-15/10/26, 15,8h).
    estado = _estado_da_conexao()
    if estado and estado != "open":
        # `close` e terminal: exige re-pareamento por QR, nao volta sozinho.
        # `connecting` ganha carencia -- religamento normal passa por ele.
        if estado == "close" or quieto_min >= CONNECTING_MIN:
            return {
                "status": "desconectado",
                "alerta": True,
                "estado_instancia": estado,
                "instancia": INSTANCIA,
                "quieto_min": round(quieto_min, 1),
                "ultimo_evento": ultimo.isoformat(),
                # Sem par na Evolution: ela nao tem mensagem mais nova porque
                # tambem nao esta recebendo. Quem formata o aviso NAO pode
                # supor `atraso_min` -- ver o ramo em main.py.
                "exige_qr": estado == "close",
            }

    # Instancia de pe (ou estado desconhecido): vale o comparativo historico.
    epoch_evolution = _ultima_mensagem_na_evolution()
    if not epoch_evolution:
        return {"status": "indeterminado", "alerta": False,
                "quieto_min": round(quieto_min, 1),
                "detalhe": "Evolution nao respondeu; sem veredito"}

    from datetime import datetime, timezone
    msg_evolution = datetime.fromtimestamp(epoch_evolution, tz=timezone.utc)
    atraso_seg = (msg_evolution - ultimo).total_seconds()

    if atraso_seg > TOLERANCIA_SEG:
        return {
            "status": "parado",
            "alerta": True,
            "quieto_min": round(quieto_min, 1),
            "ultimo_evento": ultimo.isoformat(),
            "ultima_msg_evolution": msg_evolution.isoformat(),
            "atraso_min": round(atraso_seg / 60.0, 1),
        }

    # Os dois lados quietos: nao ha o que ingerir. Silencio legitimo.
    return {"status": "mundo_quieto", "alerta": False,
            "quieto_min": round(quieto_min, 1),
            "ultimo_evento": ultimo.isoformat()}


def janela_perdida(veredito: Dict) -> Dict:
    """Intervalo a recuperar quando a ingestao voltar.

    A CoS pediu isso explicitamente em 03/08: "webhook voltou" nao e "banco em
    dia" -- quando o cano religa, o que caiu no gap continua fora ate alguem ir
    buscar. Devolve as horas a pedir ao catchup.
    """
    if not veredito.get("alerta"):
        return {}
    horas = max(1, int(veredito.get("quieto_min", 0) // 60) + 1)
    return {"desde": veredito.get("ultimo_evento"), "horas": horas}
