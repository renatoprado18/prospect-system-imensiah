"""
Task Reconciler — sweep periódico que FECHA tasks pending já resolvidas por
comunicação direta (Renato agindo no WhatsApp/email, ou o contato respondendo),
fechando o gap do action-blindness: o task_auto_resolver (task_auto_resolver.py)
só roda em AÇÃO DO BOT (whatsapp_sent/email_sent via intel_bot), nunca quando o
Renato age direto do celular.

Design (ratificado 21/07):
- Escopo apertado: só tasks pending COM contact_id. As sem contato ficam fora do
  v0 (não dá pra casar com uma comunicação de forma confiável) — a contagem é
  logada, sem cap silencioso.
- Match LLM semântico (Haiku), NÃO keyword — o keyword do auto-resolver gerou
  falso-positivo em 13/07. Barra de confiança 0.85.
- SÓ FECHA — nunca cria proposta/ação/pergunta (anti-gen-1: não vira ruído).
- Undo + audit via agent_actions.log_action(undo_hint) + mark_undone (já prontos).
- Kill-switch DB: analyzer_settings 'task_reconciler_enabled' (default ON; 'off'/
  'false'/'0' desliga sem deploy).
- Silêncio quando nada fecha. Surfacing passivo (pill via route_to_renato urg 3).

Calibração de fechamento (ratificada, ver feedback_timeline_triage / C1):
- Task de AÇÃO (enviar/mandar/cobrar/falar/contatar/responder): concluída quando
  há OUTBOUND do Renato que cumpre a ação.
- Task de ESPERA (aguardar/esperar retorno de X): concluída quando o CONTATO
  respondeu (INCOMING) o que era esperado.

FASE 2 — saída do `on_hold` (30/07, migration 058):
`on_hold` era um beco. A convenção de 29/07 (feedback_aguardar_terceiro_on_hold)
manda a task de espera ficar parqueada nos primeiros 7 dias de atraso, mas exige
que ela RESSURJA por dois gatilhos — e nenhum existia: a string "on_hold"
aparecia em 5 lugares em app/ inteiro e nenhum reabria. Task parqueada não
voltava nunca, e a CoS tinha de reverter à mão pra ela não sumir do portão.
Agora `sweep_on_hold` reabre por (a) resposta substantiva do contato depois do
parqueio e (b) janela de espera esgotada. `parqueio_indefinido` fica de fora do
aging de propósito — ver a migration.

FASE 3 — A RESPOSTA QUE VOLTA POR E-MAIL (03-06/08, pedido do Renato):
Caso #999695 "[Reorg 7] FUP Piccino", espera de `joao@piccino.com.br`. Task de
espera cujo canal é e-mail ficava CEGA pra sempre. Dois furos somados:

  (a) O cruzamento era `m.contact_id = t.contact_id`, e **81% dos e-mails têm
      `messages.contact_id` NULL** (1.436 de 1.769) — o cano só via WhatsApp na
      prática, apesar de `messages` já guardar e-mail desde jul/2025;
  (b) **ficha irmã**: a task aponta pra ficha #2869 e a thread inteira está
      gravada na #2858 — as duas com `joao@piccino.com.br`. Medido: 56
      endereços em mais de uma ficha, 133 fichas envolvidas.

E um terceiro, que é metade do problema: **task sem `contact_id`** (73 das 119
abertas, 61%) some de qualquer gate que case por ficha.

O casamento agora é por IDENTIDADE (`services/contact_identity`): ficha da task
+ fichas irmãs pelo mesmo endereço + endereço citado no texto da task quando não
há ficha. NÃO houve backfill de `contact_id` nos e-mails órfãos — medido, só 27
dos 1.436 órfãos têm remetente que já é contato (1,9%): re-linkar dado
resolveria quase nada, casar por endereço na hora da leitura resolve o caso.
"""
import json
import logging
import os
import re
from datetime import timedelta
from database import get_db
from services import llm, llm_usage
from services.contact_identity import (
    contact_emails,
    contact_ids_by_emails,
    extract_emails,
    message_emails_sql,
    owner_contact_ids,
    owner_emails,
)

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.85
MAX_MSGS_PER_TASK = 8
MAX_MSG_CHARS = 500

# COTA POR CANAL (06/08) — sem ela, cruzar e-mail não teria adiantado nada.
# Medido no teste contra prod: a #999735 ("Aguardar retorno do Nick") tinha 6
# mensagens de WhatsApp trocadas com a Fran no dia 06/08 e o e-mail ao Nick era
# de 05/08 — ordenando só por data, as 8 vagas eram todas de WhatsApp e o e-mail
# ficava de fora. A cegueira voltaria pela porta dos fundos: justamente na task
# de espera POR E-MAIL, que é o caso que esta frente existe pra resolver.
# Cada canal garante suas N mais recentes; o resto das vagas é por data.
CANAL_MIN_SLOTS = 3
MAX_MSGS_TOTAL = MAX_MSGS_PER_TASK + 4

# Janela de espera da convenção de 29/07: 7 dias de atraso sem resposta antes de
# a task voltar ao radar. Contada de GREATEST(data_vencimento, on_hold_since) —
# re-parquear concede janela nova, que é o que o gesto humano quer dizer.
ON_HOLD_WAIT_DAYS = 7

# Motivos de parqueio (coluna tasks.on_hold_reason, migration 058).
REASON_ESPERA = "espera_terceiro"
REASON_INDEFINIDO = "parqueio_indefinido"


def is_reconciler_enabled() -> bool:
    """Kill-switch DB. Default ON. Desliga com analyzer_settings
    'task_reconciler_enabled' = off/false/0 (sem deploy). Mesmo padrão do freeze
    de propostas (action_proposals.is_proposals_frozen)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM analyzer_settings WHERE setting_key = 'task_reconciler_enabled' LIMIT 1"
            )
            row = cur.fetchone()
            if not row or row['setting_value'] is None:
                return True
            val = str(row['setting_value']).strip().strip('"').lower()
            return val not in ("off", "false", "0", "no")
    except Exception:
        return True


def is_on_hold_sweep_enabled() -> bool:
    """Kill-switch DB da FASE 2, separado do fechamento: 'on_hold_sweep_enabled'
    off/false/0 desliga só a reabertura, sem derrubar a reconciliação que fecha
    (e sem deploy)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM analyzer_settings WHERE setting_key = 'on_hold_sweep_enabled' LIMIT 1"
            )
            row = cur.fetchone()
            if not row or row['setting_value'] is None:
                return True
            val = str(row['setting_value']).strip().strip('"').lower()
            return val not in ("off", "false", "0", "no")
    except Exception:
        return True


def _fetch_candidate_tasks():
    """Tasks pending. Retorna (candidates, n_sem_identidade).

    Até 06/08 o filtro era `contact_id IS NOT NULL` e as sem ficha ficavam fora
    por completo. Agora elas entram QUANDO o texto traz um endereço de e-mail —
    é o que resgata a #999735 ("Aguardar retorno do Nick", `nick@luminosita.it`
    na descrição, `contact_id` NULL). Quem não tem ficha NEM endereço continua
    fora e segue contada, sem cap silencioso.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, contact_id, project_id, data_criacao, data_vencimento
            FROM tasks
            WHERE status = 'pending'
            ORDER BY data_criacao ASC
        """)
        todas = [dict(r) for r in cur.fetchall()]

    cands, sem_identidade, so_o_dono = [], 0, 0
    for task in todas:
        scope = _task_scope(task)
        if scope["contact_ids"] or scope["emails"]:
            task["_scope"] = scope
            cands.append(task)
        else:
            sem_identidade += 1
            # Contada à parte: é a classe que fechou a #426 errado. O número tem
            # de aparecer no resumo, senão a correção vira um silêncio a mais e
            # ninguém sabe quantas tasks pararam de ser avaliadas nem por quê.
            if scope["origem"] == "dono":
                so_o_dono += 1
    return cands, sem_identidade, so_o_dono


def _task_scope(task) -> dict:
    """Identidade do TERCEIRO da task: fichas equivalentes + endereços de e-mail.

    Ordem, e o porquê de cada degrau:
      1. `contact_id` da task — o que sempre existiu;
      2. fichas IRMÃS (mesmo endereço em outra ficha) — sem isto o #999695 fica
         cego mesmo com o `contact_id` preenchido: a task está na #2869 e a
         thread na #2858, ambas `joao@piccino.com.br`;
      3. sem ficha nenhuma, os endereços CITADOS NO TEXTO da task, menos os do
         próprio Renato (a #999704 cita `renato@almeida-prado.com`; sem essa
         subtração ela passaria a se fechar com o eco do próprio sistema).

    Devolve `{'contact_ids': [...], 'emails': [...], 'origem': ...}`. `origem`
    entra no log pra a CoS conseguir ver quais tasks só foram alcançadas pelo
    texto — essas têm dado a corrigir (linkar a ficha), não é para virar norma.
    `origem='dono'` é a task cuja única identidade era a ficha do próprio Renato:
    ela sai do escopo (ver abaixo) e é contada à parte, sem cap silencioso.
    """
    contact_ids, emails, origem = [], [], "nenhum"
    with get_db() as conn:
        cur = conn.cursor()
        # O e-mail do DONO nunca é critério de "o terceiro respondeu" — nos dois
        # degraus, não só no do texto (22/08). A guarda nascera só no `else`, e o
        # ramo da ficha entregava o eco de bandeja: 21 das 148 tasks pending
        # apontam pra ficha #23419, que é a do PRÓPRIO Renato (as 4 fichas dele
        # viraram uma em 06/08). Para essas, `emails` vinha
        # ['renato@almeida-prado.com', 'renato.almeida.prado@gmail.com'] e a perna
        # de endereço casava QUALQUER e-mail que ele mandou ou recebeu — 266
        # mensagens entrando como evidência de resposta de terceiro. O reconciler
        # ainda passa por LLM a 0.85, então não fecharia sozinho; mas alimentar o
        # julgamento com evidência falsa é pior que não alimentar
        # ([[feedback_maquina_ouve_o_proprio_eco]]).
        do_dono = set(owner_emails(cur))
        # A FICHA do dono também não é terceiro — não só os endereços dele (23/08).
        # A guarda de 22/08 (acima) fechou a perna de e-mail e deixou a de
        # `contact_id` aberta; pior, um teste passou a ratificar a lacuna
        # ("a task não some do gate: segue alcançável pelo contact_id"). O que
        # sobrava não era um resíduo: a ficha #23419 é onde mora o SELF-CHAT, o
        # canal por onde o sistema fala com o Renato. Medido em 23/08 na #426
        # ("Definir microlote separável para Portugal", 26 das 149 pending
        # apontam pra lá): as 11 mensagens que foram a julgamento eram briefings
        # automáticos, e-mails dele e A PRÓPRIA NOTIFICAÇÃO DO RECONCILER
        # ("🤖 Reconciliação — fechei 2 tarefa(s)"). Nenhuma falava do assunto da
        # task, e ela foi fechada assim mesmo, com 0,95 de confiança.
        # Reconciliar é casar a task com o que UM TERCEIRO disse; quando o único
        # interlocutor é o próprio dono, não há terceiro — e conversa nenhuma é
        # melhor que a conversa da máquina consigo mesma
        # ([[feedback_maquina_ouve_o_proprio_eco]]).
        fichas_do_dono = set(owner_contact_ids(cur))
        ficha = task.get("contact_id")
        if ficha and ficha in fichas_do_dono:
            # Cai no degrau do TEXTO de propósito, em vez de descartar: se a
            # descrição citar o endereço de um terceiro, a task continua
            # alcançável — é só a ficha do dono que não vale como identidade.
            ficha = None
            origem = "dono"
        if ficha:
            origem = "ficha"
            contact_ids = [ficha]
            cur.execute("SELECT emails FROM contacts WHERE id = %s", (ficha,))
            row = cur.fetchone()
            if row:
                emails = [e for e in contact_emails({"emails": row["emails"]})
                          if e not in do_dono]
            # Sem endereço de terceiro sobrando, a perna de e-mail não casa nada e
            # a task segue só pelo `contact_id` — que é exatamente o cano de antes.
            for cid in contact_ids_by_emails(cur, emails):
                if cid not in contact_ids and cid not in fichas_do_dono:
                    contact_ids.append(cid)
        else:
            texto = f"{task.get('titulo') or ''} {task.get('descricao') or ''}"
            emails = [e for e in extract_emails(texto) if e not in do_dono]
            if emails:
                # Ficha irmã do dono achada pelo endereço entra pela porta dos
                # fundos se não for filtrada aqui também.
                achadas = [c for c in contact_ids_by_emails(cur, emails)
                           if c not in fichas_do_dono]
                if achadas or emails:
                    origem = "texto"
                contact_ids = achadas
    return {"contact_ids": contact_ids, "emails": emails, "origem": origem}


def _scope_where(scope, alias="m"):
    """(fragmento SQL, params) que casa uma mensagem com a identidade da task.

    Duas pernas em OR, porque as duas metades da base são diferentes:
      - `contact_id = ANY(...)` pega WhatsApp (sempre linkado) e o e-mail que já
        veio linkado;
      - `canal='email' AND <endereços>` pega o resto — os 81% de e-mails com
        `contact_id` NULL e os que estão na ficha irmã.
    O casamento por endereço é direcional (incoming pelo `from`, outgoing pelo
    `to`), senão um e-mail de OUTRA pessoa com o contato em cópia contaria como
    "o contato respondeu".
    """
    pernas, params = [], []
    if scope.get("contact_ids"):
        pernas.append(f"{alias}.contact_id = ANY(%s)")
        params.append(scope["contact_ids"])
    if scope.get("emails"):
        pernas.append(f"(cv.canal = 'email' AND {message_emails_sql(alias)})")
        params.extend([scope["emails"], scope["emails"]])  # o fragmento consome 2×
    if not pernas:
        return "FALSE", []
    return "(" + " OR ".join(pernas) + ")", params


def _fetch_messages_since(scope, since):
    """Mensagens (ambas direções) trocadas com o TERCEIRO depois de `since` —
    WhatsApp e e-mail na mesma peneira, casadas por identidade."""
    cond, params = _scope_where(scope)
    if not params:
        return []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            WITH base AS (
                SELECT m.direcao, m.conteudo,
                       COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS ts,
                       COALESCE(cv.canal, 'whatsapp') AS canal,
                       -- QUEM falou. Uma task sem ficha pode citar mais de um
                       -- endereço (a #999735 cita o Nick E a Fran, em cópia), e
                       -- sem o nome o julgamento leria "contato→você" da Fran
                       -- como o retorno do Nick — fecharia a espera errada.
                       COALESCE(ct.nome, m.metadata->>'from_name',
                                m.metadata->>'from') AS parte
                FROM messages m
                LEFT JOIN conversations cv ON cv.id = m.conversation_id
                LEFT JOIN contacts ct ON ct.id = m.contact_id
                WHERE {cond}
                  AND m.conteudo IS NOT NULL AND m.conteudo <> ''
                  AND COALESCE(m.enviado_em, m.recebido_em, m.criado_em) > %s
            ),
            ranqueada AS (
                SELECT *, row_number() OVER (ORDER BY ts DESC) AS rn_geral,
                          row_number() OVER (PARTITION BY canal ORDER BY ts DESC) AS rn_canal
                FROM base
            )
            SELECT direcao, conteudo, ts, canal, parte
            FROM ranqueada
            WHERE rn_geral <= %s OR rn_canal <= %s   -- cota por canal: ver CANAL_MIN_SLOTS
            ORDER BY ts DESC
            LIMIT %s
        """, tuple(params) + (since, MAX_MSGS_PER_TASK, CANAL_MIN_SLOTS, MAX_MSGS_TOTAL))
        return [dict(r) for r in cur.fetchall()]


# ==================== A citação tem que existir (23/08) ====================
#
# A #426 foi fechada com 0,95 de confiança e esta justificativa:
#   "Mensagem de 14/07 documenta decisão técnica completa: microlote = 10%
#    peneira mais alta (~50 sacas), separação na classificação Guaxupé..."
# Não havia mensagem de 14/07 no lote (24/06, 06/08, 18–23/08) e NENHUMA das 11
# citava microlote. A frase é, palavra por palavra, a última linha da DESCRIÇÃO
# da própria tarefa — que vai no prompt. O modelo devolveu o enunciado como se
# fosse a prova.
#
# Barra de confiança não pega isto: 0,85 mede o quanto o modelo acredita, não se
# o que ele leu existe. A guarda é obrigar uma CITAÇÃO LITERAL e conferi-la
# contra o texto que foi mostrado — verificação determinística, feita em Python.
# Não confere, não fecha: `done` vira false e o motivo fica no log.
EVIDENCE_MIN_CHARS = 25

# Aspas curvas e reticências: o modelo reescreve `"` como `“` ao copiar, e a
# citação verdadeira falharia por um caractere. Normalizar não afrouxa a guarda
# — o que ela testa é se o TEXTO existe, não a pontuação com que foi copiado.
_QUOTE_FIX = str.maketrans({"“": '"', "”": '"', "‘": "'",
                            "’": "'", "…": "...", " ": " "})


def _norm_evidencia(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").translate(_QUOTE_FIX)).strip().lower()


def _display_text(m) -> str:
    """O texto da mensagem COMO ele aparece no prompt — já truncado.

    A conferência compara contra isto, não contra o `conteudo` inteiro: o modelo
    só pode citar o que viu, e aceitar citação de trecho cortado deixaria passar
    exatamente a invenção que a guarda existe pra pegar."""
    return (m.get("conteudo") or "")[:MAX_MSG_CHARS]


def _evidencia_confere(trecho, exibidas) -> tuple:
    """A citação existe em alguma das mensagens mostradas? → (ok, motivo).

    Trecho curto só vale se for a mensagem INTEIRA — a evidência real da #999921
    era "Já fiz a vídeo" (14 caracteres), e um piso cego mataria o fechamento
    certo junto com o inventado. O que o piso barra é o trecho genérico ("ok",
    "confirmado") que casaria por acaso em meia base."""
    alvo = _norm_evidencia(trecho)
    if not alvo:
        return False, "veredito sem citação"
    corpos = {i: _norm_evidencia(t) for i, t in exibidas.items()}
    achou = [i for i, c in corpos.items() if alvo and alvo in c]
    if not achou:
        return False, "citação não aparece em nenhuma das mensagens"
    if len(alvo) < EVIDENCE_MIN_CHARS and not any(corpos[i] == alvo for i in achou):
        return False, f"citação curta demais ({len(alvo)} caracteres) e parcial"
    return True, ""


def _judge(task, msgs) -> dict:
    """LLM (Haiku) decide se a task foi concluída à luz das mensagens. JSON estrito.
    Retorna {done, confidence, reason, evidencia}. Best-effort: erro → done=false.

    `done=true` só sobrevive se a citação que o modelo devolveu for encontrada
    no texto que ele viu (`_evidencia_confere`)."""
    # O CANAL vai no prompt (06/08): com e-mail na peneira, o julgamento muda —
    # "respondeu por e-mail" é a evidência que fecha a #999695, e sem o rótulo o
    # modelo lê uma thread de e-mail como se fosse recado de WhatsApp.
    ordenadas = list(reversed(msgs))  # mais antigas primeiro, leitura cronológica
    exibidas, linhas = {}, []
    for i, m in enumerate(ordenadas, start=1):
        texto = _display_text(m)
        exibidas[i] = texto
        quem = (
            f"você→{m.get('parte') or 'contato'}" if m["direcao"] == "outgoing"
            else f"{m.get('parte') or 'contato'}→você"
        )
        linhas.append(f"[M{i} | {quem} por {m.get('canal') or 'whatsapp'} "
                      f"| {m['ts']:%d/%m %H:%M}] {texto}")
    convo = "\n".join(linhas)

    prompt = f"""Você decide se uma TAREFA pendente já foi CONCLUÍDA, à luz das mensagens trocadas com o contato DEPOIS que a tarefa foi criada.

TAREFA:
Título: {task['titulo']}
Descrição: {task.get('descricao') or '(sem descrição)'}
Criada em: {task['data_criacao']:%d/%m/%Y}

MENSAGENS DESDE A CRIAÇÃO (cronológico, rotuladas M1, M2, ...):
{convo}

REGRAS:
- Cada linha traz QUEM falou e POR QUAL CANAL (whatsapp/email). A tarefa nomeia de quem se espera o retorno — resposta de OUTRA pessoa da mesma conversa NÃO conclui a espera.
- Tarefa de AÇÃO (enviar/mandar/cobrar/falar/contatar/responder): só está concluída se HÁ mensagem SUA (você→…) que CUMPRE a ação.
- Tarefa de ESPERA (aguardar/esperar retorno de alguém): só está concluída se a PESSOA ESPERADA respondeu (…→você) o que era esperado — por WhatsApp ou por e-mail, tanto faz o canal.
- PLANO REGISTRADO NÃO É AÇÃO CUMPRIDA. Texto que anuncia intenção, descreve como algo será feito, ou registra uma decisão ("vamos separar X", "o critério é Y", "ficou definido que...") não conclui nada — a tarefa fecha quando o que ela pedia FOI FEITO, não quando alguém escreveu o que pretende fazer.
- TAREFA QUE PEDE VÁRIAS COISAS só fecha com TODAS satisfeitas. Se ela pede 5 definições e as mensagens resolvem 2, done=false.
- A DESCRIÇÃO DA TAREFA NÃO É EVIDÊNCIA. Ela é o enunciado do que falta fazer. Só as linhas M1..Mn acima contam como prova.
- Na dúvida, done=false. Conversa tangencial NÃO conclui a tarefa.

Se (e só se) done=true, você DEVE apontar a prova:
- "evidencia_id": o rótulo da mensagem que conclui a tarefa (ex.: "M3");
- "evidencia_trecho": um trecho COPIADO LITERALMENTE dessa mensagem, palavra por palavra, sem parafrasear e sem juntar pedaços de mensagens diferentes.
O trecho é conferido contra o texto acima. Se você não encontrar nas mensagens um trecho que sustente o fechamento, a resposta correta é done=false.

Responda APENAS um JSON: {{"done": true|false, "confidence": 0.0-1.0, "evidencia_id": "M1", "evidencia_trecho": "...", "reason": "1 frase curta"}}"""

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {"done": False, "confidence": 0.0, "reason": "sem API key"}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=llm.FAST,
            max_tokens=400,  # o veredito agora carrega a citação junto
            messages=[{"role": "user", "content": prompt}],
        )
        try:  # F-E: custo por-funcao (telemetria nunca quebra a chamada real)
            llm_usage.record_response("task_reconciler.judge", llm.FAST, msg.model_dump())
        except Exception:
            pass
        raw = msg.content[0].text if msg.content else ""
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"done": False, "confidence": 0.0, "reason": "parse falhou"}
        data = json.loads(m.group(0))
        verdict = {
            "done": bool(data.get("done")),
            "confidence": float(data.get("confidence") or 0.0),
            "reason": str(data.get("reason") or "")[:200],
            "evidencia_id": str(data.get("evidencia_id") or "")[:8],
            "evidencia": str(data.get("evidencia_trecho") or "")[:400],
        }
        if verdict["done"]:
            ok, motivo = _evidencia_confere(verdict["evidencia"], exibidas)
            if not ok:
                logger.warning(
                    "task_reconciler: veredito DESCARTADO na task %s — %s. "
                    "Citação: %r | reason: %r",
                    task["id"], motivo, verdict["evidencia"][:160], verdict["reason"],
                )
                return {
                    "done": False,
                    "confidence": 0.0,
                    "reason": f"evidência não confere: {motivo}",
                    "evidencia": verdict["evidencia"],
                    "evidencia_falha": motivo,
                }
        return verdict
    except Exception as e:
        logger.warning(f"task_reconciler judge falhou (task {task['id']}): {e}")
        return {"done": False, "confidence": 0.0, "reason": f"erro: {e}"}


def _close_task(task, verdict):
    """Fecha a task + registra em agent_actions com undo_hint (reverte via mark_undone)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET status='completed', data_conclusao=NOW() WHERE id=%s AND status='pending'",
            (task["id"],),
        )
        conn.commit()
    from services.agent_actions import log_action
    log_action(
        action_type='task_resolved',
        category='tasks',
        title=f"Tarefa concluída (reconciler): {task['titulo']}",
        details=(
            f"Fechada por comunicação direta. Confiança {verdict['confidence']:.2f}. "
            f"{verdict['reason']}"
            # A citação CONFERIDA vai pra trilha, não só a prosa do modelo: é o
            # que permite auditar um fechamento sem reabrir o prompt. Na #426 a
            # justificativa soava impecável e a mensagem não existia.
            + (f" | Evidência ({verdict.get('evidencia_id') or '?'}): "
               f"\"{verdict['evidencia'][:200]}\"" if verdict.get('evidencia') else "")
        ),
        scope_ref={'task_id': task['id'], 'contact_id': task.get('contact_id'), 'project_id': task.get('project_id')},
        source='task_reconciler',
        payload={'confidence': verdict['confidence'], 'reason': verdict['reason'],
                 'evidencia': verdict.get('evidencia'),
                 'evidencia_id': verdict.get('evidencia_id')},
        undo_hint=f"UPDATE tasks SET status='pending', data_conclusao=NULL WHERE id={task['id']}",
    )


async def _notify_closed(closed):
    """Pill passivo (urgência 3) — 'te conto o que fiz': lista o que fechou, POR QUÊ
    (a conversa que resolveu) e COMO DESFAZER (comando desfaz N, reusa run_smart_undo
    do agent_actions — o _close_task loga task_resolved+undo_hint). Não interrompe.
    Padrão espelha a camada esperta do inbox (email)."""
    from services.notification_router import notify
    lines = "\n".join(
        f"  • #{c['id']} {c['titulo']}" +
        (f" — {(c.get('reason') or '').strip()[:80]}" if c.get('reason') else "")
        for c in closed
    )
    msg = (
        f"🤖 Reconciliação (conversa/WA) — fechei {len(closed)} tarefa(s) que você "
        f"resolveu direto:\n{lines}\n\n"
        f"Se alguma foi engano, responde \"desfaz N\" (N = nº da tarefa) que eu reabro."
    )
    dedup = "tasks_reconciled:" + "-".join(str(c['id']) for c in sorted(closed, key=lambda x: x['id']))
    await notify(
        "task_reconciler", "Reconciliação: tarefas fechadas", msg, 3,
        msg_type="tasks_reconciled", dedup=dedup,
    )


# ==================== FASE 2: saída do on_hold ====================

def _fetch_on_hold_tasks():
    """Tasks parqueadas, com o ciclo de vida da migration 058."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, contact_id, project_id, data_vencimento,
                   atualizado_em, on_hold_since, on_hold_reason
            FROM tasks
            WHERE status = 'on_hold'
            ORDER BY id ASC
        """)
        # `descricao` entrou em 06/08: é de lá que sai o endereço de e-mail da
        # task sem ficha (16 das 30 parqueadas não têm `contact_id`).
        return [dict(r) for r in cur.fetchall()]


def _classify_on_hold(task) -> None:
    """Carimba `on_hold_since`/`on_hold_reason` numa task parqueada que ainda não
    os tem — parqueio feito por SQL manual, pela UI ou pelo snooze da Tônia, que
    não declaram intenção.

    O default é `espera_terceiro`, porque é o que a convenção de 29/07 descreve e
    o que a CoS de fato faz ao parquear. `on_hold_since` recebe NOW(), não
    `atualizado_em`: o carimbo tem que valer a partir de AGORA, senão uma task
    parqueada hoje com vencimento antigo já nasceria fora da janela e reabriria
    na mesma run — parqueio sem efeito nenhum.

    Quem carimba nesta run NÃO é avaliado nela (carência de 1 run): a janela de
    espera precisa existir antes de poder estar esgotada."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET on_hold_since = COALESCE(on_hold_since, NOW()),
                on_hold_reason = COALESCE(on_hold_reason, %s)
            WHERE id = %s AND status = 'on_hold'
        """, (REASON_ESPERA, task["id"]))
        conn.commit()


def _last_substantive_incoming(scope, since):
    """Mensagem do terceiro (INCOMING) posterior ao parqueio que NÃO é cortesia.

    O filtro de cortesia é o mesmo de raci_smart_updates, já calibrado em prod:
    sem ele, um "obrigado, bom domingo" reabre a task e a convenção vira ruído —
    é exatamente o falso-positivo que o check G da /cos levou um dia pra corrigir
    (uma cortesia às 16:11 ressuscitou item respondido às 16:10).

    Desde 06/08 recebe a IDENTIDADE (fichas + endereços), não um `contact_id`:
    era aqui que a task de espera por e-mail morria. O e-mail de resposta do
    Piccino cairia em `messages` com `contact_id` NULL e este SELECT nunca o
    veria — a #999695 esperaria os 7 dias e voltaria como "o terceiro sumiu",
    dizendo o contrário do que os dados mostram."""
    from services.raci_smart_updates import _is_courtesy_only

    cond, params = _scope_where(scope)
    if not params:
        return None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            WITH base AS (
                SELECT m.conteudo, COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS ts,
                       COALESCE(cv.canal, 'whatsapp') AS canal
                FROM messages m
                LEFT JOIN conversations cv ON cv.id = m.conversation_id
                WHERE {cond}
                  AND m.direcao = 'incoming'
                  AND m.conteudo IS NOT NULL AND m.conteudo <> ''
                  AND COALESCE(m.enviado_em, m.recebido_em, m.criado_em) > %s
            ),
            ranqueada AS (
                SELECT *, row_number() OVER (ORDER BY ts DESC) AS rn_geral,
                          row_number() OVER (PARTITION BY canal ORDER BY ts DESC) AS rn_canal
                FROM base
            )
            SELECT conteudo, ts, canal FROM ranqueada
            -- mesma cota por canal do `_fetch_messages_since`: um contato
            -- tagarela no WhatsApp não pode esconder a resposta que veio por
            -- e-mail — é ELA que encerra a espera.
            WHERE rn_geral <= %s OR rn_canal <= %s
            ORDER BY ts DESC
            LIMIT %s
        """, tuple(params) + (since, MAX_MSGS_PER_TASK, CANAL_MIN_SLOTS, MAX_MSGS_TOTAL))
        msgs = [dict(r) for r in cur.fetchall()]

    for m in msgs:
        if not _is_courtesy_only(m["conteudo"] or ""):
            return m
    return None


def _now_naive_utc():
    """Agora em UTC SEM tzinfo, pra comparar com as colunas de `tasks`, que são
    TIMESTAMP naive gravadas em UTC.

    `datetime.now()` aqui seria naive LOCAL: coincide por acidente no Railway
    (UTC) e erra 3h rodando da máquina do Renato — a classe de bug do `eeb41ce`.
    `utcnow()` está deprecated em 3.12+. Ver a convenção no CLAUDE.md."""
    from services.tz import now_utc
    return now_utc().replace(tzinfo=None)


def _wait_deadline(task):
    """Fim da janela de espera: 7 dias após o MAIOR entre vencimento e parqueio.

    Usar só `data_vencimento` faria uma task re-parqueada com atraso antigo
    reabrir na run seguinte, para sempre (feedback_livelock_reprocessamento).
    Usar só `on_hold_since` daria 7 dias novos a quem já estava atrasado há um
    mês quando foi parqueada — a janela é de ESPERA, e a espera começou no
    vencimento."""
    base = task["on_hold_since"]
    dv = task.get("data_vencimento")
    if dv and dv > base:
        base = dv
    return base + timedelta(days=ON_HOLD_WAIT_DAYS)


def _reopen_task(task, reason_code: str, reason: str) -> None:
    """on_hold → pending, com undo e trilha.

    Limpa `on_hold_since`/`on_hold_reason`: sem isso, a mensagem que causou a
    reabertura seguiria sendo "posterior ao parqueio" para sempre e a task
    reabriria toda run mesmo depois de re-parqueada. Zerar significa que um
    re-parqueio futuro começa uma janela nova.

    `atualizado_em = NOW()` é deliberado — é o que arma a guarda de conflito do
    tasks_sync (atualizado_em > last_synced_at) e impede o pull do Google de
    passar por cima da decisão que acabou de ser tomada aqui."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status = 'pending',
                on_hold_since = NULL,
                on_hold_reason = NULL,
                atualizado_em = NOW()
            WHERE id = %s AND status = 'on_hold'
        """, (task["id"],))
        conn.commit()

    since = task["on_hold_since"]
    since_sql = f"'{since:%Y-%m-%d %H:%M:%S}'" if since else "NULL"
    prev_reason = task.get("on_hold_reason") or REASON_ESPERA
    from services.agent_actions import log_action
    log_action(
        action_type='task_reopened',
        category='tasks',
        title=f"Tarefa reaberta ({reason_code}): {task['titulo']}",
        details=reason,
        scope_ref={'task_id': task['id'], 'contact_id': task.get('contact_id'), 'project_id': task.get('project_id')},
        source='task_reconciler.on_hold_sweep',
        payload={'trigger': reason_code, 'reason': reason, 'on_hold_since': str(since)},
        undo_hint=(
            f"UPDATE tasks SET status='on_hold', on_hold_since={since_sql}, "
            f"on_hold_reason='{prev_reason}' WHERE id={task['id']}"
        ),
    )


def sweep_on_hold(dry_run: bool = False) -> dict:
    """FASE 2 — devolve ao radar a task parqueada cuja espera terminou.

    Dois gatilhos, ambos determinísticos (sem LLM: são fatos, não julgamento):
      (a) RESPOSTA — o contato mandou algo substantivo depois do parqueio. A bola
          voltou pro Renato, então a espera acabou (tenha ela resolvido ou não —
          se resolveu, a fase de fechamento fecha no mesmo run).
      (b) JANELA — passaram os 7 dias sem resposta. O terceiro sumiu; volta a
          vencida pro Renato decidir se cobra.

    `parqueio_indefinido` está fora dos DOIS de propósito: é frente que o Renato
    tirou do radar sem prazo, e reabri-la por tempo devolveria ao portão o que
    foi silenciado por decisão. Ela só sai de lá por gesto humano."""
    if not is_on_hold_sweep_enabled():
        logger.info("on_hold_sweep: desligado (kill-switch)")
        return {"disabled": True}

    tasks = _fetch_on_hold_tasks()
    classified, reopened, held = [], [], []

    for task in tasks:
        # Parqueio sem intenção declarada: carimba e deixa maturar 1 run.
        if not task.get("on_hold_since") or not task.get("on_hold_reason"):
            if not dry_run:
                _classify_on_hold(task)
            classified.append({"id": task["id"], "titulo": task["titulo"]})
            continue

        if task["on_hold_reason"] == REASON_INDEFINIDO:
            held.append({"id": task["id"], "motivo": REASON_INDEFINIDO})
            continue

        trigger, reason = None, None

        # A guarda era `if task.get("contact_id")` — e as 16 parqueadas sem
        # ficha (de 30) nunca podiam reabrir por resposta, só por tempo. Agora o
        # gatilho é a IDENTIDADE, que também cobre e-mail e ficha irmã.
        scope = _task_scope(task)
        if scope["contact_ids"] or scope["emails"]:
            msg = _last_substantive_incoming(scope, task["on_hold_since"])
            if msg:
                trigger = "reply"
                canal = msg.get("canal") or "whatsapp"
                reason = (
                    f"O contato respondeu por {canal} em {msg['ts']:%d/%m %H:%M} "
                    f"(parqueada em {task['on_hold_since']:%d/%m}): "
                    f"\"{(msg['conteudo'] or '')[:120]}\""
                )

        if not trigger:
            deadline = _wait_deadline(task)
            agora = _now_naive_utc()
            if agora > deadline:
                dias = (agora - deadline).days + ON_HOLD_WAIT_DAYS
                trigger = "aging"
                reason = (
                    f"{dias} dias de espera sem resposta (janela de "
                    f"{ON_HOLD_WAIT_DAYS} dias esgotada em {deadline:%d/%m}) — "
                    f"hora de decidir se cobra."
                )

        if trigger:
            rec = {"id": task["id"], "titulo": task["titulo"],
                   "trigger": trigger, "reason": reason}
            if not dry_run:
                _reopen_task(task, trigger, reason)
            reopened.append(rec)
        else:
            held.append({"id": task["id"], "motivo": "dentro da janela",
                         "ate": f"{_wait_deadline(task):%d/%m}"})

    summary = {
        "disabled": False,
        "dry_run": dry_run,
        "scanned": len(tasks),
        "classified": len(classified),      # carimbadas agora, avaliadas no próximo run
        "reopened": len(reopened),
        "still_held": len(held),
        "items": reopened,
        "classified_items": classified,
        "held_items": held,
    }
    logger.info(f"on_hold_sweep: {summary}")
    return summary


async def _notify_reopened(reopened):
    """Pill passivo (urgência 3), mesmo padrão do _notify_closed: conta o que
    voltou pro radar, POR QUÊ, e como desfazer. Reabertura NÃO interrompe — ela
    devolve a task pro portão, que é onde o Renato já olha."""
    from services.notification_router import notify
    lines = "\n".join(
        f"  • #{r['id']} {r['titulo']} — "
        f"{'o contato respondeu' if r['trigger'] == 'reply' else 'passou a janela de espera'}"
        for r in reopened
    )
    msg = (
        f"⏰ Espera encerrada — devolvi {len(reopened)} tarefa(s) parqueada(s) "
        f"pro radar:\n{lines}\n\n"
        f"Se alguma deve continuar parada, responde \"desfaz N\" que eu parqueio de volta."
    )
    dedup = "tasks_reopened:" + "-".join(str(r['id']) for r in sorted(reopened, key=lambda x: x['id']))
    await notify(
        "task_reconciler", "Espera encerrada: tarefas de volta ao radar", msg, 3,
        msg_type="tasks_reopened", dedup=dedup,
    )


async def run_task_reconciler(dry_run: bool = False) -> dict:
    """Sweep. Fecha tasks pending resolvidas por comunicação direta.
    dry_run=True: julga e loga o que fecharia, mas NÃO fecha nem notifica."""
    # O kill-switch barra a ESCRITA, não a medição (23/08). Enquanto ele também
    # abortava o dry_run, a única forma de saber se o conserto funcionou era
    # religar em produção e olhar — gate que se valida ligando não é gate. Com a
    # chave `off`, `dry_run=True` julga e relata sem fechar nada.
    desligado = not is_reconciler_enabled()
    if desligado and not dry_run:
        logger.info("task_reconciler: desligado (kill-switch)")
        return {"disabled": True}

    # FASE 2 ANTES da fase 1, de propósito: task reaberta por resposta do contato
    # vira `pending` e entra no escopo de fechamento no MESMO run. Sem essa ordem,
    # uma espera que a resposta já resolveu passaria 24h como pendente vencida
    # antes de o julgamento a fechar — o pior dos dois mundos pro Renato.
    on_hold = sweep_on_hold(dry_run=dry_run)

    candidates, n_sem_contato, n_so_dono = _fetch_candidate_tasks()
    judged = 0
    closed = []
    would_close = []
    sem_evidencia = []
    por_texto = sum(1 for t in candidates if (t.get("_scope") or {}).get("origem") == "texto")

    for task in candidates:
        msgs = _fetch_messages_since(task.get("_scope") or _task_scope(task), task["data_criacao"])
        if not msgs:
            continue
        verdict = _judge(task, msgs)
        judged += 1
        if verdict.get("evidencia_falha"):
            sem_evidencia.append({"id": task["id"], "titulo": task["titulo"],
                                  "motivo": verdict["evidencia_falha"],
                                  "citacao": (verdict.get("evidencia") or "")[:160]})
        if verdict["done"] and verdict["confidence"] >= CONFIDENCE_THRESHOLD:
            rec = {
                "id": task["id"], "titulo": task["titulo"],
                "confidence": verdict["confidence"], "reason": verdict["reason"],
                "evidencia": verdict.get("evidencia"),
            }
            if dry_run:
                would_close.append(rec)
            else:
                _close_task(task, verdict)
                closed.append(rec)

    if closed and not dry_run:
        await _notify_closed(closed)

    # Task reaberta que a fase 1 fechou no mesmo run não é "de volta ao radar" —
    # é resolvida. Sai do aviso de reabertura pra não contar a mesma história duas
    # vezes com desfechos diferentes.
    closed_ids = {c["id"] for c in closed}
    reopened_net = [r for r in (on_hold.get("items") or []) if r["id"] not in closed_ids]
    if reopened_net and not dry_run:
        await _notify_reopened(reopened_net)

    summary = {
        "disabled": desligado,   # dry_run com a chave off: mediu, não escreveu
        "dry_run": dry_run,
        "scanned_with_contact": len(candidates),
        "skipped_no_contact": n_sem_contato,  # v0 boundary — logado, sem cap silencioso
        # Tasks cuja única identidade era a ficha do PRÓPRIO Renato (23/08). Eram
        # 26 de 149 quando a #426 foi fechada errado; se este número crescer, é a
        # CoS linkando task ao dono em vez do terceiro — dado a corrigir, não
        # régua a afrouxar.
        "skipped_owner_only": n_so_dono,
        # Vereditos `done=true` DERRUBADOS por citação que não existe. Zero aqui
        # não prova que a guarda funciona — prova que ninguém tentou. Ver o log
        # (WARNING) pra a citação inventada em si.
        "blocked_no_evidence": len(sem_evidencia),
        "blocked_items": sem_evidencia,
        # Alcançadas SÓ pelo endereço no texto: cada uma é uma task com dado a
        # corrigir (falta o `contact_id`). O número existe pra a CoS linkar as
        # fichas, não pra o texto virar o caminho normal.
        "scoped_by_text": por_texto,
        "judged": judged,
        "closed": len(closed),
        "would_close": len(would_close),
        "items": (would_close if dry_run else closed),
        "on_hold_sweep": on_hold,
        "reopened_net": len(reopened_net),
    }
    logger.info(f"task_reconciler: {summary}")
    return summary
