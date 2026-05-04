"""
Google Sheets Integration for INTEL

Reusa o token storage existente (google_accounts) e helpers de refresh
do google_drive.py. Nao cria tabela nova.

Endpoints da API:
- https://sheets.googleapis.com/v4/spreadsheets/{id}
- https://sheets.googleapis.com/v4/spreadsheets/{id}:batchUpdate
- https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}:append

Scope necessario: https://www.googleapis.com/auth/spreadsheets
"""
import os
import httpx
from typing import Optional, List, Dict, Any
from urllib.parse import quote

from database import get_db


SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


class GoogleSheets:
    """Wrapper minimalista pra Google Sheets API v4."""

    def __init__(self):
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    async def _get_token(self, account_type: str = "professional") -> Optional[str]:
        """
        Pega access_token valido (com refresh se expirado).
        Reusa get_valid_token do google_drive.py — single source of truth
        pra OAuth Google.
        """
        from integrations.google_drive import get_valid_token
        with get_db() as conn:
            return await get_valid_token(conn, account_type)

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Wrapper HTTP padronizado."""
        async with httpx.AsyncClient() as client:
            kwargs = {
                "headers": {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                "timeout": 30.0,
            }
            if json_body is not None:
                kwargs["json"] = json_body
            if params:
                kwargs["params"] = params

            response = await client.request(method, url, **kwargs)

            if response.status_code == 401:
                raise PermissionError(
                    "Token expirado ou sem permissao pra Sheets. "
                    "Reconecte Google OAuth (scope spreadsheets)."
                )
            if response.status_code == 403:
                raise PermissionError(
                    f"Sheets API negou (403). Verifique scope spreadsheets "
                    f"ou permissao da planilha. Body: {response.text}"
                )
            if response.status_code not in (200, 201):
                raise RuntimeError(
                    f"Sheets API error {response.status_code}: {response.text}"
                )

            return response.json() if response.text else {}

    async def get_spreadsheet(
        self, spreadsheet_id: str, access_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Retorna metadata da planilha (sheets/abas, propriedades, etc)."""
        if access_token is None:
            access_token = await self._get_token()
        if not access_token:
            raise RuntimeError("Token Google indisponivel. Reconecte OAuth.")

        url = f"{SHEETS_API_BASE}/{spreadsheet_id}"
        return await self._request("GET", url, access_token)

    async def get_or_create_sheet(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Verifica se a aba existe; cria se nao.
        Retorna {created: bool, sheet_id: int, title: str}.
        """
        if access_token is None:
            access_token = await self._get_token()
        if not access_token:
            raise RuntimeError("Token Google indisponivel. Reconecte OAuth.")

        meta = await self.get_spreadsheet(spreadsheet_id, access_token)
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                return {
                    "created": False,
                    "sheet_id": props.get("sheetId"),
                    "title": props.get("title"),
                }

        # Criar nova aba via batchUpdate
        url = f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate"
        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {"title": sheet_name}
                    }
                }
            ]
        }
        result = await self._request("POST", url, access_token, json_body=body)
        replies = result.get("replies", [])
        if replies and "addSheet" in replies[0]:
            props = replies[0]["addSheet"].get("properties", {})
            return {
                "created": True,
                "sheet_id": props.get("sheetId"),
                "title": props.get("title"),
            }
        return {"created": True, "sheet_id": None, "title": sheet_name}

    async def append_rows(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        rows: List[List[Any]],
        access_token: Optional[str] = None,
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """
        Append em batch ao final da aba.
        value_input_option: USER_ENTERED interpreta formulas/datas; RAW grava literal.
        """
        if not rows:
            return {"updates": {"updatedRows": 0}}

        if access_token is None:
            access_token = await self._get_token()
        if not access_token:
            raise RuntimeError("Token Google indisponivel. Reconecte OAuth.")

        # Range: aba inteira (Sheets API descobre primeira linha vazia)
        range_str = quote(sheet_name, safe="")
        url = (
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range_str}:append"
            f"?valueInputOption={value_input_option}&insertDataOption=INSERT_ROWS"
        )
        body = {"values": rows}
        return await self._request("POST", url, access_token, json_body=body)

    async def read_range(
        self,
        spreadsheet_id: str,
        range_str: str,
        access_token: Optional[str] = None,
    ) -> List[List[Any]]:
        """Le range A1 (ex: 'Historico!A1:G10'). Retorna list of rows."""
        if access_token is None:
            access_token = await self._get_token()
        if not access_token:
            raise RuntimeError("Token Google indisponivel. Reconecte OAuth.")

        encoded = quote(range_str, safe="!:")
        url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded}"
        result = await self._request("GET", url, access_token)
        return result.get("values", [])
