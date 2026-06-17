"""
Debounce de inputs do bot WA.

Problema: Renato manda 3 audios + 1 texto em rajada (5s). Cada msg gera um
POST independente no /process-message ou /transcribe. Cada um spawna _run_bot
em paralelo — multiplas respostas conflitantes, tool calls duplicadas, cost
spike.

Solucao: buffer in-memory por phone. Cada msg nova cancela timer pendente +
agenda novo flush. Apos N segundos sem msg nova, concatena e processa 1 vez.

Funciona pq Railway worker eh single-instance (max_instances=1 nos jobs
APScheduler). Restart no meio da janela perde aglutinacao mas nao msgs —
elas estao persistidas em whatsapp_messages.

Gate: BOT_DEBOUNCE_ENABLED=1 pra ativar. Default off ate validacao.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return (os.getenv("BOT_DEBOUNCE_ENABLED", "0") or "0").strip() in ("1", "true", "TRUE", "yes")


def _delay_seconds() -> float:
    try:
        return float((os.getenv("BOT_DEBOUNCE_SECONDS", "") or "6.0").strip())
    except ValueError:
        return 6.0


# Per-phone buffer slot:
#   messages: list[str] em ordem de chegada
#   last_message_id: ID da ultima msg (usado pelo runner pra ack/dedup)
#   timer: asyncio.Task do _flush pendente
_BUFFER: Dict[str, Dict[str, Any]] = {}
_LOCK = asyncio.Lock()


RunnerFn = Callable[[str, str, str], Awaitable[Optional[str]]]


async def enqueue(
    phone: str, content: str, message_id: str, runner: RunnerFn
) -> Dict[str, Any]:
    """Adiciona msg ao buffer do phone. Cancela timer pendente e reagenda.

    `runner(phone, combined_content, last_message_id) -> Optional[response]`
    eh chamado no flush. Runner eh responsavel por enviar resposta (este
    modulo nao toca em WA).
    """
    delay = _delay_seconds()
    async with _LOCK:
        slot = _BUFFER.get(phone)
        if slot is None:
            slot = {"messages": [], "last_message_id": message_id, "timer": None}
            _BUFFER[phone] = slot

        slot["messages"].append(content)
        slot["last_message_id"] = message_id

        if slot["timer"] is not None and not slot["timer"].done():
            slot["timer"].cancel()

        slot["timer"] = asyncio.create_task(_flush(phone, runner, delay))
        count = len(slot["messages"])

    logger.info(
        f"debounce: enqueued phone={phone} count={count} delay={delay:.1f}s"
    )
    return {"queued": True, "phone": phone, "count": count, "delay_s": delay}


async def _flush(phone: str, runner: RunnerFn, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return

    async with _LOCK:
        slot = _BUFFER.pop(phone, None)

    if not slot or not slot["messages"]:
        return

    messages = slot["messages"]
    combined = "\n\n".join(messages)
    last_id = slot["last_message_id"]

    logger.info(
        f"debounce: flushing phone={phone} msgs={len(messages)} "
        f"combined_len={len(combined)} last_id={last_id}"
    )

    try:
        await runner(phone, combined, last_id)
    except Exception as e:
        logger.exception(f"debounce: runner crashed phone={phone}: {e}")


def buffer_size() -> int:
    """Diagnostico — quantos phones com buffer aberto."""
    return len(_BUFFER)
