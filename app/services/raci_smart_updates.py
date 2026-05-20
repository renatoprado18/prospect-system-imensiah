"""
RACI Smart Updates — interpretacao livre de mensagens do grupo WhatsApp.

Substitui o filtro de keyword + regex puro do parse_raci_update por classificador
Claude que aceita texto livre (e futuramente docs). Mantem parse_raci_update
como fast-path: regex roda primeiro, AI so dispara se regex nao matchar.

Fluxo:
1. Webhook recebe msg do grupo Conselho
2. Tenta parse_raci_update (regex) — se match, aplica
3. Se nao match, chama propose_updates_from_text (AI)
4. Filtra por confianca: alta = auto-apply; media = notif Renato; baixa = ignora
5. Aplica alta confianca + responde no grupo
6. Notifica Renato no WA privado se houver propostas de media confianca

Cost: ~Haiku 4.5 ~$0.001/msg. Grupo ativo 20 msg/dia = ~$0.6/mes/grupo.
"""
from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Tamanho minimo de msg pra rodar AI (filtra "ok", "👍", reactions)
MIN_TEXT_LEN_FOR_AI = 12

# Phase 2: limite anti-loop pra docs gigantes baixados da Evolution.
MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 20MB


def _get_open_items(empresa_id: str) -> List[Dict]:
    """Retorna itens nao-concluidos da empresa pra usar como contexto do classifier."""
    import psycopg2, psycopg2.extras
    if not CONSELHOOS_DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id, acao, status, prazo, responsavel_r
            FROM raci_itens
            WHERE empresa_id = %s AND status != 'concluido'
            ORDER BY prazo NULLS LAST
        """, (empresa_id,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.warning(f"_get_open_items({empresa_id}) falhou: {e}")
        return []


async def propose_updates_from_text(text: str, empresa_id: str) -> List[Dict]:
    """Le msg livre + contexto dos itens abertos da empresa. Retorna lista de
    propostas: [{item_id, acao_short, action, new_status, new_prazo, notes,
    evidencia, confianca}]. Vazio se nada matchou.

    confianca = 'alta' (auto-apply) | 'media' (notif Renato) | 'baixa' (ignora).
    """
    if not ANTHROPIC_API_KEY:
        return []
    text = (text or "").strip()
    if len(text) < MIN_TEXT_LEN_FOR_AI:
        return []
    items = _get_open_items(empresa_id)
    if not items:
        return []

    items_str = "\n".join([
        f"[{i+1}] id={it['id']} | {it['acao']}\n    Responsavel: {it.get('responsavel_r') or '?'} | Prazo: {it['prazo']} | Status: {it['status']}"
        for i, it in enumerate(items)
    ])

    prompt = f"""Voce e analista de governanca interpretando mensagens informais de grupo WhatsApp do Conselho.

CONTEXTO: itens RACI abertos (nao-concluidos) da empresa. Cada um tem um id UUID.

{items_str}

MENSAGEM RECEBIDA: \"\"\"{text}\"\"\"

TAREFA: identifique se a mensagem reporta status sobre algum dos itens acima. Pode mencionar 0, 1 ou varios itens.

Responda JSON array (vazio se nada matchou):

[
  {{
    "item_id": "<uuid exato>",
    "action": "update_status" | "update_prazo" | "add_note" | "complete",
    "new_status": "concluido" | "em_andamento" | "pendente" | "cancelado" | null,
    "new_prazo": "YYYY-MM-DD" | null,
    "notes": "<texto extraido da msg, max 200 chars>" | null,
    "evidencia": "<trecho exato da msg que justifica, max 100 chars>",
    "confianca": "alta" | "media" | "baixa"
  }}
]

Regras:
- confianca=alta: msg cita item de forma inequivoca (acao, nome do responsavel, dados especificos). Ex: "Kommo CRM ativado hoje" + tem item "Ativar Kommo CRM".
- confianca=media: msg pode estar relacionada mas ambigua. Ex: "Reuniao com Camila ontem foi boa" + tem item "Conversar com Camila" (pode ser update ou so update informal sem mudanca de status).
- confianca=baixa: matching forçado, contexto insuficiente. Prefira retornar vazio em vez de baixa.
- Mensagens irrelevantes (saudacao, off-topic, emoji isolado): retorne array vazio [].
- Se a msg diz "concluido", "feito", "pronto", "ok", "ja foi" sobre item → action=complete, new_status=concluido.
- Se reporta progresso sem finalizar → action=update_status, new_status=em_andamento.
- Se anuncia novo prazo → action=update_prazo + new_prazo.
- Se adiciona contexto sem mudar status → action=add_note.

Responda APENAS o JSON array."""

    try:
        async with httpx.AsyncClient(timeout=20.0) as cli:
            r = await cli.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1200,
                      "messages": [{"role": "user", "content": prompt}]}
            )
        if r.status_code != 200:
            logger.warning(f"propose_updates_from_text: API {r.status_code}: {r.text[:200]}")
            return []
        text_out = r.json()["content"][0]["text"]
        # Extrai JSON array
        start = text_out.find('[')
        end = text_out.rfind(']') + 1
        if start < 0:
            return []
        proposals = json.loads(text_out[start:end])
        # Validacao basica
        valid = [p for p in proposals if isinstance(p, dict) and p.get('item_id') and p.get('confianca')]
        return valid
    except Exception as e:
        logger.warning(f"propose_updates_from_text falhou: {type(e).__name__}: {e}")
        return []


def apply_proposal(proposal: Dict, empresa_id: str) -> Optional[Dict]:
    """Aplica 1 proposta no DB + audit log. Retorna {acao, old_status, new_status}
    pra UI/confirmacao no grupo. None se item nao encontrado."""
    import psycopg2, psycopg2.extras
    if not CONSELHOOS_DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id, acao, status FROM raci_itens WHERE id = %s", (proposal['item_id'],))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        target = dict(row)

        # Build update — skip se nao tem mudanca real (msg-pergunta ou contexto)
        sets = []
        params = []
        new_status = proposal.get('new_status')
        if new_status and new_status != target['status']:
            sets.append("status = %s"); params.append(new_status)
        if proposal.get('new_prazo'):
            sets.append("prazo = %s"); params.append(proposal['new_prazo'])
        if proposal.get('notes'):
            sets.append("notas = COALESCE(notas, '') || %s")
            params.append(f"\n[{datetime.now().strftime('%d/%m')}] {proposal['notes'][:200]}")
        if not sets:
            conn.close()
            return None  # no-op — nao polui audit log nem reply no grupo
        sets.append("updated_at = NOW()")
        params.append(target['id'])
        cur.execute(f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s", tuple(params))
        conn.commit()
        conn.close()

        # Audit log
        try:
            from services.agent_actions import log_action
            log_action(
                action_type='raci_status_updated',
                category='conselho',
                title=f"RACI (AI): '{(target['acao'] or '')[:60]}' → {proposal.get('new_status') or 'note'}",
                scope_ref={'raci_item_id': str(target['id']), 'empresa_id': str(empresa_id)},
                source='raci_smart_updates.apply_proposal',
                payload={
                    'old_status': target['status'],
                    'new_status': proposal.get('new_status'),
                    'new_prazo': proposal.get('new_prazo'),
                    'notes': proposal.get('notes'),
                    'evidencia': proposal.get('evidencia'),
                    'confianca': proposal.get('confianca'),
                },
                undo_hint=f"UPDATE raci_itens SET status='{target['status']}' WHERE id='{target['id']}'::uuid;",
            )
        except Exception as e:
            logger.warning(f"audit log apply_proposal falhou: {e}")

        return {
            'item_id': target['id'],
            'acao': target['acao'],
            'old_status': target['status'],
            'new_status': proposal.get('new_status') or target['status'],
            'evidencia': proposal.get('evidencia'),
        }
    except Exception as e:
        logger.warning(f"apply_proposal falhou: {e}")
        return None


async def _download_media_from_evolution(message_key: Dict, instance: str) -> Optional[Dict]:
    """Baixa media (base64 + mime) da Evolution API via key.id.
    Returns {'base64', 'mimetype'} ou None. Timeout 15s pra nao bloquear webhook."""
    evo_url = (os.getenv("EVOLUTION_API_URL") or "").rstrip('/')
    evo_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    if not evo_url or not evo_key or not instance:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r = await cli.post(
                f"{evo_url}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": evo_key, "Content-Type": "application/json"},
                json={"message": {"key": message_key}, "convertToMp4": False},
            )
        if r.status_code not in (200, 201):
            logger.warning(f"download media: HTTP {r.status_code}")
            return None
        d = r.json()
        b64 = d.get("base64") or ""
        mime = (d.get("mimetype") or "").split(";")[0].strip()
        if not b64:
            return None
        if len(b64) * 0.75 > MAX_MEDIA_BYTES:
            logger.warning(f"media too large: ~{int(len(b64)*0.75/1024/1024)}MB")
            return None
        return {"base64": b64, "mimetype": mime or "application/octet-stream"}
    except Exception as e:
        logger.warning(f"download media error: {e}")
        return None


async def _claude_media_to_text(b64: str, mime: str, instruction: str,
                                  model: str = "claude-haiku-4-5-20251001") -> Optional[str]:
    """Envia media pro Claude com instrucao de extracao. Audio/PDF = type=document; image = type=image."""
    if not ANTHROPIC_API_KEY:
        return None
    is_image = mime.startswith("image/")
    block = (
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
        if is_image
        else {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    )
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            r = await cli.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": 2000,
                      "messages": [{"role": "user", "content": [block, {"type": "text", "text": instruction}]}]},
            )
        if r.status_code != 200:
            logger.warning(f"claude media: HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"claude media error: {e}")
        return None


def _extract_docx_text(b64: str) -> Optional[str]:
    """Extrai texto de .docx localmente via python-docx (paragrafos + tabelas)."""
    try:
        import base64, io
        from docx import Document
        doc = Document(io.BytesIO(base64.b64decode(b64)))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        text += "\n" + cell.text
        return text or None
    except Exception as e:
        logger.warning(f"docx extract error: {e}")
        return None


async def extract_text_from_media(message: Dict, message_key: Dict, instance: str,
                                    caption: str = "") -> Optional[str]:
    """Detecta tipo de media (audio/image/document) e extrai texto via Claude
    multimodal ou python-docx. Junta caption se houver. Returns text ou None."""
    has_audio = "audioMessage" in message
    has_image = "imageMessage" in message
    has_doc = "documentMessage" in message
    if not (has_audio or has_image or has_doc):
        return None

    media = await _download_media_from_evolution(message_key, instance)
    if not media:
        return None
    b64 = media["base64"]
    mime = media["mimetype"]

    extracted: Optional[str] = None

    if has_audio:
        if not mime or mime == "application/octet-stream":
            mime = "audio/ogg"  # WA default
        extracted = await _claude_media_to_text(
            b64, mime,
            "Transcreva este audio em portugues. Responda APENAS a transcricao, sem comentarios."
        )

    elif has_image:
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        extracted = await _claude_media_to_text(
            b64, mime,
            "Descreva o conteudo desta imagem em portugues — texto, dados, contexto. "
            "Se for screenshot de planilha/dashboard/conversa, transcreva o conteudo literal. "
            "Maximo 500 palavras."
        )

    elif has_doc:
        doc_msg = message.get("documentMessage", {}) or {}
        filename = (doc_msg.get("fileName") or "").lower()
        if filename.endswith(".docx") or "wordprocessingml" in mime:
            extracted = _extract_docx_text(b64)
        elif filename.endswith(".pdf") or "pdf" in mime:
            extracted = await _claude_media_to_text(
                b64, "application/pdf",
                "Extraia o conteudo textual deste PDF em portugues. Foque em dados, decisoes, "
                "prazos, nomes. Pule cabecalhos/rodapes repetitivos. Maximo 2000 palavras."
            )
        elif filename.endswith((".txt", ".md")) or mime.startswith("text/"):
            try:
                import base64
                extracted = base64.b64decode(b64).decode("utf-8", errors="replace")[:5000]
            except Exception:
                pass
        else:
            logger.info(f"documentMessage tipo nao suportado: {filename} mime={mime}")

    if not extracted:
        return None

    extracted = extracted.strip()
    if caption and caption.strip():
        extracted = f"[Legenda: {caption.strip()}]\n\n{extracted}"
    return extracted


async def process_group_message(text: str, empresa_id: str, empresa_nome: str = "") -> Dict:
    """Pipeline completo: tenta regex, fallback AI, aplica alta confianca,
    notifica Renato pra media confianca. Retorna resumo do que rolou.

    Returns dict com:
      - applied: list de itens atualizados (pra confirmar no grupo)
      - pending_review: list de propostas que precisam aprovacao Renato
      - skipped_low: count de propostas baixa-conf descartadas
    """
    # 1. Fast-path: regex (free)
    try:
        from services.raci_weekly_report import parse_raci_update
        regex_result = parse_raci_update(text, empresa_id)
        if regex_result:
            return {"applied": [regex_result], "pending_review": [], "skipped_low": 0, "source": "regex"}
    except Exception as e:
        logger.warning(f"regex fast-path falhou: {e}")

    # 2. AI fallback
    proposals = await propose_updates_from_text(text, empresa_id)
    if not proposals:
        return {"applied": [], "pending_review": [], "skipped_low": 0, "source": "ai_no_match"}

    applied = []
    pending = []
    skipped = 0
    for p in proposals:
        conf = (p.get('confianca') or '').lower()
        if conf == 'alta':
            r = apply_proposal(p, empresa_id)
            if r: applied.append(r)
        elif conf == 'media':
            pending.append(p)
        else:
            skipped += 1

    # 3. Notif Renato pra media
    if pending:
        try:
            from services.intel_bot import send_intel_notification
            lines = [f"⚠️ Update RACI {empresa_nome or 'empresa'} precisa aprovacao:"]
            for p in pending:
                lines.append(f"- _{(p.get('evidencia') or '')[:80]}_")
                lines.append(f"  → propor {p.get('new_status') or 'note'} (conf {p.get('confianca')})")
            lines.append(f"\nResponda no app /editorial ou edite manual no ConselhoOS.")
            await send_intel_notification("\n".join(lines))
        except Exception as e:
            logger.warning(f"notif Renato pending review falhou: {e}")

    return {"applied": applied, "pending_review": pending, "skipped_low": skipped, "source": "ai"}
