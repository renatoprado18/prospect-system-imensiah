# Bot routing v2
"""
Evolution API Integration
Cliente para comunicação com Evolution API (WhatsApp via Baileys)

Docs: https://doc.evolution-api.com/v2/en
GitHub: https://github.com/EvolutionAPI/evolution-api
"""
import os
from services import llm
import json
import httpx
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime

from services.worker_secret import get_worker_secret

logger = logging.getLogger(__name__)


# ==================== TELEMETRIA WEBHOOK ====================
# Why: mensagens do Felipe Orioli somem entre webhook e tabela `messages`.
# webhook_audit grava cada chamada — decision em {processed, skipped, error}
# + reason, pra diagnosticar perda silenciosa.
# Defensivo: INSERT em try/except — telemetria NUNCA pode falhar o webhook.

def _record_webhook_audit(
    source: str,
    event_type: str = None,
    instance: str = None,
    remote_jid: str = None,
    remote_jid_alt: str = None,
    from_me: bool = None,
    message_id: str = None,
    decision: str = "unknown",
    decision_reason: str = None,
    resulting_message_id: int = None,
    payload: Dict = None,
    processing_ms: int = None,
) -> None:
    """Insere row em webhook_audit. Nunca raise — falha silenciosa via warning."""
    try:
        from database import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO webhook_audit
                  (source, event_type, instance, remote_jid, remote_jid_alt,
                   from_me, message_id, decision, decision_reason,
                   resulting_message_id, payload, processing_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source,
                    event_type,
                    instance,
                    remote_jid,
                    remote_jid_alt,
                    from_me,
                    message_id,
                    decision,
                    decision_reason,
                    resulting_message_id,
                    json.dumps(payload or {}, default=str),
                    processing_ms,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"webhook_audit insert failed: {e}")


def _is_bot_phone(phone: str) -> bool:
    """
    Verifica se o telefone pertence ao proprio intel-bot.
    Evita loop: bot envia briefing -> webhook chega -> sistema interpreta como msg do contato.
    """
    if not phone:
        return False
    bot_number = os.getenv("INTEL_BOT_NUMBER", "5511915020192")
    bot_clean = ''.join(filter(str.isdigit, bot_number))
    phone_clean = ''.join(filter(str.isdigit, phone))
    if not bot_clean or not phone_clean:
        return False
    if phone_clean == bot_clean:
        return True
    # Match por sufixo (ultimos 10 digitos cobre celulares com/sem DDI)
    if len(bot_clean) >= 10 and len(phone_clean) >= 10:
        if phone_clean[-10:] == bot_clean[-10:]:
            return True
    return False


class EvolutionAPIClient:
    """Cliente para Evolution API v2"""

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        instance_name: str = None
    ):
        # Vercel as vezes cola "\n" literal (2 chars) no final do valor — incidente
        # 06/jun/2026, brico 15 dias o cron silenciosamente. Limpa explicitamente.
        raw_url = (base_url or os.getenv("EVOLUTION_API_URL", ""))
        self.base_url = raw_url.replace('\\n', '').replace('\\r', '').strip().rstrip('/')
        self.api_key = (api_key or os.getenv("EVOLUTION_API_KEY", "")).strip()
        self.instance_name = instance_name or os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

        self.headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

    @property
    def is_configured(self) -> bool:
        """Verifica se a API está configurada"""
        return bool(self.base_url and self.api_key)

    def send_text_sync(
        self,
        phone: str,
        message: str,
        instance_name: str = None,
        timeout: float = 30.0,
    ) -> Dict:
        """Versao sync de send_text. Usado por callers sem event loop (dispatcher
        sync do Tonha brain). Mesma normalizacao de phone."""
        if not self.is_configured:
            return {"error": "Evolution API nao configurada"}

        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))
        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        url = f"{self.base_url}/message/sendText/{name}"
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=self.headers, json={
                    "number": phone_clean,
                    "text": message,
                    "delay": 1200,
                })
                if resp.status_code in (200, 201):
                    return resp.json()
                logger.error(f"Evolution sync send error: {resp.status_code} - {resp.text}")
                return {"error": resp.text, "status_code": resp.status_code}
        except httpx.TimeoutException:
            return {"error": "Timeout na requisicao"}
        except Exception as e:
            logger.exception(f"Evolution sync send exception: {e}")
            return {"error": str(e)}

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Dict = None,
        timeout: float = 30.0
    ) -> Dict:
        """Faz requisição à API"""
        if not self.is_configured:
            return {"error": "Evolution API não configurada"}

        url = f"{self.base_url}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(url, headers=self.headers)
                elif method == "POST":
                    response = await client.post(url, headers=self.headers, json=data)
                elif method == "PUT":
                    response = await client.put(url, headers=self.headers, json=data)
                elif method == "DELETE":
                    response = await client.delete(url, headers=self.headers)
                else:
                    return {"error": f"Método não suportado: {method}"}

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 201:
                    return response.json()
                else:
                    error_text = response.text
                    logger.error(f"Evolution API error: {response.status_code} - {error_text}")
                    return {"error": error_text, "status_code": response.status_code}

        except httpx.TimeoutException:
            logger.error(f"Evolution API timeout: {url}")
            return {"error": "Timeout na requisição"}
        except Exception as e:
            logger.error(f"Evolution API exception: {e}")
            return {"error": str(e)}

    # ==================== INSTANCE MANAGEMENT ====================

    async def create_instance(self, instance_name: str = None) -> Dict:
        """Cria nova instância do WhatsApp"""
        name = instance_name or self.instance_name
        return await self._request("POST", "/instance/create", {
            "instanceName": name,
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
            "number": "",
            "token": ""
        })

    async def get_instance_status(self, instance_name: str = None) -> Dict:
        """Obtém status da instância"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/instance/connectionState/{name}")

    async def get_all_instances(self) -> Dict:
        """Lista todas as instâncias"""
        return await self._request("GET", "/instance/fetchInstances")

    async def delete_instance(self, instance_name: str = None) -> Dict:
        """Deleta uma instância"""
        name = instance_name or self.instance_name
        return await self._request("DELETE", f"/instance/delete/{name}")

    async def logout_instance(self, instance_name: str = None) -> Dict:
        """Desconecta a instância (logout)"""
        name = instance_name or self.instance_name
        return await self._request("DELETE", f"/instance/logout/{name}")

    async def restart_instance(self, instance_name: str = None) -> Dict:
        """Reinicia a instância"""
        name = instance_name or self.instance_name
        return await self._request("PUT", f"/instance/restart/{name}")

    # ==================== CONNECTION ====================

    async def get_qr_code(self, instance_name: str = None) -> Dict:
        """
        Obtém QR Code para conexão.

        Resposta da Evolution API:
        {
            "pairingCode": "WZYEH1YY",
            "code": "2@y8eK+bjtEjUWy9/...",
            "base64": "data:image/png;base64,iVBORw0KGgo...",
            "count": 1
        }
        """
        name = instance_name or self.instance_name
        result = await self._request("GET", f"/instance/connect/{name}")

        if "error" in result:
            return result

        # Formato padrão v2: base64 direto no root
        return {
            "qr_base64": result.get("base64"),
            "qr_code": result.get("code"),
            "pairingCode": result.get("pairingCode"),
            "count": result.get("count", 0)
        }

    async def check_connection(self, instance_name: str = None) -> Dict:
        """Verifica se está conectado"""
        status = await self.get_instance_status(instance_name)

        if "error" in status:
            return {"connected": False, "state": "error", "error": status["error"]}

        state = status.get("state") or status.get("instance", {}).get("state")

        return {
            "connected": state == "open",
            "state": state,
            "instance": status.get("instance", {})
        }

    # ==================== MESSAGES ====================

    async def send_text(
        self,
        phone: str,
        message: str,
        instance_name: str = None
    ) -> Dict:
        """Envia mensagem de texto"""
        name = instance_name or self.instance_name

        # Normalizar telefone (remover caracteres não numéricos)
        phone_clean = ''.join(filter(str.isdigit, phone))

        # Garantir formato correto (com código do país)
        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/message/sendText/{name}", {
            "number": phone_clean,
            "text": message,
            "delay": 1200  # Delay para parecer mais humano
        })

    async def send_media(
        self,
        phone: str,
        media_url: str,
        caption: str = "",
        media_type: str = "image",
        instance_name: str = None
    ) -> Dict:
        """Envia mídia (imagem, vídeo, documento, áudio)"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        endpoint = f"/message/sendMedia/{name}"

        return await self._request("POST", endpoint, {
            "number": phone_clean,
            "mediatype": media_type,
            "media": media_url,
            "caption": caption
        })

    async def send_document(
        self,
        phone: str,
        document_url: str,
        filename: str,
        caption: str = "",
        instance_name: str = None
    ) -> Dict:
        """Envia documento"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/message/sendWhatsAppAudio/{name}", {
            "number": phone_clean,
            "mediatype": "document",
            "media": document_url,
            "fileName": filename,
            "caption": caption
        })

    # ==================== CONTACTS ====================

    async def check_is_whatsapp(
        self,
        phones: List[str],
        instance_name: str = None
    ) -> Dict:
        """Verifica se números têm WhatsApp"""
        name = instance_name or self.instance_name

        # Normalizar telefones
        clean_phones = []
        for phone in phones:
            phone_clean = ''.join(filter(str.isdigit, phone))
            if not phone_clean.startswith('55') and len(phone_clean) <= 11:
                phone_clean = '55' + phone_clean
            clean_phones.append(phone_clean)

        return await self._request("POST", f"/chat/whatsappNumbers/{name}", {
            "numbers": clean_phones
        })

    async def get_profile_picture(
        self,
        phone: str,
        instance_name: str = None
    ) -> Dict:
        """Obtém foto de perfil do contato via Evolution API v2"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        # Evolution API v2 usa POST com body JSON
        return await self._request("POST", f"/chat/fetchProfilePictureUrl/{name}", {
            "number": phone_clean
        })

    async def get_contacts(self, instance_name: str = None) -> Dict:
        """Lista contatos do WhatsApp"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/chat/findContacts/{name}")

    # ==================== CHATS ====================

    async def get_chats(self, instance_name: str = None) -> Dict:
        """Lista todas as conversas"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/chat/findChats/{name}")

    async def get_messages(
        self,
        phone: str,
        limit: int = 100,
        instance_name: str = None
    ) -> Dict:
        """Obtém mensagens de uma conversa"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/chat/findMessages/{name}", {
            "where": {
                "key": {
                    "remoteJid": f"{phone_clean}@s.whatsapp.net"
                }
            },
            "limit": limit
        })

    # ==================== WEBHOOKS ====================

    async def set_webhook(
        self,
        webhook_url: str,
        events: List[str] = None,
        instance_name: str = None
    ) -> Dict:
        """
        Configura webhook para receber eventos.

        Eventos disponíveis:
        - MESSAGES_UPSERT: Nova mensagem recebida
        - MESSAGES_UPDATE: Mensagem atualizada (lida, etc)
        - SEND_MESSAGE: Confirmação de envio
        - CONNECTION_UPDATE: Status da conexão mudou
        - QRCODE_UPDATED: Novo QR code gerado
        - CONTACTS_UPSERT: Contato adicionado/atualizado
        """
        name = instance_name or self.instance_name

        default_events = [
            "MESSAGES_UPSERT",
            "MESSAGES_UPDATE",
            "SEND_MESSAGE",
            "CONNECTION_UPDATE",
            "QRCODE_UPDATED"
        ]

        # Try Evolution API v2 format with "webhook" wrapper
        payload = {
            "webhook": {
                "url": webhook_url,
                "events": events or default_events,
                "enabled": True,
                "webhookByEvents": True,
                "webhookBase64": False
            }
        }

        result = await self._request("POST", f"/webhook/set/{name}", payload)

        # If that fails, try without wrapper (v1 format)
        if result.get("error") and "webhook" in str(result.get("error", "")):
            result = await self._request("POST", f"/webhook/set/{name}", {
                "url": webhook_url,
                "events": events or default_events,
                "enabled": True,
                "webhookByEvents": True,
                "webhookBase64": False
            })

        return result

    async def get_webhook(self, instance_name: str = None) -> Dict:
        """Obtém configuração do webhook"""
        name = instance_name or self.instance_name

        # Try different endpoint formats (varies by Evolution API version)
        endpoints = [
            f"/webhook/find/{name}",
            f"/webhook/{name}",
            f"/instance/fetchInstances"  # Fallback to get instance info
        ]

        for endpoint in endpoints:
            try:
                result = await self._request("GET", endpoint)
                if result and "error" not in result:
                    # For fetchInstances, extract webhook from instance data
                    if "fetchInstances" in endpoint and isinstance(result, list):
                        for inst in result:
                            if inst.get("instanceName") == name:
                                return inst.get("webhook", {})
                    return result
            except Exception as e:
                logger.debug(f"Webhook endpoint {endpoint} failed: {e}")
                continue

        return {"error": "Could not fetch webhook configuration"}


# Singleton
_evolution_client: Optional[EvolutionAPIClient] = None


def get_evolution_client() -> EvolutionAPIClient:
    """Obtém instância singleton do cliente"""
    global _evolution_client
    if _evolution_client is None:
        _evolution_client = EvolutionAPIClient()
    return _evolution_client


# ==================== WEBHOOK HANDLER ====================

async def handle_evolution_webhook(payload: Dict) -> Dict:
    """
    Processa webhook da Evolution API.
    Chamado pelo endpoint POST /api/webhooks/whatsapp

    Eventos principais:
    - messages.upsert: Nova mensagem recebida
    - connection.update: Mudança no status da conexão
    - qrcode.updated: Novo QR Code gerado
    - send.message: Mensagem enviada confirmada
    """
    from database import get_db
    from services.whatsapp_batch_import import get_batch_importer

    started = datetime.now()
    event = (payload.get("event") or "").lower().replace("_", ".")
    instance = payload.get("instance")
    data = payload.get("data", {})

    # Best-effort key extraction pra telemetria entry-point
    _key = (data or {}).get("key", {}) if isinstance(data, dict) else {}
    audit_ctx = {
        "source": "evolution_webhook",
        "event_type": event,
        "instance": instance if isinstance(instance, str) else (instance or {}).get("instanceName"),
        "remote_jid": _key.get("remoteJid"),
        "remote_jid_alt": _key.get("remoteJidAlt"),
        "from_me": _key.get("fromMe"),
        "message_id": _key.get("id"),
        "payload": payload,
    }

    logger.info(f"Evolution webhook: {event} from {instance}")

    # Route intel-bot messages to the bot handler
    intel_bot_instance = os.getenv("INTEL_BOT_INSTANCE", "intel-bot-v2").strip()
    instance_name = instance if isinstance(instance, str) else (instance or {}).get("instanceName", "")
    if instance_name == intel_bot_instance and event == "messages.upsert":
        result = await _handle_intel_bot_message(data)
        _record_webhook_audit(
            **{**audit_ctx, "source": "intel_bot"},
            decision="processed" if result.get("processed") else "skipped",
            decision_reason=result.get("reason"),
            processing_ms=int((datetime.now() - started).total_seconds() * 1000),
        )
        return result

    result = {"event": event, "processed": False}

    try:
        if event == "messages.upsert":
            # Nova mensagem recebida
            result = await process_incoming_message(data, audit_ctx=audit_ctx, started=started)

        elif event == "connection.update":
            # Status da conexão mudou
            state = data.get("state")
            logger.info(f"Connection state changed: {state}")
            result = {"event": event, "state": state, "processed": True}
            _record_webhook_audit(
                **audit_ctx,
                decision="processed",
                decision_reason=f"connection.update:{state}",
                processing_ms=int((datetime.now() - started).total_seconds() * 1000),
            )

        elif event == "qrcode.updated":
            # Novo QR Code
            logger.info("QR Code updated")
            result = {"event": event, "processed": True}
            _record_webhook_audit(
                **audit_ctx,
                decision="processed",
                decision_reason="qrcode.updated",
                processing_ms=int((datetime.now() - started).total_seconds() * 1000),
            )

        elif event == "send.message":
            # Mensagem enviada
            result = await process_sent_message(data)
            _record_webhook_audit(
                **audit_ctx,
                decision="processed" if result.get("processed") else "skipped",
                decision_reason=result.get("reason") or "send.message",
                processing_ms=int((datetime.now() - started).total_seconds() * 1000),
            )
        else:
            _record_webhook_audit(
                **audit_ctx,
                decision="skipped",
                decision_reason=f"unhandled_event:{event}",
                processing_ms=int((datetime.now() - started).total_seconds() * 1000),
            )

    except Exception as e:
        logger.exception(f"Error processing webhook: {e}")
        result["error"] = str(e)
        _record_webhook_audit(
            **audit_ctx,
            decision="error",
            decision_reason=str(e)[:500],
            processing_ms=int((datetime.now() - started).total_seconds() * 1000),
        )

    return result


async def process_incoming_message(data: Dict, audit_ctx: Dict = None, started: datetime = None) -> Dict:
    """Processa mensagem recebida e analisa com IA"""
    from database import get_db
    import asyncio

    message = data.get("message", {})
    key = data.get("key", {})

    # Extrair dados
    remote_jid = key.get("remoteJid", "")
    remote_jid_alt = key.get("remoteJidAlt", "")
    from_me = key.get("fromMe", False)
    message_id = key.get("id")

    # Telemetria — propaga ctx do entry-point ou cria
    if started is None:
        started = datetime.now()
    if audit_ctx is None:
        audit_ctx = {
            "source": "evolution_webhook",
            "event_type": "messages.upsert",
            "instance": None,
            "remote_jid": remote_jid,
            "remote_jid_alt": remote_jid_alt,
            "from_me": from_me,
            "message_id": message_id,
            "payload": data,
        }
    else:
        # Garante que key fields venham preenchidos mesmo se entry-point pulou
        audit_ctx = {**audit_ctx,
                     "remote_jid": audit_ctx.get("remote_jid") or remote_jid,
                     "remote_jid_alt": audit_ctx.get("remote_jid_alt") or remote_jid_alt,
                     "from_me": from_me if audit_ctx.get("from_me") is None else audit_ctx["from_me"],
                     "message_id": audit_ctx.get("message_id") or message_id}

    def _audit(decision: str, reason: str, resulting_id: int = None):
        _record_webhook_audit(
            **audit_ctx,
            decision=decision,
            decision_reason=reason,
            resulting_message_id=resulting_id,
            processing_ms=int((datetime.now() - started).total_seconds() * 1000),
        )

    # Group messages: check for RACI updates (smart matcher — texto livre + media)
    if "@g.us" in remote_jid:
        if not from_me:
            text_content = ""
            # Fix 28/06/26: payload['data'] ja eh passado como `data` (ver
            # handle_evolution_webhook). Acesso era data.data.message — paih
            # extra de 'data' que sempre retornava {}. RACI smart_updates
            # estava dead silently desde commit b846825 (Evolution v2.x).
            message_obj = data.get("message", {}) or {}
            caption = ""
            if message_obj.get("conversation"):
                text_content = message_obj["conversation"]
            elif message_obj.get("extendedTextMessage", {}).get("text"):
                text_content = message_obj["extendedTextMessage"]["text"]
            # Captions de imagem/documento — Phase 2 sera extraido junto com media
            elif message_obj.get("imageMessage", {}).get("caption"):
                caption = message_obj["imageMessage"]["caption"]
            elif message_obj.get("documentMessage", {}).get("caption"):
                caption = message_obj["documentMessage"]["caption"]

            # Detecta se tem media a processar
            has_media = any(k in message_obj for k in ("audioMessage", "imageMessage", "documentMessage"))

            if text_content or has_media:
                try:
                    from database import get_db
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT pwg.project_id, p.nome FROM project_whatsapp_groups pwg
                            JOIN projects p ON p.id = pwg.project_id
                            WHERE pwg.group_jid = %s AND pwg.ativo = TRUE
                        """, (remote_jid,))
                        group_project = cursor.fetchone()

                    if group_project:
                        cos_db = os.getenv("CONSELHOOS_DATABASE_URL", "")
                        if cos_db:
                            import psycopg2 as pg2
                            conn2 = pg2.connect(cos_db)
                            cur2 = conn2.cursor()
                            cur2.execute("SELECT id, nome FROM empresas WHERE LOWER(nome) LIKE LOWER(%s) LIMIT 1",
                                        (f"%{group_project['nome']}%",))
                            emp = cur2.fetchone()
                            conn2.close()
                            if emp:
                                from services.raci_smart_updates import process_group_message, extract_text_from_media

                                # Phase 2: extrai texto de media se houver. Junta com text_content/caption.
                                if has_media:
                                    instance = data.get("instance") or os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")
                                    media_text = await extract_text_from_media(message_obj, key, instance, caption=caption)
                                    if media_text:
                                        combined = (text_content + "\n\n" if text_content else "") + media_text
                                        text_content = combined
                                        logger.info(f"RACI media extracted: {len(media_text)} chars")

                                if text_content:
                                    result = await process_group_message(text_content, emp[0], emp[1])
                                    applied = result.get("applied", [])
                                    if applied:
                                        logger.info(f"RACI smart_updates applied {len(applied)} via {result.get('source')}: {applied}")
                                        client = get_evolution_client()
                                        for a in applied:
                                            await client.send_text(
                                                remote_jid,
                                                f"✅ Atualizado: *{a['acao'][:60]}* → {a['new_status']}",
                                                instance_name="rap-whatsapp"
                                            )
                                    elif result.get("pending_review"):
                                        logger.info(f"RACI smart_updates {len(result['pending_review'])} pending review")
                except Exception as e:
                    logger.warning(f"RACI group update error: {e}")

        # F4' — anexos WA sempre-on em grupos (incoming + outgoing):
        # Persiste anexos via worker independente do RACI (que so roda em
        # incoming + grupo c/ projeto). Idempotente por (message_id, kind).
        message_obj_g = data.get("message") or {}
        has_media_g = any(
            k in message_obj_g
            for k in ("audioMessage", "imageMessage", "documentMessage")
        )
        if has_media_g:
            try:
                from services.wa_attachment_dispatch import dispatch_attachment_to_worker
                # participant existe pra incoming; pra outgoing fallback no group_jid
                participant = key.get("participant", "") or remote_jid
                sender_phone = participant.split("@")[0] if "@" in participant else participant
                # Bloqueante (~500ms): worker ACK fast quando silent, processa
                # em background do lado dele. Evita CancelledError do Vercel
                # serverless que matava asyncio.create_task fire-and-forget.
                await dispatch_attachment_to_worker(
                    message_obj_g, key, sender_phone, message_id,
                    source="main_group",
                )
            except Exception as e:
                logger.warning(f"wa_attachment dispatch (group) failed: {e}")

        _audit("skipped", "group_message")
        return {"processed": False, "reason": "group_message"}

    # Extrair telefone - handle LID format (WhatsApp Meta migration)
    # LID format: 179220563128482@lid — use remoteJidAlt for real phone
    if "@lid" in remote_jid and remote_jid_alt and "@s.whatsapp.net" in remote_jid_alt:
        phone = remote_jid_alt.replace("@s.whatsapp.net", "")
        logger.info(f"LID resolved: {remote_jid} -> {phone} via remoteJidAlt")
    elif "@s.whatsapp.net" in remote_jid:
        phone = remote_jid.replace("@s.whatsapp.net", "")
    else:
        # Unknown format — try alt, skip if nothing works
        if remote_jid_alt and "@s.whatsapp.net" in remote_jid_alt:
            phone = remote_jid_alt.replace("@s.whatsapp.net", "")
        else:
            logger.warning(f"Cannot resolve phone from JID: {remote_jid} alt: {remote_jid_alt}")
            _audit("skipped", "unresolvable_jid")
            return {"processed": False, "reason": "unresolvable_jid"}

    # Filtro anti-loop: descartar mensagens originadas do proprio intel-bot.
    # Why: bot envia briefing/notificacao via intel-bot instance -> webhook na rap-whatsapp
    # com fromMe=False (Renato recebeu de bot). Sem esse filtro, o sistema interpreta
    # o output do bot como mensagem incoming de "contato bot" e cria propostas/eventos
    # do proprio texto do briefing.
    if _is_bot_phone(phone):
        logger.info(f"Skipping bot-origin message from {phone} (anti-loop guard)")
        _audit("skipped", f"bot_origin_skipped:{phone}")
        return {"processed": False, "reason": "bot_origin_skipped", "phone": phone}

    # Persistência 1:1 ao vivo (S08 follow-up): grava DM direta de contato
    # relevante em whatsapp_messages. Side-effect independente do fluxo de
    # proposta/IA abaixo. Escopado (mesma política do backfill) + feature-flag.
    #   WA_PERSIST_1TO1=on     -> grava
    #   WA_PERSIST_1TO1=shadow -> só loga o que faria (rollout seguro)
    #   (não setado)           -> desligado; o cron de backfill cobre o gap
    _wa_persist_mode = os.getenv("WA_PERSIST_1TO1", "").strip().lower()
    if _wa_persist_mode in ("on", "shadow"):
        try:
            from services.wa_backfill import persist_live_direct_message
            _pres = persist_live_direct_message(data, shadow=(_wa_persist_mode == "shadow"))
            logger.info(f"wa_persist_1to1[{_wa_persist_mode}] msg={message_id}: {_pres}")
        except Exception as e:
            logger.warning(f"wa_persist_1to1 falhou msg={message_id}: {e}")

    # Extrair conteúdo
    content = ""
    message_type = "text"

    if "conversation" in message:
        content = message["conversation"]
    elif "extendedTextMessage" in message:
        content = message["extendedTextMessage"].get("text", "")
    elif "imageMessage" in message:
        content = message["imageMessage"].get("caption", "[Imagem]")
        message_type = "image"
    elif "videoMessage" in message:
        content = message["videoMessage"].get("caption", "[Vídeo]")
        message_type = "video"
    elif "audioMessage" in message:
        content = "[Áudio]"
        message_type = "audio"
    elif "documentMessage" in message:
        content = message["documentMessage"].get("fileName", "[Documento]")
        message_type = "document"
    elif "stickerMessage" in message:
        content = "[Figurinha]"
        message_type = "sticker"

    if not content:
        _audit("skipped", "empty_content")
        return {"processed": False, "reason": "empty_content"}

    # Verificar se e uma resposta do Renato a uma proposta de acao
    # fromMe = True significa que a mensagem foi enviada do celular conectado (Renato)
    if from_me and is_proposal_response(content):
        asyncio.create_task(process_renato_reply(content, phone))
        _audit("processed", "proposal_response")
        return {"processed": True, "reason": "proposal_response", "content": content}

    # Buscar contato pelo telefone
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contato
        cursor.execute("""
            SELECT id, nome FROM contacts
            WHERE telefones::text LIKE %s
            LIMIT 1
        """, (f'%{phone[-8:]}%',))
        contact = cursor.fetchone()

        if not contact:
            # Política B (ratificada 11/06/26): criar contato fantasma em vez de dropar.
            # Rate limit: max 5 fantasmas/hora pra evitar spam virar ruido.
            cursor.execute("""
                SELECT COUNT(*) AS c FROM contacts
                WHERE origem = 'wa_unknown' AND criado_em > NOW() - INTERVAL '1 hour'
            """)
            recent_phantoms = cursor.fetchone()["c"]
            if recent_phantoms >= 5:
                logger.warning(f"Rate limit fantasma atingido ({recent_phantoms}); dropando {phone}")
                _audit("skipped", f"phantom_rate_limit:{phone}")
                return {"processed": False, "reason": "phantom_rate_limit", "phone": phone}

            # Cria fantasma. pushName se disponivel; senao usa "Desconhecido +{phone}".
            # circulo=5 = circulo mais distante (escala 1=intimo a 5=desconhecido).
            push_name = (data.get("data", {}).get("pushName") or "").strip()
            display_name = push_name if push_name else f"Desconhecido +{phone}"
            telefones_json = json.dumps([{"type": "mobile", "number": f"+{phone}", "whatsapp": True}])
            cursor.execute("""
                INSERT INTO contacts (nome, telefones, origem, circulo, criado_em, atualizado_em)
                VALUES (%s, %s::jsonb, 'wa_unknown', 5, NOW(), NOW())
                RETURNING id
            """, (display_name, telefones_json))
            contact_id = cursor.fetchone()["id"]
            conn.commit()
            logger.info(f"Created phantom contact #{contact_id} ({display_name}) for unknown phone {phone}")
            _audit("info", f"phantom_created:{contact_id}:{phone}")
            # Continua fluxo normal — mensagem sera gravada com este contact_id.
            contact = {"id": contact_id, "nome": display_name}

        contact_id = contact["id"]

        # Buscar ou criar conversa
        cursor.execute("""
            SELECT id FROM conversations
            WHERE contact_id = %s AND canal = 'whatsapp'
        """, (contact_id,))
        conv = cursor.fetchone()

        if conv:
            conversation_id = conv["id"]
        else:
            cursor.execute("""
                INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
                VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
                RETURNING id
            """, (contact_id,))
            conversation_id = cursor.fetchone()["id"]

        # Verificar se mensagem já existe
        cursor.execute("""
            SELECT id FROM messages WHERE external_id = %s
        """, (message_id,))

        if cursor.fetchone():
            _audit("skipped", "duplicate")
            return {"processed": False, "reason": "duplicate"}

        # Inserir mensagem
        direction = "outgoing" if from_me else "incoming"
        timestamp = datetime.now()

        cursor.execute("""
            INSERT INTO messages
            (conversation_id, contact_id, external_id, direcao, conteudo, enviado_em, metadata, criado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (
            conversation_id,
            contact_id,
            message_id,
            direction,
            content,
            timestamp,
            json.dumps({"phone": phone, "type": message_type, "from_webhook": True})
        ))

        new_msg_id = cursor.fetchone()["id"]

        # Atualizar conversa
        cursor.execute("""
            UPDATE conversations
            SET ultimo_mensagem = %s,
                total_mensagens = COALESCE(total_mensagens, 0) + 1,
                requer_resposta = %s,
                atualizado_em = NOW()
            WHERE id = %s
        """, (timestamp, not from_me, conversation_id))

        # Atualizar contato
        cursor.execute("""
            UPDATE contacts
            SET ultimo_contato = %s,
                total_interacoes = COALESCE(total_interacoes, 0) + 1
            WHERE id = %s
        """, (timestamp, contact_id))

        conn.commit()

    OWNER_CONTACT_ID = 14911

    # Outbound (Renato respondeu): auto-resolve action_proposals pendentes.
    # Webhook é caminho real-time; sem isso, propostas ficam pending mesmo após resposta
    # (whatsapp_sync.py:248 já faz isso no path de polling, mas webhook não passava por lá).
    if direction == "outgoing" and contact_id and contact_id != OWNER_CONTACT_ID:
        try:
            from services.action_proposals import ActionProposalsService
            ActionProposalsService().dismiss_stale_on_reply(contact_id, timestamp)
        except Exception as e:
            logger.warning(f"dismiss_stale_on_reply via webhook falhou (contact={contact_id}): {e}")

    # Analisar mensagem com IA em background (apenas mensagens recebidas de contatos, não do próprio Renato)
    if direction == "incoming" and content and contact_id != OWNER_CONTACT_ID:
        asyncio.create_task(
            analyze_message_in_background(new_msg_id, contact_id, content)
        )

    # Audio inbound na instancia principal: transcreve inline via Groq (fire-and-forget).
    # Groq Whisper large-v3 leva ~2s pra 76s de audio — bem dentro do limite Vercel.
    # Substitui dispatch pro Railway worker (deletado 23/06/26).
    if direction == "incoming" and message_type == "audio":
        asyncio.create_task(_transcribe_audio_inline(key, phone, message_id, new_msg_id))

    # F4' — anexos WA sempre-on (DM rap-whatsapp, qualquer direcao):
    # PDF/imagem in+out e audio outgoing dispatcham pro Railway worker (audio
    # incoming ja foi tratado inline acima). Skip se contraparte for o bot —
    # intel-bot dispatcha do proprio lado, evita extração duplicada.
    should_dispatch_attachment = (
        not _is_bot_phone(phone)
        and (
            message_type in ("image", "document")
            or (message_type == "audio" and direction == "outgoing")
        )
    )
    if should_dispatch_attachment:
        try:
            from services.wa_attachment_dispatch import dispatch_attachment_to_worker
            # Bloqueante (~500ms): worker ACK fast quando silent.
            await dispatch_attachment_to_worker(
                message, key, phone, message_id,
                source="main_instance",
            )
        except Exception as e:
            logger.warning(f"wa_attachment dispatch (DM) failed: {e}")

    _audit("processed", f"ok:{direction}", resulting_id=new_msg_id)
    return {
        "processed": True,
        "message_id": new_msg_id,
        "contact_id": contact_id,
        "direction": direction
    }


async def analyze_message_in_background(message_id: int, contact_id: int, content: str):
    """Sunset gen-1 (11/07/26): o realtime_analyzer (pending_response / follow_up_alert /
    urgent_alert em tempo real, a cada msg WA) foi DESLIGADO — so gerava ruido. O julgamento
    agora e dos detectores (signals, cron detectors-run) + Tonia (briefing/urgent). Esta funcao
    passa a rodar SO o smart_message_processor (email/reuniao/telefone), que e util e separado.
    O modulo services/realtime_analyzer.py (orfao, sem caller) foi removido na limpeza
    pos-sunset (12/07/26) — historico no git."""

    # Smart Message Processor: detecta emails, reunioes, telefones
    try:
        from services.smart_message_processor import process_message_intelligence
        await process_message_intelligence(
            message_id=message_id,
            contact_id=contact_id,
            content=content,
            direction="incoming"
        )
    except Exception as e:
        logger.error(f"Error in smart message processor for msg {message_id}: {e}")


async def _transcribe_audio_inline(
    key: Dict,
    phone: str,
    wa_message_id: str,
    db_message_id: int,
) -> None:
    """Baixa audio da Evolution, transcreve via Groq Whisper, salva em wa_attachments.

    Fire-and-forget — erros so logados, nunca bloqueiam o ingest principal.
    Substituiu o dispatch pro Railway worker (deletado 23/06/26).
    """
    import base64
    evo_url = os.getenv("EVOLUTION_API_URL", "").strip().rstrip("/")
    evo_key = os.getenv("EVOLUTION_API_KEY", "").strip()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp").strip()

    if not evo_url or not groq_key:
        logger.warning(f"inline-audio: EVOLUTION_API_URL ou GROQ_API_KEY ausente — skip msg={db_message_id}")
        return

    try:
        # 1. Download audio como base64
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl = await client.post(
                f"{evo_url}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": evo_key, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False},
            )
        if dl.status_code not in (200, 201):
            logger.warning(f"inline-audio: download failed status={dl.status_code} msg={db_message_id}")
            return
        dl_data = dl.json()
        audio_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "audio/ogg")
        if not audio_b64:
            logger.warning(f"inline-audio: base64 vazio msg={db_message_id}")
            return

        audio_bytes = base64.b64decode(audio_b64)
        ext_map = {"audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
        clean_mime = mimetype.split(";")[0].strip()
        ext = ext_map.get(clean_mime, "ogg")

        # 2. Transcreve via Groq Whisper large-v3
        whisper_prompt = (
            "Renato Almeida Prado, Tonha, ImensIAH, ConselhoOS, Vallen Clinic, "
            "Almeida Prado, Assespro, Wadhwani, Despertar, Emma, Orestes, "
            "RACI, briefing, CoS, conselheiro, board, governanca corporativa."
        )
        async with httpx.AsyncClient(timeout=45.0) as client:
            gr = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": (f"audio.{ext}", audio_bytes, clean_mime)},
                data={
                    "model": "whisper-large-v3",
                    "language": "pt",
                    "prompt": whisper_prompt,
                    "temperature": "0",
                    "response_format": "verbose_json",
                },
            )
        if gr.status_code != 200:
            logger.warning(f"inline-audio: groq failed status={gr.status_code} msg={db_message_id}")
            return

        gr_data = gr.json()
        transcription = (gr_data.get("text") or "").strip()

        # Filtro anti-alucinacao: no_speech_prob alto ou avg_logprob muito negativo
        segments = gr_data.get("segments") or []
        if segments:
            avg_no_speech = sum(s.get("no_speech_prob", 0) for s in segments) / len(segments)
            avg_logprob = sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
            if avg_no_speech > 0.6 or avg_logprob < -1.0:
                logger.warning(f"inline-audio: hallucination filter triggered (no_speech={avg_no_speech:.2f} logprob={avg_logprob:.2f}) msg={db_message_id}")
                return

        if not transcription:
            logger.warning(f"inline-audio: transcricao vazia msg={db_message_id}")
            return

        logger.info(f"inline-audio: transcribed {len(transcription)} chars msg={db_message_id}: {transcription[:80]}")

        # 3. Salva em wa_attachments (idempotente via ON CONFLICT)
        try:
            from database import get_db
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO wa_attachments (
                            message_id, phone, kind, mime_type, size_bytes,
                            extracted_text, extraction_model
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_id, kind) DO UPDATE
                            SET extracted_text   = EXCLUDED.extracted_text,
                                extraction_model = EXCLUDED.extraction_model
                    """, (
                        wa_message_id, phone, "audio", clean_mime, len(audio_bytes),
                        transcription, "whisper-large-v3",
                    ))
                conn.commit()
        except Exception as db_err:
            logger.warning(f"inline-audio: db save failed msg={db_message_id}: {db_err}")

    except Exception as e:
        logger.warning(f"inline-audio: unexpected error msg={db_message_id}: {e}")


async def process_sent_message(data: Dict) -> Dict:
    """Processa confirmação de mensagem enviada - tambem verifica respostas a propostas"""
    import asyncio

    key = data.get("key", {})
    message = data.get("message", {})

    message_id = key.get("id")
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)

    logger.info(f"Message sent: {message_id}, fromMe: {from_me}, to: {remote_jid}")

    # Extrair conteudo
    content = ""
    if "conversation" in message:
        content = message["conversation"]
    elif "extendedTextMessage" in message:
        content = message["extendedTextMessage"].get("text", "")

    # Verificar se e resposta a proposta (mensagem enviada por Renato)
    if from_me and content and is_proposal_response(content):
        remote_jid_alt = key.get("remoteJidAlt", "")
        if "@lid" in remote_jid and remote_jid_alt and "@s.whatsapp.net" in remote_jid_alt:
            phone = remote_jid_alt.replace("@s.whatsapp.net", "")
        else:
            phone = remote_jid.replace("@s.whatsapp.net", "")
        logger.info(f"Detected proposal response in send.message: {content}")
        asyncio.create_task(process_renato_reply(content, phone))
        return {"processed": True, "reason": "proposal_response", "content": content}

    return {"processed": True, "event": "send.message"}


def is_proposal_response(content: str) -> bool:
    """
    Verifica se o conteudo parece ser uma resposta a uma proposta de acao.

    Respostas validas:
    - Numeros: 1, 2, 3, 4
    - Emojis numericos: 1️⃣, 2️⃣, 3️⃣, 4️⃣
    - Referencias: #123
    - Comandos: pendentes, ignorar, lista
    """
    text = content.strip().lower()

    # Respostas numericas simples
    if text in ['1', '2', '3', '4']:
        return True

    # Emojis numericos
    if text in ['1️⃣', '2️⃣', '3️⃣', '4️⃣']:
        return True

    # Referencia a proposta (#123)
    if text.startswith('#') and any(c.isdigit() for c in text):
        return True

    # Comandos especiais
    if text in ['pendentes', 'pending', 'lista', 'list', 'ignorar', 'ignore', 'skip', 'pular']:
        return True

    # Ref: #123 no texto
    if 'ref:' in text and '#' in text:
        return True

    return False


async def process_renato_reply(content: str, phone: str):
    """
    Processa resposta do Renato a uma notificacao de proposta.
    """
    try:
        from services.whatsapp_notifications import get_whatsapp_notifications

        notifications = get_whatsapp_notifications()
        result = await notifications.process_reply(content, phone)

        if result:
            logger.info(f"Processed Renato reply: {content} -> {result}")
        else:
            logger.debug(f"Reply not processed: {content}")

    except Exception as e:
        logger.error(f"Error processing Renato reply: {e}")


async def _transcribe_bot_audio(key: Dict, data: Dict) -> str:
    """Download audio from WhatsApp and transcribe using Claude."""
    import base64

    evo_url = os.getenv("EVOLUTION_API_URL", "").replace('\\n', '').replace('\\r', '').strip().rstrip("/")
    evo_key = os.getenv("EVOLUTION_API_KEY", "").strip()
    bot_instance = os.getenv("INTEL_BOT_INSTANCE", "intel-bot-v2").strip()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not evo_url or not api_key:
        return ""

    # Download audio as base64 (tight timeout for Vercel)
    async with httpx.AsyncClient(timeout=8.0) as client:
        dl_resp = await client.post(
            f"{evo_url}/chat/getBase64FromMediaMessage/{bot_instance}",
            headers={"apikey": evo_key, "Content-Type": "application/json"},
            json={"message": {"key": key}, "convertToMp4": False}
        )
        if dl_resp.status_code not in (200, 201):
            return ""

        dl_data = dl_resp.json()
        audio_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "audio/mp4")
        if not audio_b64:
            return ""

    # Transcribe with Claude (multimodal, tight timeout)
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": llm.FAST,
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": mimetype if "/" in mimetype else "audio/mp4",
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

    if resp.status_code == 200:
        result = resp.json()
        return result.get("content", [{}])[0].get("text", "")

    logger.warning(f"Claude transcription failed: {resp.status_code}")
    return ""


async def _handle_intel_bot_message(data: Dict) -> Dict:
    """
    Handle messages arriving on the intel-bot instance.
    Routes to the conversational bot handler and sends response back.
    """
    import asyncio

    key = data.get("key", {})
    message = data.get("message", {})
    remote_jid = key.get("remoteJid", "")
    remote_jid_alt = key.get("remoteJidAlt", "")
    from_me = key.get("fromMe", False)
    message_id = key.get("id", "")

    # Ignore group messages and messages sent by the bot itself
    if "@g.us" in remote_jid or from_me:
        return {"processed": False, "reason": "group_or_self"}

    # Handle LID format
    if "@lid" in remote_jid and remote_jid_alt and "@s.whatsapp.net" in remote_jid_alt:
        phone = remote_jid_alt.replace("@s.whatsapp.net", "")
    else:
        phone = remote_jid.replace("@s.whatsapp.net", "").replace("@lid", "")

    # Extract text content
    content = ""
    is_audio = False
    is_image = False
    is_pdf = False
    pdf_filename = ""
    pdf_caption = ""
    if "conversation" in message:
        content = message["conversation"]
    elif "extendedTextMessage" in message:
        content = message["extendedTextMessage"].get("text", "")
    elif "audioMessage" in message:
        is_audio = True
        content = "__AUDIO_PENDING__"
    elif "imageMessage" in message:
        is_image = True
        caption = message["imageMessage"].get("caption", "")
        content = caption or "__IMAGE_PENDING__"
    elif "documentMessage" in message:
        doc = message["documentMessage"]
        mime = (doc.get("mimetype") or "").lower()
        if "pdf" in mime or (doc.get("fileName") or "").lower().endswith(".pdf"):
            is_pdf = True
            pdf_filename = doc.get("fileName") or "documento.pdf"
            pdf_caption = doc.get("caption", "") or ""
            content = pdf_caption or "__PDF_PENDING__"

    if not content:
        return {"processed": False, "reason": "no_text_content"}

    logger.info(f"Intel bot message from {phone}: {'[audio]' if is_audio else content[:100]}")

    # ===== News Watcher Digest Reply (Modo D, 28/06/2026) =====
    # Se Renato responde a um digest pendente ("ok" / numero / nome do projeto),
    # curto-circuita o bot principal. Sai antes do worker dispatch pra evitar
    # bot interpretar "ok" como confirmacao de outra coisa.
    # So pra mensagens de texto (audio/image/pdf cai no fluxo normal).
    if not (is_audio or is_image or is_pdf) and content and content.strip():
        try:
            from services.project_news_watcher import handle_digest_response
            digest_reply = await handle_digest_response(content, phone)
            if digest_reply:
                logger.info(f"intel_bot: digest reply handled, curto-circuita worker")
                return {"processed": True, "reason": "news_digest_reply"}
        except Exception as e:
            # Falha silenciosa: deixa o bot processar normalmente
            logger.warning(f"intel_bot: handle_digest_response falhou: {e}")

    # For audio: dispatch to Railway worker (Vercel 10s timeout is too short)
    if is_audio:
        audio_worker_url = os.getenv("AUDIO_WORKER_URL", "")
        worker_secret = get_worker_secret()
        if audio_worker_url and not worker_secret:
            logger.error("WORKER_SECRET não configurado — audio dispatch abortado (sem fallback)")
            return {"processed": False, "reason": "worker_secret_missing"}
        if audio_worker_url:
            try:
                # Fire-and-forget: don't wait for response (Railway processes async)
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(
                        f"{audio_worker_url}/transcribe",
                        json={"key": key, "phone": phone, "message_id": message_id, "secret": worker_secret}
                    )
                    logger.info(f"Audio dispatched to worker: {resp.status_code}")
                return {"processed": True, "reason": "audio_dispatched_to_worker"}
            except Exception as e:
                logger.error(f"Audio worker dispatch failed: {e}")
                # Don't send fallback - worker might still be processing
                return {"processed": True, "reason": f"audio_dispatch_error: {e}"}
        # Only if AUDIO_WORKER_URL not configured at all
        logger.warning("AUDIO_WORKER_URL not set, audio not supported")
        return {"processed": False, "reason": "audio_no_worker_url"}

    # For PDFs: dispatch to Railway worker /analyze-pdf
    if is_pdf:
        worker_url = os.getenv("AUDIO_WORKER_URL", "")
        worker_secret = get_worker_secret()
        if worker_url and not worker_secret:
            logger.error("WORKER_SECRET não configurado — pdf dispatch abortado (sem fallback)")
            return {"processed": False, "reason": "worker_secret_missing"}
        if worker_url:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(
                        f"{worker_url}/analyze-pdf",
                        json={
                            "key": key, "phone": phone, "message_id": message_id,
                            "filename": pdf_filename, "caption": pdf_caption,
                            "secret": worker_secret,
                        }
                    )
                    logger.info(f"PDF dispatched to worker: {resp.status_code}")
                return {"processed": True, "reason": "pdf_dispatched_to_worker"}
            except Exception as e:
                logger.error(f"PDF worker dispatch failed: {e}")
        logger.warning("AUDIO_WORKER_URL not set, PDF not supported")
        return {"processed": False, "reason": "pdf_no_worker_url"}

    # For images: dispatch to Railway worker for Claude Vision analysis
    if is_image:
        worker_url = os.getenv("AUDIO_WORKER_URL", "")
        worker_secret = get_worker_secret()
        if worker_url and not worker_secret:
            logger.error("WORKER_SECRET não configurado — image dispatch abortado (sem fallback)")
            return {"processed": False, "reason": "worker_secret_missing"}
        if worker_url:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(
                        f"{worker_url}/analyze-image",
                        json={"key": key, "phone": phone, "message_id": message_id,
                              "caption": content if content != "__IMAGE_PENDING__" else "",
                              "secret": worker_secret}
                    )
                    logger.info(f"Image dispatched to worker: {resp.status_code}")
                return {"processed": True, "reason": "image_dispatched_to_worker"}
            except Exception as e:
                logger.error(f"Image worker dispatch failed: {e}")

    # Dispatch ALL bot messages to Railway worker (Vercel 10s timeout is too short for tool_use)
    worker_url = os.getenv("AUDIO_WORKER_URL", "")
    worker_secret = get_worker_secret()
    if worker_url and not worker_secret:
        logger.error("WORKER_SECRET não configurado — bot dispatch abortado (sem fallback)")
        return {"processed": False, "reason": "worker_secret_missing"}
    if worker_url:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{worker_url}/process-message",
                    json={"phone": phone, "content": content, "message_id": message_id, "secret": worker_secret}
                )
                logger.info(f"Bot message dispatched to worker: {resp.status_code}")
            return {"processed": True, "reason": "bot_dispatched_to_worker"}
        except Exception as e:
            logger.error(f"Bot worker dispatch failed: {e}")

    # Fallback: process in Vercel (may timeout)
    asyncio.create_task(_process_and_respond_bot(phone, content, message_id))

    return {"processed": True, "reason": "intel_bot", "phone": phone}


async def _process_audio_and_respond_bot(phone: str, key: Dict, data: Dict, message_id: str):
    """Transcribe audio then process as bot message. Runs in background."""
    try:
        from services.intel_bot import send_intel_notification

        content = await _transcribe_bot_audio(key, data)
        if not content:
            await send_intel_notification("Nao consegui transcrever o audio. Pode digitar?", phone=phone)
            return

        content = f"[Audio transcrito] {content}"
        await _process_and_respond_bot(phone, content, message_id)

    except Exception as e:
        logger.error(f"Audio bot processing error: {e}")
        try:
            from services.intel_bot import send_intel_notification
            await send_intel_notification("Erro ao processar audio. Tenta digitar?", phone=phone)
        except Exception:
            pass


async def _process_and_respond_bot(phone: str, content: str, message_id: str):
    """Process bot message and send response back via intel-bot instance."""
    try:
        from services.intel_bot import handle_bot_message, send_intel_notification

        response = await handle_bot_message(phone, content, message_id)

        # Empty response means skip (trivial message like emoji)
        if not response:
            return

        # Send response back via intel-bot
        await send_intel_notification(response, phone=phone)

    except Exception as e:
        logger.error(f"Error in intel bot processing: {e}")
        try:
            from services.intel_bot import send_intel_notification
            await send_intel_notification(
                "Desculpa, tive um erro interno. Tenta de novo?",
                phone=phone
            )
        except Exception:
            logger.error("Failed to send error message to user")
