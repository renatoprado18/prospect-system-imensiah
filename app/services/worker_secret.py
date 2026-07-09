"""
WORKER_SECRET — helper único, SEM fallback hardcoded.

Auth compartilhada entre o app (Vercel) e o Railway worker (audio-transcriber).
Historicamente existia default hardcoded espalhado em 12+ call sites —
removido em 08/07/2026. Env ausente agora:
  - lado que ENVIA: loga erro e falha explícito (nunca envia default);
  - lado que VALIDA: rejeita com 401 (nunca aceita default).

O worker é deployable separado e tem cópia local desta lógica em
workers/audio-transcriber/main.py (_check_worker_secret).
"""
import logging
import os

logger = logging.getLogger(__name__)


def get_worker_secret() -> str:
    """Lê WORKER_SECRET do env, com strip() (Vercel cola \\n em env vars).

    Retorna "" se ausente — sem fallback. Callers devem tratar "" como
    misconfiguração (nunca comparar/enviar direto sem checar).
    """
    return os.environ.get("WORKER_SECRET", "").strip()


def require_worker_secret() -> str:
    """Para SENDERS: retorna o secret ou levanta RuntimeError se ausente."""
    secret = get_worker_secret()
    if not secret:
        logger.error("WORKER_SECRET não configurado — abortando envio (sem fallback)")
        raise RuntimeError("WORKER_SECRET não configurado")
    return secret


def check_worker_secret(provided) -> bool:
    """Para VALIDATORS: compara secret recebido com o env.

    Env ausente => loga erro e retorna False (request deve virar 401).
    Nunca aceita default.
    """
    expected = get_worker_secret()
    if not expected:
        logger.error("WORKER_SECRET não configurado — rejeitando request (401)")
        return False
    return bool(provided) and str(provided).strip() == expected
