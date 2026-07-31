"""
Camada de inteligência — loop de revisão por frente (CoS, FASE 0: só observa).

Origem: o Renato pediu (24/07) "uma camada rodando o tempo todo, verificando
email/WA, sabendo como dar andamento em cada frente, e só me acionando quando
necessário, segundo as políticas aprovadas". A fase 0 (ratificada por ele) é
READ-ONLY: a camada LÊ e DIZ — não toca em nada — pra ele julgar a qualidade do
julgamento antes de ganhar qualquer poder de agir.

O que faz: pra cada frente ativa com grupo/canal (project_whatsapp_groups ativo),
reúne o estado real (projeto + tasks + membros + DMs + mensagens de grupo + notas)
e produz um DEBRIEFING por frente:
  estado · movimento recente · trava · precisa_de_voce (só portão genuíno) ·
  vigílias · nota honesta (drift / task alarmista / spam a ignorar).
O placar ("de N frentes, X precisam de você") é derivado DETERMINÍSTICO das
frentes (sem 2ª chamada LLM) — mais barato e confiável.

Espelha o gather de `project_smart_update.analyze_project_updates` (mesmas queries,
read-only) + o padrão de custo `llm_usage.record_response`. NÃO executa ação
(sem create_task/draft/send) — a promoção pra Auto/Propor é decisão futura do
Renato, sob [[feedback_cos_autonomy_policy]].
"""
import os
import re
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from database import get_db
from services import llm, llm_usage
from services.group_message_sync import get_group_messages
from services.tz import now_utc

logger = logging.getLogger(__name__)

# Janelas de leitura
_GROUP_DAYS = 12
_DM_DAYS = 21
_NOTES_LIMIT = 3
_MEM_LIMIT = 5        # memórias duráveis (boards/memos) recuperadas por frente
_ESTADO_TIPO = "estado_cos"  # #4: nota de estado durável, UMA por projeto (UPSERT)

# Quanto de cada memória chega ao prompt. Era 600, e medido em 28/07 isso
# entregava 20% de um memo típico (2-10k chars) — o fato relevante podia estar
# depois do corte e simplesmente não existir pra camada. 2500 entrega inteiros
# os memos de até ~2,5k (a maioria) sem estourar contexto; com os documentos de
# PROCESSO fora da busca (ver _MEMORY_TITLE_DENYLIST) o custo por frente cai
# mesmo subindo o corte.
_MEM_CHARS = 2500
# 2500, era 500. Medido em 30/07: das 58 notas que a camada de fato lê (3 mais
# recentes × frentes ativas), **42 eram cortadas em 500 — 72%**. Não é borda, é
# a regra. O caso que provou: a nota #123 da FAAP tem 708 chars e a data de
# início das aulas ("17/08") ficava 40 caracteres DEPOIS do corte; a camada
# reportou honestamente "dado incompleto, não posso afirmar" sobre uma
# informação que estava no mesmo campo, quatro palavras adiante.
# Com 2500 sobram 12 cortadas (as notas-documento, de 4k a 19k chars — essas
# são longas por natureza e truncar é correto). Alinhado com _MEM_CHARS pelo
# mesmo raciocínio de 28/07. Custo medido: ~1,5k tokens a mais por frente,
# ~US$0,13/dia no lote de 29 — o preço de a camada parar de decidir sobre 28%
# de uma nota.
_NOTE_CHARS = 2500

# Documentos de PROCESSO da sessão, não fato sobre nenhuma frente. Eles casam
# com qualquer termo (são o log de tudo) e por isso apareciam em TODA frente:
# medido em 28/07, o `session_locks` ocupava 1 dos 5 slots de memória das
# frentes #47, #24 e #28 — servindo 600 de 167.503 chars, 0,36% de si mesmo. O
# board do CoS ocupava um segundo slot na #47. Dois quintos da memória da
# frente gastos com documento de processo entregue a 0,4%.
#
# Tirá-los NÃO perde fato: o que eles têm de durável já vive nos memos
# temáticos (é de lá que o /fim os monta). Casa por prefixo do título, em
# minúsculas.
_MEMORY_TITLE_DENYLIST = (
    "session-locks",
    "cos status board",
    "dev backlog",
)


def _is_process_doc(titulo: Optional[str]) -> bool:
    """Documento de processo da sessão (board/lock), não fato de frente."""
    t = (titulo or "").strip().lower()
    return any(t.startswith(p) for p in _MEMORY_TITLE_DENYLIST)
_OUTBOUND_DAYS = 10   # janela do "o que o Renato JÁ fez" (outbound email+WA)
_OUTBOUND_LIMIT = 12  # cap de ações outbound recentes surfaçadas por frente


def _outbound_awareness_on() -> bool:
    """Kill-switch da consciência de outbound (fix action-blindness 25/07).
    Muda comportamento em prod (rebaixa precisa_de_voce quando o Renato já agiu),
    então é reversível por env: COS_OUTBOUND_AWARENESS=0 volta ao comportamento
    anterior (sem bloco de outbound, sem regra no prompt). Default = ligado."""
    return os.getenv("COS_OUTBOUND_AWARENESS", "1").strip().lower() not in ("0", "false", "off", "no")


# Regra injetada no system prompt SÓ quando a consciência de outbound está ligada.
_OUTBOUND_RULE = """

REGRA DE OUTBOUND (o Renato já agiu?): ANTES de marcar precisa_de_voce, confira o bloco "AÇÕES RECENTES DO RENATO" e as mensagens de grupo com remetente RENATO. Se ele JÁ EXECUTOU a ação que você ia pedir (enviou o e-mail, mandou a mensagem, comunicou a decisão), a expectativa JÁ FOI CUMPRIDA — NÃO marque precisa_de_voce por ela; na nota, registre que a ação já foi feita, com data e evidência (ex.: "e-mail aos 4 enviado 24/07"). Um ANÚNCIO de intenção ("vou mandar em breve") NÃO conta como feito — mas o e-mail/mensagem efetivamente ENVIADO que aparece no bloco de outbound SIM conta. Nunca re-cobre o que ele já fez."""

_SYSTEM = """Você é a camada de inteligência do Renato — o Chief of Staff digital dele, rodando sozinho.

Sua tarefa NESTA fase é SÓ LER E DIZER. Você NÃO age (não cria task, não rascunha, não envia). Você lê o estado real de UMA frente e devolve um debriefing curto e afiado, na altitude de um chief of staff — não um relatório, um julgamento.

O usuário do sistema é RENATO. Em mensagem de grupo/DM, "Renato"/outgoing = ele.

Para a frente, decida:
- **estado**: onde a frente está, em 1-2 frases. O essencial, não a história.
- **movimento**: o que andou de concreto nos últimos dias (quem fez o quê). Vazio se nada andou.
- **trava**: o que está segurando o avanço agora (1 frase). Vazio se nada trava.
- **precisa_de_voce**: SÓ marque sim se for um PORTÃO GENUÍNO — uma decisão/ação que SÓ o Renato pode fazer (autoridade/relação/gosto/info que só ele tem) E que é irreversível ou sensível ao tempo agora. Logística, status, "aguardar terceiro", próximo-passo-óbvio NÃO são portão. Na dúvida, é NÃO. Se sim, diga em 1 frase O QUE ele precisa fazer e por quê.
- **vigilias**: o que VOCÊ ficaria de olho (retorno de terceiro esperado, prazo se aproximando, execução a confirmar) — coisas que não pedem decisão dele agora, mas você acompanharia. Máx 2.
- **nota**: honestidade — se uma task está alarmista/desatualizada, se há spam/ruído a ignorar, se algo está driftando. Vazio se nada.

Regras duras:
- **PARTICIPANTE COMPARTILHADO — não atribua a esta frente o assunto de outra.** Quem aparece marcado com "⚠️ TAMBÉM participa de" conversa com o Renato sobre VÁRIOS assuntos, e a DM dele chega aqui inteira. Uma mensagem dessa pessoa só é evidência DESTA frente se ela **menciona o objeto desta frente**. Se falar de dinheiro, prazo ou entrega de outro tema, **ignore e diga na `nota` que ignorou** — nunca registre como movimento daqui. Na dúvida sobre a qual frente uma mensagem pertence, ela NÃO é movimento desta.
- **TAREFA PARQUEADA NÃO É PORTÃO.** Uma task marcada `[PARQUEADA]` foi tirada do radar deliberadamente pelo Renato (ou está na janela normal de espera por um terceiro). Ela **não conta como atrasada**, **não vira `precisa_de_voce`** e **não se cobra** — mesmo que a data de vencimento já tenha passado, que é o normal nesse estado. Só `[ATRASADA]` é atraso de verdade. Se o parqueio parecer errado (o terceiro já respondeu, o motivo caiu), diga isso na `nota` em vez de abrir portão.
- **HIERARQUIA DAS FONTES.** Se o bloco "MEMÓRIA / DECISÕES REGISTRADAS" contradisser qualquer outra coisa (nota da frente, task, mensagem), **a MEMÓRIA vence** — ela é o registro durável, revisado e corrigido pelo Renato; nota e task são o rascunho do dia e envelhecem sem que ninguém volte pra consertar. Quando houver contradição, use o que diz a MEMÓRIA **e registre a divergência no campo `nota`**, nomeando o que contradiz (ex.: "a nota de 27/07 trata o Orestes como sócio; o memo diz que ele só financia — nota desatualizada").
- Prioridade no INTEL: número MAIOR = mais importante (8-10 gate estratégico; 1-3 baixa).
- Cite evidência ao afirmar (ID de task, quem disse no grupo). Nunca invente.
- DATAS E VALORES: copie EXATO da fonte, nunca parafraseie nem aproxime (se a nota diz "17/08", escreva 17/08 — não "início de agosto" nem "01/08").
- Ignore spam/ruído (ex: "aumente seu limite de crédito", robô) — e diga na nota que ignorou.
- Português correto, com acento. Tom: informal-com-gravidade, direto, sem preâmbulo, sem emoji.

Retorne APENAS JSON válido (sem markdown):
{
  "estado": "...",
  "movimento": "...",
  "trava": "...",
  "precisa_de_voce": {"sim": false, "o_que": ""},
  "vigilias": ["..."],
  "nota": "..."
}"""


def _target_frentes(cursor) -> List[Dict[str, Any]]:
    """TODOS os projetos ativos (o Renato ampliou 24/07 de 'grupo/canal' pra todos).
    Frentes sem nenhum sinal (task/membro/grupo) sao puladas em run_daily_review."""
    cursor.execute("""
        SELECT p.id, p.nome, p.descricao, p.prioridade
        FROM projects p
        WHERE p.status = 'ativo'
        ORDER BY p.prioridade DESC NULLS LAST, p.id
    """)
    return [dict(r) for r in cursor.fetchall()]


def _gather_memories(project_name: str, description: Optional[str],
                     member_names: List[str]) -> List[Dict[str, Any]]:
    """#1 — recupera memórias DURÁVEIS relevantes à frente do store embeddado
    (`system_memories`, o espelho dos boards/memos .md do Claude Code). Sem isso
    a camada é CEGA: um fato registrado num board nunca chega ao debriefing.

    ESCOLHA keyword (não semântica) DE PROPÓSITO: (a) a identidade de uma frente é
    proper-noun — nome do projeto, empresa, membros; keyword casa exato e barato;
    (b) `search_memories(mode='hybrid'/'semantic')` dispara 1 embedding Voyage por
    query, e o batch diário (~18 frentes × vários termos, com Semaphore(6)) estouraria
    o rate limit do Voyage (free = 3 RPM). Keyword é determinístico e sem rede.
    Se um dia migrar pra semântica, fazer 1 query combinada por frente + throttle.

    Busca por termo distintivo (nome do projeto + tokens do nome + nomes dos membros),
    merge dedupe por id, mantendo ordem de descoberta (nome do projeto primeiro).
    Read-only. Falha graciosa = lista vazia (nunca quebra o gather)."""
    from services.system_memory import search_memories

    terms: List[str] = []
    seen_terms = set()

    def _add_term(t: Optional[str]) -> None:
        t = (t or "").strip()
        if len(t) >= 4 and t.lower() not in seen_terms:
            terms.append(t)
            seen_terms.add(t.lower())

    _add_term(project_name)
    for nm in member_names:
        _add_term(nm)
    # tokens distintivos do nome do projeto (empresa/proj), ignorando emoji/separadores
    for tok in re.split(r"[\s—\-–|·:,/]+", project_name or ""):
        tok = tok.strip()
        if tok and tok[:1].isalpha() and len(tok) >= 5:
            _add_term(tok)

    out: List[Dict[str, Any]] = []
    seen_ids = set()
    for term in terms[:8]:  # teto de termos por frente
        try:
            hits = search_memories(term, limit=3, mode="keyword")
        except Exception as e:
            logger.warning("frente_review: memoria skip (%s)", e)
            break
        for m in hits:
            mid = m.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            # Board/lock casa com qualquer termo (é o log de tudo) e ocupava
            # slot em TODA frente entregando 0,4% de si mesmo. Marcar como
            # visto e pular: assim ele não volta pelos termos seguintes.
            if _is_process_doc(m.get("titulo")):
                continue
            out.append(m)
            if len(out) >= _MEM_LIMIT:
                return out
    return out


def _gather_renato_outbound(cursor, project_id: int, member_ids: List[int],
                            owner_ids: List[int]) -> List[Dict[str, Any]]:
    """AÇÕES do Renato: outbound (email+WA) que ELE já enviou nesta frente, por
    MEMBERSHIP (DM a membro) OU por LINK do roteador (não-membro). Recency-first,
    cap pequeno. Fecha a cegueira de outbound: a prova do que ele JÁ fez chega
    SEMPRE ao debriefing, num bloco próprio — independente do LIMIT do window de
    DM (que truncava o e-mail recém-enviado) e independente do roteador ter ligado
    a mensagem. Read-only; falha graciosa = lista vazia (nunca quebra o gather).

    ⚠️ O SELF-CHAT FICA DE FORA — e isso é o conserto de um CIRCUITO FECHADO.
    Tudo que a máquina manda pro Renato (ponte cruzada, "URGENTE: reunião em
    30min", update de RACI, digest de grupos, spike de custo) é gravado como
    `direcao='outgoing'` na conversa dele com ele mesmo. Medido em 30/07: 395
    dessas em 30 dias, e o roteador tinha ligado **31 a 10 frentes** (8 na
    Vallen, 7 na Alba, 7 na Reorg). Elas chegavam aqui sob o título "AÇÕES
    RECENTES DO RENATO — o que ELE JÁ enviou", e a REGRA DE OUTBOUND manda não
    cobrar o que ele já fez. Ou seja: o aviso da máquina virava prova de que o
    Renato agiu, e **podia derrubar um portão legítimo no dia seguinte**. Falha
    silenciosa — não gera ruído, gera omissão, que é o que ninguém vê.

    Mandar mensagem PRA SI MESMO nunca é execução de ação numa frente (não há
    terceiro do outro lado), então excluir é correto por semântica, não só por
    higiene. As fichas do dono vêm de `contact_identity.owner_contact_ids` —
    LISTA, não id: a 1ª versão deste fix usou `users.id=1.contact_id` e ficaria
    INERTE, porque esse aponta pra #14911 (1 conversa) e o self-chat está na
    #23419 (18). `owner_ids` vazio ⇒ não filtra nada (nunca filtra tudo)."""
    rows: List[Dict[str, Any]] = []
    try:
        cursor.execute("""
            SELECT DISTINCT m.id, cv.canal, COALESCE(m.enviado_em, m.recebido_em) AS ts,
                   c.nome AS para, LEFT(m.conteudo, 500) AS conteudo
            FROM messages m
            JOIN conversations cv ON cv.id = m.conversation_id
            JOIN contacts c ON c.id = cv.contact_id
            LEFT JOIN message_project_links l
                   ON l.message_id = m.id AND l.project_id = %s
            WHERE m.direcao = 'outgoing'
              AND cv.canal IN ('email', 'whatsapp')
              AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' days')::interval
              AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 10
              AND (cv.contact_id = ANY(%s) OR l.project_id = %s)
              AND NOT (cv.contact_id = ANY(%s))
              -- mesmo filtro anti-vazamento do bloco de DMs: outbound que o
              -- roteador atribuiu EXCLUSIVAMENTE a outra frente não é prova de
              -- ação NESTA. Sem isto, "já mandei o e-mail" de uma frente fecha
              -- o portão de outra — e este bloco é justamente o que a REGRA DE
              -- OUTBOUND usa pra decidir não cobrar.
              AND (
                    EXISTS (SELECT 1 FROM message_project_links le
                             WHERE le.message_id = m.id AND le.project_id = %s)
                 OR NOT EXISTS (SELECT 1 FROM message_project_links lo
                                 WHERE lo.message_id = m.id AND lo.project_id > 0)
              )
            ORDER BY ts DESC
            LIMIT %s
        """, (project_id, _OUTBOUND_DAYS, member_ids or [0], project_id,
              owner_ids or [0], project_id, _OUTBOUND_LIMIT))
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        # message_project_links (053) pode não existir — degrada gracioso.
        logger.warning("frente_review: renato_outbound skip (%s)", e)
        cursor.connection.rollback()
    return rows


def _gather_frente(cursor, project_id: int,
                   owner_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Reúne o estado real de uma frente (read-only). Espelha o smart_update.

    `owner_ids` = fichas do próprio Renato, pra excluir o self-chat das três
    janelas de mensagem. Injetável pra o batch resolver UMA vez em vez de 29."""
    if owner_ids is None:
        from services.contact_identity import owner_contact_ids
        owner_ids = owner_contact_ids(cursor)
    cursor.execute("SELECT id, nome, descricao FROM projects WHERE id = %s", (project_id,))
    project = cursor.fetchone()
    if not project:
        return {}
    project = dict(project)

    # `on_hold_since`/`on_hold_reason` (migration 058) entram no SELECT porque o
    # prompt PRECISA distinguir atrasada de PARQUEADA — ver _task_marker.
    cursor.execute("""
        SELECT t.id, t.titulo, t.descricao, t.status, t.data_vencimento, t.prioridade,
               t.on_hold_since, t.on_hold_reason,
               c.nome AS responsavel
        FROM tasks t
        LEFT JOIN contacts c ON c.id = t.contact_id
        WHERE t.project_id = %s AND t.status NOT IN ('completed', 'cancelled')
        ORDER BY t.data_vencimento ASC NULLS LAST
    """, (project_id,))
    tasks = [dict(r) for r in cursor.fetchall()]

    # `outras_frentes` existe pra o prompt AVISAR que o participante é compartilhado.
    # Sem isso o modelo adivinha: em 30/07 ele separou certo na #20 ("as conversas
    # Renato–Orestes são sobre Phisalia, não têm relação com esta frente") e errou
    # na #38, com o MESMO contato. Não é falta de capacidade, é falta de rótulo.
    cursor.execute("""
        SELECT pm.contact_id, c.nome, pm.papel,
               COALESCE((
                   SELECT string_agg(p2.nome, ' · ' ORDER BY p2.nome)
                   FROM project_members pm2
                   JOIN projects p2 ON p2.id = pm2.project_id
                   WHERE pm2.contact_id = pm.contact_id
                     AND pm2.project_id <> %s
                     AND p2.status = 'ativo'
               ), '') AS outras_frentes
        FROM project_members pm JOIN contacts c ON c.id = pm.contact_id
        WHERE pm.project_id = %s
    """, (project_id, project_id))
    members = [dict(r) for r in cursor.fetchall()]

    # DMs recentes dos membros (fonte de sinal das frentes SEM grupo). Espelha
    # o smart_update: agrupa por contato, janela _DM_DAYS, corta mensagem curta.
    #
    # ⚠️ FILTRO ANTI-VAZAMENTO ENTRE FRENTES (30/07). Puxar TODA a conversa de
    # cada membro faz quem participa de várias frentes carregar tudo o que disse
    # para todas elas. Provado no mesmo dia: a nota que a camada escreveu no
    # projeto #38 (FAAP) registrava como movimento da FAAP o pagamento de R$5.000
    # do título do Paulistano do FILHO (projeto #49) — porque o Orestes é membro
    # dos dois. Alcance medido: 15 das 31 frentes ativas têm membro compartilhado.
    #
    # O conserto NÃO adivinha: usa o rótulo que o roteador JÁ produziu. Duas das
    # quatro mensagens vazadas (25826, 25827 — as que carregam "título do DAP" e
    # os dados bancários) já estavam ligadas ao #49 em `message_project_links`, e
    # a camada as exibiu no #38 mesmo assim, sem nunca consultar a etiqueta.
    #
    # Regra: mensagem entra se está ligada A ESTA frente, OU se não está ligada a
    # frente nenhuma. Sai só quando o roteador a atribuiu EXCLUSIVAMENTE a outra —
    # aí não é palpite meu, é uma decisão que já foi tomada e conferida por LLM.
    # Mensagem sem link segue entrando (a maioria não tem link); para essas, quem
    # protege é o aviso de participante compartilhado no prompt.
    dms: List[Dict[str, Any]] = []
    member_ids = [m["contact_id"] for m in members if m.get("contact_id")]
    if member_ids:
        # RECÊNCIA-FIRST: ordena por ts DESC antes do LIMIT. O ORDER BY ts ASC
        # antigo mantinha os 40 MAIS ANTIGOS e truncava as mensagens recentes —
        # foi o que escondeu o e-mail que o Renato JÁ tinha enviado (Luminosità,
        # 25/07: 48 msgs no window, o outbound recém-enviado caía fora do LIMIT).
        # O _fmt_gather re-ordena cronológico por contato na hora de exibir.
        cursor.execute("""
            SELECT m.id, m.conteudo, m.direcao, COALESCE(m.enviado_em, m.recebido_em) AS ts,
                   c.nome AS contact_nome
            FROM messages m
            JOIN conversations cv ON cv.id = m.conversation_id
            JOIN contacts c ON c.id = cv.contact_id
            WHERE cv.contact_id = ANY(%s)
              AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' days')::interval
              AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 10
              AND NOT (cv.contact_id = ANY(%s))
              AND (
                    EXISTS (SELECT 1 FROM message_project_links le
                             WHERE le.message_id = m.id AND le.project_id = %s)
                 OR NOT EXISTS (SELECT 1 FROM message_project_links lo
                                 WHERE lo.message_id = m.id AND lo.project_id > 0)
              )
            ORDER BY COALESCE(m.enviado_em, m.recebido_em) DESC
            LIMIT 40
        """, (member_ids, _DM_DAYS, owner_ids or [0], project_id))
        dms = [dict(r) for r in cursor.fetchall()]

    # Mensagens ROTEADAS por conteúdo (roteador B) — de QUALQUER um, membro ou não.
    # Fecha o furo do gather-por-membership. Exclui as já cobertas pelo DM dos membros.
    dm_ids = {m.get("id") for m in dms if m.get("id")}
    routed: List[Dict[str, Any]] = []
    try:
        cursor.execute("""
            SELECT m.id, cv.canal, m.direcao, COALESCE(m.enviado_em, m.recebido_em) AS ts,
                   c.nome AS sender, LEFT(m.conteudo, 400) AS conteudo
            FROM message_project_links l
            JOIN messages m ON m.id = l.message_id
            JOIN conversations cv ON cv.id = m.conversation_id
            JOIN contacts c ON c.id = cv.contact_id
            WHERE l.project_id = %s
              AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' days')::interval
              AND NOT (cv.contact_id = ANY(%s))
            ORDER BY ts DESC LIMIT 25
        """, (project_id, _DM_DAYS, owner_ids or [0]))
        routed = [dict(r) for r in cursor.fetchall() if dict(r)["id"] not in dm_ids]
    except Exception as e:
        # tabela do roteador (053) pode não existir ainda — degrada gracioso
        logger.warning("frente_review: routed skip (%s)", e)
        cursor.connection.rollback()

    # Notas recentes (memória curta da frente). Exclui a própria nota de estado
    # (tipo=estado_cos, escrita pelo #4) — senão a nota-máquina evictaria as notas
    # humanas do window LIMIT N e a camada só leria eco de si mesma.
    cursor.execute("""
        SELECT titulo, conteudo, criado_em FROM project_notes
        WHERE project_id = %s AND tipo <> %s
        ORDER BY criado_em DESC LIMIT %s
    """, (project_id, _ESTADO_TIPO, _NOTES_LIMIT))
    notes = [dict(r) for r in cursor.fetchall()]

    # Grupos vinculados
    cursor.execute("""
        SELECT group_jid, group_name FROM project_whatsapp_groups
        WHERE project_id = %s AND ativo = TRUE
    """, (project_id,))
    groups = [dict(r) for r in cursor.fetchall()]

    # #1 — memórias duráveis (boards/memos .md espelhados em system_memories).
    # Abre conexão própria (search_memories usa get_db) — read-only, nested OK.
    member_names = [m["nome"] for m in members if m.get("nome")]
    memories = _gather_memories(project["nome"], project.get("descricao"), member_names)

    # Ações outbound do Renato (o que ELE já fez) — só quando a consciência de
    # outbound está ligada (kill-switch). Reversível: off = gather sem o bloco.
    renato_outbound = (_gather_renato_outbound(cursor, project_id, member_ids, owner_ids)
                       if _outbound_awareness_on() else [])

    return {"project": project, "tasks": tasks, "members": members, "dms": dms,
            "routed": routed, "notes": notes, "groups": groups, "memories": memories,
            "renato_outbound": renato_outbound}


def _has_signal(g: Dict[str, Any]) -> bool:
    """Frente tem algo pra raciocinar? (evita gastar LLM em casca vazia)."""
    return bool(g.get("tasks") or g.get("dms") or g.get("routed") or g.get("groups") or g.get("notes"))


def _task_marker(t: Dict[str, Any], today: str) -> str:
    """Rótulo de estado da task no prompt.

    Até 30/07 a ÚNICA informação de estado que chegava ao modelo era
    `[ATRASADA]`, derivada só da data — e o filtro do gather deixa `on_hold`
    passar. Consequência medida em prod: **12 das 16 tasks apresentadas como
    atrasadas eram exatamente as que a CoS tinha PARQUEADO**, com janela de
    espera correndo. O modelo lia "atrasada há 8 dias", aplicava a regra dos 7
    dias da memória e mandava cobrar — desfazendo o parqueio todo dia. Era a
    raiz do portão do Filito de 30/07.

    A janela vem de `task_reconciler.ON_HOLD_WAIT_DAYS` de propósito: a regra
    dos 7 dias existe UMA vez no código e este módulo a LÊ, nunca a recopia.
    Espelha `_wait_deadline` (GREATEST(vencimento, parqueio) + janela), que é
    o que o `sweep_on_hold` de fato usa pra reabrir."""
    status = (t.get("status") or "").strip()

    if status == "on_hold":
        from services.task_reconciler import ON_HOLD_WAIT_DAYS, REASON_INDEFINIDO
        if (t.get("on_hold_reason") or "") == REASON_INDEFINIDO:
            return " [PARQUEADA — o Renato tirou do radar; NÃO cobrar, NÃO é portão]"
        base = t.get("on_hold_since")
        dv = t.get("data_vencimento")
        if base and dv and dv > base:
            base = dv
        if base:
            volta = (base + timedelta(days=ON_HOLD_WAIT_DAYS)).strftime("%d/%m")
            return (f" [PARQUEADA aguardando terceiro até {volta} — dentro da janela "
                    f"de espera; NÃO cobrar, NÃO é portão]")
        return " [PARQUEADA aguardando terceiro — NÃO cobrar, NÃO é portão]"

    if status == "in_progress":
        return " [EM ANDAMENTO]"

    if t.get("data_vencimento") and str(t["data_vencimento"])[:10] < today:
        return " [ATRASADA]"
    return ""


def _fmt_gather(g: Dict[str, Any]) -> str:
    """Serializa o gather num texto compacto pro prompt."""
    p = g["project"]
    today = date.today().isoformat()
    parts = [f"FRENTE: {p['nome']}", f"DESCRIÇÃO: {(p.get('descricao') or '')[:400]}", f"HOJE: {today}"]

    if g["members"]:
        # Participante compartilhado vem MARCADO. A DM dele traz assunto de todas
        # as frentes em que ele está, e sem o aviso o modelo atribui a esta.
        linhas = []
        for m in g["members"]:
            base = f"{m['nome']} ({m.get('papel') or 'membro'})"
            outras = (m.get("outras_frentes") or "").strip()
            if outras:
                base += f"  ⚠️ TAMBÉM participa de: {outras}"
            linhas.append(base)
        parts.append("PARTICIPANTES:\n- " + "\n- ".join(linhas))

    if g["tasks"]:
        parts.append("\nTAREFAS ABERTAS:")
        for t in g["tasks"]:
            venc = str(t["data_vencimento"])[:10] if t.get("data_vencimento") else "sem prazo"
            marca = _task_marker(t, today)
            resp = f" resp:{t['responsavel']}" if t.get("responsavel") else ""
            desc = f" — {t['descricao'][:220]}" if t.get("descricao") else ""
            parts.append(f"- [#{t['id']} prio{t.get('prioridade')}]{marca} {t['titulo']} (vence {venc}{resp}){desc}")

    # #1 — decisões/fatos registrados nos memos duráveis. Vem ANTES das notas
    # de propósito: é a fonte de MAIOR autoridade (registro revisado e corrigido
    # pelo Renato) e a hierarquia está declarada no _SYSTEM. Até 28/07 esta
    # ordem era a inversa e a hierarquia existia só num comentário Python — que
    # o modelo nunca lê. Numa contradição, o que decidia era a saliência: a nota
    # vinha primeiro e era mais recente, então a fonte mais frágil ganhava.
    if g.get("memories"):
        parts.append("\nMEMÓRIA / DECISÕES REGISTRADAS (FONTE DE MAIOR AUTORIDADE — "
                     "prevalece sobre notas e tasks em caso de contradição):")
        for m in g["memories"]:
            dt = str(m.get("criado_em") or "?")[:10]
            corpo = m.get("conteudo") or ""
            corte = "" if len(corpo) <= _MEM_CHARS else "\n[…truncado]"
            parts.append(f"--- {dt} · {m.get('titulo') or ''} ---\n{corpo[:_MEM_CHARS]}{corte}")

    if g["notes"]:
        parts.append("\nNOTAS RECENTES DA FRENTE (rascunho do dia — envelhece; "
                     "perde pra MEMÓRIA quando as duas discordam):")
        for n in reversed(g["notes"]):
            dt = str(n.get("criado_em") or "?")[:10]
            corpo = n.get("conteudo") or ""
            corte = "" if len(corpo) <= _NOTE_CHARS else "\n[…truncado]"
            parts.append(f"--- {dt} · {n.get('titulo') or ''} ---\n{corpo[:_NOTE_CHARS]}{corte}")

    # AÇÕES RECENTES DO RENATO (outbound) — o que ELE já fez. Bloco próprio e no
    # topo pra o julgamento de precisa_de_voce não re-cobrar ação já executada.
    if g.get("renato_outbound"):
        parts.append("\n✅ AÇÕES RECENTES DO RENATO (outbound — e-mail/WA que ELE JÁ enviou nesta frente):")
        for m in g["renato_outbound"]:
            dt = str(m.get("ts") or "?")[:16]
            parts.append(f"[{dt} · {m.get('canal')} → {m.get('para') or '?'}] RENATO: {(m.get('conteudo') or '')[:450]}")

    # DMs dos membros (agrupadas por contato) — sinal das frentes sem grupo.
    # O fetch vem ts DESC (recência sobrevive ao LIMIT); re-ordena cronológico
    # por contato aqui e mostra as 12 MAIS RECENTES.
    if g.get("dms"):
        from collections import defaultdict
        convos = defaultdict(list)
        for m in g["dms"]:
            convos[m["contact_nome"]].append(m)
        for nome, msgs in convos.items():
            msgs = sorted(msgs, key=lambda x: str(x.get("ts") or ""))
            parts.append(f"\n--- DM com {nome} (recentes) ---")
            for m in msgs[-12:]:
                dt = str(m.get("ts") or "?")[:16]
                sender = "RENATO" if m.get("direcao") == "outgoing" else nome
                parts.append(f"[{dt}] {sender}: {(m.get('conteudo') or '')[:400]}")

    # Mensagens ROTEADAS por conteúdo (roteador B) — inclui NÃO-membros
    if g.get("routed"):
        parts.append("\n--- MENSAGENS LIGADAS À FRENTE POR CONTEÚDO (podem ser de não-membros) ---")
        for m in g["routed"]:
            dt = str(m.get("ts") or "?")[:16]
            who = "RENATO" if m.get("direcao") == "outgoing" else (m.get("sender") or "?")
            parts.append(f"[{dt} · {m.get('canal')}] {who}: {(m.get('conteudo') or '')[:400]}")

    # Mensagens de grupo (fonte principal do 'o que anda')
    for grp in g["groups"]:
        msgs = []
        try:
            msgs = get_group_messages(grp["group_jid"], limit=30) or []
        except Exception as e:
            logger.warning("frente_review: get_group_messages %s: %s", grp["group_jid"], e)
        if not msgs:
            continue
        parts.append(f"\n--- GRUPO WHATSAPP: {grp['group_name']} (recentes) ---")
        for m in msgs[-25:]:
            dt = str(m.get("timestamp") or m.get("created_at") or "?")[:16]
            sender = "RENATO" if m.get("from_me") else (m.get("sender_name") or "?")
            content = (m.get("content") or "")[:400]
            parts.append(f"[{dt}] {sender}: {content}")

    return "\n".join(parts)


async def review_frente(project_id: int, gather: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Debriefing de UMA frente (read-only, 1 chamada LLM). Falha graciosa."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"project_id": project_id, "error": "ANTHROPIC_API_KEY ausente"}

    if gather is None:
        with get_db() as conn:
            gather = _gather_frente(conn.cursor(), project_id)
    if not gather:
        return {"project_id": project_id, "error": "frente nao encontrada"}

    prompt = _fmt_gather(gather)
    system = _SYSTEM + (_OUTBOUND_RULE if _outbound_awareness_on() else "")
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": llm.BALANCED,
                    # 1400, não 900: com a regra de hierarquia de fontes o
                    # debriefing passou a incluir a divergência no campo `nota`
                    # quando acha uma, e 900 cortava o JSON no meio — a resposta
                    # inteira virava erro de parse e a frente saía sem
                    # debriefing. Custo de saída só é cobrado pelo que o modelo
                    # de fato escreve; o teto é limite, não consumo.
                    "max_tokens": 1400,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.error("frente_review LLM %s: %s", resp.status_code, resp.text[:200])
            return {"project_id": project_id, "error": f"LLM {resp.status_code}"}
        result = resp.json()
        llm_usage.record_response("cos.frente_review", llm.BALANCED, result)  # F-E custo por-função
        text = result.get("content", [{}])[0].get("text", "")
        start, end = text.find("{"), text.rfind("}") + 1
        if start < 0 or end <= start:
            return {"project_id": project_id, "error": "resposta nao-JSON", "raw": text[:200]}
        deb = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        logger.error("frente_review parse %s: %s", project_id, e)
        return {"project_id": project_id, "error": "JSON invalido"}
    except Exception as e:
        logger.error("frente_review %s: %s", project_id, e)
        return {"project_id": project_id, "error": str(e)}

    # normaliza + carimba metadados
    pdv = deb.get("precisa_de_voce") or {}
    return {
        "project_id": project_id,
        "frente": gather["project"]["nome"],
        "estado": (deb.get("estado") or "").strip(),
        "movimento": (deb.get("movimento") or "").strip(),
        "trava": (deb.get("trava") or "").strip(),
        "precisa_de_voce": {"sim": bool(pdv.get("sim")), "o_que": (pdv.get("o_que") or "").strip()},
        "vigilias": [v for v in (deb.get("vigilias") or []) if v][:2],
        "nota": (deb.get("nota") or "").strip(),
    }


def _fmt_estado_conteudo(d: Dict[str, Any]) -> str:
    """Corpo legível da nota de estado (o que chat/MCP/humano lê direto)."""
    pdv = d.get("precisa_de_voce") or {}
    lines: List[str] = []
    if d.get("estado"):
        lines.append(f"ESTADO: {d['estado']}")
    if d.get("movimento"):
        lines.append(f"MOVIMENTO: {d['movimento']}")
    if d.get("trava"):
        lines.append(f"TRAVA: {d['trava']}")
    lines.append(f"PRECISA DE VOCÊ: {pdv.get('o_que') or 'sim'}" if pdv.get("sim")
                 else "PRECISA DE VOCÊ: não")
    return "\n".join(lines) if lines else "(sem estado)"  # conteudo é NOT NULL


def _persist_estado_notes(debriefs: List[Dict[str, Any]]) -> int:
    """#4 — write-back estruturado. Depois de revisar cada frente, UPSERT numa
    ÚNICA nota de estado durável por projeto (tipo='estado_cos') capturando
    {estado, movimento, trava, precisa_de_voce}. UMA linha por projeto: DELETE+INSERT
    (substitui, NÃO append — sem poluir o histórico). Torna o estado da frente
    durável+CORRENTE — a camada/chat/MCP leem o estado atual sem re-derivar de janela
    velha. Nota = reversível = Auto-seguro por [[feedback_cos_autonomy_policy]].
    Idempotente por (project_id, tipo). Falha graciosa: não quebra o pipeline read-only.

    RealDictCursor: acesso por chave, nunca índice."""
    if not debriefs:
        return 0
    n = 0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            ts = now_utc()
            for d in debriefs:
                pid = d.get("project_id")
                if not pid:
                    continue
                pdv = d.get("precisa_de_voce") or {}
                meta = {
                    "estado": d.get("estado") or "",
                    "movimento": d.get("movimento") or "",
                    "trava": d.get("trava") or "",
                    "precisa_de_voce": {"sim": bool(pdv.get("sim")),
                                        "o_que": (pdv.get("o_que") or "")},
                    "run_at": ts.isoformat(),
                }
                # UPSERT sem constraint única: apaga a anterior, grava a atual.
                cur.execute(
                    "DELETE FROM project_notes WHERE project_id = %s AND tipo = %s",
                    (pid, _ESTADO_TIPO),
                )
                cur.execute(
                    """INSERT INTO project_notes
                           (project_id, tipo, titulo, conteudo, autor, metadata, criado_em, atualizado_em)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                    (pid, _ESTADO_TIPO, "Estado atual (camada CoS)",
                     _fmt_estado_conteudo(d), "cos_layer",
                     json.dumps(meta, ensure_ascii=False), ts, ts),
                )
                n += 1
            conn.commit()
    except Exception as e:
        logger.warning("frente_review: estado write-back falhou (%s)", e)
    return n


async def run_daily_review(limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Roda a camada sobre todas as frentes-alvo (grupo/canal), read-only.
    Retorna o payload do dia: frentes + placar derivado. NÃO persiste (o caller
    decide) — mas é o formato que a storage/cockpit consomem.
    """
    with get_db() as conn:
        cur = conn.cursor()
        frentes = _target_frentes(cur)
        if limit:
            frentes = frentes[:limit]
        # Resolve as fichas do dono UMA vez pro batch inteiro (eram 29 frentes
        # × 2 queries se ficasse dentro do _gather_frente).
        from services.contact_identity import owner_contact_ids
        owner_ids = owner_contact_ids(cur)
        gathers = {f["id"]: _gather_frente(cur, f["id"], owner_ids) for f in frentes}

    # Pula frentes sem nenhum sinal (casca vazia) — não gasta LLM.
    frentes = [f for f in frentes if _has_signal(gathers.get(f["id"], {}))]

    # Paraleliza as revisões (semáforo) — sequencial estourava 280s com ~18 frentes.
    import asyncio
    sem = asyncio.Semaphore(6)

    async def _one(f):
        async with sem:
            return await review_frente(f["id"], gather=gathers.get(f["id"]))

    results = await asyncio.gather(*[_one(f) for f in frentes], return_exceptions=True)

    debriefs: List[Dict[str, Any]] = []
    for f, r in zip(frentes, results):
        if isinstance(r, Exception) or (isinstance(r, dict) and r.get("error")):
            logger.warning("frente_review pulou #%s: %s", f["id"], r)
        else:
            debriefs.append(r)

    # #4 — write-back: grava o estado CORRENTE de cada frente numa nota durável
    # (UPSERT, 1 por projeto). Depois dos debriefs, best-effort — não bloqueia o payload.
    _persist_estado_notes(debriefs)

    precisa = [{"frente": d["frente"], "project_id": d["project_id"], "o_que": d["precisa_de_voce"]["o_que"]}
               for d in debriefs if d["precisa_de_voce"]["sim"]]
    vigilias = [{"frente": d["frente"], "project_id": d["project_id"], "item": v}
                for d in debriefs for v in d["vigilias"]]
    cobertas = [d["frente"] for d in debriefs if not d["precisa_de_voce"]["sim"]]

    # 2º corte cross-frente: dos N "precisa de você", quais ≤3 são de HOJE (portão
    # real). O resto continua visível como "esta semana" — vira lista, não portão.
    syn = await _synthesize_portao(precisa)
    hoje_ids = set(syn["hoje_ids"])
    hoje = [{**p, "porque": syn["porques"].get(p["project_id"], "")}
            for p in precisa if p["project_id"] in hoje_ids]
    esta_semana = [p for p in precisa if p["project_id"] not in hoje_ids]

    return {
        "run_at": now_utc().isoformat(),
        "n_frentes": len(debriefs),
        "frentes": debriefs,
        "placar": {"hoje": hoje, "esta_semana": esta_semana,
                   "precisa_de_voce": precisa,  # compat
                   "vigilias": vigilias, "cobertas": cobertas},
    }


async def _synthesize_portao(precisa: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Dos itens 'precisa de você' de TODAS as frentes, escolhe os <=3 que
    GENUINAMENTE precisam do Renato HOJE (prazo hoje/vencido, irreversível, janela
    fecha hoje). O resto espera. 1 chamada LLM; falha graciosa = top-3 crus."""
    import os
    if len(precisa) <= 3:
        return {"hoje_ids": [p["project_id"] for p in precisa], "porques": {}}
    api_key = os.getenv("ANTHROPIC_API_KEY")
    fallback = {"hoje_ids": [p["project_id"] for p in precisa[:3]], "porques": {}}
    if not api_key:
        return fallback
    lst = "\n".join(f"[{p['project_id']}] {p['frente']}: {p['o_que']}" for p in precisa)
    prompt = (
        f"Hoje é {date.today().isoformat()}. Cada item abaixo é algo que uma frente diz precisar de "
        "uma decisão ou ação do Renato. Mas nem tudo é pra HOJE.\n\n"
        "Escolha no MÁXIMO 3 que GENUINAMENTE precisam dele HOJE — prazo hoje/vencido, irreversível, "
        "ou a janela fecha hoje. Logística e 'pode esperar' ficam de fora. Na dúvida, deixa fora.\n\n"
        f"{lst}\n\n"
        "Retorne SÓ os IDS, ordenados por urgência (o mais urgente primeiro), no máximo 3. "
        "JSON: {\"hoje\": [42, 26, 47]}"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": llm.BALANCED, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if resp.status_code != 200:
            return fallback
        result = resp.json()
        llm_usage.record_response("cos.portao_synthesis", llm.BALANCED, result)
        text = result.get("content", [{}])[0].get("text", "")
        s, e = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[s:e])
        valid = {p["project_id"] for p in precisa}
        # aceita [42, 26] OU (defensivo) [{"project_id":42}, ...] — só ids, sem
        # porque re-gerado pelo LLM (era a fonte da troca de fios #42↔#41).
        raw = data.get("hoje") or []
        hoje_ids = []
        for h in raw:
            pid = h.get("project_id") if isinstance(h, dict) else h
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if pid in valid and pid not in hoje_ids:
                hoje_ids.append(pid)
        return {"hoje_ids": hoje_ids[:3], "porques": {}}
    except Exception as ex:
        logger.warning("synthesize_portao: %s", ex)
        return fallback


def persist_review(payload: Dict[str, Any]) -> int:
    """Grava o payload do dia em cos_daily_review. Retorna o id."""
    from services.tz import to_brt
    run_date = to_brt(now_utc()).date()
    n_frentes = payload.get("n_frentes", 0)
    n_precisa = len((payload.get("placar") or {}).get("precisa_de_voce") or [])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO cos_daily_review (run_date, n_frentes, n_precisa, payload)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (run_date, n_frentes, n_precisa, json.dumps(payload, ensure_ascii=False)),
        )
        rid = cur.fetchone()["id"]  # RealDictCursor: acesso por chave, não índice
        conn.commit()
    return rid


def latest_review() -> Optional[Dict[str, Any]]:
    """Último debriefing gravado (o que o cockpit lê). None se ainda não rodou."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM cos_daily_review ORDER BY run_at DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return None
    payload = row["payload"]  # RealDictCursor: por chave
    return payload if isinstance(payload, dict) else json.loads(payload)


async def run_and_persist() -> Dict[str, Any]:
    """Roda o loop completo e persiste. Chamado pelo cron diário."""
    payload = await run_daily_review()
    rid = persist_review(payload)
    return {"id": rid, "n_frentes": payload["n_frentes"],
            "n_precisa": len(payload["placar"]["precisa_de_voce"])}
