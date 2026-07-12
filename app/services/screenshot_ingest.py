"""
Ingestão de screenshots de conversas WhatsApp via Claude Vision.

Fluxo:
  1. Recebe imagem (base64 ou bytes)
  2. Claude Sonnet extrai estrutura da conversa (remetente, horário, conteúdo)
  3. Mensagens são salvas na tabela `messages` com metadata.imported_from='screenshot'
  4. analyze_message_in_background é disparado para mensagens incoming
"""

import base64
from services import llm
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from database import get_connection

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

SCREENSHOT_EXTRACT_PROMPT = """Esta é um screenshot de uma conversa do WhatsApp.
Extraia TODAS as mensagens visíveis e retorne um JSON array com o seguinte formato exato:

[
  {
    "sender_type": "me" ou "them",
    "sender_name": "nome exibido (ou null se não visível)",
    "time": "horário exibido (ex: 09:14, 14:26)",
    "content": "conteúdo textual da mensagem"
  }
]

Regras:
- "me" = mensagem enviada pelo dono do chat (geralmente balão à direita, verde/azul)
- "them" = mensagem recebida (balão à esquerda, branco/cinza)
- Para áudios: content = "[Áudio]"
- Para imagens: content = "[Imagem]"
- Para figurinhas: content = "[Figurinha]"
- Para chamadas: content = "[Chamada de voz]" ou "[Chamada de vídeo]"
- Inclua indicadores de sistema como "Mensagem apagada" se visíveis
- Preserve emojis
- Se não conseguir determinar sender_type, use "unknown"
- Retorne APENAS o JSON array, sem markdown, sem comentários"""


async def _ocr_screenshot(image_b64: str, mime: str = "image/jpeg") -> Optional[list]:
    """Chama Claude Sonnet para extrair conversa do screenshot. Retorna lista de msgs ou None."""
    if not ANTHROPIC_API_KEY:
        logger.warning("screenshot_ingest: ANTHROPIC_API_KEY não configurada")
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            r = await cli.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": llm.BALANCED,
                    "max_tokens": 4000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                            {"type": "text", "text": SCREENSHOT_EXTRACT_PROMPT}
                        ]
                    }]
                },
            )
        if r.status_code != 200:
            logger.warning(f"screenshot OCR: HTTP {r.status_code}: {r.text[:300]}")
            return None
        raw = r.json()["content"][0]["text"].strip()
        # Remove markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"screenshot OCR error: {e}")
        return None


def _find_or_create_contact(cursor, contact_id: Optional[int], phone: Optional[str],
                             contact_name: Optional[str]) -> Optional[int]:
    """Resolve contact_id a partir de contact_id, phone ou nome. Cria phantom se não encontrar."""
    if contact_id:
        cursor.execute("SELECT id FROM contacts WHERE id = %s", (contact_id,))
        if cursor.fetchone():
            return contact_id

    if phone:
        clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        cursor.execute(
            "SELECT id FROM contacts WHERE telefones::text ILIKE %s LIMIT 1",
            (f"%{clean}%",)
        )
        row = cursor.fetchone()
        if row:
            return row["id"]

    # Cria phantom
    display = contact_name or phone or "Desconhecido (screenshot)"
    cursor.execute("""
        INSERT INTO contacts (nome, tipo, criado_em)
        VALUES (%s, 'pessoa', NOW())
        RETURNING id
    """, (display,))
    new_id = cursor.fetchone()["id"]
    logger.info(f"screenshot_ingest: phantom contact criado #{new_id} ({display})")
    return new_id


def _find_or_create_conversation(cursor, contact_id: int) -> int:
    cursor.execute(
        "SELECT id FROM conversations WHERE contact_id = %s AND canal = 'whatsapp'",
        (contact_id,)
    )
    row = cursor.fetchone()
    if row:
        return row["id"]
    cursor.execute("""
        INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
        VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
        RETURNING id
    """, (contact_id,))
    return cursor.fetchone()["id"]


async def ingest_screenshot(
    image_bytes: bytes,
    mime: str = "image/jpeg",
    contact_id: Optional[int] = None,
    phone: Optional[str] = None,
    contact_name: Optional[str] = None,
    context_note: Optional[str] = None,
    trigger_analysis: bool = True,
) -> dict:
    """
    Pipeline principal de ingestão de screenshot.

    Returns dict com:
      - messages_saved: int
      - contact_id: int
      - extracted: list (raw OCR output)
      - skipped: int (duplicatas ou sem conteúdo)
      - errors: list
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    extracted = await _ocr_screenshot(image_b64, mime)
    if not extracted:
        return {"messages_saved": 0, "contact_id": None, "extracted": [], "skipped": 0,
                "errors": ["OCR falhou — imagem não reconhecida ou API indisponível"]}

    if not isinstance(extracted, list) or not extracted:
        return {"messages_saved": 0, "contact_id": None, "extracted": extracted,
                "skipped": 0, "errors": ["OCR retornou formato inesperado"]}

    conn = get_connection()
    cursor = conn.cursor()

    resolved_contact_id = _find_or_create_contact(cursor, contact_id, phone, contact_name)
    conversation_id = _find_or_create_conversation(cursor, resolved_contact_id)

    saved, skipped, errors = 0, 0, []
    saved_ids = []

    for i, msg in enumerate(extracted):
        try:
            content = (msg.get("content") or "").strip()
            sender_type = msg.get("sender_type", "unknown")
            sender_name = msg.get("sender_name")
            time_str = msg.get("time", "")

            if not content:
                skipped += 1
                continue

            direcao = "outgoing" if sender_type == "me" else "incoming"

            # external_id único baseado em hash para evitar duplicatas em reimports
            import hashlib
            dedup_key = hashlib.md5(f"screenshot:{resolved_contact_id}:{i}:{content[:50]}".encode()).hexdigest()
            ext_id = f"screenshot_{dedup_key}"

            cursor.execute("SELECT id FROM messages WHERE external_id = %s", (ext_id,))
            if cursor.fetchone():
                skipped += 1
                continue

            metadata = {
                "imported_from": "screenshot",
                "sender_name": sender_name,
                "time_in_screenshot": time_str,
                "context_note": context_note,
            }

            cursor.execute("""
                INSERT INTO messages
                (conversation_id, contact_id, external_id, direcao, conteudo, metadata, criado_em)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
            """, (conversation_id, resolved_contact_id, ext_id, direcao, content,
                  json.dumps(metadata)))

            new_id = cursor.fetchone()["id"]
            saved_ids.append((new_id, direcao, content))
            saved += 1

        except Exception as e:
            errors.append(f"msg {i}: {e}")
            logger.warning(f"screenshot_ingest msg {i} error: {e}")

    # Atualiza conversa
    if saved > 0:
        cursor.execute("""
            UPDATE conversations
            SET total_mensagens = COALESCE(total_mensagens, 0) + %s,
                atualizado_em = NOW()
            WHERE id = %s
        """, (saved, conversation_id))

    conn.commit()

    # Dispara análise em background para incoming
    if trigger_analysis:
        try:
            import asyncio
            from integrations.evolution_api import analyze_message_in_background
            for msg_id, direcao, content in saved_ids:
                if direcao == "incoming" and content:
                    asyncio.create_task(
                        analyze_message_in_background(msg_id, resolved_contact_id, content)
                    )
        except Exception as e:
            logger.warning(f"screenshot_ingest: analyze_in_background error: {e}")

    logger.info(f"screenshot_ingest: {saved} saved, {skipped} skipped, contact={resolved_contact_id}")
    return {
        "messages_saved": saved,
        "contact_id": resolved_contact_id,
        "conversation_id": conversation_id,
        "extracted": extracted,
        "skipped": skipped,
        "errors": errors,
    }
