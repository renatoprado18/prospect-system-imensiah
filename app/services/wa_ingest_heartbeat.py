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
    """Ultima MENSAGEM desta instancia -- os dois filtros sao o veredito.

    Sem `WHERE instance`, o `MAX` pega o evento mais novo de QUALQUER instancia:
    em 15/10/26 o `intel-bot-v2` encurtou um silencio de 15,8h para 3,8h e, ao
    deixar o `ultimo` mais novo que a fonte, zerou o alarme. Ver o cabecalho.

    🔴 E SEM `event_type` o MESMO DEFEITO VOLTA PELA PORTA DO LADO (14/11/26, 8h).
    `connection.update` chega 2x por hora, toda hora -- medidos 80 eventos e 40
    ciclos `connecting`->`open` em 30h, intervalo de ~50 min. Esse keep-alive
    deixava `ultimo` quase sempre recente e, pior, MAIS NOVO que qualquer
    mensagem que a Evolution conhecia: `atraso_seg` dava negativo e o veredito
    "mundo_quieto" era -- de novo -- o unico resultado aritmeticamente possivel.
    Em 14-15/10 a contaminacao vinha de outra INSTANCIA; aqui, de outro TIPO DE
    EVENTO da mesma instancia. Um evento que o proprio cano gera nao prova que
    DADO passou por ele.

    🔴 E O TERCEIRO FILTRO, `from_me`, pego pelo controle positivo meia hora
    depois do segundo (14/11/26). Com instance + event_type o medidor ainda deu
    `quieto_min: 0.9` -- porque dois upserts `fromMe=True` (envios do proprio
    Renato) tinham entrado as 11h30 enquanto a ENTRADA seguia morta havia 16h32.
    E a volta de [[feedback_maquina_ouve_o_proprio_eco]] por uma terceira porta:
    a pergunta e "estao me MANDANDO mensagem?", e mensagem que sai nao responde.
    Os tres filtros sao a mesma licao em camadas: instancia errada, evento errado,
    direcao errada -- e qualquer um deles sozinho basta pra zerar o alarme.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT MAX(received_at) AS ultimo FROM webhook_audit
                WHERE instance = %s
                  AND event_type = 'messages.upsert'
                  AND from_me IS NOT TRUE""",
            (INSTANCIA,),
        )
        row = cur.fetchone()
        return row["ultimo"] if row else None


# Atraso de ENTREGA que autoriza alertar, em minutos. Medido, nao estimado: em 60
# dias e 4.216 mensagens do periodo saudavel o p50 e 0,02 min e o p95 e 0,04 min
# -- entrega normal e instantanea, ~2 segundos. 15 min fica 375x acima do p95
# saudavel e teria gritado em 10/11, quando a mediana ja era de 3h33.
ATRASO_ENTREGA_MIN = int((os.getenv("WA_HEARTBEAT_ATRASO_MIN") or "15").strip())

# Quantas mensagens entram na mediana. A ultima sozinha vira alarme com um unico
# deploy lento; a media e refem de um outlier. Com p50 de 30 e preciso que METADE
# da janela recente esteja atrasada pra gritar.
ATRASO_JANELA = 30

# 🔴 E A JANELA TAMBEM E DE TEMPO, nao so de contagem (23/11/26). So "ultimas 30
# gravadas" nao distingue "o que chega AGORA chega velho" de "as ultimas 30 sao
# de um episodio que JA ACABOU". Medido no proprio conserto da madrugada: com o
# canal restaurado e mensagem entrando em 0 min, o heartbeat seguia respondendo
# `atraso_entrega_min: 1350.8` e `dias_seguidos: 6`, porque nenhuma DM nova havia
# entrado ainda e a janela continuava cheia do backlog do dia anterior. O alarme
# que tinha acabado de acertar passaria a mentir na direcao oposta, e o aviso
# chega no WhatsApp do Renato a cada ciclo.
#
# 3h e 5 mensagens, escolhidos por simulacao hora a hora sobre 11 dias de
# `messages` (nao estimados): pega as panes conhecidas de 12, 14 e 17-22/11,
# fica VERDE nos dois dias saos (13/11 com 9 horas verdes, 16/11 com 12) e
# devolve "nao medido" nas horas sem amostra, em vez de repetir o veredito
# velho. Amostra pequena nao vira veredito -- mediana de 1 ou 2 mensagens e
# ruido com cara de medicao.
ATRASO_JANELA_H = int((os.getenv("WA_HEARTBEAT_ATRASO_JANELA_H") or "3").strip())
ATRASO_MIN_AMOSTRA = int((os.getenv("WA_HEARTBEAT_ATRASO_MIN_AMOSTRA") or "5").strip())


def _dias_atrasados_seguidos() -> int:
    """Ha quantos dias SEGUIDOS a entrega mediana do dia passa do limiar.

    🔴 POR QUE PRECISA EXISTIR (22/11/26). O aviso de atraso saiu 42 vezes em 9
    dias, com o mesmo texto -- "as mensagens estao presas, vao aparecer depois,
    nao concluir que alguem nao respondeu enquanto isto durar". No primeiro dia
    aquilo descrevia um incidente. No quinto, descrevia o regime normal, e
    seguia escrito como se fosse transitorio. Medido por dia de gravacao: 16/11
    0,0 min (o conserto do dia anterior funcionou por UM dia), 18/11 20h, 19/11
    24h, 20/11 24h, 21/11 20h, 22/11 21h.

    O que muda a acao do Renato nao e o atraso de agora -- e saber que ele ja
    dura cinco dias, porque "espera a fila drenar" deixou de ser resposta. Aviso
    que repete identico ensina a ser ignorado
    ([[feedback_superficie_nova_mata_o_aviso]]).

    A conta e por DIA, nao pela ultima mensagem pontual: entre lotes atrasados
    entram algumas mensagens em tempo real (as de 22/11 14h01 vieram em 0,3 min)
    e um "desde a ultima pontual" leria 10h onde ha 5 dias. Devolve 0 quando o
    dia corrente esta em dia -- entao `>= 2` e o que caracteriza cronico.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT date_trunc('day', m.criado_em) AS dia,
                      percentile_cont(0.5) WITHIN GROUP (
                          ORDER BY EXTRACT(EPOCH FROM (m.criado_em - m.enviado_em)) / 60.0
                      ) AS p50
                 FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                WHERE c.canal ILIKE %s
                  AND m.direcao = 'incoming'
                  AND m.enviado_em IS NOT NULL
                  AND m.criado_em > now() - interval '30 days'
                GROUP BY 1
                ORDER BY 1 DESC""",
            ("%whats%",),
        )
        dias = 0
        for row in cur.fetchall():
            p50 = row["p50"]
            if p50 is None or float(p50) <= ATRASO_ENTREGA_MIN:
                break
            dias += 1
        return dias


def _atraso_de_entrega_min():
    """Mediana de `criado_em - enviado_em` nas ultimas mensagens recebidas.

    🔴 O TERCEIRO DETECTOR, e o unico que viu a pane de 14/11/26. Os outros dois
    perguntam "chegou algo?" (frescor) e "a Evolution tem o que o INTEL nao tem?"
    (comparativo). Nenhum serve quando a entrega nao PARA, so ATRASA: a cada lote
    de madrugada o frescor zerava, e o comparativo ficava cego porque os DOIS
    lados estavam igualmente atrasados -- o store da Evolution tambem so tinha
    ate 19h00 do dia anterior. A pane era degradante (0,0 min em 03/11 -> 3h33 em
    10/11 -> 10h39 em 11/11) e degradacao nao aparece em medidor de "quando foi a
    ultima".

    A conta e INTERNA ao dado, e isso e a propriedade que importa: nao depende de
    haver mensagem NOVA (logo nao da falso positivo de madrugada), nao depende da
    Evolution responder, e nao pode ser satisfeita pelo dreno do backlog -- que e
    exatamente o que satisfazia os medidores de `criado_em`
    ([[feedback_medidor_de_ingestao_satisfeito_pelo_backlog]]).

    ⚠️ A janela e de TEMPO **e** de contagem (ver `ATRASO_JANELA_H`). So contagem
    nao distingue "o que chega agora chega velho" de "as ultimas 30 pertencem a um
    episodio encerrado", e foi assim que o medidor seguiu acusando 1350 min com a
    entrega ja restaurada em 0 min.

    Devolve `None` quando nao ha amostra suficiente na janela. Nao-saber nao vira
    alerta, mas tambem nao vira "em dia": quem chama distingue os dois. Quem cobre
    o caso "nao chega NADA" e o frescor (`quieto_min`) e o comparativo, nao este
    detector -- a divisao e de proposito: este responde "o que chega, chega
    velho?", e sem nada chegando a pergunta nao tem resposta.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) AS n,
                      percentile_cont(0.5) WITHIN GROUP (
                          ORDER BY EXTRACT(EPOCH FROM (m.criado_em - m.enviado_em)) / 60.0
                      ) AS p50
                 FROM (SELECT m2.criado_em, m2.enviado_em
                         FROM messages m2
                         JOIN conversations c ON c.id = m2.conversation_id
                        WHERE c.canal ILIKE %s
                          AND m2.direcao = 'incoming'
                          AND m2.enviado_em IS NOT NULL
                          AND m2.criado_em > now() - make_interval(hours => %s)
                        ORDER BY m2.criado_em DESC
                        LIMIT %s) m""",
            ("%whats%", ATRASO_JANELA_H, ATRASO_JANELA),
        )
        row = cur.fetchone()
        if not row or (row["n"] or 0) < ATRASO_MIN_AMOSTRA:
            return None
        valor = row["p50"]
        return None if valor is None else float(valor)


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
    atraso = _atraso_de_entrega_min()

    def _atrasada() -> Dict:
        """Veredito de entrega lenta. Separado por ser um estado DIFERENTE de
        parada: o dado vem, vem velho, e as mensagens estao presas na fila --
        nao perdidas. Quem le precisa saber disso, porque a leitura natural de
        uma fonte atrasada e "ninguem falou comigo", e foi essa leitura que em
        12/11 fez a CoS afirmar que a Sandra nao respondera quando ela havia
        respondido em 29 horas."""
        return {
            "status": "entrega_atrasada",
            "alerta": True,
            "atraso_entrega_min": round(atraso, 1),
            "atraso_entrega_limiar_min": ATRASO_ENTREGA_MIN,
            "janela": ATRASO_JANELA,
            # Quantos dias seguidos o problema dura. Sem isto o aviso do 5o dia
            # e indistinguivel do 1o, e foi assim que 42 avisos em 9 dias nao
            # produziram uma acao.
            "dias_seguidos": _dias_atrasados_seguidos(),
            "quieto_min": round(quieto_min, 1),
            "ultimo_evento": ultimo.isoformat(),
            # Nada a recuperar por catchup: o que falta ainda nao saiu da
            # Evolution. `janela_perdida` nao deve pedir busca aqui.
            "nada_a_recuperar": True,
        }

    # ORDEM DOS VEREDITOS, e cada posicao tem um motivo:
    #
    # 1º `desconectado` -- porque e o unico que pede ACAO FISICA (QR no
    #    aparelho). Se o atraso viesse antes, o aviso mandaria esperar a fila
    #    drenar quando o certo e ir ao celular, e os dois sintomas aparecem
    #    juntos: com a sessao caida, as ultimas mensagens gravadas sao
    #    justamente as do lote atrasado.
    # 2º `entrega_atrasada` -- antes do atalho de silencio curto, senao um lote
    #    entrando agora zera `quieto_min`, o `return "ok"` dispara e a entrega
    #    de 8h sai verde. Foi assim que a pane de 14/11 passou por saudavel
    #    durante 9 dias: o relogio reiniciava a cada madrugada.
    # 3º o resto, como antes.
    #
    # A checagem de conexao segue atras do limiar de silencio para preservar o
    # custo do caminho feliz: com mensagem entrando agora, nao se pergunta nada
    # a ninguem.
    if quieto_min >= SILENCIO_MIN:
        # Banco quieto. ANTES de comparar, a pergunta que o comparativo nao sabe
        # fazer: o cano esta LIGADO? Se a instancia caiu, a Evolution tambem
        # parou de receber, os dois lados ficam vazios e o comparativo diz
        # "mundo quieto" justamente no pior caso (14-15/10/26, 15,8h).
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

    if atraso is not None and atraso > ATRASO_ENTREGA_MIN:
        return _atrasada()

    if quieto_min < SILENCIO_MIN:
        veredito = {"status": "ok", "alerta": False,
                    "quieto_min": round(quieto_min, 1),
                    "ultimo_evento": ultimo.isoformat()}
        if atraso is None:
            # Nao medir nao e estar em dia: diz-se alto, sem virar alerta.
            veredito["atraso_entrega_min"] = None
            veredito["detalhe"] = (
                f"atraso de entrega NAO MEDIDO (menos de {ATRASO_MIN_AMOSTRA} "
                f"mensagens nas ultimas {ATRASO_JANELA_H}h)"
            )
        else:
            veredito["atraso_entrega_min"] = round(atraso, 1)
        return veredito

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
            "atraso_entrega_min": None if atraso is None else round(atraso, 1),
            "ultimo_evento": ultimo.isoformat()}


def janela_perdida(veredito: Dict) -> Dict:
    """Intervalo a recuperar quando a ingestao voltar.

    A CoS pediu isso explicitamente em 03/08: "webhook voltou" nao e "banco em
    dia" -- quando o cano religa, o que caiu no gap continua fora ate alguem ir
    buscar. Devolve as horas a pedir ao catchup.
    """
    if not veredito.get("alerta"):
        return {}
    # Entrega atrasada nao tem janela a recuperar: o que falta ainda esta DENTRO
    # da Evolution, nao caiu num gap. Pedir catchup aqui gastaria chamada para
    # buscar o que ninguem tem ainda, e devolveria "recuperei 0" como se o
    # problema estivesse resolvido.
    if veredito.get("nada_a_recuperar"):
        return {}
    horas = max(1, int(veredito.get("quieto_min", 0) // 60) + 1)
    return {"desde": veredito.get("ultimo_evento"), "horas": horas}
