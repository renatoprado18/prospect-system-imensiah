"""
oauth_store.py — Persistencia SQLite do estado OAuth 2.1 do CoPiloto MCP HTTP.

Motivacao: o `SingleUserOAuthProvider` (http_server.py) guardava clients (DCR),
authorization codes e tokens EM MEMORIA. A cada redeploy/restart do servico o
estado zerava e o Renato tinha que re-autorizar no claude.ai. Este store persiste
esse estado num arquivo SQLite local (caminho por env `MCP_OAUTH_DB`), de forma
idempotente na subida, para sobreviver a reinicio de processo.

Escopo: persiste clients / auth_codes / access_tokens / refresh_tokens. O
`pending` (transacao de consentimento em curso, TTL 10 min) fica em memoria de
proposito — e efemero e completa em segundos no meio do fluxo do browser.

O bearer estatico (MCP_HTTP_TOKEN) NAO passa por aqui: e caminho paralelo,
validado direto em memoria no provider, e continua intacto.

Serializacao: os objetos sao modelos pydantic do SDK `mcp`
(OAuthClientInformationFull, AuthorizationCode, AccessToken, RefreshToken).
Guardamos o JSON canonico (`model_dump_json`) + a coluna `expires_at` desnormalizada
pra permitir GC em SQL sem desserializar.

Thread-safety: streamable-http do FastMCP pode chamar de threads/loops distintos.
Conexao com check_same_thread=False + Lock global cobrindo toda operacao.
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Dict, Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger("copilot_mcp.oauth_store")


def default_db_path() -> str:
    """Caminho do arquivo SQLite. Env `MCP_OAUTH_DB` (ex: /data/oauth_state.db num
    volume Railway pra sobreviver a redeploy) ou `mcp/oauth_state.db` ao lado deste
    modulo por padrao (sobrevive a restart de processo dentro do mesmo container)."""
    env = (os.getenv("MCP_OAUTH_DB") or "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth_state.db")


class OAuthStore:
    """Store SQLite dos 4 tipos de estado OAuth. Criacao de schema idempotente."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        self._lock = threading.Lock()
        # isolation_level=None -> autocommit; controlamos consistencia com o Lock.
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._gc()
        logger.info("OAuth store SQLite pronto: %s", self.path)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    data      TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_auth_codes (
                    code       TEXT PRIMARY KEY,
                    expires_at REAL,
                    data       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                    token      TEXT PRIMARY KEY,
                    expires_at REAL,
                    data       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token      TEXT PRIMARY KEY,
                    expires_at REAL,
                    data       TEXT NOT NULL
                );
                """
            )

    def _gc(self) -> None:
        """Remove codes e access tokens expirados. Refresh tokens do provider tem
        expires_at=None (sem expiracao) — nao mexe. Defensivo: nao derruba a subida."""
        now = time.time()
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM oauth_auth_codes WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
                self._conn.execute(
                    "DELETE FROM oauth_access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
                self._conn.execute(
                    "DELETE FROM oauth_refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
        except Exception as e:  # pragma: no cover
            logger.warning("OAuth store GC falhou (ignorado): %s", e)

    # --- clients -------------------------------------------------------------
    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, data) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )

    def load_clients(self) -> Dict[str, OAuthClientInformationFull]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM oauth_clients").fetchall()
        out: Dict[str, OAuthClientInformationFull] = {}
        for (data,) in rows:
            try:
                c = OAuthClientInformationFull.model_validate_json(data)
                out[c.client_id] = c
            except Exception as e:  # pragma: no cover
                logger.warning("client corrompido no store, ignorado: %s", e)
        return out

    # --- auth codes ----------------------------------------------------------
    def save_code(self, code: AuthorizationCode) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_auth_codes (code, expires_at, data) VALUES (?, ?, ?)",
                (code.code, code.expires_at, code.model_dump_json()),
            )

    def delete_code(self, code: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM oauth_auth_codes WHERE code = ?", (code,))

    def load_codes(self) -> Dict[str, AuthorizationCode]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM oauth_auth_codes"
            ).fetchall()
        out: Dict[str, AuthorizationCode] = {}
        for (data,) in rows:
            try:
                c = AuthorizationCode.model_validate_json(data)
                out[c.code] = c
            except Exception as e:  # pragma: no cover
                logger.warning("auth_code corrompido no store, ignorado: %s", e)
        return out

    # --- access tokens -------------------------------------------------------
    def save_access(self, at: AccessToken) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_access_tokens (token, expires_at, data) VALUES (?, ?, ?)",
                (at.token, at.expires_at, at.model_dump_json()),
            )

    def delete_access(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM oauth_access_tokens WHERE token = ?", (token,)
            )

    def load_access(self) -> Dict[str, AccessToken]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM oauth_access_tokens"
            ).fetchall()
        out: Dict[str, AccessToken] = {}
        for (data,) in rows:
            try:
                a = AccessToken.model_validate_json(data)
                out[a.token] = a
            except Exception as e:  # pragma: no cover
                logger.warning("access_token corrompido no store, ignorado: %s", e)
        return out

    # --- refresh tokens ------------------------------------------------------
    def save_refresh(self, rt: RefreshToken) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_refresh_tokens (token, expires_at, data) VALUES (?, ?, ?)",
                (rt.token, rt.expires_at, rt.model_dump_json()),
            )

    def delete_refresh(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token = ?", (token,)
            )

    def load_refresh(self) -> Dict[str, RefreshToken]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM oauth_refresh_tokens"
            ).fetchall()
        out: Dict[str, RefreshToken] = {}
        for (data,) in rows:
            try:
                r = RefreshToken.model_validate_json(data)
                out[r.token] = r
            except Exception as e:  # pragma: no cover
                logger.warning("refresh_token corrompido no store, ignorado: %s", e)
        return out
