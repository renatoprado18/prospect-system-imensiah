"""
Audio Transcriber Worker - Railway
Receives audio transcription requests, downloads from Evolution API,
transcribes with Claude, and sends bot response via WhatsApp.
"""
import os
import json
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="INTEL Audio Transcriber")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_API_URL = os.getenv("INTEL_API_URL", "https://intel.almeida-prado.com")
WORKER_SECRET = os.getenv("WORKER_SECRET", "intel-audio-2026")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audio-transcriber"}


@app.post("/transcribe")
async def transcribe_audio(request: Request):
    """
    Receive audio transcription request from Vercel webhook.
    Downloads audio, transcribes, processes bot message, sends response.
    """
    data = await request.json()

    # Verify secret
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")

    if not phone or not key:
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    logger.info(f"Transcription request for {phone}")

    try:
        # Step 1: Download audio from Evolution API
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            logger.error(f"Download failed: {dl_resp.status_code}")
            await _send_response(phone, "Nao consegui baixar o audio. Pode digitar?")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        audio_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "audio/ogg")

        if not audio_b64:
            await _send_response(phone, "Audio vazio. Pode digitar?")
            return {"error": "empty_audio"}

        # Clean mimetype (WhatsApp sends "audio/ogg; codecs=opus")
        clean_mimetype = mimetype.split(";")[0].strip() if mimetype else "audio/ogg"
        logger.info(f"Audio downloaded: {len(audio_b64)} chars, type={clean_mimetype}")

        # Step 2: Transcribe with Claude
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": clean_mimetype,
                                    "data": audio_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": "Transcreva este audio em portugues. Retorne APENAS a transcricao, sem comentarios."
                            }
                        ]
                    }]
                }
            )

        if resp.status_code != 200:
            error_detail = resp.text[:500]
            logger.error(f"Claude transcription failed: {resp.status_code} - {error_detail}")
            await _send_response(phone, f"Erro na transcricao ({resp.status_code}). Pode digitar?")
            return {"error": f"transcription_failed: {resp.status_code}", "detail": error_detail}

        transcription = resp.json().get("content", [{}])[0].get("text", "")
        if not transcription:
            await _send_response(phone, "Nao consegui entender o audio. Pode digitar?")
            return {"error": "empty_transcription"}

        logger.info(f"Transcribed: {transcription[:100]}")

        # Step 3: Send transcribed text to INTEL bot for processing
        content = f"[Audio transcrito] {transcription}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id}
            )

        if bot_resp.status_code == 200:
            return {"status": "success", "transcription": transcription[:200]}
        else:
            # Fallback: send transcription directly as response
            logger.warning(f"Bot API failed: {bot_resp.status_code}, sending direct")
            await _send_response(phone, f"Transcrevi seu audio:\n\n_{transcription}_\n\n(Nao consegui processar pelo bot)")
            return {"status": "partial", "transcription": transcription[:200]}

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await _send_response(phone, "Erro ao processar audio. Tenta digitar?")
        return {"error": str(e)}


async def _send_response(phone: str, message: str):
    """Send WhatsApp message via intel-bot instance."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{EVOLUTION_API_URL}/message/sendText/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": phone, "text": message}
            )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
