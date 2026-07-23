"""
Google Docs — leitura e reescrita de corpo via Drive API (sem scope Docs).

O token INTEL (conta profissional) tem `auth/drive` COMPLETO mas NÃO tem o
scope `auth/documents` (Docs API). Com Drive completo dá pra:
  - LER o texto de um Doc via export (`/files/{id}/export?mimeType=text/plain`)
  - REESCREVER o corpo via media update (`PATCH .../files/{id}?uploadType=media`,
    Content-Type text/plain — o Google reconverte pro formato Doc)

Reescreve o corpo INTEIRO (não faz insert incremental). O caller e responsavel
por passar o texto final ja mesclado. Usa text/plain de proposito: o Playbook
Andressa e estruturado com dividers ASCII (`====`), sem headings nativos do Doc
— text/plain preserva o estilo sem a complexidade de HTML→Doc.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


async def read_doc_text(access_token: str, doc_id: str) -> str:
    """Le o corpo de um Google Doc como texto plano (export via Drive API).

    Retorna string vazia se o Doc estiver vazio. Levanta em erro HTTP.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GOOGLE_DRIVE_API}/files/{doc_id}/export",
            params={"mimeType": "text/plain"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise Exception(f"read_doc_text falhou ({resp.status_code}): {resp.text[:300]}")
    # Drive exporta com BOM (﻿) e \r\n; normaliza
    return resp.text.replace("﻿", "").replace("\r\n", "\n")


async def overwrite_doc_text(access_token: str, doc_id: str, text: str) -> bool:
    """Reescreve o corpo INTEIRO de um Google Doc com `text` (text/plain).

    Substitui todo o conteudo — nao e append incremental. Retorna True em 200.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.patch(
            f"{GOOGLE_UPLOAD_API}/files/{doc_id}",
            params={"uploadType": "media"},
            content=text.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "text/plain; charset=UTF-8",
            },
        )
    if resp.status_code not in (200, 201):
        raise Exception(f"overwrite_doc_text falhou ({resp.status_code}): {resp.text[:300]}")
    return True
