"""
INTEL Worker - Railway
Handles bot message processing, audio transcription, and image analysis.
Runs on Railway with no timeout limit.
"""
import os
import json
import logging
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="INTEL Worker")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")
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
    Process bot message directly on Railway with full DB access.
    No timeout limit. Has access to INTEL + ConselhoOS databases.
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
        response = await _run_bot(phone, content, message_id)
        if response:
            await _send_response(phone, response)
        return {"status": "success", "response_length": len(response or "")}
    except Exception as e:
        logger.error(f"Bot processing error: {e}")
        await _send_response(phone, "Desculpa, tive um erro. Tenta de novo?")
        return {"status": "error", "error": str(e)}


# ==================== BOT ENGINE (runs on Railway) ====================

BOT_TOOLS = [
    {
        "name": "query_intel",
        "description": "SELECT no banco INTEL (contatos, mensagens, projetos, tarefas, memorias). Apenas SELECT. LIMIT 20.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "query_conselhoos",
        "description": "SELECT no banco ConselhoOS (empresas, reunioes, raci_itens, decisoes, pessoas). Apenas SELECT.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "execute_conselhoos",
        "description": "INSERT/UPDATE/DELETE no ConselhoOS. IDs UUID (gen_random_uuid()). Tabelas: empresas, reunioes, raci_itens, decisoes, pessoas, documentos.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "execute_intel",
        "description": (
            "Executa acao no INTEL:\n"
            "- create_task: {titulo, descricao?, project_id?, contact_id?, data_vencimento? YYYY-MM-DD}\n"
            "- complete_task: {task_id}\n"
            "- save_note: {project_id, titulo, conteudo}\n"
            "- save_memory: {contact_id, titulo, resumo, tipo?}\n"
            "- save_feedback: {conteudo, tipo? bug|melhoria|ideia}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["action", "params"]
        }
    }
]


def _db_query(url: str, sql: str, write: bool = False) -> str:
    """Execute SQL on a database."""
    if not url:
        return json.dumps({"erro": "Database URL nao configurada"})

    sql = sql.strip().rstrip(";").strip()
    sql_upper = sql.upper()

    if not write:
        if not sql_upper.startswith("SELECT"):
            return json.dumps({"erro": "Apenas SELECT permitido"})
        if "LIMIT" not in sql_upper:
            sql += " LIMIT 20"

    try:
        conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            if write:
                result = f"{cursor.rowcount} registro(s) afetado(s)"
                try:
                    rows = cursor.fetchall()
                    if rows:
                        result += "\n" + " | ".join(f"{k}: {v}" for k, v in dict(rows[0]).items())
                except Exception:
                    pass
                conn.commit()
                return json.dumps({"sucesso": True, "resultado": result}, ensure_ascii=False)
            else:
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    return "Nenhum resultado"
                lines = []
                for i, row in enumerate(rows):
                    parts = [f"{k}: {str(v)[:200]}" for k, v in row.items() if v is not None]
                    lines.append(f"[{i+1}] " + " | ".join(parts))
                return f"{len(rows)} resultados:\n" + "\n".join(lines)
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"erro": str(e)})


def _execute_intel_action(action: str, params: dict) -> str:
    """Execute an INTEL CRM action."""
    if not DATABASE_URL:
        return json.dumps({"erro": "DATABASE_URL nao configurada"})

    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()

        if action == "create_task":
            dv = params.get("data_vencimento")
            if dv:
                try:
                    dv = datetime.strptime(str(dv)[:10], "%Y-%m-%d")
                except Exception:
                    dv = None
            if not dv and params.get("prazo_dias"):
                dv = datetime.now() + timedelta(days=params["prazo_dias"])

            cursor.execute("""
                INSERT INTO tasks (titulo, descricao, project_id, contact_id, data_vencimento,
                    prioridade, ai_generated, origem, status)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending') RETURNING id
            """, (params.get("titulo"), params.get("descricao", ""), params.get("project_id"),
                  params.get("contact_id"), dv, params.get("prioridade", 5)))
            tid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            return f"Tarefa #{tid} criada: {params.get('titulo')}"

        elif action == "complete_task":
            cursor.execute("UPDATE tasks SET status='completed', data_conclusao=NOW() WHERE id=%s RETURNING titulo",
                          (params["task_id"],))
            r = cursor.fetchone()
            conn.commit()
            conn.close()
            return f"Tarefa concluida: {r['titulo']}" if r else "Tarefa nao encontrada"

        elif action == "save_note":
            cursor.execute("INSERT INTO project_notes (project_id, titulo, conteudo, tipo, autor) VALUES (%s,%s,%s,%s,'INTEL Bot') RETURNING id",
                          (params.get("project_id"), params.get("titulo", ""), params.get("conteudo", ""), params.get("tipo", "nota")))
            nid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            return f"Nota #{nid} salva"

        elif action == "save_memory":
            cursor.execute("INSERT INTO contact_memories (contact_id, titulo, resumo, tipo) VALUES (%s,%s,%s,%s) RETURNING id",
                          (params["contact_id"], params.get("titulo", ""), params.get("resumo", ""), params.get("tipo", "nota")))
            mid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            return f"Memoria #{mid} salva"

        elif action == "save_feedback":
            cursor.execute("INSERT INTO system_feedback (tipo, conteudo) VALUES (%s,%s) RETURNING id",
                          (params.get("tipo", "feedback"), params.get("conteudo", "")))
            fid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            return f"Feedback #{fid} registrado"

        conn.close()
        return f"Acao desconhecida: {action}"
    except Exception as e:
        return f"Erro: {e}"


def _run_tool(name: str, input_data: dict) -> str:
    """Execute a bot tool."""
    if name == "query_intel":
        return _db_query(DATABASE_URL, input_data["sql"])
    elif name == "query_conselhoos":
        return _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"])
    elif name == "execute_conselhoos":
        return _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"], write=True)
    elif name == "execute_intel":
        return _execute_intel_action(input_data.get("action", ""), input_data.get("params", {}))
    return "Tool desconhecida"


def _load_history(phone: str, limit: int = 15) -> list:
    """Load conversation history from INTEL DB."""
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM bot_conversations WHERE phone=%s ORDER BY created_at DESC LIMIT %s", (phone, limit))
        rows = list(reversed([dict(r) for r in cursor.fetchall()]))
        conn.close()
        return rows
    except Exception:
        return []


def _save_msg(phone: str, role: str, content: str):
    """Save message to conversation history."""
    if not content or not content.strip():
        return
    garbage = ['demorou demais', 'Erro interno', '__IMAGE_PENDING__', '__AUDIO_PENDING__']
    if any(g in content for g in garbage):
        return
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO bot_conversations (phone, role, content) VALUES (%s,%s,%s)", (phone, role, content))
        conn.commit()
        conn.close()
    except Exception:
        pass


async def _run_bot(phone: str, message: str, message_id: str) -> str:
    """Full bot processing with tool_use loop. Runs on Railway (no timeout)."""
    now = datetime.now()

    system_prompt = f"""Voce e o INTEL Bot, assistente pessoal de Renato Almeida Prado (executivo, tecnologia e governanca).

TOOLS:
- query_intel: consultar CRM (contatos, mensagens, projetos, tarefas, memorias, calendario)
- query_conselhoos: consultar sistema de governanca (empresas, reunioes, atas, RACI, decisoes)
- execute_conselhoos: CRIAR/MODIFICAR no ConselhoOS (INSERT/UPDATE/DELETE). IDs UUID (gen_random_uuid())
- execute_intel: criar tarefas, salvar notas, memorias, feedback

REGRAS:
- NUNCA invente informacoes. Consulte antes de afirmar.
- Quando pedir para CRIAR algo no ConselhoOS, use execute_conselhoos com INSERT direto.
- Responda em portugues, conciso (WhatsApp). Use *negrito* para destaques.
- Data atual: {now.strftime('%Y-%m-%d %H:%M')}
- Audios transcritos: "[Audio transcrito] texto"
- Imagens analisadas: "[Imagem analisada] descricao"
- Feedback do sistema: use execute_intel save_feedback"""

    # Load history
    history = _load_history(phone)
    _save_msg(phone, "user", message)
    messages = [{"role": r["role"], "content": r["content"]} for r in history] + [{"role": "user", "content": message}]

    # Tool loop
    async with httpx.AsyncClient(timeout=30.0) as client:
        for iteration in range(3):
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                      "system": system_prompt, "tools": BOT_TOOLS, "messages": messages}
            )

            if resp.status_code != 200:
                logger.error(f"Claude error: {resp.status_code}")
                return None

            result = resp.json()
            text_parts = []
            tool_uses = []

            for block in result.get("content", []):
                if block["type"] == "text":
                    text_parts.append(block["text"])
                elif block["type"] == "tool_use":
                    tool_uses.append(block)

            if result.get("stop_reason") == "end_turn" or not tool_uses:
                response = "\n".join(text_parts)
                _save_msg(phone, "assistant", response)
                return response

            # Execute tools
            messages.append({"role": "assistant", "content": result["content"]})
            tool_results = []
            for tool in tool_uses:
                logger.info(f"Tool: {tool['name']} input: {json.dumps(tool.get('input', {}))[:200]}")
                output = _run_tool(tool["name"], tool.get("input", {}))
                tool_results.append({"type": "tool_result", "tool_use_id": tool["id"], "content": output})
            messages.append({"role": "user", "content": tool_results})

    return None


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
