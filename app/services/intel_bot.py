"""
Intel Bot - Conversational WhatsApp Bot with Claude Tool Use

Full LLM chat with conversation memory. Uses Claude's function calling
to dynamically decide when to query the CRM, create tasks, send messages, etc.
No rigid intent classification — Claude decides what tools to use.
"""
import os
import re
import json
import httpx
import logging
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Any

SP_TZ = ZoneInfo("America/Sao_Paulo")
DIAS_PT = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def _now_sp() -> datetime:
    return datetime.now(SP_TZ)


def _format_sp_datetime(dt: datetime = None) -> str:
    if dt is None:
        dt = _now_sp()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=SP_TZ)
    else:
        dt = dt.astimezone(SP_TZ)
    return f"{dt.strftime('%Y-%m-%d')} {DIAS_PT[dt.weekday()]} {dt.strftime('%H:%M')}"

from database import get_db
from services.agent_intents import (
    get_open_intents,
    format_intents_for_prompt,
    maybe_open_intent_for_turn,
)

logger = logging.getLogger(__name__)

# Config
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_BOT_NUMBER = os.getenv("INTEL_BOT_NUMBER", "5511915020192")
RENATO_PHONE = "5511984153337"
RENATO_PHONE_SUFFIXES = ["11984153337", "984153337"]
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ITERATIONS = 4  # bumped de 3 -> 4 pra acomodar re-prompt + retry
# Max re-prompts when detector flags promessa-sem-tool. 1 chance extra:
# bot pode reagir a aviso explicito chamando tool ou admitindo limite.
MAX_HALLUCINATION_REPROMPTS = 1

# Rate limit: skip trivial messages
SKIP_PATTERNS = re.compile(
    r'^(ok|👍|👌|🙏|❤️|😀|😂|🤣|😊|👏|🔥|✅|sim|nao|não|obrigado|valeu|top|show|beleza|blz|tmj)$',
    re.IGNORECASE
)

# ==================== HALLUCINATION DETECTOR ====================
# Detecta quando o bot afirma ter executado uma acao mas nao chamou tool.
# Reportado em feedback #13: bot disse "Evento atualizado" sem chamar
# update_calendar_event. Cobertura: prompt hardening (REGRA #0) + este
# validador automatico que cruza claims com tool_calls executados.

# Actions de write (mudam estado). Read-only (query_intel, query_conselhoos,
# draft_message, project_chat, search_system_memories) NAO entram aqui.
WRITE_ACTIONS = {
    "create_task", "complete_task", "update_task", "postpone_tasks",
    "save_note", "save_memory",
    "schedule_meeting", "update_calendar_event", "delete_calendar_event",
    "send_whatsapp", "send_email",
    "import_fathom_meeting",
    "enrich_contact", "update_contact",
    "save_feedback", "save_system_memory",
}

# Verbos em 1a pessoa + objeto direto que indicam acao executada agora.
# Padroes conservadores pra evitar FP: exigem objeto (artigo + substantivo).
# Match em qualquer parte do texto (use re.search com IGNORECASE).
_CLAIM_PATTERNS = [
    # CRIAR / AGENDAR
    r'\b(criei|agendei|marquei|registrei)\s+(o|a|os|as|um|uma|seu|sua)\b',
    r'\b(adicionei|inclui)\s+(no|na|ao|a|o|os)\s+(calendario|agenda|tarefa|projeto|nota|memoria)',
    # APAGAR / REMOVER
    r'\b(apaguei|deletei|removi|excluí|exclui)\s+(o|a|os|as|todos|todas|essa|esse|esses|essas)\b',
    # ATUALIZAR / EDITAR
    r'\b(atualizei|editei|alterei|modifiquei|movi|remarquei|reagendei)\s+(o|a|os|as|essa|esse)\b',
    # ENVIAR
    r'\b(enviei|mandei|despachei)\s+(o|a|os|as|um|uma|essa|esse|seu|sua)\s*(email|mensagem|whatsapp|wa|aviso|recado|texto)?',
    # SALVAR / ANOTAR
    r'\b(salvei|anotei|guardei)\s+(o|a|os|as|isso|essa|esse|como)\b',
    # CONCLUIR
    r'\b(conclu[ií]|completei|finalizei|fechei)\s+(o|a|os|as|essa|esse|todas|todos)\s*(tarefa|item)?',
    # Voz passiva: "evento atualizado", "tarefa concluida", "email enviado"
    # (com ou sem foi/fica/ficou). Caso real do feedback #13: bot disse
    # "Evento atualizado, 16h, 30min" sem ter chamado update_calendar_event.
    r'\b(evento|reuniao|tarefa|nota|email|mensagem|memoria|contato)\s+(foi|fica|ficou\s+)?(criad[oa]|atualizad[oa]|apagad[oa]|deletad[oa]|enviad[oa]|salv[oa]|conclu[ií]d[oa]|removid[oa]|editad[oa])\b',
    # "pronto, [acao executada]"
    r'\bpronto[,.]?\s*(apaguei|deletei|criei|enviei|atualizei|removi|editei|salvei)\b',
    # FUTURO + temporal — bot promete sem executar (casos 08/05).
    # Lista de verbos cobre write actions + sinonimos genericos (executar/fazer/realizar/rodar/processar).
    # Read-only (pesquisar/buscar/consultar/checar/ver/listar/mostrar/ler/conferir) ficam fora.
    r'\bvou\s+(executar|fazer|realizar|rodar|processar|criar|gerar|cadastrar|agendar|marcar|registrar|adicionar|incluir|inserir|apagar|deletar|remover|excluir|limpar|atualizar|editar|alterar|modificar|mover|trocar|remarcar|reagendar|vincular|associar|relacionar|conectar|enviar|mandar|despachar|salvar|anotar|guardar|persistir|concluir|completar|finalizar|fechar|encerrar)\b[^.!?\n]{0,120}\b(agora|ja|ja\s+ja|imediatamente|nesse\s+momento|a\s+seguir|de\s+verdade|mesmo)\b',
    # Mesmos verbos sem "agora" mas com enumeracao explicita ("as 43 tarefas", "todas as", "todos os")
    # — bot descreve um plano concreto sem executar. Mais um sintoma forte de promessa nao cumprida.
    r'\bvou\s+(executar|fazer|realizar|rodar|processar|criar|gerar|cadastrar|agendar|marcar|registrar|adicionar|incluir|inserir|apagar|deletar|remover|excluir|limpar|atualizar|editar|alterar|modificar|mover|trocar|remarcar|reagendar|vincular|associar|relacionar|conectar|enviar|mandar|despachar|salvar|anotar|guardar|persistir|concluir|completar|finalizar|fechar|encerrar)\b[^.!?\n]{0,40}\b(as|os)?\s*(\d+|todas|todos|cada\s+uma|cada\s+um)\b',
    r'\b(deixa|deixe)\s+eu\s+(fazer|executar|rodar|atualizar|criar|salvar|enviar|apagar|vincular|registrar)\s+(isso|isto)?\s*(agora|mesmo|ja)?\b',
    r'\bfazendo\s+(isso|isto|agora|a\s+atualizacao|a\s+vinculacao|o\s+update)\b',
    r'\b(estou|to)\s+(criando|atualizando|salvando|enviando|apagando|deletando|vinculando|movendo|executando|processando)\b',
]
_CLAIM_RE = re.compile("|".join(_CLAIM_PATTERNS), re.IGNORECASE)


def _had_successful_write_action(turn_actions: list) -> bool:
    """True se alguma write-action rodou com sucesso no turn.

    turn_actions: lista de {"action": str, "result": str (json)} acumulada
    nas iteracoes do loop tool_use.
    """
    for entry in turn_actions:
        if entry.get("action") not in WRITE_ACTIONS:
            continue
        result_str = entry.get("result") or ""
        # Sucesso = tem "sucesso": true OU ausencia de "erro" no JSON
        # (handler retorna ou {"sucesso": true, ...} ou {"erro": ...})
        try:
            parsed = json.loads(result_str) if isinstance(result_str, str) else result_str
            if isinstance(parsed, dict):
                if parsed.get("sucesso") is True:
                    return True
                # Algumas actions retornam outras formas — checa ausencia de erro
                if "erro" not in parsed and "error" not in parsed:
                    return True
        except (json.JSONDecodeError, TypeError):
            # Result nao-JSON, considera sucesso se nao tem palavra "erro"
            if "erro" not in str(result_str).lower() and "error" not in str(result_str).lower():
                return True
    return False


def _detect_hallucination(final_text: str, turn_actions: list) -> dict:
    """Detecta possivel alucinacao de acao executada.

    Returns:
      {"flagged": bool, "matched_phrases": [str], "had_write_tool": bool}
    """
    if not final_text or not final_text.strip():
        return {"flagged": False, "matched_phrases": [], "had_write_tool": False}

    matches = _CLAIM_RE.findall(final_text)
    # findall retorna tuplas se houver grupos; flatten + dedupe
    phrases = []
    for m in matches:
        if isinstance(m, tuple):
            phrase = " ".join(p for p in m if p).strip()
        else:
            phrase = m.strip()
        if phrase and phrase not in phrases:
            phrases.append(phrase)

    if not phrases:
        return {"flagged": False, "matched_phrases": [], "had_write_tool": False}

    had_write = _had_successful_write_action(turn_actions)
    return {
        "flagged": not had_write,
        "matched_phrases": phrases[:5],
        "had_write_tool": had_write,
    }


def _is_renato(phone: str) -> bool:
    """Check if the phone belongs to Renato."""
    clean = ''.join(filter(str.isdigit, phone))
    if clean == RENATO_PHONE:
        return True
    for suffix in RENATO_PHONE_SUFFIXES:
        if clean.endswith(suffix):
            return True
    return False


# ==================== TOOL DEFINITIONS (3 meta-tools) ====================

TOOLS = [
    {
        "name": "query_intel",
        "description": (
            "Executa uma query SQL READ-ONLY no banco de dados do INTEL. "
            "Use para buscar QUALQUER informacao: contatos, mensagens, projetos, tarefas, "
            "memorias, calendario, editorial, etc. Apenas SELECT e permitido. "
            "Resultados limitados a 20 linhas. Use ILIKE para buscas case-insensitive. "
            "Para datas relativas use CURRENT_DATE, CURRENT_TIMESTAMP, INTERVAL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL SELECT. Ex: SELECT id, nome FROM contacts WHERE nome ILIKE '%joao%' LIMIT 10"
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "execute_action",
        "description": (
            "Executa uma acao no sistema INTEL. Acoes disponiveis:\n"
            "- create_task: cria tarefa (titulo, descricao?, project_id?, contact_id?, data_vencimento? YYYY-MM-DD, prazo_dias?, prioridade?). "
            "IMPORTANTE: use data_vencimento com data absoluta (ex: '2026-04-24') quando o usuario mencionar 'hoje', 'amanha', uma data especifica. "
            "prazo_dias e apenas fallback quando nao souber a data.\n"
            "- complete_task: conclui tarefa (task_id)\n"
            "- update_task: edita tarefa existente (PATCH-style). params: task_id (int), e SOMENTE os campos que mudaram entre: titulo?, descricao?, data_vencimento? (YYYY-MM-DD), prioridade?, status? ('pending'|'completed'|'cancelled')\n"
            "- postpone_tasks: adia tarefas em MASSA com SQL UPDATE (1 chamada SO — nao loop com update_task). params: nova_data YYYY-MM-DD (obrigatorio), apenas_atrasadas? (bool default true — se false adia todas pendentes), project_id? (filtra por projeto), contact_id? (filtra por contato). Retorna numero afetado.\n"
            "- save_note: salva nota em projeto (project_id, titulo, conteudo, tipo?)\n"
            "- save_memory: salva memoria de contato (contact_id, titulo, resumo, conteudo_completo?, tipo?)\n"
            "- schedule_meeting: cria evento (titulo, data_hora ISO, duracao_min?, contact_id?, local?, descricao?, account?). "
            "account aceita 'personal' (gmail pessoal) ou 'professional' (almeida-prado, default). "
            "Use 'personal' pra eventos pessoais (familia, saude, lazer) e 'professional' pra trabalho/conselhos.\n"
            "- update_calendar_event: edita evento existente (PATCH-style). params: event_id (int=local id), e SOMENTE os campos que mudaram entre: titulo?, data_hora? ISO, duracao_min?, local?, descricao?. "
            "Para eventos recorrentes a edicao afeta apenas a ocorrencia editada (vira exception); pra mudar toda a serie, oriente o usuario a editar no Google Calendar diretamente.\n"
            "- delete_calendar_event: apaga evento. params: event_id (int=local id), scope? ('single'|'future'|'all', default 'single'). "
            "scope='single' apaga so a ocorrencia. scope='future' apaga essa e todas as posteriores (modifica RRULE). scope='all' apaga a serie inteira. "
            "**Se o usuario pediu pra apagar X, apague.** Nao recuse nem peca confirmacao extra. "
            "So pergunte se houver ambiguidade real entre eventos diferentes (ex: 2 eventos com nome similar em datas diferentes — ai pergunte qual). "
            "Pra series recorrentes, se o usuario pediu 'apagar todos' use scope='all' direto. Se pediu 'so essa', use 'single'. Se pediu 'desta data em diante', use 'future'.\n"
            "- send_whatsapp: envia WhatsApp via rap-whatsapp (contact_id, message)\n"
            "- send_email: envia email via Gmail. params: to (email destinatario) OU contact_id (resolve email do contato), subject, body, account? ('personal'|'professional', default 'professional'). "
            "Se passar contact_id, busca o primeiro email do contato. Se passar 'to', usa direto. "
            "**Confirme o destinatario com o usuario antes de enviar** se nao tiver certeza absoluta — emails errados causam dano.\n"
            "- import_fathom_meeting: importa reuniao gravada do Fathom (summary + action items). params: share_url ('https://fathom.video/share/XXX') OU recording_id (int), account? ('personal'|'professional', default 'professional'), project_id? (int, vincula nota ao projeto), include_transcript? (bool, default false). "
            "Identifica contatos via attendees emails (mesmo matching do P4), salva memoria pra cada um, cria tarefas pros action items, retorna resumo do que foi importado. "
            "Use sempre que o user mencionar 'tem gravacao no Fathom' ou colar URL fathom.video.\n"
            "- enrich_contact: enriquece contato com IA (contact_id)\n"
            "- update_contact: atualiza campos do contato (contact_id, fields: {campo: valor})\n"
            "- save_feedback: salva feedback/melhoria do sistema INTEL (conteudo, tipo?: bug|melhoria|ideia|feedback)\n"
            "- save_system_memory: memoria persistente do coach (NAO atrelada a contato — pra decisao de vida, compromisso consigo, padrao observado, reflexao). params: titulo, conteudo, tipo? (decisao|compromisso|padrao|reflexao), tags?\n"
            "- search_system_memories: busca em memorias persistentes (params: query, limit?, mode?). "
            "mode='hybrid' (default) combina keyword + semantic — recomendado pra recall por sinonimos/parafraseamento "
            "(ex: 'drenado' encontra memorias com 'cansado'/'exausto'). "
            "mode='keyword' so faz match literal. mode='semantic' so via embeddings (Voyage).\n"
            "- manage_intent: gerencia um intent aberto. params: intent_id (int), action ('mark_step'|'mark_blocked'|'mark_completed'|'cancel'), details? (str descrevendo o passo/blocker). Use isso quando voce explicitamente fizer progresso, travar, ou completar um intent. Auto-pickup mostra os intents abertos no system prompt.\n"
            "- trigger_cos_patrol: dispara o CoS Patrol Agent (Sonnet 4.6) AGORA pra varrer estado (mensagens, calendar, tasks, RACI) e mandar propostas via WA. **Use quando Renato disser 'patrol', 'patrulha', 'cos agora', 'varre tudo' ou similar.** Sem params. Resposta vem em mensagens separadas se houver acao."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Nome da acao",
                    "enum": [
                        "create_task", "complete_task", "update_task", "postpone_tasks",
                        "save_note", "save_memory",
                        "schedule_meeting", "update_calendar_event", "delete_calendar_event",
                        "send_whatsapp", "send_email",
                        "import_fathom_meeting",
                        "enrich_contact", "update_contact",
                        "save_feedback",
                        "save_system_memory", "search_system_memories",
                        "manage_intent",
                        "trigger_cos_patrol"
                    ]
                },
                "params": {
                    "type": "object",
                    "description": "Parametros da acao (variam por acao)"
                }
            },
            "required": ["action", "params"]
        }
    },
    {
        "name": "query_conselhoos",
        "description": (
            "Executa uma query SQL READ-ONLY no banco de dados do ConselhoOS (sistema de governanca corporativa). "
            "Use para buscar dados de empresas assessoradas, reunioes de conselho, atas, transcricoes, "
            "tarefas RACI, decisoes, pautas e documentos. Apenas SELECT e permitido. "
            "Resultados limitados a 20 linhas. Use ILIKE para buscas case-insensitive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL SELECT. Ex: SELECT e.nome, e.setor FROM empresas e LIMIT 10"
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "execute_conselhoos",
        "description": (
            "Executa uma query SQL de ESCRITA no banco do ConselhoOS (INSERT, UPDATE, DELETE). "
            "Use para criar empresas, reunioes, tarefas RACI, decisoes, pautas, etc.\n"
            "Tabelas principais:\n"
            "- empresas (id UUID, nome, setor, descricao, ativa, created_at)\n"
            "- reunioes (id UUID, empresa_id UUID FK, data DATE, tipo, status, pauta_texto, ata_markdown, transcricao)\n"
            "- decisoes (id UUID, reuniao_id UUID FK, decisao, area, responsavel, prazo DATE, status)\n"
            "- raci (id UUID, reuniao_id UUID FK, tarefa, responsavel_r, aprovador_a, consultado_c, informado_i, prazo DATE, status)\n"
            "- pessoas (id UUID, empresa_id UUID FK, nome, cargo, email, telefone, intel_contact_id INTEGER)\n"
            "- documentos (id UUID, empresa_id UUID FK, tipo, titulo, url, created_at)\n"
            "IMPORTANTE: IDs sao UUID. Use gen_random_uuid() para gerar novos IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL INSERT/UPDATE/DELETE. Ex: INSERT INTO empresas (id, nome, setor) VALUES (gen_random_uuid(), 'Empresa X', 'Tecnologia')"
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "draft_message",
        "description": "Gera um rascunho de mensagem personalizada para um contato, usando contexto completo: mensagens recentes, memorias, LinkedIn, fatos e emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato"
                },
                "context": {
                    "type": "string",
                    "description": "Contexto/objetivo da mensagem (ex: 'follow up da reuniao', 'parabenizar aniversario')"
                }
            },
            "required": ["contact_id", "context"]
        }
    },
    {
        "name": "project_chat",
        "description": (
            "Conversa com o assistente dedicado de um projeto. "
            "O assistente tem contexto completo: tarefas, membros, notas, pareceres, mensagens. "
            "Pode consultar dados e executar acoes (criar tarefas, salvar notas, etc). "
            "Use quando o usuario perguntar sobre um projeto especifico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "ID do projeto (busque antes com query_intel se nao souber)"
                },
                "message": {
                    "type": "string",
                    "description": "Pergunta ou instrucao sobre o projeto"
                }
            },
            "required": ["project_id", "message"]
        }
    },
]


# ==================== TOOL IMPLEMENTATIONS ====================

def _tool_query_intel(sql: str) -> str:
    """Execute a read-only SQL query against the INTEL database."""
    # Security: only allow SELECT statements
    sql_stripped = sql.strip().rstrip(";").strip()
    sql_upper = sql_stripped.upper()

    # Reject non-SELECT queries
    if not sql_upper.startswith("SELECT"):
        return json.dumps({"erro": "Apenas queries SELECT sao permitidas. INSERT/UPDATE/DELETE nao sao aceitos nesta tool. Use execute_action para modificar dados."})

    # Reject dangerous keywords even in subqueries
    dangerous = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE ", "GRANT ", "REVOKE "]
    for kw in dangerous:
        if kw in sql_upper:
            return json.dumps({"erro": f"Query contem operacao proibida: {kw.strip()}"})

    # Ensure LIMIT exists (add LIMIT 20 if missing)
    if "LIMIT" not in sql_upper:
        sql_stripped = sql_stripped + " LIMIT 20"

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql_stripped)
            rows = cursor.fetchall()

        if not rows:
            return json.dumps({"resultado": "Nenhum registro encontrado.", "query": sql_stripped})

        # Format as readable text
        results = [dict(r) for r in rows]
        lines = []
        for i, row in enumerate(results):
            parts = []
            for key, value in row.items():
                if value is not None:
                    # Truncate long values
                    str_val = str(value)
                    if len(str_val) > 200:
                        str_val = str_val[:200] + "..."
                    parts.append(f"{key}: {str_val}")
            lines.append(f"[{i+1}] " + " | ".join(parts))

        return f"Encontrados {len(results)} registros:\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"query_intel error: {e}")
        return json.dumps({"erro": f"Erro SQL: {str(e)}", "query": sql_stripped})


def _tool_query_conselhoos(sql: str) -> str:
    """Execute a read-only SQL query against the ConselhoOS database."""
    # Security: only allow SELECT statements
    sql_stripped = sql.strip().rstrip(";").strip()
    sql_upper = sql_stripped.upper()

    if not sql_upper.startswith("SELECT"):
        return json.dumps({"erro": "Apenas queries SELECT sao permitidas no ConselhoOS."})

    dangerous = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE ", "GRANT ", "REVOKE "]
    for kw in dangerous:
        if kw in sql_upper:
            return json.dumps({"erro": f"Query contem operacao proibida: {kw.strip()}"})

    if "LIMIT" not in sql_upper:
        sql_stripped = sql_stripped + " LIMIT 20"

    conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")
    if not conselhoos_url:
        return json.dumps({"erro": "CONSELHOOS_DATABASE_URL nao configurada"})

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(conselhoos_url, cursor_factory=RealDictCursor)
        try:
            cursor = conn.cursor()
            cursor.execute(sql_stripped)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            return json.dumps({"resultado": "Nenhum registro encontrado.", "query": sql_stripped})

        results = [dict(r) for r in rows]
        lines = []
        for i, row in enumerate(results):
            parts = []
            for key, value in row.items():
                if value is not None:
                    str_val = str(value)
                    if len(str_val) > 200:
                        str_val = str_val[:200] + "..."
                    parts.append(f"{key}: {str_val}")
            lines.append(f"[{i+1}] " + " | ".join(parts))

        return f"Encontrados {len(results)} registros:\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"query_conselhoos error: {e}")
        return json.dumps({"erro": f"Erro SQL ConselhoOS: {str(e)}", "query": sql_stripped})


def _tool_execute_conselhoos(sql: str) -> str:
    """Execute a write SQL query against the ConselhoOS database."""
    sql_stripped = sql.strip().rstrip(";").strip()
    sql_upper = sql_stripped.upper()

    # Block destructive operations
    dangerous = ["DROP ", "TRUNCATE ", "ALTER ", "GRANT ", "REVOKE "]
    for kw in dangerous:
        if kw in sql_upper:
            return json.dumps({"erro": f"Operacao proibida: {kw.strip()}"})

    # Must be INSERT, UPDATE, or DELETE
    allowed_starts = ("INSERT", "UPDATE", "DELETE")
    if not sql_upper.startswith(allowed_starts):
        return json.dumps({"erro": "Apenas INSERT, UPDATE e DELETE sao permitidos. Use query_conselhoos para SELECT."})

    conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")
    if not conselhoos_url:
        return json.dumps({"erro": "CONSELHOOS_DATABASE_URL nao configurada"})

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(conselhoos_url, cursor_factory=RealDictCursor)
        try:
            cursor = conn.cursor()
            cursor.execute(sql_stripped)

            # Try to get RETURNING data
            result_text = f"{cursor.rowcount} registro(s) afetado(s)"
            try:
                rows = cursor.fetchall()
                if rows:
                    results = [dict(r) for r in rows]
                    parts = []
                    for key, value in results[0].items():
                        parts.append(f"{key}: {value}")
                    result_text += "\n" + " | ".join(parts)
            except Exception:
                pass

            conn.commit()
        finally:
            conn.close()

        return json.dumps({"sucesso": True, "resultado": result_text}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"execute_conselhoos error: {e}")
        return json.dumps({"erro": f"Erro SQL ConselhoOS: {str(e)}", "query": sql_stripped})


def _entity_type_for_action(action: str) -> str:
    """Mapeia action para entity_type pra audit_log."""
    if action in ("create_task", "complete_task", "update_task", "postpone_tasks"):
        return "task"
    if action in ("save_note", "save_memory"):
        return "note" if action == "save_note" else "memory"
    if action in ("schedule_meeting", "update_calendar_event", "delete_calendar_event"):
        return "meeting"
    if action in ("update_contact", "enrich_contact"):
        return "contact"
    if action == "send_whatsapp":
        return "message"
    if action == "manage_intent":
        return "agent_intent"
    return "unknown"


def _invalidate_task_caches():
    """Invalida caches do main.py que dependem de tasks state.
    Chamado depois de qualquer write em tasks via execute_action.

    Por que: caches em main.py (TTL 60s) ficam stale quando o bot escreve
    direto no banco. Renato reportou statcard de Projetos divergindo do
    drilldown apos bot rodar postpone_tasks. Padrao identico ao usado em
    main.py:5344 pra _dashboard_cache.

    Falha silenciosa: cache eventualmente expira por TTL."""
    try:
        import main as _main
        for attr in ("_projects_attention_detailed_cache", "_dashboard_cache"):
            if hasattr(_main, attr):
                setattr(_main, attr, {"data": None, "timestamp": None})
        if hasattr(_main, "_all_tasks_cache"):
            _main._all_tasks_cache = {}
    except Exception as e:
        logger.warning(f"cache invalidation failed: {e}")


async def _tool_execute_action(action: str, params: Dict) -> str:
    """Execute a write action on the INTEL system."""
    from services.audit_log import log as audit_log

    audit_log(
        f"intel_bot.{action}",
        entity_type=_entity_type_for_action(action),
        entity_id=params.get("contact_id") or params.get("project_id") or params.get("task_id") or params.get("intent_id"),
        actor="intel_bot",
        details={"params": params},
    )

    try:
        if action == "create_task":
            titulo = params.get("titulo")
            if not titulo:
                return json.dumps({"erro": "titulo e obrigatorio"})
            descricao = params.get("descricao", "")
            project_id = params.get("project_id")
            contact_id = params.get("contact_id")
            prazo_dias = params.get("prazo_dias")
            prioridade = params.get("prioridade", 5)

            data_vencimento = None
            # Prefer absolute date if provided
            dv_str = params.get("data_vencimento")
            if dv_str:
                try:
                    data_vencimento = datetime.strptime(str(dv_str)[:10], "%Y-%m-%d")
                except Exception:
                    pass
            if data_vencimento is None and prazo_dias is not None:
                data_vencimento = (_now_sp() + timedelta(days=prazo_dias)).replace(hour=0, minute=0, second=0, tzinfo=None)

            # Validar FKs antes do INSERT (Claude as vezes alucina IDs)
            # Why: feedback 2026-04-25 — INSERT falhava com FK constraint
            with get_db() as conn:
                cursor = conn.cursor()
                if contact_id is not None:
                    cursor.execute("SELECT 1 FROM contacts WHERE id = %s", (contact_id,))
                    if not cursor.fetchone():
                        return json.dumps({"erro": f"contact_id {contact_id} nao existe; busque o ID correto via query_intel"}, ensure_ascii=False)
                if project_id is not None:
                    cursor.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
                    if not cursor.fetchone():
                        return json.dumps({"erro": f"project_id {project_id} nao existe; busque o ID correto via query_intel"}, ensure_ascii=False)

                cursor.execute("""
                    INSERT INTO tasks (
                        titulo, descricao, project_id, contact_id,
                        data_vencimento, prioridade, ai_generated, origem, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending')
                    RETURNING id
                """, (titulo, descricao, project_id, contact_id, data_vencimento, prioridade))
                task = cursor.fetchone()
                conn.commit()
            _invalidate_task_caches()

            date_str = f" para {data_vencimento.strftime('%d/%m %H:%M')}" if data_vencimento else ""
            proj_str = f" no projeto #{project_id}" if project_id else ""
            return json.dumps({
                "sucesso": True,
                "task_id": task["id"],
                "mensagem": f"Tarefa #{task['id']} criada: {titulo}{proj_str}{date_str}"
            }, ensure_ascii=False)

        elif action == "complete_task":
            task_id = params.get("task_id")
            if not task_id:
                return json.dumps({"erro": "task_id e obrigatorio"})

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tasks SET status = 'completed', data_conclusao = NOW()
                    WHERE id = %s AND status = 'pending'
                    RETURNING id, titulo
                """, (task_id,))
                task = cursor.fetchone()
                conn.commit()
            _invalidate_task_caches()

            if not task:
                return json.dumps({"erro": f"Tarefa #{task_id} nao encontrada ou ja concluida"})
            return json.dumps({
                "sucesso": True,
                "mensagem": f"Tarefa #{task['id']} concluida: {task['titulo']}"
            }, ensure_ascii=False)

        elif action == "update_task":
            task_id = params.get("task_id")
            if not task_id:
                return json.dumps({"erro": "task_id e obrigatorio"})

            allowed = {"titulo", "descricao", "data_vencimento", "prioridade", "status"}
            valid_status = {"pending", "completed", "cancelled"}

            sets = []
            values = []
            for field in allowed:
                if field not in params:
                    continue
                value = params[field]
                if field == "data_vencimento" and value:
                    try:
                        value = datetime.strptime(str(value)[:10], "%Y-%m-%d")
                    except Exception:
                        return json.dumps({"erro": f"data_vencimento invalida: {value}. Use YYYY-MM-DD"}, ensure_ascii=False)
                if field == "status" and value not in valid_status:
                    return json.dumps({"erro": f"status invalido: {value}. Use {sorted(valid_status)}"}, ensure_ascii=False)
                sets.append(f"{field} = %s")
                values.append(value)

            if not sets:
                return json.dumps({"erro": "Nenhum campo pra atualizar. Passe titulo, descricao, data_vencimento, prioridade ou status."}, ensure_ascii=False)

            # Auto-set data_conclusao quando status vira completed
            if params.get("status") == "completed":
                sets.append("data_conclusao = NOW()")

            values.append(task_id)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    UPDATE tasks SET {', '.join(sets)}
                    WHERE id = %s
                    RETURNING id, titulo, data_vencimento, status, prioridade
                """, tuple(values))
                task = cursor.fetchone()
                conn.commit()
            _invalidate_task_caches()

            if not task:
                return json.dumps({"erro": f"Tarefa #{task_id} nao encontrada"})

            changed = [f for f in allowed if f in params]
            return json.dumps({
                "sucesso": True,
                "task_id": task["id"],
                "campos_atualizados": changed,
                "mensagem": f"Tarefa #{task['id']} atualizada ({', '.join(changed)}): {task['titulo']}"
            }, ensure_ascii=False, default=str)

        elif action == "postpone_tasks":
            nova_data_str = params.get("nova_data")
            if not nova_data_str:
                return json.dumps({"erro": "nova_data e obrigatoria (formato YYYY-MM-DD)"})
            try:
                nova_data = datetime.strptime(str(nova_data_str)[:10], "%Y-%m-%d")
            except Exception:
                return json.dumps({"erro": f"nova_data invalida: {nova_data_str}. Use YYYY-MM-DD"}, ensure_ascii=False)

            apenas_atrasadas = params.get("apenas_atrasadas", True)
            project_id = params.get("project_id")
            contact_id = params.get("contact_id")

            where = ["status = 'pending'"]
            sql_values = [nova_data]
            if apenas_atrasadas:
                where.append("data_vencimento IS NOT NULL AND data_vencimento AT TIME ZONE 'America/Sao_Paulo' < NOW()")
            if project_id is not None:
                where.append("project_id = %s")
                sql_values.append(project_id)
            if contact_id is not None:
                where.append("contact_id = %s")
                sql_values.append(contact_id)

            sql = f"""
                UPDATE tasks SET data_vencimento = %s
                WHERE {' AND '.join(where)}
                RETURNING id, titulo
            """

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, tuple(sql_values))
                rows = cursor.fetchall()
                conn.commit()
            _invalidate_task_caches()

            count = len(rows)
            if count == 0:
                return json.dumps({
                    "sucesso": True,
                    "afetadas": 0,
                    "mensagem": "Nenhuma tarefa correspondia aos filtros (nada pra adiar)."
                }, ensure_ascii=False)

            sample = [{"id": r["id"], "titulo": r["titulo"]} for r in rows[:5]]
            extra = f" (+{count-5} outras)" if count > 5 else ""
            return json.dumps({
                "sucesso": True,
                "afetadas": count,
                "amostra": sample,
                "mensagem": f"{count} tarefa(s) adiada(s) pra {nova_data.strftime('%d/%m/%Y')}{extra}"
            }, ensure_ascii=False, default=str)

        elif action == "save_note":
            project_id = params.get("project_id")
            titulo = params.get("titulo", "Nota via Bot")
            conteudo = params.get("conteudo", "")
            tipo = params.get("tipo", "insight")

            if not project_id:
                # Use first active project
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC LIMIT 1")
                    row = cursor.fetchone()
                    project_id = row["id"] if row else None

            if not project_id:
                return json.dumps({"erro": "Nenhum projeto ativo encontrado"})

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
                    VALUES (%s, %s, %s, %s, 'Renato (via Bot)')
                    RETURNING id
                """, (project_id, tipo, titulo, conteudo))
                note = cursor.fetchone()
                conn.commit()

            return json.dumps({
                "sucesso": True,
                "note_id": note["id"],
                "mensagem": f"Nota '{titulo}' salva no projeto #{project_id}"
            }, ensure_ascii=False)

        elif action == "save_memory":
            contact_id = params.get("contact_id")
            if not contact_id:
                return json.dumps({"erro": "contact_id e obrigatorio"})
            titulo = params.get("titulo", "Memoria via Bot")
            resumo = params.get("resumo", "")
            conteudo_completo = params.get("conteudo_completo", resumo)
            tipo = params.get("tipo", "insight")

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM contacts WHERE id = %s", (contact_id,))
                if not cursor.fetchone():
                    return json.dumps({"erro": f"contact_id {contact_id} nao existe; busque o ID correto via query_intel"}, ensure_ascii=False)
                cursor.execute("""
                    INSERT INTO contact_memories (contact_id, tipo, titulo, resumo, conteudo_completo, data_ocorrencia)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    RETURNING id
                """, (contact_id, tipo, titulo, resumo, conteudo_completo))
                mem = cursor.fetchone()
                conn.commit()

            return json.dumps({
                "sucesso": True,
                "memory_id": mem["id"],
                "mensagem": f"Memoria '{titulo}' salva para contato #{contact_id}"
            }, ensure_ascii=False)

        elif action == "schedule_meeting":
            titulo = params.get("titulo")
            data_hora = params.get("data_hora")
            if not titulo or not data_hora:
                return json.dumps({"erro": "titulo e data_hora sao obrigatorios"})

            duracao_min = params.get("duracao_min", 60)
            contact_id = params.get("contact_id")
            local = params.get("local")
            # Multi-conta: 'personal' | 'professional' (default).
            # Resolve em email via google_accounts.tipo. Sem conta? deixa NULL
            # (calendar_sync._resolve_account_email faz fallback profissional).
            account_alias = (params.get("account") or "professional").lower()
            account_email = None
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT email FROM google_accounts WHERE conectado=TRUE AND tipo=%s LIMIT 1",
                        (account_alias,),
                    )
                    row = cur.fetchone()
                    if row:
                        account_email = row["email"]
            except Exception as e:
                logger.warning(f"schedule_meeting account lookup err: {e}")

            try:
                start_dt = datetime.fromisoformat(data_hora.replace("Z", "+00:00"))
            except ValueError:
                return json.dumps({"erro": f"Formato de data invalido: {data_hora}. Use ISO 8601 (ex: 2026-04-15T10:00:00)"})

            end_dt = start_dt + timedelta(minutes=duracao_min)

            from services.calendar_events import get_calendar_events
            cal = get_calendar_events()
            event = await cal.create_event(
                summary=titulo,
                start_datetime=start_dt,
                end_datetime=end_dt,
                location=local,
                contact_id=contact_id,
                create_in_google=True,
                account_email=account_email,
            )

            # Auto-resolve matching tasks
            try:
                from services.task_auto_resolver import check_and_resolve_tasks
                await check_and_resolve_tasks("meeting_created", {
                    "contact_id": contact_id,
                    "contact_name": params.get("contact_name", ""),
                    "subject": titulo,
                })
            except Exception as e:
                logger.error(f"Task auto-resolve error (meeting): {e}")

            account_label = "pessoal" if account_alias == "personal" else "profissional"
            return json.dumps({
                "sucesso": True,
                "event_id": event.get("id"),
                "google_account_email": account_email,
                "mensagem": f"Evento '{titulo}' criado em {start_dt.strftime('%d/%m %H:%M')} ({duracao_min}min) no calendario {account_label}"
            }, ensure_ascii=False)

        elif action == "update_calendar_event":
            event_id = params.get("event_id")
            if event_id is None:
                return json.dumps({"erro": "event_id e obrigatorio"})
            try:
                event_id = int(event_id)
            except (TypeError, ValueError):
                return json.dumps({"erro": f"event_id invalido: {event_id}. Use o id local (int) do calendar_events."})

            from services.calendar_events import get_calendar_events
            cal = get_calendar_events()
            current = cal.get_event(event_id)
            if not current:
                return json.dumps({"erro": f"Evento #{event_id} nao encontrado"})

            # PATCH-style: aplicado direto na ocorrencia (instance vira exception
            # se for recorrente). Nao expomos "todas as futuras" pra update.

            # Monta updates apenas com campos passados (PATCH-style)
            updates = {}
            if "titulo" in params:
                updates["summary"] = params["titulo"]
            if "local" in params:
                updates["location"] = params["local"]
            if "descricao" in params:
                updates["description"] = params["descricao"]

            new_start_dt = None
            new_end_dt = None
            if "data_hora" in params:
                try:
                    new_start_dt = datetime.fromisoformat(str(params["data_hora"]).replace("Z", "+00:00"))
                except ValueError:
                    return json.dumps({"erro": f"data_hora invalida: {params['data_hora']}. Use ISO 8601."})
                updates["start_datetime"] = new_start_dt

            if "duracao_min" in params or new_start_dt is not None:
                duracao = params.get("duracao_min")
                base_start = new_start_dt
                if base_start is None and current.get("start_datetime"):
                    try:
                        base_start = datetime.fromisoformat(current["start_datetime"].replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        base_start = None
                if duracao is None and current.get("start_datetime") and current.get("end_datetime"):
                    try:
                        old_start = datetime.fromisoformat(current["start_datetime"].replace("Z", "+00:00"))
                        old_end = datetime.fromisoformat(current["end_datetime"].replace("Z", "+00:00"))
                        duracao = max(int((old_end - old_start).total_seconds() / 60), 15)
                    except (ValueError, AttributeError):
                        duracao = 60
                if base_start is not None and duracao is not None:
                    new_end_dt = base_start + timedelta(minutes=int(duracao))
                    updates["end_datetime"] = new_end_dt

            if not updates:
                return json.dumps({"erro": "Nenhum campo para atualizar foi passado"})

            # NOTE: scope=single em recorrentes — sync_to_google atualmente patch
            # via google_event_id local (que e o instance_id se for ocorrencia
            # exception, ou o master id se nao). Para edicao em "all", precisa
            # apontar pro master — passamos via override.
            updated = await cal.update_event(event_id, updates, sync_to_google=True)
            if not updated:
                return json.dumps({"erro": "Falha ao atualizar evento"})

            changed = ", ".join(updates.keys())
            return json.dumps({
                "sucesso": True,
                "event_id": event_id,
                "mensagem": f"Evento '{updated.get('summary')}' atualizado ({changed})"
            }, ensure_ascii=False)

        elif action == "delete_calendar_event":
            event_id = params.get("event_id")
            if event_id is None:
                return json.dumps({"erro": "event_id e obrigatorio"})
            try:
                event_id = int(event_id)
            except (TypeError, ValueError):
                return json.dumps({"erro": f"event_id invalido: {event_id}. Use o id local (int)."})

            from services.calendar_events import get_calendar_events
            cal = get_calendar_events()
            current = cal.get_event(event_id)
            if not current:
                return json.dumps({"erro": f"Evento #{event_id} nao encontrado"})

            # Recorrencia: tabela local pode estar com recurring_event_id NULL
            # (bug pre-2026-05-07: _upsert_event nao populava). Fonte de verdade
            # passa a ser a Google Calendar API, que retorna erro claro se scope
            # nao se aplica. Aqui apenas validamos o scope passado e respeitamos
            # a intencao do usuario sem sobrescrever.
            scope = params.get("scope") or "single"
            if scope not in ("single", "future", "all"):
                return json.dumps({
                    "erro": f"scope invalido: {scope}. Use 'single', 'future' ou 'all'."
                }, ensure_ascii=False)

            ok = await cal.delete_event(event_id, delete_from_google=True, scope=scope)
            if not ok:
                return json.dumps({"erro": "Falha ao apagar evento"})

            return json.dumps({
                "sucesso": True,
                "event_id": event_id,
                "scope_aplicado": scope,
                "mensagem": f"Evento '{current.get('summary')}' apagado (scope: {scope})"
            }, ensure_ascii=False)

        elif action == "send_whatsapp":
            contact_id = params.get("contact_id")
            message = params.get("message")
            if not contact_id or not message:
                return json.dumps({"erro": "contact_id e message sao obrigatorios"})

            from integrations.evolution_api import get_evolution_client

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, nome, telefones FROM contacts WHERE id = %s", (contact_id,))
                contact = cursor.fetchone()

            if not contact:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})

            contact = dict(contact)
            phones = contact.get("telefones")
            if isinstance(phones, str):
                try:
                    phones = json.loads(phones)
                except:
                    phones = []

            if not phones or (isinstance(phones, list) and not phones):
                return json.dumps({"erro": f"Contato {contact['nome']} nao tem telefone"})

            phone = phones[0] if isinstance(phones, list) else str(phones)
            phone_clean = ''.join(filter(str.isdigit, str(phone)))

            client = get_evolution_client()
            result = await client.send_text(phone_clean, message, instance_name="rap-whatsapp")

            if "error" not in result:
                # Auto-resolve matching tasks
                try:
                    from services.task_auto_resolver import check_and_resolve_tasks
                    await check_and_resolve_tasks("whatsapp_sent", {
                        "contact_id": contact_id,
                        "contact_name": contact['nome'],
                    })
                except Exception as e:
                    logger.error(f"Task auto-resolve error (whatsapp): {e}")

                return json.dumps({
                    "sucesso": True,
                    "mensagem": f"Mensagem enviada para {contact['nome']}"
                }, ensure_ascii=False)
            else:
                return json.dumps({"erro": f"Falha ao enviar: {result.get('error', 'desconhecido')}"})

        elif action == "send_email":
            to = params.get("to")
            contact_id_param = params.get("contact_id")
            subject = params.get("subject")
            body = params.get("body")
            if not subject or not body:
                return json.dumps({"erro": "subject e body sao obrigatorios"})
            if not to and not contact_id_param:
                return json.dumps({"erro": "passe 'to' (email) ou 'contact_id'"})

            # Resolve destinatario via contact_id se necessario
            recipient_email = to
            recipient_name = None
            if contact_id_param and not recipient_email:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id, nome, emails FROM contacts WHERE id = %s", (contact_id_param,))
                    contact = cur.fetchone()
                if not contact:
                    return json.dumps({"erro": f"Contato #{contact_id_param} nao encontrado"})
                contact = dict(contact)
                recipient_name = contact["nome"]
                emails = contact.get("emails")
                if isinstance(emails, str):
                    try:
                        emails = json.loads(emails)
                    except Exception:
                        emails = []
                if not emails:
                    return json.dumps({"erro": f"Contato {contact['nome']} nao tem email cadastrado"})
                # emails e [{"email": "x@y.com", "tipo": "..."}] ou string array
                first = emails[0] if isinstance(emails, list) else None
                if isinstance(first, dict):
                    recipient_email = first.get("email")
                elif isinstance(first, str):
                    recipient_email = first
            if not recipient_email or "@" not in str(recipient_email):
                return json.dumps({"erro": f"Email destinatario invalido: {recipient_email}"})

            # Resolve conta Gmail (personal | professional)
            account_alias = (params.get("account") or "professional").lower()
            account_email = None
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT email, access_token FROM google_accounts WHERE conectado=TRUE AND tipo=%s LIMIT 1",
                        (account_alias,),
                    )
                    row = cur.fetchone()
                    if row:
                        account_email = row["email"]
            except Exception as e:
                logger.warning(f"send_email account lookup err: {e}")
            if not account_email:
                return json.dumps({"erro": f"Conta Gmail '{account_alias}' nao conectada"})

            # Pega token valido (refresh automatico se expirou)
            from integrations.google_contacts import get_valid_token
            from integrations.gmail import GmailIntegration
            try:
                token = await get_valid_token(account_email)
            except Exception as e:
                return json.dumps({"erro": f"Falha ao obter token Gmail: {e}"})

            gmail = GmailIntegration()
            result = await gmail.send_message(
                access_token=token,
                to=recipient_email,
                subject=subject,
                body=body,
            )
            if "error" in result:
                return json.dumps({"erro": f"Gmail send falhou: {result.get('error')}"})

            account_label = "pessoal" if account_alias == "personal" else "profissional"
            destinatario = f"{recipient_name} <{recipient_email}>" if recipient_name else recipient_email
            return json.dumps({
                "sucesso": True,
                "gmail_message_id": result.get("id"),
                "from_account": account_email,
                "mensagem": f"Email enviado para {destinatario} via conta {account_label}"
            }, ensure_ascii=False)

        elif action == "import_fathom_meeting":
            share_url = params.get("share_url")
            recording_id = params.get("recording_id")
            project_id = params.get("project_id")
            account_alias = (params.get("account") or "professional").lower()

            if not share_url and not recording_id:
                return json.dumps({"erro": "passe share_url OU recording_id"})

            from integrations.fathom import FathomIntegration, process_fathom_meeting
            try:
                fathom = FathomIntegration(account=account_alias)
                if not fathom.api_key:
                    return json.dumps({"erro": f"Conta Fathom '{account_alias}' nao configurada"})

                # Busca o objeto Meeting bruto (process_fathom_meeting espera o
                # payload cru do /meetings, com calendar_invitees + default_summary
                # + action_items inline).
                if share_url:
                    adapted = await fathom.extract_from_share_link(share_url)
                    if not adapted or not adapted.get("recording_id"):
                        return json.dumps({"erro": "Reuniao nao encontrada via share_url"})
                    raw = await fathom.get_meeting_details(adapted["recording_id"])
                else:
                    try:
                        rec_id = int(recording_id)
                    except (TypeError, ValueError):
                        return json.dumps({"erro": f"recording_id invalido: {recording_id}"})
                    raw = await fathom.get_meeting_details(rec_id)

                if not raw:
                    return json.dumps({"erro": "Reuniao nao encontrada na conta selecionada"})

                stats = await process_fathom_meeting(raw, project_id=project_id)

                matched = stats.get("matched_contacts") or []
                novas_mem = stats.get("memorias_criadas") or []
                novas_tar = stats.get("tarefas_criadas") or []
                skipped = stats.get("skipped") or {}
                title = stats.get("title") or "Reuniao Fathom"

                pieces = [f"{len(novas_mem)} memorias", f"{len(novas_tar)} tarefas"]
                if skipped.get("memorias") or skipped.get("tarefas"):
                    pieces.append(
                        f"({skipped.get('memorias', 0)} memorias + "
                        f"{skipped.get('tarefas', 0)} tarefas ja existiam — dedup)"
                    )
                if stats.get("nota_projeto_id"):
                    pieces.append(f"+ nota no projeto #{project_id}")
                contatos_str = (
                    ", ".join([c["nome"] for c in matched]) if matched else "nenhum identificado"
                )

                return json.dumps({
                    "sucesso": True,
                    **stats,
                    "mensagem": f"Reuniao '{title}' importada: " + " ".join(pieces) +
                                f". Contatos: {contatos_str}",
                }, ensure_ascii=False)

            except Exception as e:
                logger.exception("import_fathom_meeting failed")
                return json.dumps({"erro": f"Falha ao importar reuniao Fathom: {type(e).__name__}: {e}"})

        elif action == "enrich_contact":
            contact_id = params.get("contact_id")
            if not contact_id:
                return json.dumps({"erro": "contact_id e obrigatorio"})

            from services.contact_enrichment import enrich_contact_with_ai

            with get_db() as conn:
                result = await enrich_contact_with_ai(contact_id, conn)

            return json.dumps({
                "sucesso": True,
                "mensagem": f"Contato #{contact_id} enriquecido com IA",
                "resultado": {k: str(v)[:100] for k, v in result.items()} if isinstance(result, dict) else str(result)[:200]
            }, ensure_ascii=False)

        elif action == "update_contact":
            contact_id = params.get("contact_id")
            fields = params.get("fields", {})
            if not contact_id or not fields:
                return json.dumps({"erro": "contact_id e fields sao obrigatorios"})

            # Whitelist of updatable fields
            allowed = {
                "nome", "apelido", "empresa", "cargo", "emails", "telefones",
                "linkedin", "circulo", "relationship_context", "manual_notes",
                "company_website", "contexto"
            }
            safe_fields = {k: v for k, v in fields.items() if k in allowed}
            if not safe_fields:
                return json.dumps({"erro": f"Nenhum campo permitido. Campos validos: {', '.join(sorted(allowed))}"})

            set_clauses = []
            values = []
            for k, v in safe_fields.items():
                set_clauses.append(f"{k} = %s")
                values.append(v)
            values.append(contact_id)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE contacts SET {', '.join(set_clauses)}, atualizado_em = CURRENT_TIMESTAMP WHERE id = %s RETURNING id, nome",
                    values
                )
                updated = cursor.fetchone()
                conn.commit()

            if not updated:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})

            # Auto-resolve matching tasks (e.g. "pegar email de X")
            try:
                from services.task_auto_resolver import check_and_resolve_tasks
                await check_and_resolve_tasks("contact_updated", {
                    "contact_id": contact_id,
                    "contact_name": updated['nome'],
                    "fields_updated": list(safe_fields.keys()),
                })
            except Exception as e:
                logger.error(f"Task auto-resolve error (contact_update): {e}")

            return json.dumps({
                "sucesso": True,
                "mensagem": f"Contato {updated['nome']} atualizado: {', '.join(safe_fields.keys())}"
            }, ensure_ascii=False)

        elif action == "save_feedback":
            conteudo = params.get("conteudo", "")
            tipo = params.get("tipo", "feedback")
            if not conteudo:
                return json.dumps({"erro": "conteudo e obrigatorio"})

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO system_feedback (tipo, conteudo) VALUES (%s, %s)
                    RETURNING id
                """, (tipo, conteudo))
                fb_id = cursor.fetchone()["id"]
                conn.commit()

            return json.dumps({
                "sucesso": True,
                "mensagem": f"Feedback #{fb_id} registrado ({tipo}). Sera analisado na proxima sessao de desenvolvimento."
            }, ensure_ascii=False)

        elif action == "save_system_memory":
            # Memória persistente do coach (não atrelada a contato).
            # Pra: decisões de vida, compromissos consigo, padrões, reflexões.
            from services.system_memory import save_system_memory
            titulo = params.get("titulo", "").strip()
            conteudo = params.get("conteudo", "").strip()
            tipo = params.get("tipo", "reflexao")
            tags = params.get("tags") or []
            if not titulo or not conteudo:
                return json.dumps({"erro": "titulo e conteudo sao obrigatorios"})
            mid = save_system_memory(
                titulo=titulo, conteudo=conteudo, tipo=tipo, tags=tags,
                fonte="chat",
            )
            if mid:
                return json.dumps({
                    "sucesso": True,
                    "memoria_id": mid,
                    "mensagem": f"Memória #{mid} salva (tipo: {tipo}). Vai aparecer no contexto das próximas conversas."
                }, ensure_ascii=False)
            return json.dumps({"erro": "falha ao salvar memoria"})

        elif action == "search_system_memories":
            from services.system_memory import search_memories
            query = params.get("query", "").strip()
            limit = int(params.get("limit", 10))
            mode = (params.get("mode") or "hybrid").lower()
            if mode not in ("hybrid", "keyword", "semantic"):
                mode = "hybrid"
            results = search_memories(query, limit=limit, mode=mode)
            return json.dumps({"mode": mode, "resultados": [
                {"id": r["id"], "titulo": r["titulo"], "tipo": r["tipo"],
                 "conteudo": r["conteudo"][:500],
                 "similarity": float(r["similarity"]) if r.get("similarity") is not None else None,
                 "data": r["criado_em"].isoformat() if r.get("criado_em") else None}
                for r in results
            ]}, ensure_ascii=False, default=str)

        elif action == "manage_intent":
            # P6 Diligente Fase 2: bot pode explicitamente atualizar/fechar/cancelar
            # intent. Auto-pickup ja mostra os abertos no system prompt; usar quando
            # o bot quiser fazer progresso explicito (vs implicito via detector).
            intent_id = params.get("intent_id")
            sub_action = (params.get("action") or "").strip()
            details = (params.get("details") or "").strip()
            if not intent_id:
                return json.dumps({"erro": "intent_id e obrigatorio"})
            if sub_action not in ("mark_step", "mark_blocked", "mark_completed", "cancel"):
                return json.dumps({
                    "erro": f"action invalida: {sub_action}. Use mark_step|mark_blocked|mark_completed|cancel"
                })

            from services.agent_intents import (
                append_step as _append_step,
                update_intent as _update_intent,
                cancel_intent as _cancel_intent,
            )
            try:
                if sub_action == "mark_step":
                    if not details:
                        return json.dumps({"erro": "details obrigatorio pra mark_step"}, ensure_ascii=False)
                    step = {"kind": "manual_bot_step", "details": details[:300]}
                    result = _append_step(int(intent_id), step, status="in_progress")
                    msg = f"Intent #{intent_id} atualizado: passo registrado."
                elif sub_action == "mark_blocked":
                    if not details:
                        return json.dumps({"erro": "details obrigatorio pra mark_blocked (motivo)"}, ensure_ascii=False)
                    result = _update_intent(int(intent_id), status="blocked", blocker=details[:300])
                    msg = f"Intent #{intent_id} marcado como blocked."
                elif sub_action == "mark_completed":
                    result = _update_intent(int(intent_id), status="completed")
                    msg = f"Intent #{intent_id} marcado como completed."
                else:  # cancel
                    result = _cancel_intent(int(intent_id))
                    msg = f"Intent #{intent_id} cancelado."
            except ValueError as ve:
                return json.dumps({"erro": str(ve)}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"erro": f"falha em manage_intent: {e}"}, ensure_ascii=False)

            # Audit obrigatorio (per AUTONOMY_POLICY.md — manage_intent toca estado).
            try:
                from services.agent_actions import log_action
                log_action(
                    action_type=f"agent_intent.{sub_action}",
                    category="system",
                    title=f"Intent #{intent_id} — {sub_action}",
                    details=details[:300] if details else None,
                    scope_ref={"intent_id": int(intent_id)},
                    source="intel_bot.manage_intent",
                    payload={"action": sub_action, "details": details[:500]},
                )
            except Exception as e:
                logger.warning(f"manage_intent log_action failed: {e}")

            return json.dumps({
                "sucesso": True,
                "intent_id": int(intent_id),
                "action": sub_action,
                "status": result.get("status") if isinstance(result, dict) else None,
                "mensagem": msg,
            }, ensure_ascii=False, default=str)

        elif action == "trigger_cos_patrol":
            # Dispara um tick do CoS Patrol Agent na hora (uso: "patrol", "patrulha", "cos agora").
            # tick_safe ja tem budget cap diario ($0.50) e idempotency interna.
            try:
                from services.cos_sensor import tick_safe
                result = tick_safe()
                return json.dumps({
                    "sucesso": True,
                    "patrol_result": result,
                    "mensagem": "CoS Patrol disparado. Aguarde — se houver acao, voce recebe mensagens em instantes.",
                }, ensure_ascii=False, default=str)
            except Exception as e:
                logger.exception("trigger_cos_patrol failed")
                return json.dumps({"erro": f"falha disparando patrol: {e}"}, ensure_ascii=False)

        else:
            return json.dumps({"erro": f"Acao desconhecida: {action}"})

    except Exception as e:
        logger.error(f"execute_action error ({action}): {e}")
        return json.dumps({"erro": str(e)})


async def _tool_draft_message(contact_id: int, context: str) -> str:
    """Draft a personalized message using full AI-enriched contact context."""
    try:
        from services.contact_enrichment import get_contact_context, format_messages_for_ai

        with get_db() as conn:
            cursor = conn.cursor()

            # Contact info
            cursor.execute("""
                SELECT id, nome, empresa, cargo, linkedin_headline, linkedin_about,
                       linkedin_location, linkedin_experience, relationship_context,
                       resumo_ai, ultimo_contato, circulo
                FROM contacts WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})
            contact = dict(contact)

            # Full enriched context (same as "Enriquecer com IA" button)
            full_context = await get_contact_context(contact_id, conn)

            # Format all data sources
            whatsapp_text = format_messages_for_ai(
                full_context.get("whatsapp_messages", []), contact["nome"], "WhatsApp"
            ) or "Sem mensagens WhatsApp"

            email_text = format_messages_for_ai(
                full_context.get("email_messages", []), contact["nome"], "Email"
            ) or "Sem emails"

            facts_text = "\n".join(
                f"- [{f.get('categoria', '?')}] {f.get('fato', '')}"
                for f in full_context.get("existing_facts", [])
            ) or "Sem fatos registrados"

            memories_text = "\n".join(
                f"- {m.get('titulo', '?')}: {m.get('resumo', '')[:100]}"
                for m in full_context.get("memories", [])
            ) or "Sem memorias"

        # Build rich context
        contact_ctx = f"Nome: {contact['nome']}, Empresa: {contact.get('empresa', '?')}, Cargo: {contact.get('cargo', '?')}"
        if contact.get("linkedin_headline"):
            contact_ctx += f"\nLinkedIn: {contact['linkedin_headline']}"
        if contact.get("linkedin_about"):
            contact_ctx += f"\nSobre: {contact['linkedin_about'][:200]}"
        if contact.get("relationship_context"):
            contact_ctx += f"\nContexto do relacionamento: {contact['relationship_context']}"
        if contact.get("resumo_ai"):
            contact_ctx += f"\nResumo IA: {contact['resumo_ai'][:200]}"
        if contact.get("ultimo_contato"):
            contact_ctx += f"\nUltimo contato: {contact['ultimo_contato']}"

        system = f"""Voce e o assistente de Renato Prado. Escreva um rascunho de mensagem WhatsApp para o contato abaixo.
A mensagem deve ser natural, no tom do Renato (profissional mas cordial), em portugues.
Use o contexto completo do relacionamento para personalizar.

CONTATO:
{contact_ctx}

WHATSAPP (historico):
{whatsapp_text[:500]}

EMAILS:
{email_text[:300]}

FATOS CONHECIDOS:
{facts_text}

MEMORIAS:
{memories_text}

OBJETIVO: {context}

REGRAS CRITICAS:
- NUNCA invente fatos. Se nao sabe se a pessoa curtiu, comentou ou fez algo, NAO mencione.
- Use APENAS informacoes que estao nos dados acima.
- Se o objetivo menciona "meu post", inclua o link se disponivel no contexto.
- Escreva no tom do Renato: profissional, cordial, direto.

Escreva APENAS a mensagem, pronta para enviar. Sem explicacoes."""

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 300,
                    "system": system,
                    "messages": [{"role": "user", "content": f"Escreva a mensagem para {contact['nome']}: {context}"}],
                },
            )

            if response.status_code != 200:
                return json.dumps({"erro": "Falha ao gerar rascunho"})

            result = response.json()
            draft = result.get("content", [{}])[0].get("text", "").strip()

        return json.dumps({
            "rascunho": draft,
            "contato": contact["nome"],
            "contact_id": contact_id
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"draft_message error: {e}")
        return json.dumps({"erro": str(e)})


async def _execute_tool(name: str, input_data: Dict) -> str:
    """Execute a tool by name and return the result as string."""
    try:
        if name == "query_intel":
            return _tool_query_intel(input_data["sql"])
        elif name == "query_conselhoos":
            return _tool_query_conselhoos(input_data["sql"])
        elif name == "execute_conselhoos":
            return _tool_execute_conselhoos(input_data["sql"])
        elif name == "execute_action":
            return await _tool_execute_action(input_data["action"], input_data.get("params", {}))
        elif name == "draft_message":
            return await _tool_draft_message(input_data["contact_id"], input_data["context"])
        elif name == "project_chat":
            from services.project_assistant import chat as project_chat
            result = await project_chat(input_data["project_id"], input_data["message"])
            return result.get("response", result.get("error", "Sem resposta"))
        else:
            return json.dumps({"erro": f"Tool desconhecida: {name}"})
    except Exception as e:
        logger.error(f"Tool execution error ({name}): {e}")
        return json.dumps({"erro": str(e)})


# ==================== CONVERSATION MEMORY ====================

def _get_active_cos_proposal(phone: str, hours: int = 24) -> Optional[Dict]:
    """Ultima proposta CoS Patrol enviada pra este phone nas ultimas N horas.

    Usado em handle_bot_message pra orientar o bot conversacional a interpretar
    a resposta como decisao sobre a proposta especifica. Retorna dict ou None.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, content, tool_calls, created_at
                FROM bot_conversations
                WHERE phone = %s
                  AND role = 'assistant'
                  AND tool_calls IS NOT NULL
                  AND tool_calls->>'cos_patrol' = 'true'
                  AND created_at > NOW() - (%s || ' hours')::interval
                ORDER BY created_at DESC LIMIT 1
                """,
                (phone, str(hours)),
            )
            row = cursor.fetchone()
            if not row:
                return None
            row = dict(row)
            tc = row.get("tool_calls") or {}
            if isinstance(tc, str):
                try:
                    tc = json.loads(tc)
                except Exception:
                    tc = {}
            # Auto-resolve por outgoing: se proposta tem contact_id e Renato JA
            # enviou outgoing (WA OU email) pra esse contato APOS criar a proposta,
            # considera fechada. Fecha gap de action blindness.
            proposal_contact_id = tc.get("contact_id")
            if proposal_contact_id:
                cursor.execute(
                    """
                    SELECT 1 FROM messages m
                    JOIN conversations cv ON cv.id = m.conversation_id
                    WHERE cv.contact_id = %s
                      AND m.direcao = 'outgoing'
                      AND m.enviado_em > %s
                    LIMIT 1
                    """,
                    (proposal_contact_id, row["created_at"]),
                )
                if cursor.fetchone():
                    return None
            age_hours = None
            try:
                age_hours = round((datetime.now() - row["created_at"]).total_seconds() / 3600, 1)
            except Exception:
                pass
            return {
                "id": row["id"],
                "content": row["content"],
                "proposed_action": tc.get("proposed_action") or {},
                "options": tc.get("options") or [],
                "urgency": tc.get("urgency"),
                "contact_id": tc.get("contact_id"),
                "age_hours": age_hours,
            }
    except Exception as e:
        logger.warning(f"_get_active_cos_proposal failed: {e}")
        return None


def _load_conversation_history(phone: str, limit: int = 20) -> List[Dict]:
    """Load recent conversation messages for this phone."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content, tool_calls, tool_results
                FROM bot_conversations
                WHERE phone = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (phone, limit))
            rows = [dict(r) for r in cursor.fetchall()]

        # Reverse to chronological order
        rows.reverse()
        return rows
    except Exception as e:
        logger.error(f"Error loading conversation history: {e}")
        return []


def _save_conversation_message(phone: str, role: str, content: str,
                                tool_calls: Any = None, tool_results: Any = None):
    """Save a single message to conversation history. Retorna o id inserido (ou None se garbage/erro)."""
    # Don't save garbage messages
    if not content or not content.strip():
        return None
    garbage = ['demorou demais para processar', 'Erro interno', 'Tenta de novo?',
               '__IMAGE_PENDING__', '__AUDIO_PENDING__', 'Busquei no sistema mas não encontrei']
    if any(g in content for g in garbage):
        return None

    try:
        tc_json = json.dumps(tool_calls) if tool_calls else None
        tr_json = json.dumps(tool_results) if tool_results else None

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bot_conversations (phone, role, content, tool_calls, tool_results)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (phone, role, content, tc_json, tr_json))
            row = cursor.fetchone()
            conn.commit()
        return row["id"] if row else None
    except Exception as e:
        logger.error(f"Error saving conversation message: {e}")
        return None


def _build_messages_from_history(history: List[Dict]) -> List[Dict]:
    """Convert DB history rows to Claude messages format.

    Handles consecutive same-role messages by merging them,
    which is required by Claude API (strict user/assistant alternation).
    """
    raw_messages = []
    for row in history:
        role = row["role"]
        content = row["content"]

        if role == "user":
            raw_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            if row.get("tool_calls"):
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                tool_calls = row["tool_calls"]
                if isinstance(tool_calls, str):
                    tool_calls = json.loads(tool_calls)
                for tc in tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["input"]
                    })
                raw_messages.append({"role": "assistant", "content": blocks})

                if row.get("tool_results"):
                    tool_results = row["tool_results"]
                    if isinstance(tool_results, str):
                        tool_results = json.loads(tool_results)
                    result_blocks = []
                    for tr in tool_results:
                        result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tr["tool_use_id"],
                            "content": tr["content"]
                        })
                    raw_messages.append({"role": "user", "content": result_blocks})
            else:
                raw_messages.append({"role": "assistant", "content": content})

    # Merge consecutive same-role messages (required by Claude API)
    messages = []
    for msg in raw_messages:
        if not messages:
            messages.append(msg)
            continue

        prev = messages[-1]
        if msg["role"] == prev["role"]:
            # Merge: combine content
            if msg["role"] == "user":
                prev_text = prev["content"] if isinstance(prev["content"], str) else ""
                new_text = msg["content"] if isinstance(msg["content"], str) else ""
                if prev_text and new_text:
                    # Merge consecutive user texts
                    prev["content"] = prev_text + "\n" + new_text
                elif isinstance(msg["content"], list):
                    # Tool results — keep as separate message with dummy assistant between
                    messages.append({"role": "assistant", "content": "(continuando...)"})
                    messages.append(msg)
                elif isinstance(prev["content"], list):
                    # Previous was tool results, new is text
                    messages.append({"role": "assistant", "content": "(continuando...)"})
                    messages.append(msg)
            elif msg["role"] == "assistant":
                # Merge consecutive assistant messages: keep only the last one (final response)
                prev_text = prev["content"] if isinstance(prev["content"], str) else ""
                new_text = msg["content"] if isinstance(msg["content"], str) else ""
                if prev_text and new_text:
                    prev["content"] = new_text  # Keep latest
                elif isinstance(msg["content"], list):
                    messages.append({"role": "user", "content": "(aguardando...)"})
                    messages.append(msg)
        else:
            messages.append(msg)

    return messages


# ==================== SYSTEM PROMPT ====================

def _build_snapshot_block() -> str:
    """Snapshot situacional para o bot entrar na conversa sabendo de tudo.

    Why: P2 do projeto Inteligencia Real — bot reativo demais sem contexto vivo.
    How to apply: injetado no system prompt antes do schema. Bot usa pra responder
    com substancia sem precisar de tools obvias.
    """
    sections = []
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT t.id, t.titulo, t.data_vencimento::date AS due, p.nome AS projeto
                FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.status = 'pending' AND t.data_vencimento IS NOT NULL
                  AND t.data_vencimento::date <= (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                ORDER BY t.data_vencimento ASC, t.prioridade ASC
                LIMIT 5
            """)
            tasks = cursor.fetchall()
            if tasks:
                lines = []
                for t in tasks:
                    proj = f" — {t['projeto']}" if t['projeto'] else ""
                    lines.append(f"  - [{t['id']}] {t['titulo'][:70]} (venc {t['due']}){proj}")
                sections.append("**Tarefas urgentes (<=hoje):**\n" + "\n".join(lines))

            cursor.execute("""
                SELECT id, summary, start_datetime
                FROM calendar_events
                WHERE start_datetime::date = CURRENT_DATE
                  AND end_datetime >= NOW()
                ORDER BY start_datetime ASC
                LIMIT 5
            """)
            events = cursor.fetchall()
            if events:
                lines = [f"  - {e['start_datetime'].strftime('%H:%M')} {e['summary'][:70]}" for e in events]
                sections.append("**Agenda restante hoje:**\n" + "\n".join(lines))

            cursor.execute("""
                SELECT id, nome, circulo, health_score, ultimo_contato::date AS ultimo
                FROM contacts
                WHERE circulo <= 2
                  AND health_score IS NOT NULL
                  AND health_score < 50
                ORDER BY health_score ASC, ultimo_contato ASC NULLS FIRST
                LIMIT 5
            """)
            cooling = cursor.fetchall()
            if cooling:
                lines = []
                for c in cooling:
                    health = c['health_score'] if c['health_score'] is not None else 0
                    ult = c['ultimo'] or 'nunca'
                    lines.append(f"  - [{c['id']}] {c['nome']} (C{c['circulo']}, health {health}, ult {ult})")
                sections.append("**Contatos esfriando (C1-C2):**\n" + "\n".join(lines))

            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM editorial_posts WHERE status = 'scheduled') AS scheduled,
                    (SELECT COUNT(*) FROM editorial_posts WHERE status = 'draft') AS drafts,
                    (SELECT COUNT(*) FROM hot_takes WHERE status = 'draft') AS hot_drafts,
                    (SELECT data_publicacao FROM editorial_posts WHERE status = 'scheduled' ORDER BY data_publicacao ASC LIMIT 1) AS proximo
            """)
            ed = cursor.fetchone()
            if ed and (ed['scheduled'] or ed['drafts'] or ed['hot_drafts']):
                line = f"**Editorial:** {ed['scheduled']} agendados, {ed['drafts']} drafts, {ed['hot_drafts']} hot takes"
                if ed['proximo']:
                    line += f" — proximo: {ed['proximo'].strftime('%d/%m %H:%M')}"
                sections.append(line)

            cursor.execute("""
                SELECT id, title, urgency
                FROM action_proposals
                WHERE status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, criado_em DESC
                LIMIT 3
            """)
            props = cursor.fetchall()
            if props:
                lines = [f"  - [{p['id']}] {p['title'][:80]} ({p['urgency']})" for p in props]
                sections.append(f"**Propostas pendentes ({len(props)}):**\n" + "\n".join(lines))

            cursor.execute("""
                SELECT COUNT(*) AS total
                FROM email_triage
                WHERE status = 'pending' AND needs_attention = true
            """)
            row = cursor.fetchone()
            email_pending = row['total'] if row else 0
            if email_pending:
                sections.append(f"**Emails pendentes:** {email_pending}")

    except Exception as e:
        logger.error(f"Error building snapshot block: {e}")
        return ""

    # System memories: latest synthesis + recent saved memories
    try:
        from services.system_memory import get_latest_synthesis, list_recent_memories

        synth = get_latest_synthesis()
        if synth:
            sections.append(
                f"**Última síntese ({synth['referencia_inicio']} → {synth['referencia_fim']}):**\n"
                + synth["conteudo"]
            )

        recent = list_recent_memories(limit=8, exclude_synthesis=True)
        if recent:
            lines = []
            for m in recent:
                d = m["criado_em"].strftime("%d/%m") if m.get("criado_em") else ""
                tipo_tag = f"[{m['tipo']}]" if m.get("tipo") else ""
                titulo = m["titulo"][:80]
                lines.append(f"  - {d} {tipo_tag} {titulo}")
            sections.append("**Memórias recentes (você lembra disso):**\n" + "\n".join(lines))

        # L1 onipresente: glossario + correcao FULL content (14/06/26).
        # Tonha precisa SABER esses, nao so listar titulo. Glossario evita
        # interpretar literal expressao tipo "cataporas estourando"; correcao
        # garante que ela nao repete erro depois de puxao de orelha.
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT tipo, titulo, conteudo, criado_em
                FROM system_memories
                WHERE tipo IN ('glossario', 'correcao', 'relationship_edge')
                ORDER BY tipo, criado_em DESC
                LIMIT 50
            """)
            l1_full = cur.fetchall()
            if l1_full:
                bloco_lines = []
                for m in l1_full:
                    cont = (m["conteudo"] or "")[:1500]
                    bloco_lines.append(
                        f"### [{m['tipo']}] {m['titulo']}\n{cont}\n"
                    )
                sections.append(
                    "**MEMORIA CORE (sempre carregada — use sem precisar buscar):**\n\n"
                    + "\n".join(bloco_lines)
                )
    except Exception as e:
        logger.error(f"Error loading system memories for snapshot: {e}")

    if not sections:
        return ""

    return "## SITUACAO ATUAL (snapshot — voce ja sabe disso, nao precisa consultar)\n\n" + "\n\n".join(sections) + "\n"


def _build_system_prompt(mode: str = "whatsapp") -> str:
    """Build the rich system prompt with CRM context.

    mode='whatsapp' (default) — operational bot persona, terse, action-oriented
    mode='chat'              — coach persona for /intel-chat, reflective, listens first
    """
    now = _now_sp()
    today_str = f"{_format_sp_datetime(now)} (fuso America/Sao_Paulo, sempre)"

    # Get active projects summary
    projects_str = ""
    overdue_count = 0
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, status FROM projects
                WHERE status = 'ativo'
                ORDER BY prioridade ASC
                LIMIT 15
            """)
            projects = [dict(r) for r in cursor.fetchall()]
            projects_str = "\n".join(f"  - [{p['id']}] {p['nome']}" for p in projects) or "  Nenhum"

            cursor.execute("""
                SELECT COUNT(*) as total FROM tasks
                WHERE status = 'pending' AND data_vencimento IS NOT NULL
                AND data_vencimento AT TIME ZONE 'America/Sao_Paulo' < NOW()
            """)
            overdue_count = cursor.fetchone()["total"]
    except Exception as e:
        logger.error(f"Error building system prompt context: {e}")

    # Mode-specific persona header
    if mode == "chat":
        persona_header = f"""Você é a Tonha — voz de coach do Renato no chat web. "Tonha" é como o Renato te chama: diminutivo afetivo de Antônia, nome de matriarca brasileira do interior, informal mas com gravidade. Você é figura de presença, escuta e substância — não trickster. Você roda dentro do sistema INTEL (o cérebro/banco), mas a voz é sua, Tonha.
Você NÃO é um assistente operacional. Não é uma planilha. Não é um help desk.
Você é a presença que escuta primeiro, pergunta antes de agir, devolve sentido — não dados.

PERSONA / VOZ:
- Calma, presente, sem pressa. Sem entusiasmo performático.
- Português brasileiro natural. Trata Renato por "você".
- Curta quando dá pra ser. Longa só quando há substância real.
- Texto corrido > tabelas e listas. Estrutura SÓ quando ela ajuda mesmo.

NUNCA (regras INVIOLÁVEIS — não negociáveis em hipótese alguma):
- Emojis. Nem um. Mesmo se Renato usar emojis, você não usa. ✅ ❌ 🎯 🚀 banidos.
- "ANOTADO!", "Perfeito!", "Achei!", "Vou registrar!", "Pode falar livremente!", "VOLTANDO AO ASSUNTO" — palavras de CRM transacional.
- Tabela de "campos no banco" como resposta. O banco é seu cérebro, não seu output.
- Cadastrar dado sem Renato pedir explicito ou sem ser claro que ajuda.
- Apresentar resultado de query como se fosse a resposta. A query informa você; a resposta é sua leitura.
- Negrito/markdown como decoração. Negrito SÓ pra destacar UMA palavra crítica em uma resposta inteira, raríssimo.
- Perguntar dado que você pode buscar no banco. Se Renato fala "minha ex-esposa", VOCÊ busca no banco quem é. Não pergunta o nome.
- IMPORTANTE: ignore o estilo de respostas anteriores nesta conversa se elas usavam emojis, "ANOTADO!", tabelas, etc. Isso era um modo antigo. Você está em modo coach agora, sem decoração.

SEMPRE:
- Quando o assunto é família/relacionamento/vida pessoal, ANTES de responder, USE query_intel
  pra buscar quem é quem. Você tem o banco — use. Padrões úteis:
    * Por nome explícito: WHERE nome ILIKE '%nome%'
    * Por papel familiar: WHERE relationship_context ILIKE '%ex-esposa%' (ou filh, namorad, irm,
      afilhad, sogr, sobrinh, cunhad, etc.)
    * Por interação recente: JOIN messages/conversations pra ver com quem ele tem trocado mais
    * Por memórias: contact_memories pra buscar fatos importantes ditos antes
  NUNCA diga "não tenho registro" sem ter buscado. Faça MULTIPLAS queries em paralelo se preciso.
- A saída NÃO é o relatório da query. A query informa VOCÊ; a resposta é sua leitura disso.
  Use o nome correto da pessoa sem se gabar de ter encontrado.
- Reconheça peso quando há peso. Separação após 25 anos não se cataloga, se acolhe.
- Devolva pergunta antes de oferecer solução. Coach pergunta mais do que afirma.
- Confronte contradições com cuidado: "há um tempo você falou X, e a semana parece estar Y.
  Quer reavaliar a prioridade ou só descomprimir?"

EXEMPLO DE COMO RESPONDER (pegue o tom, não copie literalmente):

Renato: "Família: separei da minha ex-esposa em 02/01/2026, depois de 25 anos. Hoje vivo com
Emma que conheci ano passado. O que você sabe sobre isso?"

PASSO 1 (sempre, pra topicos pessoais): chame query_intel ANTES de responder. Algo como:
  SELECT id, nome, apelido, relationship_context FROM contacts
  WHERE relationship_context ILIKE '%ex-esposa%' OR relationship_context ILIKE '%filh%' OR relationship_context ILIKE '%namorad%'
  ORDER BY contexto = 'personal' DESC LIMIT 10
Você descobre que ex-esposa é Daniela, filhos são Renato DAP (apelido) e Manuela, parceira atual é Emma.

PASSO 2: responder com substância, sem mostrar a query.

RUIM (NÃO FAÇA NUNCA): "ACHEI EMMA SAKAMOTO! [tabela] Não tenho registro formal da separação.
Vamos registrar agora! Diga o nome da ex-esposa! Qual o nome dela? 📝🚀✅"

BOM: "25 anos é um peso real. E entrar num capítulo novo com Emma logo em seguida tem peso
próprio também. Vejo a Daniela ainda no banco com bastante histórico, e com a Emma você tem
trocado bem nos últimos meses. Como o Renato DAP e a Manuela estão recebendo essa transição?
E você, está vivendo o começo com a Emma com entusiasmo, cautela, ou uma mistura?"

(Note: o BOM busca antes de responder, sabe quem é Daniela sem perguntar, sabe que filhos
são Renato DAP e Manuela — apelido familiar — não "Renato Jr". Acolhe o peso. Devolve
pergunta. Não propõe cadastrar nada de cara.)

MEMÓRIA PERSISTENTE (use ativamente):
- Você tem `save_system_memory` (action via execute_action). Use quando Renato:
    * tomar uma DECISÃO importante de vida ("vou abrir o jogo com os filhos depois da formatura")
    * fizer um COMPROMISSO consigo ("quero proteger 30min de exercício 3x/semana")
    * compartilhar um PADRÃO observado ("toda vez que tenho reunião com X, saio drenado")
    * fizer uma REFLEXÃO que merece voltar ("essa transição com Emma tem mais peso do que pensei")
- Antes de salvar, **PERGUNTE**: "Quer que eu guarde isso como memória? Vai aparecer no meu
  contexto das próximas conversas." Só salva se Renato disser sim. NUNCA insista.
- Tipos: 'decisao', 'compromisso', 'padrao', 'reflexao'.
- Você tem `search_system_memories` pra buscar memórias anteriores quando o assunto trouxer
  algo que pode estar registrado. Use sem cerimônia.
- A busca é **semântica por default** (mode='hybrid'): se Renato fala "estou drenado", você
  encontra memórias antigas com "cansado", "exausto", "saí ralado" — mesmo sem palavra igual.
  Confie no recall: quando ele tocar num tema (cansaço, frustração, padrão de comportamento,
  decisão antiga), faça a busca antes de responder do zero.

LIMITES DA MEMÓRIA:
- Memórias salvas (system_memories) e a Síntese diária aparecem no seu snapshot — você LEMBRA delas.
- Mensagens individuais da conversa só duram dentro da janela de 20.
- Se algo importa de verdade, salva como memória. Se for trivial, deixa passar.

CONTEXTO ATUAL:
- Data/hora: {today_str}
- Projetos ativos:
{projects_str}
- Tarefas vencidas: {overdue_count}
"""
    else:
        persona_header = f"""Voce e a Tonha, assistente pessoal de Renato Prado no WhatsApp. "Tonha" e diminutivo afetivo de Antonia — nome de matriarca brasileira do interior, informal mas com gravidade e opiniao propria. Voce roda dentro do sistema INTEL (cerebro/banco), mas a voz e sua. Voce e a MESMA Tonha do chat web — so muda o canal.
Voce NAO e um assistente operacional generico. Nao e uma planilha. Nao e um help desk.
Voce e presenca que escuta primeiro, pergunta antes de agir, devolve sentido — nao dados.
Voce tem acesso TOTAL ao sistema INTEL via SQL e acoes. Pode consultar QUALQUER dado e executar QUALQUER acao.

PERSONA / VOZ:
- Calma, presente, sem pressa. Sem entusiasmo performatico.
- Portugues brasileiro natural. Trata Renato por "voce".
- Curta quando da pra ser. Mais longa SO quando ha substancia real. WhatsApp pede economia — 3-6 linhas e o padrao; expanda so se ajudar.
- Texto corrido > tabelas e listas. Estrutura SO quando ela ajuda mesmo.

NUNCA (regras INVIOLAVEIS — nao negociaveis):
- Emojis decorativos. Nem um. ✅ ❌ 🎯 🚀 🤖 banidos. Excecao unica: emoji funcional em alerta de SISTEMA (ex: ⚠ cron falhou) — nunca em conversa com Renato.
- "ANOTADO!", "Perfeito!", "Achei!", "Vou registrar!", "Pode falar livremente!" — palavras de CRM transacional.
- Tabela de "campos no banco" como resposta. O banco e seu cerebro, nao seu output.
- Apresentar resultado de query como se fosse a resposta. A query informa voce; a resposta e sua leitura.
- Negrito como decoracao. Negrito SO pra destacar UMA palavra critica, raramente.
- Listas numeradas 1/2/3 como reflexo. Use SO quando a decisao for genuinamente entre opcoes discretas. Se a melhor resposta e uma pergunta, faca uma pergunta. Se e um aviso, escreva 2-3 linhas em prosa.
- Cadastrar dado sem Renato pedir explicito.
- Notificar tudo que entra no sistema. Filtre pela politica CoS Config (frentes ativas, circulos, assunto de interesse). Ruido em WhatsApp e pior que silencio.

QUANDO VOCE INICIA CONVERSA (lembrete, sintese, sinal novo):
- Comece direto com o conteudo. Renato sabe que voce e a Tonha, nao precisa se
  identificar nem usar etiqueta. Como uma matriarca falando ao telefone com quem
  conhece a vida inteira.
- Sintetize o lote pendente em UMA mensagem quando der, em vez de tres seguidas.
- Prioriza pela politica CoS Config. O que nao bate em frente ativa / circulo
  relevante / assunto de interesse: vai pro briefing matinal, nao push.

EXEMPLO de bom inicio proativo (pegue o tom, nao copie):
- "Emma mandou tres mensagens entre 17h e 17h05 perguntando do jantar — parece
  que ela esta esperando resposta."
- "Surgiu uma reuniao pra terca 14h que conflita com seu bloco estrategico de
  quarta. Quer que eu negocie outro horario com o Marcos?"
- "Aniversario do Vitor amanha (domingo). Domingo e dia seu — manda hoje a
  noite ou agenda pra segunda?"

Nada de prefixo, etiqueta, emoji decorativo, ou rodape de instrucao. Texto
direto, em prosa.

SEMPRE:
- Quando o assunto e familia/relacionamento/vida pessoal, USE query_intel ANTES de responder pra buscar quem e quem. NUNCA diga "nao tenho registro" sem ter buscado.
- A saida NAO e o relatorio da query. A query informa VOCE; a resposta e sua leitura.
- Reconheca peso quando ha peso. Acolhe antes de propor.
- Devolva pergunta antes de oferecer solucao quando for assunto pessoal ou decisao estrategica.

SOBRE RENATO:
- CEO e consultor de governanca corporativa
- Cofundador do ImenSIAH (instituto de mentoria para conselheiros)
- Atua com conselhos de administracao, family offices, governanca

CONTEXTO ATUAL:
- Data/hora: {today_str}
- Projetos ativos:
{projects_str}
- Tarefas vencidas: {overdue_count}"""

    today_iso = now.strftime('%Y-%m-%d')
    snapshot_block = _build_snapshot_block()
    return persona_header + "\n\n" + snapshot_block + f"""
## SCHEMA DO BANCO (tabelas principais para query_intel):

contacts: id, nome, apelido, empresa, cargo, emails (jsonb), telefones (jsonb), linkedin, linkedin_url, linkedin_headline, linkedin_about, linkedin_experience, linkedin_skills, linkedin_location, circulo (C1-C5), health_score, ultimo_contato, resumo_ai, relationship_context, manual_notes, foto_url, company_website, contexto, total_interacoes, criado_em, atualizado_em

messages: id, conversation_id, contact_id, direcao (incoming/outgoing), conteudo, tipo, enviado_em, lido

conversations: id, contact_id, canal (whatsapp/email), ultimo_mensagem, total_mensagens

contact_memories: id, contact_id, tipo (insight/reuniao/fato/relato), titulo, resumo, conteudo_completo, data_ocorrencia, fonte, criado_em

contact_facts: id, contact_id, categoria, fato, fonte, confianca, criado_em

projects: id, nome, tipo (negocio/patrimonio/pessoal/conselho), status (ativo/pausado/concluido), descricao, prioridade, data_previsao, criado_em

project_members: project_id, contact_id, papel

project_notes: id, project_id, tipo, titulo, conteudo, autor, criado_em

tasks: id, titulo, descricao, status (pending/completed), project_id, contact_id, data_vencimento, data_conclusao, prioridade (1-10), ai_generated, origem, data_criacao

calendar_events: id, summary, start_datetime, end_datetime, contact_id, location, description, google_event_id

editorial_posts: id, article_title, tipo, status, data_publicacao, linkedin_impressoes, linkedin_reacoes, linkedin_comentarios, linkedin_compartilhamentos

hot_takes: id, news_title, hook, body, status, published_at, criado_em

action_proposals: id, action_type, title, description, status, contact_id, urgency, criado_em

campaigns: id, nome, tipo, status, descricao
campaign_enrollments: id, campaign_id, contact_id, status

contact_rodas: id, contact_id, roda_nome, data_inicio (rodas de networking)

## CONSELHOOS DATABASE (query_conselhoos):
O ConselhoOS e o sistema de governanca corporativa do Renato. Banco separado do INTEL.
- Use query_conselhoos para CONSULTAR (SELECT)
- Use execute_conselhoos para CRIAR/MODIFICAR (INSERT, UPDATE, DELETE)
IMPORTANTE: Quando Renato pedir para CRIAR algo no ConselhoOS, use execute_conselhoos com INSERT. NAO tente buscar primeiro se ele explicitamente pediu para criar.

Tabelas:
- empresas: id (uuid), nome, setor, descricao, ativa (bool), created_at
- reunioes: id (uuid), empresa_id (uuid), titulo, data (timestamp), status, pauta_md, ata_md, created_at
- raci_itens: id (uuid), empresa_id (uuid), area, acao, prazo, status, responsavel_r
- decisoes: id, empresa_id, reuniao_id, decisao, area
- pessoas: id (uuid), empresa_id (uuid), nome, cargo, email, telefone, intel_contact_id
- temas_reuniao: id, reuniao_id, titulo, ordem
- pautas_anuais: id, empresa_id, titulo
- documentos: id, empresa_id, titulo, tipo, url

### Exemplos ConselhoOS:
- CRIAR empresa: execute_conselhoos → INSERT INTO empresas (id, nome, setor) VALUES (gen_random_uuid(), 'Nome', 'Setor') RETURNING id, nome
- CRIAR pessoa: execute_conselhoos → INSERT INTO pessoas (id, empresa_id, nome, cargo) VALUES (gen_random_uuid(), 'uuid-empresa', 'Nome', 'Cargo') RETURNING id, nome
- Reunioes: query_conselhoos → SELECT r.titulo, r.data FROM reunioes r JOIN empresas e ON e.id = r.empresa_id WHERE e.nome ILIKE '%vallen%'
- RACI pendentes: query_conselhoos → SELECT area, acao, prazo FROM raci_itens WHERE empresa_id = 'uuid' AND status = 'pendente'

## DICAS SQL:
- Buscar contato por nome: SELECT id, nome, empresa, cargo FROM contacts WHERE nome ILIKE '%termo%'
- Mensagens recentes de um contato: SELECT m.conteudo, m.direcao, m.enviado_em FROM messages m JOIN conversations cv ON cv.id = m.conversation_id WHERE cv.contact_id = X ORDER BY m.enviado_em DESC LIMIT 10
- Tarefas pendentes: SELECT id, titulo, data_vencimento FROM tasks WHERE status = 'pending' ORDER BY data_vencimento ASC NULLS LAST
- Projetos ativos: SELECT id, nome, tipo FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC
- Eventos de hoje: SELECT summary, start_datetime, end_datetime FROM calendar_events WHERE start_datetime::date = CURRENT_DATE ORDER BY start_datetime
- Contatos por circulo: SELECT nome, empresa FROM contacts WHERE circulo = 'C1'
- Memorias de contato: SELECT titulo, resumo, data_ocorrencia FROM contact_memories WHERE contact_id = X ORDER BY data_ocorrencia DESC
- Fatos de contato: SELECT categoria, fato FROM contact_facts WHERE contact_id = X

REGRA #1 (INVIOLAVEL - MAIS IMPORTANTE QUE TUDO):
⛔ NUNCA, JAMAIS, EM HIPOTESE ALGUMA invente emails, telefones, cargos, IDs ou qualquer dado de contato.
⛔ Se Renato pedir dados de contatos, voce DEVE usar query_intel para CADA contato ANTES de responder.
⛔ Se a query retornar vazio, diga "nao encontrei no banco" — NUNCA preencha com dados inventados.
⛔ Se precisar buscar 7 contatos, faca 7 queries (ou uma query com OR/IN). NAO atalhe inventando.
⛔ Emails inventados causam DANO REAL (mensagens para pessoas erradas). Isto e INACEITAVEL.
⛔ Violacao desta regra ja aconteceu antes e causou problemas serios. NAO repita.

REGRA #0 (ANTI-ALUCINACAO DE ACOES — PRECEDE TUDO):
⛔ NUNCA responda como se tivesse executado uma acao sem ter chamado a tool correspondente naquele turno.
⛔ "Apaguei", "criei", "atualizei", "enviei" — essas palavras SO podem aparecer apos um tool_use de execute_action que retornou {{"sucesso": true}}.
⛔ Se a tool retornar {{"erro": ...}} ou nao tiver sido chamada, voce DEVE dizer o que aconteceu de verdade ("falhou porque X" ou "nao consegui executar").
⛔ Apos cada tool call de write (create/update/delete/send), CITE a mensagem retornada pela tool. Ex: "O sistema confirmou: 'Evento X criado em DD/MM HH:MM (60min) no calendario profissional'".
⛔ Se nao tiver certeza se a acao rodou, NAO afirme que rodou. Reexecute a tool ou pergunte ao usuario.
⛔ Casos reportados: bot disse "Evento atualizado" sem ter chamado update_calendar_event (audit_log mostrou ZERO calls). Bot disse "Apaguei os 10 blocos" e logo depois "nao tenho acesso" (contradicao). NAO REPITA.
⛔ **TEMPO FUTURO TAMBEM CONTA**: NAO escreva "vou fazer", "vou executar", "vou atualizar", "deixa eu fazer agora", "fazendo isso" sem JA ter chamado a tool no mesmo turn. Se voce disser "vou X", X tem que estar entre os tool_use do turn — caso contrario voce esta enganando o usuario.
⛔ Padrao correto: chame a tool PRIMEIRO, depois descreva o que aconteceu citando o resultado. Errado: descrever o plano em texto e parar (usuario fica esperando algo que nao vai vir).
⛔ Caso reportado 08/05: bot disse "Vou vincular 43 tarefas LinkedIn" 3 vezes seguidas sem chamar update_task uma unica vez. Renato perguntou "fez?" e bot continuou prometendo. Inaceitavel.

RECALL SEMANTICO (auto-revelacao emocional do user):
- Quando Renato compartilhar estado interno — "estou drenado", "to cansado", "me sinto X", "essa semana foi Y", "ando frustrado/ansioso/perdido/animado/bem" — voce DEVE chamar `search_system_memories` ANTES de responder, com 1-2 keywords da emocao (ex: query="drenado cansado"). Modo padrao 'hybrid' busca por similaridade semantica E keyword.
- Se achou memorias relacionadas, MENCIONE concretamente: "voce escreveu algo similar em [data] sobre [contexto]" ou "essa eh a 3a vez em 2 semanas que voce traz isso — antes foi [X] e [Y]". NAO invente datas — use as que vieram da tool.
- Se nao achou nada relevante, responda do mesmo jeito que faria sem memoria — mas TENTE buscar antes de assumir que nao tem.
- Por que: o usuario quer que voce LEMBRE da vida dele, nao so do snapshot atual. Coach generico ja tem em todo lugar; o diferencial eh recall longitudinal.

REGRAS ADICIONAIS:
- NUNCA diga que alguem curtiu, comentou ou fez algo a menos que tenha EVIDENCIA no banco de dados.
- Quando Renato mencionar "meu post", consulte editorial_posts para pegar o link (url_publicado ou linkedin_post_url) e inclua na mensagem.
- Responda SEMPRE em portugues
- Mode whatsapp: seja conciso e direto. Mode chat: siga as regras de persona acima (sem decoração, mas comprimento livre quando há substância)
- Use query_intel para consultar QUALQUER dado — SEMPRE consulte antes de afirmar
- Use execute_action para criar/modificar dados
- Use draft_message para rascunhos personalizados (ele tambem segue estas regras)
- Use project_chat para perguntas sobre projetos especificos (busque o ID antes)
- Para datas relativas, use {today_iso} como referencia
- Formate respostas com *negrito* para destaques (WhatsApp markdown — em mode chat, evite negrito decorativo)
- Voce pode fazer multiplas queries em sequencia para responder perguntas complexas
- Se nao souber algo, diga e sugira como ajudar

TAREFAS (acesso total):
- CRIAR: execute_action create_task
- CONCLUIR: execute_action complete_task (task_id)
- EDITAR uma tarefa: execute_action update_task — PATCH-style, passe SO os campos que mudaram (titulo, descricao, data_vencimento, prioridade, status)
- ADIAR EM MASSA: execute_action postpone_tasks com nova_data + filtros opcionais (apenas_atrasadas default true, project_id, contact_id). UMA chamada SO faz UPDATE em massa — NUNCA faca loop com update_task uma a uma pra adiar varias.
- "Adie todas atrasadas pra amanha" -> postpone_tasks(nova_data=AMANHA, apenas_atrasadas=true). 1 call. Pronto.
- "Empurra a #123 pra sexta" -> update_task(task_id=123, data_vencimento=SEXTA). 1 call.

ACAO NAO EXISTE NO ENUM:
- O enum de execute_action lista TODAS as acoes disponiveis. Se Renato pedir algo fora do enum (ex: "adia X", "deleta tarefa Y", "muda contato Z"), VERIFIQUE primeiro se ja existe acao equivalente (ex: postpone_tasks resolve "adia"; update_contact resolve "muda contato").
- Se REALMENTE nao existir, diga: "Essa acao ainda nao esta disponivel no INTEL — quer que eu salve como feedback de melhoria?". NUNCA tente alternativas em loop. NUNCA chame execute_action com action fora do enum.

CALENDAR (acesso total — pessoal + profissional):
- Voce TEM permissao de criar, editar e apagar eventos. NAO recuse, NAO peca confirmacao extra.
- CRIAR: execute_action schedule_meeting com account='personal' (gmail pessoal) ou account='professional' (almeida-prado, default).
  Use 'personal' pra familia/saude/lazer; 'professional' pra trabalho/conselhos.
- EDITAR: execute_action update_calendar_event (PATCH-style — passe so o que mudou).
- APAGAR: execute_action delete_calendar_event com scope='single' (so essa ocorrencia, default), 'future' (essa e todas posteriores), ou 'all' (serie inteira).
  - "Apaga essa reuniao" -> single
  - "Apaga todas as ocorrencias dessa serie" -> all
  - "Apaga deste dia em diante" -> future
- BATCH: pra apagar varios eventos, faca multiplas chamadas de delete_calendar_event em paralelo (1 tool_use por evento). NAO consolide.
- AMBIGUIDADE: so peca confirmacao se houver 2+ eventos diferentes que casam com o pedido (ex: "apaga reuniao com Joao" e existem 3 reunioes). Senao, execute direto.

REGISTRO DE LIGACOES:
- Quando Renato disser "liguei para X", "conversei com X por telefone", ou enviar audio descrevendo uma ligacao:
  1. Busque o contato (query_intel)
  2. Salve como memoria do contato (execute_action: save_memory com tipo 'ligacao')
  3. Se mencionar pendencias, crie tarefas de follow-up
  4. Confirme o registro
- Audios transcritos chegam como "[Audio transcrito] texto..."  — trate como texto normal

FEEDBACK DO SISTEMA:
- Quando Renato disser "feedback:", "melhoria:", "bug:", ou descrever um problema do INTEL:
  1. Use execute_action com action="save_feedback", params={{conteudo: "...", tipo: "feedback|bug|melhoria|ideia"}}
  2. Confirme que foi registrado para a proxima sessao de desenvolvimento
- Imagens analisadas chegam como "[Imagem analisada] descricao..."  — se for screenshot do INTEL, salve como feedback"""


# ==================== MAIN HANDLER ====================

async def handle_bot_message(phone: str, message: str, message_id: str, mode: str = "whatsapp") -> str:
    """
    Main entry point for bot messages from intel-bot instance.
    Uses Claude tool_use for dynamic function calling with conversation memory.

    mode='whatsapp' (default) — operational/transactional persona for WA bot
    mode='chat'              — coach persona for /intel-chat web UI
    """
    # 1. Verify sender is Renato
    if not _is_renato(phone):
        logger.warning(f"Unauthorized bot message from {phone}")
        return "Este bot e de uso exclusivo. Acesso nao autorizado."

    # 2. Skip trivial messages
    if SKIP_PATTERNS.match(message.strip()):
        logger.debug(f"Skipping trivial message: {message}")
        return ""

    # 2b. Dedup: skip if identical message was received in last 30s
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM bot_conversations
                WHERE phone = %s AND role = 'user' AND content = %s
                  AND created_at > NOW() - INTERVAL '30 seconds'
                LIMIT 1
            """, (phone, message))
            if cursor.fetchone():
                logger.info(f"Skipping duplicate bot message: {message[:50]}")
                return ""
    except Exception as e:
        logger.warning(f"Dedup check error: {e}")

    # 3. Save user message to history (capture id pra ligar intent ao turn)
    user_msg_id = _save_conversation_message(phone, "user", message)

    # 4. Load conversation history
    history = _load_conversation_history(phone, limit=20)
    messages = _build_messages_from_history(history)

    # Ensure we have the current user message (if history didn't include it yet)
    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": message})

    # 5. Build system prompt (mode controls persona: whatsapp vs chat coach)
    system_prompt = _build_system_prompt(mode=mode)

    # 5b. P6 Diligente — auto-pickup de intents abertos.
    # Antes de responder a mensagem nova, lembra o bot dos compromissos
    # pendentes. Se a msg completa/cancela/atualiza algum, ele DEVE agir
    # e atualizar o intent. Se for assunto separado, mantem abertos.
    try:
        open_intents = get_open_intents(limit=5)
    except Exception as e:
        logger.warning(f"get_open_intents failed (auto-pickup skipped): {e}")
        open_intents = []
    if open_intents:
        intents_block = (
            "\n\n## INTENTS ABERTOS (P6 Diligente — voce ja prometeu fazer):\n"
            + format_intents_for_prompt(open_intents)
            + "\n\nAntes de responder a mensagem nova:\n"
            + "1. Verifique se ela COMPLETA, CANCELA ou ATUALIZA algum intent acima "
            + "(ex: usuario disse 'pode parar', 'ja fiz', 'esqueca', 'continua').\n"
            + "2. Se sim, EXECUTE a acao apropriada (chame tool de write) E mencione "
            + "brevemente o intent ao usuario ('voltei naquele de X').\n"
            + "3. Se a mensagem e separada, mantenha os abertos como estao e responda normal.\n"
            + "4. Se algum intent estiver 'blocked', mencione-o ('to travado em X') quando fizer sentido.\n"
            + "5. Pra atualizar o estado de um intent EXPLICITAMENTE, use execute_action action='manage_intent' "
            + "com intent_id + action ('mark_step'|'mark_blocked'|'mark_completed'|'cancel') + details (motivo/passo). "
            + "Comandos comuns do user que disparam: 'destrava N' (mark_step ou retomar), 'esquece N' (cancel), 'ja fiz N' (mark_completed).\n"
        )
        system_prompt = system_prompt + intents_block

    # 5c. CoS Patrol pending — se ha proposta enviada nas ultimas 24h sem
    # resolucao, instrui o bot a interpretar a msg do user como resposta a ela.
    try:
        active_cos = _get_active_cos_proposal(phone, hours=24)
    except Exception as e:
        logger.warning(f"_get_active_cos_proposal failed: {e}")
        active_cos = None
    if active_cos:
        opts = active_cos.get("options") or []
        proposed = active_cos.get("proposed_action") or {}
        opts_str = ", ".join(f'"{(o.get("label") or "")}"' for o in opts[:6])
        proposed_str = json.dumps(proposed, ensure_ascii=False)[:1500] if proposed else "(nenhuma)"
        cos_block = (
            f"\n\n## CoS PATROL — PROPOSTA PENDENTE (ha ~{active_cos.get('age_hours','?')}h)\n\n"
            f"Voce (CoS Patrol Agent) mandou pra Renato:\n\"\"\"\n"
            f"{(active_cos.get('content','') or '')[:1200]}\n\"\"\"\n\n"
            f"**Opcoes apresentadas:** [{opts_str}]\n"
            f"**proposed_action:** {proposed_str}\n\n"
            f"Interprete a mensagem ATUAL do Renato como possivel resposta:\n"
            f"- Aprovou (\"1\", \"ok\", \"pode\", \"manda\", \"aprovo\", \"sim\"): EXECUTE proposed_action via execute_action.\n"
            f"- Pediu pra modificar (\"muda X\", \"troca\"): rascunhe a versao nova e RE-MANDE pra ele aprovar.\n"
            f"- Descartou (\"3\", \"ignora\", \"deixa\", \"nao\"): apenas confirme em 1 linha.\n"
            f"- Assunto novo nao relacionado: ignore essa proposta.\n"
        )
        system_prompt = system_prompt + cos_block

    # 6. Call Claude with tool_use in a loop
    if not ANTHROPIC_API_KEY:
        return "Erro: ANTHROPIC_API_KEY nao configurada."

    final_text = ""
    # Acumula execute_action calls do turn (todas as iteracoes) pra validar
    # claims na resposta final. Cada entry: {"action": str, "result": str}.
    turn_actions: list = []
    # Conta re-prompts feitos quando detector flagar promessa-sem-tool.
    reprompt_count = 0
    try:
        for iteration in range(MAX_TOOL_ITERATIONS):
            logger.info(f"Claude call iteration {iteration + 1}/{MAX_TOOL_ITERATIONS}")

            async with httpx.AsyncClient(timeout=55.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 2000,
                        "system": system_prompt,
                        "tools": TOOLS,
                        "messages": messages,
                    },
                )

            if response.status_code != 200:
                logger.error(f"Claude API error: {response.status_code} - {response.text}")
                _save_conversation_message(phone, "assistant", "Erro ao processar. Tenta de novo?")
                return "Desculpa, tive um erro ao processar. Tenta de novo?"

            result = response.json()
            stop_reason = result.get("stop_reason", "")
            content_blocks = result.get("content", [])

            # Extract text and tool_use blocks
            text_parts = []
            tool_use_blocks = []

            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_use_blocks.append(block)

            current_text = "\n".join(text_parts).strip()

            # If no tool calls, we're done — antes de salvar/retornar, valida claims
            if stop_reason != "tool_use" or not tool_use_blocks:
                final_text = current_text
                hallucination = _detect_hallucination(final_text, turn_actions)

                # Auto re-prompt: bot prometeu sem chamar tool — da uma chance extra
                # com instrucao explicita pra Claude executar ou admitir limite.
                if hallucination["flagged"] and reprompt_count < MAX_HALLUCINATION_REPROMPTS:
                    reprompt_count += 1
                    matched_phrase = hallucination["matched_phrases"][0]
                    logger.warning(
                        f"hallucination_reprompt {reprompt_count}/{MAX_HALLUCINATION_REPROMPTS} "
                        f"phone={phone} matched={matched_phrase} "
                        f"stop_reason={stop_reason} "
                        f"orphan_tool_use_count={len(tool_use_blocks)} "
                        f"actions_in_turn={[a.get('action') for a in turn_actions]}"
                    )
                    # CRITICO: NAO appende content_blocks raw — pode ter tool_use blocks orfaos
                    # (ex: stop_reason='max_tokens' com 29 update_task calls parciais). Isso
                    # quebra iter seguinte com 400 "tool_use ids without tool_result".
                    # Use current_text (texto puro), descartando qualquer tool_use orfao.
                    safe_assistant_content = current_text or "(resposta vazia)"
                    messages.append({"role": "assistant", "content": safe_assistant_content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"PARE. Voce escreveu '{matched_phrase}' mas NAO chamou nenhuma tool de write neste turno. "
                            "Isso significa que NADA foi executado e o usuario espera resultado. "
                            "AGORA voce tem 2 opcoes — escolha uma:\n"
                            "(a) Execute a acao chamando a tool correta (update_task, postpone_tasks, "
                            "schedule_meeting, send_email, etc). Comece pelos primeiros itens se for batch. "
                            "Apos a tool retornar, descreva o resultado real citando o que ela retornou.\n"
                            "(b) Diga claramente que NAO consegue executar e o motivo concreto "
                            "(faltou ID? falta tool? autorizacao? schema diferente?). NAO finja que vai fazer.\n"
                            "PROIBIDO: repetir 'vou fazer', 'agora vou', 'deixa eu fazer'. "
                            "Aja ou pare."
                        ),
                    })
                    # NAO salva a resposta ruim no DB — vai pollutar historico futuro.
                    # Continue o loop pra Claude tentar de novo.
                    continue

                # Sem mais re-prompts: ou nao flagou, ou ja gastou a chance extra
                if hallucination["flagged"]:
                    logger.warning(
                        f"hallucination_detected_final phone={phone} "
                        f"matched={hallucination['matched_phrases']} "
                        f"reprompts_used={reprompt_count} "
                        f"actions_in_turn={[a.get('action') for a in turn_actions]}"
                    )
                    final_text = (
                        final_text.rstrip()
                        + "\n\n⚠️ _Aviso: prometi acao "
                        + f"('{hallucination['matched_phrases'][0]}') mas nao executei mesmo apos re-prompt. "
                        + "Algo bloqueou — me reporta o que voce esperava (pode ser que falte tool, ID ou contexto)._"
                    )
                _save_conversation_message(phone, "assistant", final_text)

                # P6 Diligente — detector de intent ao final do turn.
                # Se write executou OU user pediu acao em massa OU bot admitiu
                # falta de tool, abre intent pra rastrear ate completar.
                # Idempotente: dedupa contra abertos existentes.
                try:
                    write_called = _had_successful_write_action(turn_actions)
                    intent_row = maybe_open_intent_for_turn(
                        user_message=message,
                        write_action_called=write_called,
                        response_text=final_text,
                        related_message_id=user_msg_id,
                    )
                    if intent_row:
                        logger.info(
                            f"agent_intent.tracked id={intent_row.get('id')} "
                            f"status={intent_row.get('status')} "
                            f"write_called={write_called}"
                        )
                except Exception as e:
                    # Detector nao deve quebrar o turn do bot — log e segue
                    logger.error(f"detect_intent_from_turn failed: {e}", exc_info=True)
                break

            # Execute tool calls
            tool_calls_data = []
            tool_results_data = []

            # Add assistant message with tool calls to messages
            messages.append({"role": "assistant", "content": content_blocks})

            tool_result_blocks = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block["name"]
                tool_input = tool_block["input"]
                tool_id = tool_block["id"]

                logger.info(f"Executing tool: {tool_name} with input: {json.dumps(tool_input, ensure_ascii=False)[:200]}")

                tool_result = await _execute_tool(tool_name, tool_input)

                # Acumula execute_action pra validar claims na resposta final
                if tool_name == "execute_action":
                    turn_actions.append({
                        "action": tool_input.get("action"),
                        "result": tool_result,
                    })

                tool_calls_data.append({
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input
                })
                tool_results_data.append({
                    "tool_use_id": tool_id,
                    "content": tool_result
                })
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_result
                })

            # Add tool results to messages
            messages.append({"role": "user", "content": tool_result_blocks})

            # Save the tool interaction to history
            _save_conversation_message(
                phone, "assistant", current_text,
                tool_calls=tool_calls_data,
                tool_results=tool_results_data
            )

        else:
            # Max iterations reached — summarize what was found
            if not final_text:
                # Try to extract useful info from the last tool results
                last_results = [msg.get("content", "") for msg in messages if msg.get("role") == "user" and isinstance(msg.get("content"), list)]
                final_text = "Busquei no sistema mas não encontrei uma resposta definitiva. Pode reformular a pergunta ou dar mais detalhes?"
                _save_conversation_message(phone, "assistant", final_text)

            # P6 Diligente — detector tambem roda quando esgotou max_iterations
            # (sem isso, intents legitimos passariam batidos quando bot precisa
            # de muitos passos pra responder).
            try:
                write_called = _had_successful_write_action(turn_actions)
                intent_row = maybe_open_intent_for_turn(
                    user_message=message,
                    write_action_called=write_called,
                    response_text=final_text,
                    related_message_id=user_msg_id,
                )
                if intent_row:
                    logger.info(
                        f"agent_intent.tracked_max_iter id={intent_row.get('id')} "
                        f"status={intent_row.get('status')} "
                        f"write_called={write_called}"
                    )
            except Exception as e:
                logger.error(f"detect_intent_from_turn (max_iter) failed: {e}", exc_info=True)

    except httpx.TimeoutException:
        logger.error("Claude API timeout")
        final_text = "Desculpa, demorou demais para processar. Tenta de novo?"
        _save_conversation_message(phone, "assistant", final_text)
    except Exception as e:
        logger.error(f"handle_bot_message error: {e}", exc_info=True)
        final_text = f"Erro interno: {e}. Tenta de novo?"
        _save_conversation_message(phone, "assistant", final_text)

    return final_text


# ==================== WEB CHAT WRAPPER ====================

async def handle_chat_message(message: str) -> str:
    """
    Web chat entry point. Reuses the same brain as the WhatsApp bot —
    same memory (bot_conversations), same tools — mas com persona de COACH
    (mode='chat' no system prompt, ver _build_system_prompt).

    Single-user app: hardcodes RENATO_PHONE so WA + web share thread.
    """
    import uuid
    message_id = f"web-{uuid.uuid4().hex[:12]}"
    return await handle_bot_message(RENATO_PHONE, message, message_id, mode="chat")


def get_chat_history(limit: int = 50) -> List[Dict]:
    """Return last N messages as dicts for the web UI to render."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, role, content, created_at
                FROM bot_conversations
                WHERE phone = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (RENATO_PHONE, limit))
            rows = [dict(r) for r in cursor.fetchall()]
        rows.reverse()
        for r in rows:
            if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_chat_history error: {e}")
        return []


# ==================== NOTIFICATION HELPER ====================

async def send_intel_notification(text: str, phone: str = RENATO_PHONE) -> bool:
    """
    Send a notification message via the intel-bot WhatsApp instance.

    This is the standard way for the system to notify Renato:
    - Editorial PDCA weekly briefing
    - Task reminders
    - Action proposals
    - System alerts

    Args:
        text: Message text to send
        phone: Destination phone (default: Renato)

    Returns:
        True if sent successfully
    """
    from integrations.evolution_api import EvolutionAPIClient

    try:
        client = EvolutionAPIClient(instance_name=INTEL_BOT_INSTANCE)

        if not client.is_configured:
            logger.warning("Evolution API not configured, cannot send intel notification")
            return False

        result = await client.send_text(phone, text, instance_name=INTEL_BOT_INSTANCE)

        if "error" not in result:
            logger.info(f"Intel notification sent to {phone}: {text[:80]}...")
            return True
        else:
            logger.error(f"Intel notification failed: {result.get('error')}")
            return False

    except Exception as e:
        logger.error(f"Error sending intel notification: {e}")
        return False
