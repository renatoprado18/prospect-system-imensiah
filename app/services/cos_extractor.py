"""
CoS Extractor Noturno + Loop de Correcao — aprendizado passivo da Tonha.

Why: a Tonha (persona do CoS) precisa aprender com a conversa diaria do dia
sem virar resumo da nuvem. Esse cron roda 22h30 SP (depois da sintese 22h)
e extrai 5 buckets do que aconteceu nas ultimas 24h:

  A. Fatos novos sobre contatos          -> contact_memories
  B. Padroes/reflexoes do Renato          -> system_memories tipo='padrao'/'reflexao'
  C. Novas idiomaticas/gírias             -> system_memories tipo='glossario'
  D. Decisoes/compromissos                -> system_memories tipo='decisao'/'compromisso'
  E. Correcoes (loop de correcao)         -> system_memories tipo='correcao'

Como funciona:
1. Le bot_conversations (role='user', 24h)
2. Le messages outgoing pra contatos pessoais/circulo C0-C2 (24h)
3. Le sintese diaria mais recente (se houver)
4. Le ultimas 50 system_memories (pra dedup)
5. 1 chamada Claude Sonnet 4.6 com instrucao de classificacao rigorosa
6. Pra cada candidato: gera embedding via Voyage, faz dedup por cossine >=0.85
7. INSERT nas tabelas + audit_log

NAO mexer em cos_sensor.py, intel_bot.py, daily_synthesis.py (paralelo).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from database import get_db
from services.tz import now_utc, to_brt
from services.embeddings import embed_sync, embedding_to_pg_literal, is_enabled as embeddings_enabled
from services.system_memory import save_system_memory
from services.audit_log import log as audit_log

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
COS_EXTRACTOR_MODEL = "claude-sonnet-4-6"
COS_EXTRACTOR_MAX_TOKENS = 4096
COS_EXTRACTOR_TIMEOUT_S = 120.0

# Budget cap diario. Override via COS_EXTRACTOR_DAILY_CAP_USD.
try:
    COS_EXTRACTOR_DAILY_CAP_USD = float(os.getenv("COS_EXTRACTOR_DAILY_CAP_USD", "1.50"))
except (TypeError, ValueError):
    COS_EXTRACTOR_DAILY_CAP_USD = 1.50

# Dedup threshold (cosine similarity). >= esse valor -> NAO cria.
DEDUP_SIMILARITY_THRESHOLD = 0.85

# Limites de payload
MAX_BOT_TURNS = 80
MAX_WA_MSGS = 40
MAX_EXISTING_MEMORIES_FOR_DEDUP = 50


# ============== Budget check ==============

def _check_budget() -> Dict[str, Any]:
    """Verifica gasto Anthropic do extrator no dia. Aborta se passou cap.

    Olha cron_runs do path /api/cron/cos-extractor com result_json.cost_usd
    do dia BRT atual.
    """
    today_iso = to_brt(now_utc()).date().isoformat()
    today_usd = 0.0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM((result_json->>'cost_usd')::float), 0) AS sum_usd
                FROM cron_runs
                WHERE path = '/api/cron/cos-extractor'
                  AND started_at >= %s::date
                  AND started_at <  (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_iso, today_iso),
            )
            r = cur.fetchone()
            if r:
                today_usd = float(r.get("sum_usd") or 0)
    except Exception as e:
        logger.warning(f"cos_extractor._check_budget falhou: {e}")

    if today_usd > COS_EXTRACTOR_DAILY_CAP_USD:
        return {
            "abort": True,
            "reason": f"daily_cap_hit: ${today_usd:.3f} > ${COS_EXTRACTOR_DAILY_CAP_USD:.2f}",
            "today_usd": today_usd,
        }
    return {"abort": False, "today_usd": today_usd}


# ============== Loaders ==============

def _load_user_turns(window_hours: int) -> List[Dict[str, Any]]:
    """Le turns do Renato (role='user') das ultimas N horas.
    Inclui texto direto + audios transcritos (transcription ja gravada como content)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, content, created_at, phone
                FROM bot_conversations
                WHERE role = 'user'
                  AND content IS NOT NULL
                  AND length(trim(content)) > 0
                  AND created_at > NOW() - (%s || ' hours')::interval
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (str(window_hours), MAX_BOT_TURNS),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"_load_user_turns falhou: {e}")
        return []


def _load_outgoing_personal_messages(window_hours: int) -> List[Dict[str, Any]]:
    """Le messages outgoing do Renato pra contatos pessoais ou C0-C2.

    contexto LIKE '%personal%' (a coluna as vezes vem com lista CSV ruidosa),
    ou circulo IN (0,1,2). Joina com contacts pra pegar nome.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.id, m.conteudo, m.enviado_em, m.contact_id,
                       c.nome AS contact_nome, c.contexto, c.circulo
                FROM messages m
                JOIN contacts c ON c.id = m.contact_id
                WHERE m.direcao = 'outgoing'
                  AND m.conteudo IS NOT NULL
                  AND length(trim(m.conteudo)) > 0
                  AND COALESCE(m.enviado_em, m.criado_em) > NOW() - (%s || ' hours')::interval
                  AND (c.contexto ILIKE '%%personal%%' OR COALESCE(c.circulo, 5) <= 2)
                ORDER BY COALESCE(m.enviado_em, m.criado_em) ASC
                LIMIT %s
                """,
                (str(window_hours), MAX_WA_MSGS),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"_load_outgoing_personal_messages falhou: {e}")
        return []


def _load_recent_synthesis(window_hours: int) -> Optional[Dict[str, Any]]:
    """Le a sintese diaria mais recente criada nas ultimas N horas."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, titulo, conteudo, criado_em
                FROM system_memories
                WHERE tipo = 'sintese_diaria'
                  AND criado_em > NOW() - (%s || ' hours')::interval
                ORDER BY criado_em DESC
                LIMIT 1
                """,
                (str(window_hours),),
            )
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        logger.warning(f"_load_recent_synthesis falhou: {e}")
        return None


def _load_existing_memories_for_dedup(limit: int = MAX_EXISTING_MEMORIES_FOR_DEDUP) -> List[Dict[str, Any]]:
    """Le system_memories existentes pra dedup (com embedding quando disponivel).

    Retorna lista de dicts com {id, titulo, conteudo, tipo, embedding_vec (list[float]|None)}.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, titulo, conteudo, tipo, embedding::text AS embedding_text
                FROM system_memories
                ORDER BY criado_em DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"_load_existing_memories_for_dedup falhou: {e}")
        return []

    # Parse pgvector text '[0.1,0.2,...]' -> list[float]
    for row in rows:
        emb_text = row.pop("embedding_text", None)
        row["embedding_vec"] = _parse_pgvector(emb_text)
    return rows


def _parse_pgvector(text: Optional[str]) -> Optional[List[float]]:
    """Converte '[0.1,0.2,...]' string em list[float]. None se vazio/invalido."""
    if not text:
        return None
    try:
        s = text.strip()
        if not (s.startswith("[") and s.endswith("]")):
            return None
        inner = s[1:-1]
        if not inner:
            return None
        return [float(x) for x in inner.split(",")]
    except Exception:
        return None


# ============== Cosine similarity ==============

def _cosine(a: List[float], b: List[float]) -> float:
    """Cosseno entre dois vetores. Retorna 0 se um deles for invalido."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _max_similarity(
    candidate_vec: List[float],
    existing: List[Dict[str, Any]],
    tipo_filter: Optional[str] = None,
) -> Tuple[float, Optional[int]]:
    """Acha max similarity entre candidate_vec e existing memorias.
    Se tipo_filter passado, considera so do mesmo tipo (pra evitar dedupar
    reflexao contra padrao por acaso). Retorna (max_sim, id_match)."""
    best = 0.0
    best_id: Optional[int] = None
    for mem in existing:
        if tipo_filter and mem.get("tipo") != tipo_filter:
            continue
        vec = mem.get("embedding_vec")
        if not vec:
            continue
        sim = _cosine(candidate_vec, vec)
        if sim > best:
            best = sim
            best_id = mem.get("id")
    return best, best_id


# ============== Format inputs ==============

def _format_user_turns(turns: List[Dict[str, Any]]) -> str:
    """Formata bot_conversations como transcript curto."""
    lines = []
    for t in turns:
        ts = t.get("created_at")
        if ts and hasattr(ts, "strftime"):
            ts_str = to_brt(ts).strftime("%d/%m %H:%M")
        else:
            ts_str = "??"
        content = (t.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 1200:
            content = content[:1200] + "...[trunc]"
        lines.append(f"[{ts_str}] Renato: {content}")
    return "\n".join(lines) if lines else "(nenhum turn)"


def _format_outgoing_messages(msgs: List[Dict[str, Any]]) -> str:
    """Formata messages outgoing (Renato -> contato pessoal/C0-2)."""
    lines = []
    for m in msgs:
        ts = m.get("enviado_em")
        if ts and hasattr(ts, "strftime"):
            ts_str = to_brt(ts).strftime("%d/%m %H:%M")
        else:
            ts_str = "??"
        nome = m.get("contact_nome") or "?"
        cid = m.get("contact_id")
        content = (m.get("conteudo") or "").strip()
        if not content:
            continue
        if len(content) > 800:
            content = content[:800] + "...[trunc]"
        lines.append(f"[{ts_str}] Renato -> {nome} (id={cid}): {content}")
    return "\n".join(lines) if lines else "(nenhuma mensagem outgoing pessoal)"


def _format_existing_memories(memories: List[Dict[str, Any]]) -> str:
    """Lista de memorias existentes pra Claude evitar redundancia obvia."""
    lines = []
    for m in memories[:30]:  # so as 30 mais recentes pra nao inflar prompt
        titulo = (m.get("titulo") or "")[:120]
        tipo = m.get("tipo") or "?"
        lines.append(f"- [{tipo}] {titulo}")
    return "\n".join(lines) if lines else "(nenhuma)"


# ============== Prompt ==============

_EXTRACTOR_PROMPT = """Voce e o Extractor Noturno do CoS digital "Tonha" — persona de chefe de gabinete pessoal do Renato Almeida Prado.

Tarefa: ler 24h de conversa (chat web + WhatsApp do Renato) e EXTRAIR aprendizado durador. Voce NAO faz resumo. Voce identifica sinais que valem virar memoria persistente.

==== INPUTS ====

Hoje (BRT): {today_brt}
Janela analisada: ultimas {window_hours}h.

Turns do Renato (role='user' no bot_conversations):
{user_turns}

Mensagens outgoing pra contatos pessoais/C0-C2 (Renato -> alguem proximo):
{outgoing_msgs}

Sintese da noite anterior (referencia, NAO redundar):
{synthesis_block}

Memorias existentes (NAO criar duplicata obvia):
{existing_memories}

==== O QUE EXTRAIR ====

Classifique cada candidato em UM destes buckets:

**A. fato_contato** — fato durador sobre um contato especifico
   - Ex SIM: "Emma faz ioga toda quinta" (recorrencia clara)
   - Ex SIM: "Pai da Marina mora em Sao Carlos"
   - Ex NAO: "Emma teve massagem cedo no sabado" (evento isolado)
   - Ex NAO: "Falei com Tiago hoje" (interacao, nao fato)
   - Precisa: nome do contato (ou ref clara), e fato verificavel.

**B. padrao** ou **reflexao** — observacao sobre o Renato
   - padrao: comportamento repetido ("repete que evita reunioes pos-almoco")
   - reflexao: autoanalise dele ("disse que esta drenado")
   - Ex NAO: estado emocional momentaneo de 1 unica menção.

**C. glossario** — expressao/gíria que Renato usa e que sem contexto a Tonha entenderia errado
   - Ex SIM: "catapora estourando" = ansiedade prestes a transbordar
   - Ex SIM: "fechar a conta" no sentido de pagar promessa antiga
   - titulo = a expressao curta; conteudo = "Significado: X. Uso: Y. Regiao/contexto: Z (se aplicavel)."
   - Nao incluir vocabulario padrao PT-BR.

**D. decisao** ou **compromisso**
   - decisao: escolha estrategica clara ("decidi parar de fazer pro-bono Alba ate Q4")
   - compromisso: promessa concreta dele ("vou mandar pra Vallen ate sexta")
   - Diferenca: decisao e estrategica/durada; compromisso tem prazo curto e e checavel.

**E. correcao** — Renato corrigindo a Tonha (LOOP DE CORRECAO, ALTA PRIORIDADE)
   Sinais (qualquer um basta):
   - "nao era pra avisar isso", "nao me notifica sobre X de novo"
   - "ta errado", "isso nao e prioridade"
   - "nao me chama assim", "para de Y"
   - "deveria ter X", "esse cara nunca foi prioridade"
   - "errou o tom", "muito formal", "muito longo"
   - reclamacoes diretas sobre comportamento do bot/assistente
   Output:
   - titulo: regra curta extraida (imperativa, ex: "Nao avisar sobre updates triviais de XYZ")
   - conteudo: "Regra: <regra>.\\nTurno original: <texto literal>\\nQuando: <timestamp BRT>"
   - tags inclui 'correcao' + tema (ex: 'correcao_notificacao', 'correcao_tom', 'correcao_prioridade', 'correcao_escopo')

==== CRITERIOS RIGOROSOS ====

So extrai se:
1. DURADOURO (vale a Tonha lembrar em 30+ dias)
2. PADRAO CLARO ou FATO VERIFICAVEL (nao palpite)
3. CONTEXTO SUFICIENTE (interpretavel depois sem voltar ao transcript)

NUNCA extrai:
- Estados emocionais momentaneos ("hoje to cansado")
- Eventos isolados sem padrao
- Opiniao sobre 3os sem evidencia
- Coisa ja dita na sintese da noite (a sintese ja cobre)
- Redundancia obvia com memorias existentes (lista acima)

Em caso de duvida: NAO extrai. Vazio e melhor que ruido.

==== OUTPUT ====

Responde APENAS JSON valido (sem markdown fence, sem prosa), neste schema:

{{
  "fatos_contato": [
    {{
      "contact_nome": "Emma",
      "contact_id": 123 ou null,
      "tipo": "rotina"|"preferencia"|"familia"|"trabalho"|"saude"|"outro",
      "titulo": "Faz ioga toda quinta",
      "resumo": "Emma faz ioga toda quinta de manha (mencionado 3x em conversas)",
      "fonte_evidencia": "trecho curto que justificou"
    }}
  ],
  "padroes_reflexoes": [
    {{
      "tipo": "padrao"|"reflexao",
      "titulo": "Evita reunioes pos-almoco",
      "conteudo": "Tem repetido que reunioes pos-almoco drenam; preferiu mover pra manha 3x essa semana.",
      "tags": ["energia", "agenda"]
    }}
  ],
  "glossario": [
    {{
      "titulo": "catapora estourando",
      "conteudo": "Significado: ansiedade prestes a transbordar. Uso: descreve estado interno tenso antes de reuniao importante. Regiao: gíria sul-BR.",
      "tags": ["giria", "estado_interno"]
    }}
  ],
  "decisoes_compromissos": [
    {{
      "tipo": "decisao"|"compromisso",
      "titulo": "Pausar pro-bono Alba ate Q4",
      "conteudo": "Decidiu pausar pro-bono Alba ate Q4 pra focar em imensIAH; comunicar Tati semana que vem.",
      "tags": ["alba", "foco"]
    }}
  ],
  "correcoes": [
    {{
      "titulo": "Nao avisar sobre updates triviais Marcos",
      "conteudo": "Regra: nao notificar sobre updates rotineiros do Marcos.\\nTurno original: 'cara, para de me avisar sempre que o Marcos manda bom dia'\\nQuando: 08/06 14:23",
      "tags": ["correcao", "correcao_notificacao"]
    }}
  ]
}}

Se um bucket nao tem nada, mande lista vazia [].

LEMBRE: rigor > volume. Extrair 2 coisas bem extraidas vale mais que 10 ruidosas.
"""


# ============== Claude call ==============

def _call_claude(prompt: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Chama Sonnet 4.6 e parseia JSON. Retorna (parsed_json|None, meta_dict).

    meta_dict: {cost_usd, input_tokens, output_tokens, error?}
    """
    meta: Dict[str, Any] = {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    if not ANTHROPIC_API_KEY:
        meta["error"] = "no_api_key"
        return None, meta

    try:
        with httpx.Client(timeout=COS_EXTRACTOR_TIMEOUT_S) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": COS_EXTRACTOR_MODEL,
                    "max_tokens": COS_EXTRACTOR_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            meta["error"] = f"claude_{resp.status_code}: {resp.text[:200]}"
            logger.warning(f"cos_extractor claude error: {meta['error']}")
            return None, meta
        data = resp.json()
    except Exception as e:
        meta["error"] = f"claude_exception: {e}"
        logger.warning(f"cos_extractor claude exception: {e}")
        return None, meta

    # Token usage + cost
    usage = data.get("usage", {})
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    # Sonnet 4.6 pricing: $3/$15 per 1M
    cost = (inp * 3.0 / 1_000_000) + (out * 15.0 / 1_000_000)
    meta["input_tokens"] = inp
    meta["output_tokens"] = out
    meta["cost_usd"] = round(cost, 4)

    # Extrai texto
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    text = text.strip()
    if not text:
        meta["error"] = "empty_response"
        return None, meta

    # Strip fence se Claude ignorar instrucao e mandar markdown
    text = _strip_json_fence(text)

    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            meta["error"] = "not_a_dict"
            return None, meta
        return parsed, meta
    except json.JSONDecodeError as e:
        meta["error"] = f"json_parse: {e}; preview={text[:200]}"
        logger.warning(f"cos_extractor json parse failed: {meta['error']}")
        return None, meta


def _strip_json_fence(text: str) -> str:
    """Remove fences markdown ```json ... ``` se vierem."""
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


# ============== Save helpers ==============

def _save_contact_memory(item: Dict[str, Any]) -> Optional[int]:
    """Insere fato durador em contact_memories.

    Schema: contact_id, tipo (subtipo opcional), titulo, resumo, conteudo_completo,
    data_ocorrencia. importancia default 5.
    """
    contact_id = item.get("contact_id")
    if not contact_id:
        # Sem contact_id, nao da pra salvar em contact_memories (FK NOT NULL na pratica
        # — o sentido de contact_memories e por-contato). Retorna None — vai virar
        # ruido no audit log porque nao salvamos.
        return None
    titulo = (item.get("titulo") or "")[:500]
    resumo = item.get("resumo") or titulo
    subtipo = item.get("tipo") or "outro"
    fonte = item.get("fonte_evidencia") or ""

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO contact_memories (
                    contact_id, tipo, subtipo, titulo, resumo, conteudo_completo,
                    importancia, data_ocorrencia
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (
                    contact_id,
                    "fato",  # tipo top-level: fato durador
                    subtipo,
                    titulo,
                    resumo,
                    fonte,
                    5,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row["id"] if row else None
    except Exception as e:
        logger.warning(f"_save_contact_memory falhou (cid={contact_id}): {e}")
        return None


def _save_system_memory_via_helper(
    titulo: str,
    conteudo: str,
    tipo: str,
    tags: List[str],
) -> Optional[int]:
    """Wrapper sobre save_system_memory (que ja gera embedding via Voyage).

    referencia_inicio/fim = data BRT atual (janela de extracao = hoje).
    fonte = 'cos_extractor'.
    """
    today_brt = to_brt(now_utc()).date()
    try:
        return save_system_memory(
            titulo=titulo[:500],
            conteudo=conteudo,
            tipo=tipo,
            tags=tags,
            fonte="cos_extractor",
            referencia_inicio=today_brt,
            referencia_fim=today_brt,
        )
    except Exception as e:
        logger.warning(f"_save_system_memory_via_helper falhou ({tipo}): {e}")
        return None


# ============== Dedup ==============

def _dedup_check(
    titulo: str,
    conteudo: str,
    tipo_filter: Optional[str],
    existing: List[Dict[str, Any]],
) -> Tuple[bool, float, Optional[int]]:
    """Calcula embedding do candidato e compara com existing.
    Retorna (eh_duplicata, max_sim, id_match).

    Se Voyage nao configurado ou falhou, retorna (False, 0, None) — sem dedup
    semantico, deixa Claude controlar via prompt + memorias existentes listadas.
    """
    if not embeddings_enabled():
        return False, 0.0, None
    text = f"{titulo}\n\n{conteudo}".strip()
    if not text:
        return False, 0.0, None
    try:
        vec = embed_sync(text, input_type="document")
    except Exception as e:
        logger.warning(f"_dedup_check embed falhou: {e}")
        return False, 0.0, None
    if not vec:
        return False, 0.0, None
    max_sim, id_match = _max_similarity(vec, existing, tipo_filter=tipo_filter)
    if max_sim >= DEDUP_SIMILARITY_THRESHOLD:
        return True, max_sim, id_match
    return False, max_sim, id_match


# ============== Process buckets ==============

def _process_fatos_contato(
    items: List[Dict[str, Any]],
    existing: List[Dict[str, Any]],
    counts: Dict[str, int],
) -> None:
    """Processa bucket A. contact_memories nao usa dedup semantico via system_memories
    (sao tabelas diferentes); deixamos Claude responsavel pelo nao-duplicar."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cid = item.get("contact_id")
        nome = item.get("contact_nome")
        if not cid:
            counts["fatos_contato_skipped_no_cid"] = counts.get("fatos_contato_skipped_no_cid", 0) + 1
            audit_log(
                "cos_extractor.skip_fato_contato",
                actor="cos_extractor",
                details={"reason": "no_contact_id", "nome": nome, "titulo": item.get("titulo")},
            )
            continue
        mid = _save_contact_memory(item)
        if mid:
            counts["fatos_contato_criados"] = counts.get("fatos_contato_criados", 0) + 1
            audit_log(
                "cos_extractor.fato_contato_criado",
                entity_type="contact_memory",
                entity_id=mid,
                actor="cos_extractor",
                details={"contact_id": cid, "nome": nome, "titulo": item.get("titulo")},
            )
        else:
            counts["fatos_contato_falhou"] = counts.get("fatos_contato_falhou", 0) + 1


def _process_system_bucket(
    items: List[Dict[str, Any]],
    bucket_name: str,
    default_tipo: str,
    existing: List[Dict[str, Any]],
    counts: Dict[str, int],
    extra_tags: Optional[List[str]] = None,
    tipo_field: str = "tipo",
) -> None:
    """Generico pra buckets B/C/D/E que viram system_memories.

    bucket_name: rotulo pra counts/audit (ex 'padrao_reflexao').
    default_tipo: tipo usado se item nao especificar.
    extra_tags: tags adicionadas a todas as memorias do bucket.
    tipo_field: nome do campo no item que define tipo (alguns buckets tem tipo
                dentro do item — padrao/reflexao, decisao/compromisso).
    """
    for item in items or []:
        if not isinstance(item, dict):
            continue
        titulo = (item.get("titulo") or "").strip()
        conteudo = (item.get("conteudo") or "").strip()
        if not titulo or not conteudo:
            counts[f"{bucket_name}_skipped_invalid"] = counts.get(f"{bucket_name}_skipped_invalid", 0) + 1
            continue

        tipo = (item.get(tipo_field) or default_tipo).strip().lower()
        # Sanity: limita tipo a alguns conhecidos
        if tipo not in {"padrao", "reflexao", "glossario", "decisao", "compromisso", "correcao"}:
            tipo = default_tipo

        tags = list(item.get("tags") or [])
        if extra_tags:
            for t in extra_tags:
                if t not in tags:
                    tags.append(t)

        # Dedup semantico contra existing do mesmo tipo
        is_dup, max_sim, id_match = _dedup_check(titulo, conteudo, tipo_filter=tipo, existing=existing)
        if is_dup:
            counts[f"{bucket_name}_dedup"] = counts.get(f"{bucket_name}_dedup", 0) + 1
            audit_log(
                "cos_extractor.dedup_skipped",
                actor="cos_extractor",
                details={
                    "bucket": bucket_name,
                    "tipo": tipo,
                    "titulo": titulo,
                    "max_sim": round(max_sim, 3),
                    "matched_id": id_match,
                },
            )
            continue

        mid = _save_system_memory_via_helper(titulo, conteudo, tipo, tags)
        if mid:
            counts[f"{bucket_name}_criados"] = counts.get(f"{bucket_name}_criados", 0) + 1
            audit_log(
                "cos_extractor.system_memory_criada",
                entity_type="system_memory",
                entity_id=mid,
                actor="cos_extractor",
                details={
                    "bucket": bucket_name,
                    "tipo": tipo,
                    "titulo": titulo,
                    "tags": tags,
                    "max_sim_existing": round(max_sim, 3),
                },
            )
        else:
            counts[f"{bucket_name}_falhou"] = counts.get(f"{bucket_name}_falhou", 0) + 1


# ============== Main ==============

async def run_extractor(window_hours: int = 24) -> Dict[str, Any]:
    """Roda o extrator noturno. Async pra encaixar no padrao dos crons.

    Retorna dict com {status, counts, cost_usd, duration_ms, ...} pro endpoint
    do cron registrar via @track_cron_run.
    """
    started = time.time()
    counts: Dict[str, int] = {}

    # 1. Budget check
    budget = _check_budget()
    if budget.get("abort"):
        return {
            "status": "aborted_budget",
            "reason": budget.get("reason"),
            "today_usd": budget.get("today_usd"),
            "duration_ms": int((time.time() - started) * 1000),
            "counts": counts,
            "cost_usd": 0.0,
        }

    if not ANTHROPIC_API_KEY:
        return {
            "status": "skipped",
            "reason": "no_api_key",
            "duration_ms": int((time.time() - started) * 1000),
            "counts": counts,
            "cost_usd": 0.0,
        }

    # 2. Loaders (rodam off thread pra nao bloquear loop se houverem queries lentas)
    loop = asyncio.get_event_loop()
    user_turns, outgoing, synthesis, existing = await asyncio.gather(
        loop.run_in_executor(None, _load_user_turns, window_hours),
        loop.run_in_executor(None, _load_outgoing_personal_messages, window_hours),
        loop.run_in_executor(None, _load_recent_synthesis, window_hours),
        loop.run_in_executor(None, _load_existing_memories_for_dedup, MAX_EXISTING_MEMORIES_FOR_DEDUP),
    )

    if not user_turns and not outgoing:
        return {
            "status": "skipped",
            "reason": "no_input",
            "duration_ms": int((time.time() - started) * 1000),
            "counts": counts,
            "cost_usd": 0.0,
            "today_usd": budget.get("today_usd"),
        }

    # 3. Build prompt
    today_brt = to_brt(now_utc()).strftime("%d/%m/%Y")
    synthesis_block = (
        f"[{to_brt(synthesis['criado_em']).strftime('%d/%m %H:%M')}] {synthesis.get('titulo','')}\n{synthesis.get('conteudo','')[:2000]}"
        if synthesis else "(sem sintese nas ultimas 24h)"
    )
    prompt = _EXTRACTOR_PROMPT.format(
        today_brt=today_brt,
        window_hours=window_hours,
        user_turns=_format_user_turns(user_turns),
        outgoing_msgs=_format_outgoing_messages(outgoing),
        synthesis_block=synthesis_block,
        existing_memories=_format_existing_memories(existing),
    )

    # 4. Call Claude (sync httpx em executor pra nao bloquear)
    parsed, meta = await loop.run_in_executor(None, _call_claude, prompt)

    if parsed is None:
        result = {
            "status": "error",
            "reason": meta.get("error", "claude_failed"),
            "duration_ms": int((time.time() - started) * 1000),
            "counts": counts,
            "cost_usd": meta.get("cost_usd", 0.0),
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
        }
        audit_log(
            "cos_extractor.run_failed",
            actor="cos_extractor",
            details={"error": meta.get("error"), "cost_usd": meta.get("cost_usd")},
        )
        return result

    # 5. Process buckets
    _process_fatos_contato(parsed.get("fatos_contato"), existing, counts)
    _process_system_bucket(
        items=parsed.get("padroes_reflexoes"),
        bucket_name="padroes_reflexoes",
        default_tipo="reflexao",
        existing=existing,
        counts=counts,
    )
    _process_system_bucket(
        items=parsed.get("glossario"),
        bucket_name="glossario",
        default_tipo="glossario",
        existing=existing,
        counts=counts,
        extra_tags=["glossario"],
        tipo_field="_unused_",  # glossario sempre tipo=glossario
    )
    _process_system_bucket(
        items=parsed.get("decisoes_compromissos"),
        bucket_name="decisoes_compromissos",
        default_tipo="decisao",
        existing=existing,
        counts=counts,
    )
    _process_system_bucket(
        items=parsed.get("correcoes"),
        bucket_name="correcoes",
        default_tipo="correcao",
        existing=existing,
        counts=counts,
        extra_tags=["correcao"],
        tipo_field="_unused_",  # correcao sempre tipo=correcao
    )

    duration_ms = int((time.time() - started) * 1000)
    result = {
        "status": "success",
        "counts": counts,
        "cost_usd": meta.get("cost_usd", 0.0),
        "input_tokens": meta.get("input_tokens", 0),
        "output_tokens": meta.get("output_tokens", 0),
        "duration_ms": duration_ms,
        "inputs": {
            "user_turns": len(user_turns),
            "outgoing_msgs": len(outgoing),
            "synthesis_present": synthesis is not None,
            "existing_memories_loaded": len(existing),
        },
        "today_usd": budget.get("today_usd"),
        "embeddings_enabled": embeddings_enabled(),
    }
    audit_log(
        "cos_extractor.run_ok",
        actor="cos_extractor",
        details={
            "counts": counts,
            "cost_usd": meta.get("cost_usd"),
            "duration_ms": duration_ms,
        },
    )
    return result
