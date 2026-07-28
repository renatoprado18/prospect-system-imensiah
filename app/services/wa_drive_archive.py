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

Teto de tentativas (28/07/2026, migration 055): a fila era definida só por
`drive_file_id IS NULL` e nada marcava que uma tentativa já tinha falhado, então
um anexo cuja mídia expirou voltava a ocupar slot a cada run (a cada 20 min) até
envelhecer 3 dias — até 216 re-tentativas do mesmo item, todas custando um POST
na Evolution. Medido: 547 falhas em 215 runs de 3 dias, com o cron reportando
`status='success'` o tempo inteiro. Mesma classe do livelock do wa-catchup
(1eaf1be). Agora cada tentativa é contada e o item sai da fila ao esgotar
WA_DRIVE_MAX_ATTEMPTS.
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

# Quantas vezes tentar re-baixar um anexo antes de desistir. Calibrado contra 30
# dias de produção (906 anexos arquivados): 84% entram na 1ª tentativa, 88% em
# até 4h. Com o cron a cada 20 min, 12 tentativas ≈ 4h — preserva praticamente
# tudo que de fato chega e corta a cauda infinita. Env pra ajustar sem deploy;
# 0 (ou negativo) desliga o teto e restaura o comportamento antigo.
_DEFAULT_MAX_ATTEMPTS = 12


def max_attempts() -> int:
    """Teto de tentativas por anexo. <= 0 desliga (kill-switch)."""
    try:
        return int((os.getenv("WA_DRIVE_MAX_ATTEMPTS") or "").strip() or _DEFAULT_MAX_ATTEMPTS)
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS
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

    # `exhausted` = itens que esgotaram o teto NESTA run (viraram desistência);
    # `blocked` = quantos a fila deixou de fora por já terem esgotado antes. Sem
    # esses dois o contador de `failed` sozinho não distingue "falhou agora" de
    # "vem falhando há 200 runs", que era exatamente o que escondia o livelock.
    result = {"processed": 0, "uploaded": 0, "failed": 0, "skipped": 0,
              "exhausted": 0, "blocked": 0}
    cap = max_attempts()
    try:
        with get_db() as conn:
            token = await get_valid_token(conn, "professional")
            if not token:
                return {"error": "no google token", "processed": 0}

            root_id = await _get_or_create_folder(token, _ROOT_FOLDER)
            month_cache: Dict[str, str] = {}

            cur = conn.cursor()
            # Quantos a fila está ignorando por teto — medida do passivo, e o
            # número que diria "o teto está apertado demais" se disparasse.
            if cap > 0:
                cur.execute(
                    """
                    SELECT COUNT(*) AS c FROM wa_attachments
                     WHERE drive_file_id IS NULL
                       AND criado_em > NOW() - INTERVAL '3 days'
                       AND phone ~ '^[0-9]+$'
                       AND drive_attempts >= %s
                    """,
                    (cap,),
                )
                _b = cur.fetchone()
                result["blocked"] = int(_b["c"]) if _b else 0

            cur.execute(
                """
                SELECT a.id, a.message_id, a.phone, a.kind, a.mime_type,
                       a.original_filename, a.criado_em, a.drive_attempts, m.direcao
                  FROM wa_attachments a
                  LEFT JOIN public.messages m ON m.external_id = a.message_id
                 WHERE a.drive_file_id IS NULL
                   AND a.criado_em > NOW() - INTERVAL '3 days'
                   AND a.phone ~ '^[0-9]+$'
                   AND (%s <= 0 OR a.drive_attempts < %s)
              ORDER BY a.criado_em DESC
                 LIMIT %s
                """,
                (cap, cap, limit),
            )
            rows = cur.fetchall()

            def _fail(row, motivo: str) -> None:
                """Registra a tentativa falha e contabiliza a desistência.

                Commit imediato, e não no fim da run: se a run morrer no meio
                (timeout do cron, erro de rede no próximo item), o que já foi
                tentado precisa continuar contado — senão o contador zera a cada
                falha global e o livelock volta pela porta dos fundos.
                """
                result["failed"] += 1
                tentativas = (row.get("drive_attempts") or 0) + 1
                if 0 < cap <= tentativas:
                    result["exhausted"] += 1
                    log.warning(
                        "wa_drive_archive: desistindo do anexo id=%s apos %s tentativas (%s)",
                        row["id"], tentativas, motivo,
                    )
                try:
                    cur.execute(
                        """UPDATE wa_attachments
                              SET drive_attempts = drive_attempts + 1,
                                  drive_last_attempt_at = NOW(),
                                  drive_last_error = %s
                            WHERE id = %s""",
                        (motivo[:500], row["id"]),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    log.exception("wa_drive_archive: nao consegui marcar tentativa id=%s", row["id"])

            for r in rows:
                result["processed"] += 1
                wa_key = {
                    "remoteJid": f"{r['phone']}@s.whatsapp.net",
                    "id": r["message_id"],
                    "fromMe": (r.get("direcao") == "outgoing"),
                }
                data, mt = await _redownload(evo_url, evo_key, instance, wa_key)
                if not data:
                    # provável grupo (key ≠ DM) ou mídia expirada na Evolution
                    _fail(r, "redownload devolveu vazio (grupo ou midia expirada)")
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
                        _fail(r, "upload no Drive nao devolveu file id")
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
                except Exception as up_err:
                    log.exception("wa_drive_archive: upload falhou id=%s", r["id"])
                    # A exceção pode ter deixado a transação suja (o UPDATE de
                    # sucesso roda dentro deste try); limpa antes de marcar.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    _fail(r, f"upload: {type(up_err).__name__}: {up_err}")
    except Exception as e:
        log.exception("wa_drive_archive: erro geral")
        result["error"] = f"{type(e).__name__}: {e}"

    log.info(
        "wa_drive_archive: processed=%s uploaded=%s failed=%s exhausted=%s blocked=%s",
        result["processed"], result["uploaded"], result["failed"],
        result["exhausted"], result["blocked"],
    )
    return result
