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


def _ultimo_evento_no_banco():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(received_at) AS ultimo FROM webhook_audit")
        row = cur.fetchone()
        return row["ultimo"] if row else None


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

    # Banco quieto. Isso ainda nao diz nada -- pergunta pra fonte.
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
