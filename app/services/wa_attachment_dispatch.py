"""Dispatch WA attachments (PDF, image, audio) to Railway worker for extraction.

The worker (workers/audio-transcriber) extracts content and persists to
`wa_attachments` (idempotent via UNIQUE message_id+kind). Fire-and-forget
from caller — short timeout, errors logged.

Used by `process_incoming_message` (DM + group flows on rap-whatsapp). The
intel-bot has its own inline dispatch in `_handle_intel_bot_message`.
"""

import os
import logging
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def _detect_attachment_kind(message_obj: Dict) -> Optional[str]:
    """Returns 'audio', 'image', 'pdf', or None."""
    if not isinstance(message_obj, dict):
        return None
    if "audioMessage" in message_obj:
        return "audio"
    if "imageMessage" in message_obj:
        return "image"
    doc = message_obj.get("documentMessage") or {}
    if doc:
        mime = (doc.get("mimetype") or "").lower()
        fname = (doc.get("fileName") or "").lower()
        if "pdf" in mime or fname.endswith(".pdf"):
            return "pdf"
    return None


async def dispatch_attachment_to_worker(
    message_obj: Dict,
    key: Dict,
    phone: str,
    message_id: str,
    source: str = "main_instance",
) -> Dict:
    """Detect attachment in message_obj and dispatch to Railway worker.

    Worker endpoints (/transcribe, /analyze-pdf, /analyze-image) persist
    extracted text into `wa_attachments`. Idempotent on (message_id, kind).

    Fire-and-forget intended: caller wraps in asyncio.create_task. Timeout
    8s on the dispatch POST itself; worker continues async on its side.
    """
    kind = _detect_attachment_kind(message_obj)
    if not kind:
        return {"dispatched": False, "reason": "no_attachment"}

    worker_url = os.getenv("AUDIO_WORKER_URL", "").strip()
    worker_secret = os.getenv("WORKER_SECRET", "intel-audio-2026").strip()
    if not worker_url:
        logger.warning(
            f"wa_attachment_dispatch: AUDIO_WORKER_URL not set — "
            f"skip kind={kind} source={source} msg={message_id}"
        )
        return {"dispatched": False, "reason": "worker_url_missing", "kind": kind}

    endpoint_map = {"audio": "/transcribe", "pdf": "/analyze-pdf", "image": "/analyze-image"}
    endpoint = endpoint_map[kind]

    main_instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp").strip()
    payload = {
        "key": key,
        "phone": phone,
        "message_id": message_id,
        "secret": worker_secret,
        "source": source,
        "instance": main_instance,
        "silent": True,
    }
    if kind == "pdf":
        doc = message_obj.get("documentMessage") or {}
        payload["filename"] = doc.get("fileName") or "documento.pdf"
        payload["caption"] = doc.get("caption") or ""
    elif kind == "image":
        img = message_obj.get("imageMessage") or {}
        payload["caption"] = img.get("caption") or ""

    try:
        # Worker fast-ACK quando silent: retorna 200 em <500ms e processa em
        # background. Timeout 8s e folga generosa pro ACK.
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(f"{worker_url}{endpoint}", json=payload)
        logger.info(
            f"wa_attachment_dispatch: kind={kind} source={source} "
            f"msg={message_id} status={resp.status_code}"
        )
        return {"dispatched": True, "kind": kind, "status": resp.status_code}
    except Exception as e:
        logger.warning(
            f"wa_attachment_dispatch: failed kind={kind} source={source} "
            f"msg={message_id} err_type={type(e).__name__} err={e!r}"
        )
        return {"dispatched": False, "kind": kind, "error": f"{type(e).__name__}: {e}"}
