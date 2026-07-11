"""
Arquivamento de binários de anexos WA no Google Drive (F-2, Passo B — raw completo).

Contexto: os anexos WA (imagem/áudio/PDF) têm o conteúdo extraído em
`wa_attachments.extracted_text`, mas o binário original é descartado após a
extração. Este serviço, rodado por cron, RE-BAIXA o binário da Evolution e o
arquiva no Google Drive do Renato, gravando o file_id/link em `wa_attachments`.

Por que re-baixar em vez de tocar o ingest: o pipeline tem dois caminhos (áudio
inline no Vercel, imagem/PDF no worker Railway) e o token Google só vive no
Vercel. A `key` da Evolution é reconstruível de `phone + message_id + direcao`,
então um cron único no Vercel resolve os dois caminhos sem tocar o hot-path nem
dar credencial Google ao worker. Provado E2E: getBase64FromMediaMessage com a
key reconstruída devolve o binário.

Escopo v1: DM (phone numérico). Grupos precisam de group-jid+participant na key;
por ora falham no re-download e são pulados (best-effort, refinar depois).
Só go-forward — a mídia da Evolution expira, daí a janela dos últimos dias.
"""
import base64
import logging
import os
from typing import Any, Dict, Optional

import httpx

from database import get_db
from integrations.google_drive import (
    create_folder,
    get_valid_token,
    list_folders,
    upload_file,
)

log = logging.getLogger("wa_drive_archive")

_ROOT_FOLDER = "INTEL - WhatsApp Anexos"
# extensão amigável por mimetype (fallback = o kind do anexo)
_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav",
    "application/pdf": "pdf",
}


async def _get_or_create_folder(token: str, name: str, parent_id: Optional[str] = None) -> str:
    """Acha a pasta por nome (dentro de parent, ou no root) ou cria. Retorna o id."""
    for f in await list_folders(token, parent_id):
        if f.get("name") == name:
            return f["id"]
    created = await create_folder(token, name, parent_id)
    return created["id"]


async def _redownload(evo_url: str, evo_key: str, instance: str, wa_key: Dict[str, Any]):
    """Re-baixa o binário da Evolution via key reconstruída. (bytes, mimetype) ou (None, None)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl = await client.post(
                f"{evo_url}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": evo_key, "Content-Type": "application/json"},
                json={"message": {"key": wa_key}, "convertToMp4": False},
            )
        if dl.status_code not in (200, 201):
            return None, None
        d = dl.json()
        b64 = d.get("base64", "")
        return (base64.b64decode(b64) if b64 else None), d.get("mimetype")
    except Exception:
        return None, None


async def archive_pending_attachments(limit: int = 25) -> Dict[str, Any]:
    """
    Arquiva no Drive os anexos WA ainda sem `drive_file_id` (janela de 3 dias, DM).
    NUNCA levanta — devolve um dict com contadores.
    """
    evo_url = (os.getenv("EVOLUTION_API_URL") or "").strip().rstrip("/")
    evo_key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    instance = (os.getenv("EVOLUTION_INSTANCE") or "rap-whatsapp").strip()
    if not (evo_url and evo_key):
        return {"error": "evolution creds missing", "processed": 0}

    result = {"processed": 0, "uploaded": 0, "failed": 0, "skipped": 0}
    try:
        with get_db() as conn:
            token = await get_valid_token(conn, "professional")
            if not token:
                return {"error": "no google token", "processed": 0}

            root_id = await _get_or_create_folder(token, _ROOT_FOLDER)
            month_cache: Dict[str, str] = {}

            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.id, a.message_id, a.phone, a.kind, a.mime_type,
                       a.original_filename, a.criado_em, m.direcao
                  FROM wa_attachments a
                  LEFT JOIN public.messages m ON m.external_id = a.message_id
                 WHERE a.drive_file_id IS NULL
                   AND a.criado_em > NOW() - INTERVAL '3 days'
                   AND a.phone ~ '^[0-9]+$'
              ORDER BY a.criado_em DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

            for r in rows:
                result["processed"] += 1
                wa_key = {
                    "remoteJid": f"{r['phone']}@s.whatsapp.net",
                    "id": r["message_id"],
                    "fromMe": (r.get("direcao") == "outgoing"),
                }
                data, mt = await _redownload(evo_url, evo_key, instance, wa_key)
                if not data:
                    # provável grupo (key ≠ DM) ou mídia expirada — pula, tenta de novo
                    result["failed"] += 1
                    continue

                clean_mt = (mt or r.get("mime_type") or "application/octet-stream").split(";")[0].strip()
                ext = _EXT.get(clean_mt, r["kind"] or "bin")

                # subpasta por mês (cache dentro do run)
                ym = r["criado_em"].strftime("%Y-%m")
                if ym not in month_cache:
                    month_cache[ym] = await _get_or_create_folder(token, ym, root_id)
                folder_id = month_cache[ym]

                fname = r.get("original_filename") or f"{r['kind']}_{r['message_id']}.{ext}"
                try:
                    up = await upload_file(token, data, fname, clean_mt, folder_id)
                    fid = up.get("id")
                    if not fid:
                        result["failed"] += 1
                        continue
                    link = f"https://drive.google.com/file/d/{fid}/view"
                    cur.execute(
                        """UPDATE wa_attachments
                              SET drive_file_id = %s, drive_web_link = %s, drive_archived_at = NOW()
                            WHERE id = %s""",
                        (fid, link, r["id"]),
                    )
                    conn.commit()
                    result["uploaded"] += 1
                except Exception:
                    log.exception("wa_drive_archive: upload falhou id=%s", r["id"])
                    result["failed"] += 1
    except Exception as e:
        log.exception("wa_drive_archive: erro geral")
        result["error"] = f"{type(e).__name__}: {e}"

    log.info(
        "wa_drive_archive: processed=%s uploaded=%s failed=%s",
        result["processed"], result["uploaded"], result["failed"],
    )
    return result
