"""O grupo Governança Jabô atualiza as tasks do #28 sozinho.

DECISÃO DO RENATO, 16/08/2026: *"será que os RACI precisam mesmo passar por mim?
Se eles são auto-atualizáveis, e todos sabem que é IA, basta ir atualizando. Se o
responsável detecta algo errado, ele mesmo manda uma msg para corrigir."*

O QUE JUSTIFICA ABRIR MÃO DO GATE AQUI. De 91 propostas de RACI já geradas a
partir de grupos, 1 foi aplicada e 71 descartadas — número que parece condenar a
automação até se ler o motivo dos descartes: quase todos dizem "evidência stale
reprocessada" ou "capturada na síntese, ponte manual é o caminho". Não foram
descartadas por estarem ERRADAS, e sim por chegarem tarde ou duplicadas. O gate
filtrou TIMING, não erro — e cobrou caro: o RACI do Jabô parou em 03/08 e a
Andressa reportou seis vezes até 16/08 sem receber nada de volta.

POR QUE SÓ O JABÔ. O `raci_group_shadow` estabelece que "governança de CLIENTE
não muda sem gate humano", e isso continua valendo para Vallen/Alba/Despertar: lá
o RACI carrega implicitamente "o conselheiro está acompanhando", e um erro custa
constrangimento profissional a quem não o cometeu. No Jabô a contraparte é a
executora da própria operação — erro barato, detecção imediata, incentivo direto
de corrigir. A exceção é do domínio, não da tecnologia; não generalizar.

O LIMITE CONHECIDO DESTE DESENHO, dito de frente: autocorreção pega erro de
COMISSÃO (a máquina escreveu algo errado, e quem lê vê), não de OMISSÃO (a
máquina não escreveu nada, e ninguém reclama de uma linha que não apareceu). Por
isso duas coisas aqui não são opcionais: `jabo_group_reads` registra TODA
mensagem examinada, inclusive as que não viraram nada — sem denominador não há
taxa ([[feedback_mecanismo_que_nao_mede_o_denominador]]) —, e a procedência vai
junto da linha no RACI, para que a executora consiga ver o que mudou E o que
deixou de mudar.

ELE ESCREVE EM `raci_itens`, NÃO EM `tasks` — e essa escolha custou uma medição.
A peça que a Andressa recebe é montada por `raci_matrix` a partir dos 10 itens
curados de `raci_itens`, e os 10 têm `task_id = NULL`. Atualizar `tasks` mexeria
no backlog do Renato sem mudar uma linha do que ela lê: escrita bem-sucedida,
livro-razão em ordem, efeito pretendido inexistente. É a armadilha de escrever no
consumidor errado, e ela não dá erro nenhum.

O QUE ESTE MÓDULO NÃO FAZ:
- não cria item nem task — inventar trabalho a partir de conversa é outro risco;
- não reescreve `notas`, só `status` (+ a procedência). Campo de texto curado
  sobrescrito inteiro foi o dano da cascata de 14/08;
- não olha para trás. A janela é de dias recentes, porque reprocessar histórico
  foi exatamente o que produziu o descarte em massa de 22/07 no shadow;
- não toca as `tasks` do #28. Elas continuam sendo o backlog dele, e seguem
  desligadas dos itens do RACI (`task_id` nulo nos 10). Ligar as duas fontes é
  frente própria — ver [[project_tres_cockpits_divergentes]].
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

from database import get_db
from services import agent_write, llm, llm_usage

logger = logging.getLogger(__name__)

PROJETO_JABO = 28
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AGENTE = "jabo_group_raci"

# Janela de leitura. Curta de propósito: o cron roda diariamente, então 7 dias é
# folga para uma falha de um dia sem virar reprocessamento de histórico.
JANELA_DIAS = int(os.getenv("JABO_RACI_JANELA_DIAS", "7"))
# Teto por rodada — trava de custo e de dano. Uma rodada que proponha 40
# mudanças não é produtividade, é sintoma.
TETO_RODADA = int(os.getenv("JABO_RACI_TETO", "12"))

# Vocabulário de `raci_itens`, em PORTUGUÊS — não o de `tasks`
# (pending/completed). Trocar os dois é o erro `active` × `ativo` que já custou
# uma sessão em 13/08: o valor sai do CHECK do banco e a escrita morre, ou pior,
# passa e significa outra coisa.
STATUS_VALIDOS = {"pendente", "em_andamento", "concluido"}

# Carimbo de procedência. `concluido_em_fonte` existe desde a 073 e até hoje é
# NULL em todas as linhas; passa a valer 'grupo_wa' quando quem fechou o item foi
# esta leitura do WhatsApp. É o que torna o auto-update VISÍVEL — sem isso a
# executora não distingue o que a máquina mudou do que sempre esteve ali, e
# "manda uma msg para corrigir" vira exigência de auditoria.
FONTE_PROCEDENCIA = "grupo_wa"


def _group_jid(cur) -> Optional[str]:
    cur.execute("""
        SELECT group_jid FROM project_whatsapp_groups
         WHERE project_id = %s AND ativo = TRUE LIMIT 1
    """, (PROJETO_JABO,))
    r = cur.fetchone()
    return r["group_jid"] if r else None


def _desde_ultimo_raci(cur, jid: str) -> Optional[datetime]:
    """Quando saiu o último RACI no grupo — a âncora do backfill.

    POR QUE ESTA JANELA E NÃO O GRUPO INTEIRO. O reprocessamento de histórico é o
    que estragou o shadow do ConselhoOS em 22/07 (42 de 72 propostas eram
    evidência velha re-litigando decisão que a ponte manual já tinha absorvido).
    Aqui a situação é o INVERSO e é o que torna o backfill defensável: desde
    03/08 ninguém absorveu nada — não há ponte manual, não há decisão anterior a
    re-litigar. O que existe é uma fila de reportes que nunca chegou ao RACI.

    Antes do último RACI, porém, a peça JÁ refletia o que se sabia: aquele texto
    foi revisado e enviado. Voltar mais que isso seria reabrir o que já estava
    fechado — que é exatamente o erro de 22/07.
    """
    cur.execute("""
        SELECT MAX(timestamp) AS ts FROM group_messages
         WHERE group_jid = %s AND from_me = TRUE AND content ILIKE %s
    """, (jid, '%RACI%'))
    r = cur.fetchone()
    return r["ts"] if r else None


def _mensagens_novas(cur, jid: str, desde: Optional[datetime] = None) -> List[Dict]:
    """Mensagens do grupo ainda não examinadas por ESTE caminho.

    O anti-join é com `jabo_group_reads`, não com `group_messages.raci_processed_at`:
    aquele carimbo é do shadow do ConselhoOS, que marca as mensagens do Jabô como
    processadas justamente por não resolverem empresa (50 de 97 em 16/08). Usá-lo
    daria uma fila permanentemente vazia — consumidor morto com outro nome.
    """
    cur.execute("""
        SELECT g.id, g.timestamp, g.sender_name, g.content
          FROM group_messages g
     LEFT JOIN jabo_group_reads r ON r.group_message_id = g.id
         WHERE g.group_jid = %s
           AND g.from_me = FALSE
           AND r.id IS NULL
           AND g.timestamp > %s
           AND COALESCE(g.content, '') <> ''
      ORDER BY g.timestamp
    """, (jid, desde or (datetime.now() - timedelta(days=JANELA_DIAS))))
    return [dict(x) for x in cur.fetchall()]


def _itens_abertos(cur) -> List[Dict]:
    """Itens do RACI que a peça do grupo mostra — a fonte que a executora lê."""
    cur.execute("""
        SELECT id, acao, status, responsavel_r FROM public.raci_itens
         WHERE project_id = %s AND status IN ('pendente', 'em_andamento')
      ORDER BY id
    """, (PROJETO_JABO,))
    return [dict(x) for x in cur.fetchall()]


def _prompt(mensagens: List[Dict], tasks: List[Dict]) -> str:
    linhas_t = "\n".join(
        f"  [{t['id']}] ({t['status']}, R: {t.get('responsavel_r') or '—'}) {t['acao']}"
        for t in tasks)
    linhas_m = "\n".join(
        f"  <msg id={m['id']} de=\"{m.get('sender_name') or '?'}\" "
        f"quando=\"{m['timestamp']:%d/%m %H:%M}\">{' '.join((m.get('content') or '').split())[:600]}</msg>"
        for m in mensagens)
    return f"""Você lê o grupo de WhatsApp da governança de uma fazenda de café e mantém a matriz RACI em dia.

ITENS DO RACI EM ABERTO (id, status atual, responsável, ação):
{linhas_t or '  (nenhum)'}

MENSAGENS NOVAS DO GRUPO:
{linhas_m}

Para CADA mensagem, decida se ela reporta execução de algum item da lista.

REGRAS:
- `item_id` DEVE ser um id que aparece na lista acima. Nunca invente.
- Status possíveis: "pendente", "em_andamento", "concluido" (sem acento).
- Só marque `concluido` se a mensagem diz que a ação FOI FEITA (verbo no passado,
  resultado anunciado). "Vou enviar amanhã" não é conclusão; "amostra enviada" é.
- Um item amplo e contínuo (ex.: "prospecção de torrefações", que é trabalho
  recorrente) raramente fica `concluido` por causa de um passo isolado. Reportar
  um follow-up de uma torrefação não conclui a prospecção inteira — nesse caso
  use `em_andamento`, ou devolva item_id null se já estiver em_andamento.
- Mensagem que só pergunta, cumprimenta ou combina logística NÃO é atualização:
  devolva `item_id: null`.
- `corrige: true` se a mensagem contesta ou conserta uma informação anterior
  ("não foi a Coffee Lab, foi a Garagem", "na verdade ainda não enviei").
- `confianca`: 0.0 a 1.0. Seja honesto. Abaixo de 0.75 vira pergunta ao humano em
  vez de mudança — o que é o resultado certo quando você está em dúvida.

Responda APENAS um array JSON:
[{{"msg_id": 123, "item_id": 10 ou null, "novo_status": "concluido",
   "confianca": 0.9, "corrige": false, "motivo": "frase curta em português"}}]"""


async def _julgar(mensagens: List[Dict], tasks: List[Dict]) -> List[Dict]:
    """Chama o modelo e devolve só o que sobrevive à validação.

    O filtro de id alucinado não é zelo: o `raci_smart_updates` já registrou em
    08/06 que o modelo às vezes devolve id inventado. Aqui um id errado
    escreveria em OUTRA tarefa do projeto — e o livro-razão registraria a escrita
    como legítima.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("jabo_group_raci: ANTHROPIC_API_KEY ausente — nada julgado")
        return []
    try:
        async with httpx.AsyncClient(timeout=40.0) as cli:
            r = await cli.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": llm.FAST, "max_tokens": 1500,
                      "messages": [{"role": "user", "content": _prompt(mensagens, tasks)}]},
            )
        if r.status_code != 200:
            logger.warning("jabo_group_raci: API %s: %s", r.status_code, r.text[:200])
            return []
        resp = r.json()
        llm_usage.record_response("jabo.raci_grupo", llm.FAST, resp)
        texto = resp["content"][0]["text"]
        i, f = texto.find("["), texto.rfind("]") + 1
        if i < 0:
            return []
        bruto = json.loads(texto[i:f])
    except Exception as e:                                      # noqa: BLE001
        logger.warning("jabo_group_raci: julgamento falhou: %s", e)
        return []

    ids_msg = {m["id"] for m in mensagens}
    ids_item = {t["id"] for t in tasks}
    atual = {t["id"]: t["status"] for t in tasks}
    saida = []
    for p in bruto:
        if not isinstance(p, dict):
            continue
        try:
            msg_id = int(p.get("msg_id"))
        except (TypeError, ValueError):
            continue
        if msg_id not in ids_msg:
            logger.info("jabo_group_raci: descarta msg_id fora do contexto: %s", msg_id)
            continue
        tid = p.get("item_id")
        if tid is not None:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                tid = None
            if tid is not None and tid not in ids_item:
                logger.info("jabo_group_raci: descarta item_id alucinado %s (msg %s)", tid, msg_id)
                tid = None
        novo = (p.get("novo_status") or "").strip().lower()
        if novo not in STATUS_VALIDOS:
            novo = None
        # Escrita que não muda nada ainda assim vira linha no livro-razão e
        # ruído no placar. Se o status proposto é o que já está lá, não é
        # atualização — é eco.
        if tid is not None and novo and atual.get(tid) == novo:
            novo = None
        try:
            conf = float(p.get("confianca") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        saida.append({"msg_id": msg_id, "task_id": tid, "novo_status": novo,
                      "confianca": conf, "corrige": bool(p.get("corrige")),
                      "motivo": (p.get("motivo") or "").strip()})
    return saida


def _uma_por_item(julgamentos: List[Dict]) -> Dict[int, Dict]:
    """Um item do RACI muda no máximo uma vez por rodada — pela mensagem MAIS
    RECENTE que fala dele.

    Medido no primeiro backfill (16/08): quatro mensagens da Andressa entre 04 e
    12/08 propuseram a MESMA mudança no item 10 ("envio de amostras"). Sem isto
    seriam quatro escritas no mesmo alvo — três delas gravando o valor que a
    primeira já colocou. É a cascata consertada hoje de manhã em
    `cos_agent/run.py`, reaparecendo em outro caminho no mesmo dia.

    ⚠️ MAS O CRITÉRIO DE DESEMPATE É OUTRO, E A DIFERENÇA IMPORTA. Lá vence a de
    maior CONFIANÇA, porque a ordem da lista era acidente de iteração — não
    significava nada. Aqui a ordem é CRONOLOGIA REAL: a mensagem de 12/08 sabe
    tudo o que a de 04/08 sabia, e mais. Aplicar a "mais confiante" poderia
    gravar o estado de uma semana atrás por cima do atual. Quando a ordem carrega
    informação, respeitá-la é o certo; quando não carrega, é o defeito.
    """
    por_item: Dict[int, Dict] = {}
    for j in julgamentos:                      # `julgamentos` já vem cronológico
        if j["task_id"] and j["novo_status"]:
            por_item[j["task_id"]] = j         # o último sobrescreve, de propósito
    return por_item


def _registrar(cur, msg_id: int, desfecho: str, *, task_id=None, write_id=None,
               confianca=None, motivo="", corrige=False) -> None:
    """Toda mensagem examinada entra — inclusive a que não virou nada.

    ON CONFLICT DO NOTHING porque a unicidade é a garantia de não reprocessar; se
    duas rodadas se cruzarem, a segunda não pode reaplicar a mesma mensagem.
    """
    cur.execute("""
        INSERT INTO jabo_group_reads
            (group_message_id, desfecho, task_id, agent_write_id, confianca, motivo, corrige)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (group_message_id) DO NOTHING
    """, (msg_id, desfecho, task_id, write_id, confianca, motivo[:500], corrige))


async def sincronizar(dry_run: bool = False, backfill: bool = False) -> Dict:
    """Lê o grupo, atualiza os itens do RACI do #28 e devolve o placar.

    `backfill=True` estende a janela até o último RACI que SAIU no grupo, em vez
    dos `JANELA_DIAS` correntes. É para a primeira execução: entre 03/08 e 16/08
    a Andressa reportou seis vezes e nada foi absorvido, e essa fila não se
    recupera com uma janela de 7 dias. Rode com `dry_run` antes — o backfill é a
    única execução que olha para trás, e é onde o erro sai mais caro.
    """
    placar = {"examinadas": 0, "aplicadas": 0, "duvidas": 0, "sem_relacao": 0,
              "suprimidas": 0, "correcoes": 0, "erros": 0, "detalhe": []}

    with get_db() as conn:
        cur = conn.cursor()
        jid = _group_jid(cur)
        if not jid:
            return {**placar, "erro": "grupo do #28 não vinculado"}
        desde = _desde_ultimo_raci(cur, jid) if backfill else None
        if backfill:
            placar["backfill_desde"] = desde.isoformat() if desde else None
        mensagens = _mensagens_novas(cur, jid, desde)
        if not mensagens:
            return placar
        if len(mensagens) > TETO_RODADA:
            # Corte declarado: silenciar o excedente faria a rodada parecer
            # completa ([[feedback_regua_cobertura_parcial]]). As demais ficam na
            # fila e entram na próxima.
            logger.info("jabo_group_raci: %s mensagens novas, teto %s — as demais ficam na fila",
                        len(mensagens), TETO_RODADA)
            mensagens = mensagens[:TETO_RODADA]
        tasks = _itens_abertos(cur)

    julgamentos = await _julgar(mensagens, tasks)
    por_msg = {j["msg_id"]: j for j in julgamentos}
    # Quem efetivamente escreve: uma mensagem por item, a mais recente. As outras
    # ainda são examinadas e registradas — elas sustentam a mesma conclusão, não
    # são ruído, e sumir com elas do registro estragaria o denominador.
    vencedoras = {j["msg_id"] for j in _uma_por_item(julgamentos).values()}

    with get_db() as conn:
        cur = conn.cursor()
        for m in mensagens:
            placar["examinadas"] += 1
            j = por_msg.get(m["id"])
            corrige = bool(j and j["corrige"])
            if corrige:
                placar["correcoes"] += 1

            # O modelo não achou item, não propôs mudança, ou outra mensagem mais
            # recente já cobre o mesmo item nesta rodada.
            if not j or not j["task_id"] or not j["novo_status"]:
                if not dry_run:
                    _registrar(cur, m["id"], "corrige" if corrige else "sem_relacao",
                               motivo=(j or {}).get("motivo", ""), corrige=corrige)
                placar["sem_relacao"] += 1
                continue
            if m["id"] not in vencedoras:
                placar["suprimidas"] = placar.get("suprimidas", 0) + 1
                if not dry_run:
                    _registrar(cur, m["id"], "suprimida", task_id=j["task_id"],
                               confianca=j["confianca"],
                               motivo=f"mesma conclusão de mensagem mais recente: {j['motivo']}",
                               corrige=corrige)
                continue

            dados = {"status": j["novo_status"]}
            if j["novo_status"] == "concluido":
                # A data é do runner, não do modelo: deixar o LLM inventar
                # timestamp é abrir mão do único campo que dá para conferir.
                dados["concluido_em"] = datetime.now(timezone.utc).replace(tzinfo=None)
                # A procedência viaja junto do fechamento. Sem ela, a executora
                # não distingue o que a máquina mudou do que sempre esteve ali —
                # e "manda uma msg para corrigir" viraria exigência de auditoria.
                dados["concluido_em_fonte"] = FONTE_PROCEDENCIA

            if dry_run:
                placar["detalhe"].append({"msg": m["id"], "item": j["task_id"],
                                          "status": j["novo_status"],
                                          "confianca": j["confianca"],
                                          "motivo": j["motivo"], "dry": True})
                continue

            try:
                wid = agent_write.escrever(
                    "atualizar_status_raci", dados,
                    motivo=f"{j['motivo']} (grupo Governança Jabô, msg #{m['id']})",
                    confianca=j["confianca"], agente=AGENTE,
                    fato_origem=f"group_message:{m['id']}",
                    registro_id=j["task_id"], conn=conn,
                )
                _registrar(cur, m["id"], "aplicada", task_id=j["task_id"], write_id=wid,
                           confianca=j["confianca"], motivo=j["motivo"], corrige=corrige)
                placar["aplicadas"] += 1
                placar["detalhe"].append({"msg": m["id"], "item": j["task_id"],
                                          "status": j["novo_status"], "write": wid})
            except agent_write.PropostaPendente as p:
                # Dúvida tem destino. Abster aqui devolveria o silêncio que este
                # caminho existe para acabar.
                conn.rollback()
                _registrar(cur, m["id"], "duvida", task_id=j["task_id"],
                           confianca=p.confianca, motivo=p.motivo, corrige=corrige)
                placar["duvidas"] += 1
            except Exception as e:                              # noqa: BLE001
                conn.rollback()
                logger.warning("jabo_group_raci: msg %s falhou: %s", m["id"], e)
                _registrar(cur, m["id"], "erro",
                           task_id=j["task_id"], motivo=f"{type(e).__name__}: {e}"[:300],
                           corrige=corrige)
                placar["erros"] += 1
        conn.commit()

    logger.info("jabo_group_raci: %s examinadas, %s aplicadas, %s dúvidas, %s correções",
                placar["examinadas"], placar["aplicadas"], placar["duvidas"],
                placar["correcoes"])
    return placar


def placar_correcoes(dias: int = 30) -> Dict:
    """A medição que decide se isto se estende a clientes.

    A pergunta não é "quantas escritas fizemos" — é **quantas vezes a executora
    precisou vir consertar**. Com 1 proposta aplicada em 91 no caminho antigo, a
    precisão real nunca foi medida: o gate impediu de aparecer. Aqui ela aparece.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT desfecho, count(*) AS n FROM jabo_group_reads
             WHERE examinada_em > now() - (%s || ' days')::interval
          GROUP BY desfecho
        """, (dias,))
        por_desfecho = {r["desfecho"]: r["n"] for r in cur.fetchall()}
        cur.execute("""
            SELECT count(*) AS n FROM jabo_group_reads
             WHERE corrige AND examinada_em > now() - (%s || ' days')::interval
        """, (dias,))
        correcoes = cur.fetchone()["n"]
        cur.execute("""
            SELECT count(*) AS n FROM agent_writes
             WHERE agente = %s AND desfeito_em IS NOT NULL
               AND criado_em > now() - (%s || ' days')::interval
        """, (AGENTE, dias))
        desfeitas = cur.fetchone()["n"]

    aplicadas = por_desfecho.get("aplicada", 0)
    return {
        "dias": dias, "por_desfecho": por_desfecho,
        "aplicadas": aplicadas, "correcoes": correcoes, "desfeitas": desfeitas,
        # Denominador explícito: taxa sobre escrita, não sobre mensagem lida.
        "taxa_correcao": round(correcoes / aplicadas, 3) if aplicadas else None,
    }
