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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_API_URL = os.getenv("INTEL_API_URL", "https://intel.almeida-prado.com")
WORKER_SECRET = os.getenv("WORKER_SECRET", "intel-audio-2026")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audio-transcriber"}


@app.post("/process-message")
async def process_message(request: Request):
    """
    Process a text bot message directly on Railway (no timeout limit).
    Uses Claude API directly with the bot's system prompt and tools,
    then sends the response via WhatsApp.
    """
    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    phone = data.get("phone", "")
    content = data.get("content", "")
    message_id = data.get("message_id", "")

    if not phone or not content:
        return JSONResponse(status_code=400, content={"error": "missing phone or content"})

    logger.info(f"Processing bot message for {phone}: {content[:80]}")

    try:
        # Call INTEL bot-message endpoint (sync, Vercel has 10s but we retry)
        async with httpx.AsyncClient(timeout=55.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id,
                      "secret": WORKER_SECRET}
            )
        if bot_resp.status_code == 200:
            return {"status": "success"}
        else:
            logger.warning(f"Bot API returned {bot_resp.status_code}: {bot_resp.text[:200]}")
            # Fallback: simple Claude response without CRM tools
            await _fallback_response(phone, content)
            return {"status": "fallback"}
    except Exception as e:
        logger.error(f"Process message error: {e}")
        await _fallback_response(phone, content)
        return {"status": "fallback", "error": str(e)}


async def _fallback_response(phone: str, content: str):
    """Simple Claude response when bot-message endpoint fails."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                          "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                      "system": "Voce e o INTEL Bot. Responda de forma util e concisa em portugues.",
                      "messages": [{"role": "user", "content": content}]}
            )
        if resp.status_code == 200:
            text = resp.json().get("content", [{}])[0].get("text", "")
            if text:
                await _send_response(phone, text)
    except Exception as e:
        logger.error(f"Fallback response error: {e}")


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

        logger.info(f"Audio downloaded: {len(audio_b64)} chars, type={mimetype}")

        # Step 2: Transcribe with Groq Whisper (free, fast, supports ogg)
        import base64
        audio_bytes = base64.b64decode(audio_b64)

        # Determine file extension from mimetype
        ext_map = {"audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
        clean_mime = mimetype.split(";")[0].strip() if mimetype else "audio/ogg"
        ext = ext_map.get(clean_mime, "ogg")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", audio_bytes, clean_mime)},
                data={"model": "whisper-large-v3-turbo", "language": "pt"}
            )

        if resp.status_code != 200:
            error_detail = resp.text[:500]
            logger.error(f"Groq transcription failed: {resp.status_code} - {error_detail}")
            await _send_response(phone, f"Erro na transcricao ({resp.status_code}). Pode digitar?")
            return {"error": f"transcription_failed: {resp.status_code}", "detail": error_detail}

        transcription = resp.json().get("text", "")
        if not transcription:
            await _send_response(phone, "Nao consegui entender o audio. Pode digitar?")
            return {"error": "empty_transcription"}

        logger.info(f"Transcribed: {transcription[:100]}")

        # Step 3: Send transcribed text to INTEL bot for processing
        content = f"[Audio transcrito] {transcription}"

        # Step 3: Send to intel-bot for full processing (has query_intel, save_memory, etc)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                bot_resp = await client.post(
                    f"{INTEL_API_URL}/api/webhooks/bot-message",
                    headers={"Content-Type": "application/json"},
                    json={"phone": phone, "content": content, "message_id": message_id,
                          "secret": WORKER_SECRET},
                    timeout=55.0
                )
            if bot_resp.status_code == 200:
                return {"status": "success", "transcription": transcription[:200]}
            else:
                logger.warning(f"Bot API failed: {bot_resp.status_code}")
                await _send_response(phone, f"Transcrevi seu audio:\n\n_{transcription}_")
                return {"status": "partial", "transcription": transcription[:200]}
        except httpx.TimeoutException:
            logger.warning("Bot API timeout - sending transcription directly")
            await _send_response(phone, f"Transcrevi seu audio:\n\n_{transcription}_")
            return {"status": "partial_timeout", "transcription": transcription[:200]}

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await _send_response(phone, "Erro ao processar audio. Tenta digitar?")
        return {"error": str(e)}


@app.post("/analyze-image")
async def analyze_image(request: Request):
    """
    Receive image from WhatsApp, analyze with Claude Vision,
    send to intel-bot for processing.
    """
    import base64

    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")
    caption = data.get("caption", "")

    if not phone or not key:
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    logger.info(f"Image analysis request for {phone}, caption: {caption[:50]}")

    try:
        # Step 1: Download image from Evolution API
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            await _send_response(phone, "Nao consegui baixar a imagem.")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        image_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "image/jpeg").split(";")[0].strip()

        if not image_b64:
            await _send_response(phone, "Imagem vazia.")
            return {"error": "empty_image"}

        logger.info(f"Image downloaded: {len(image_b64)} chars, type={mimetype}")

        # Step 2: Analyze with Claude Vision
        user_instruction = caption if caption else "Descreva o que voce ve nesta imagem. Se for uma tela do sistema, identifique o que pode ser melhorado. Se for uma mensagem, resuma o conteudo."

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mimetype,
                                    "data": image_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": user_instruction
                            }
                        ]
                    }]
                }
            )

        if resp.status_code != 200:
            logger.error(f"Claude Vision failed: {resp.status_code} - {resp.text[:200]}")
            await _send_response(phone, "Erro ao analisar imagem.")
            return {"error": f"vision_failed: {resp.status_code}"}

        analysis = resp.json().get("content", [{}])[0].get("text", "")
        if not analysis:
            await _send_response(phone, "Nao consegui analisar a imagem.")
            return {"error": "empty_analysis"}

        logger.info(f"Image analyzed: {analysis[:100]}")

        # Step 3: Send to intel-bot for processing with CRM context
        content = f"[Imagem analisada] {caption + ': ' if caption else ''}{analysis}"

        async with httpx.AsyncClient(timeout=55.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id,
                      "secret": WORKER_SECRET},
                timeout=55.0
            )

        if bot_resp.status_code == 200:
            return {"status": "success", "analysis": analysis[:200]}
        else:
            # Fallback: send analysis directly
            await _send_response(phone, f"📸 *Analise da imagem:*\n\n{analysis}")
            return {"status": "partial"}

    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        await _send_response(phone, "Erro ao processar imagem.")
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
