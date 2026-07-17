"""
http_server.py — CoPiloto MCP server (transporte HTTP / streamable-http) + OAuth 2.1.

Variante REMOTA do `server.py` (stdio). Serve EXATAMENTE as mesmas ~17 tools
(reusa o objeto `mcp` de server.py — zero duplicacao de logica), so que via
HTTP publico. Habilita o CoPiloto no Claude mobile (app) e em connectors do
claude.ai — que herdam da conta.

AUTENTICACAO — dois caminhos, ambos aceitos no /mcp:

  1. OAuth 2.1 (para o claude.ai / Claude mobile). A UI de custom connector do
     claude.ai SO oferece OAuth (nao ha campo de bearer estatico). Implementamos
     o Authorization Server minimo (single-user do Renato) usando o suporte NATIVO
     do SDK `mcp` (OAuthAuthorizationServerProvider + FastMCP auth):
       - GET  /.well-known/oauth-protected-resource  (RFC 9728)
       - GET  /.well-known/oauth-authorization-server (RFC 8414)
       - POST /register   (RFC 7591 Dynamic Client Registration — claude.ai se auto-registra)
       - GET  /authorize  (PKCE S256) -> pagina de CONSENTIMENTO que pede a SENHA
       - POST /consent    (valida a senha; emite o `code` e redireciona de volta)
       - POST /token      (troca code+code_verifier por access_token; suporta refresh)
       - POST /revoke
     A pagina de consentimento e o gate: sem a senha (`MCP_OAUTH_PASSWORD`, com
     fallback pro `MCP_HTTP_TOKEN`) ninguem se auto-autoriza.

  2. Bearer estatico (para curl / Claude Desktop / Claude Code): o mesmo
     `MCP_HTTP_TOKEN` de sempre continua valendo como access_token direto no /mcp.
     O verificador de token aceita AMBOS (token OAuth emitido OU o estatico).

Sem a env `MCP_HTTP_TOKEN` o server RECUSA subir (fail-closed).

Rodar local:
    export MCP_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
    export MCP_OAUTH_PASSWORD="uma-senha-forte"   # opcional (default = MCP_HTTP_TOKEN)
    export MCP_PUBLIC_URL="http://localhost:8000"  # base publica (issuer OAuth)
    export DATABASE_URL=...            # Neon prod (ou local em dev)
    export VOYAGE_API_KEY=...          # opcional (search_memories semantica)
    export CONSELHOOS_DATABASE_URL=... # opcional (get_conselho)
    export RENATO_CONTACT_ID=4734
    python mcp/http_server.py          # escuta 0.0.0.0:$PORT (default 8000)

Endpoint MCP:  http(s)://<host>:<port>/mcp

Deploy: ver mcp/DEPLOY_HTTP.md (servico Railway `copiloto-mcp`, NAO o intel-api).

NOTA sobre persistencia: clients (DCR) / authorization codes / access+refresh
tokens sao PERSISTIDOS em SQLite (mcp/oauth_store.py, arquivo por env
`MCP_OAUTH_DB`, default `mcp/oauth_state.db`). Os dicts em memoria sao cache
write-through carregado na subida — o estado sobrevive a restart/redeploy do
processo, entao o Renato NAO precisa re-autorizar no claude.ai a cada deploy.
IMPORTANTE p/ Railway: o filesystem do container e efemero entre REDEPLOYS; pra
sobreviver a um novo build e preciso montar um volume e apontar `MCP_OAUTH_DB`
pra ele (ex: /data/oauth_state.db). Sem volume, sobrevive a restart mas nao a
redeploy. O `pending` (consentimento em curso, TTL 10 min) segue so em memoria.
Bearer estatico (MCP_HTTP_TOKEN) NAO passa pelo store — caminho paralelo intacto.
"""

import base64
import hashlib
import hmac
import html
import logging
import os
import secrets
import sys
import time

import uvicorn
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    ProviderTokenVerifier,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from oauth_store import OAuthStore

# Reuso total: importa o MESMO objeto FastMCP (com as ~17 tools ja registradas)
# do server stdio. Nada de re-registrar tools aqui — single source of truth.
from server import mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("copilot_mcp.http")


def _env(name: str, default: str = "") -> str:
    """Le env var com strip (Vercel/Railway as vezes colam \\n / espacos)."""
    return (os.getenv(name) or default).strip()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_HTTP_TOKEN = _env("MCP_HTTP_TOKEN")
# Senha da pagina de consentimento OAuth. Fallback pro bearer estatico se nao setada.
MCP_OAUTH_PASSWORD = _env("MCP_OAUTH_PASSWORD") or MCP_HTTP_TOKEN

# URL publica base (issuer do Authorization Server). Em prod, o dominio do Railway.
# Local: http://localhost:<PORT>. Sem barra final.
_PORT = int(_env("PORT", "8000") or "8000")
_default_base = f"http://localhost:{_PORT}"
_railway_domain = _env("RAILWAY_PUBLIC_DOMAIN")
if _railway_domain:
    _default_base = f"https://{_railway_domain}"
PUBLIC_BASE_URL = (_env("MCP_PUBLIC_URL") or _default_base).rstrip("/")

# TTLs
_CODE_TTL = 300            # authorization code: 5 min
_ACCESS_TTL = 60 * 60 * 24 * 30   # access token: 30 dias
_PENDING_TTL = 600         # consentimento pendente: 10 min


# ---------------------------------------------------------------------------
# Authorization Server provider — single-user, in-memory, PKCE.
# ---------------------------------------------------------------------------
class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """AS OAuth 2.1 minimo pra 1 usuario (Renato). DCR aberto (client publico,
    sem secret), mas a emissao do `code` e gated por uma pagina de consentimento
    que exige a senha. Tudo em memoria."""

    def __init__(self, static_token: str, password: str,
                 store: OAuthStore | None = None):
        self.static_token = static_token
        self.password = password
        # Persistencia SQLite: clients/codes/tokens sobrevivem a restart/redeploy
        # (sem re-autorizar no claude.ai a cada subida). O `pending` fica so em
        # memoria (efemero, TTL 10 min, meio do fluxo do browser).
        self.store = store if store is not None else OAuthStore()
        # Carrega estado persistido na subida (dict = cache write-through do SQLite).
        self.clients: dict[str, OAuthClientInformationFull] = self.store.load_clients()
        self.auth_codes: dict[str, AuthorizationCode] = self.store.load_codes()
        self.access_tokens: dict[str, AccessToken] = self.store.load_access()
        self.refresh_tokens: dict[str, RefreshToken] = self.store.load_refresh()
        logger.info(
            "OAuth state carregado do store: %d clients, %d codes, %d access, %d refresh",
            len(self.clients), len(self.auth_codes),
            len(self.access_tokens), len(self.refresh_tokens),
        )
        # txn_id -> (client, AuthorizationParams, created_at)
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}

    # --- DCR -----------------------------------------------------------------
    async def get_client(self, client_id: str):
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info
        self.store.save_client(client_info)
        logger.info("OAuth client registrado: %s (%s)", client_info.client_id,
                    client_info.client_name or "sem nome")

    # --- /authorize ----------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """Nao emite o code direto: guarda a transacao e manda o browser pra
        pagina de consentimento (/consent), onde a SENHA e exigida."""
        self._gc_pending()
        txn = secrets.token_urlsafe(32)
        self.pending[txn] = (client, params, time.time())
        return f"{PUBLIC_BASE_URL}/consent?txn={txn}"

    def complete_consent(self, txn: str) -> str:
        """Chamado pela rota /consent APOS validar a senha. Emite o authorization
        code e devolve a URL de redirect (redirect_uri + code + state)."""
        self._gc_pending()
        entry = self.pending.pop(txn, None)
        if not entry:
            raise KeyError("txn invalido ou expirado")
        client, params, _ = entry
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.auth_codes[code] = auth_code
        self.store.save_code(auth_code)
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    def pending_client_name(self, txn: str) -> str | None:
        entry = self.pending.get(txn)
        if not entry:
            return None
        return entry[0].client_name or entry[0].client_id

    # --- /token (authorization_code) ----------------------------------------
    async def load_authorization_code(self, client, authorization_code: str):
        code = self.auth_codes.get(authorization_code)
        if not code or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        # code de uso unico
        self.auth_codes.pop(authorization_code.code, None)
        self.store.delete_code(authorization_code.code)
        return self._issue_tokens(client.client_id, authorization_code.scopes)

    # --- /token (refresh_token) ---------------------------------------------
    async def load_refresh_token(self, client, refresh_token: str):
        rt = self.refresh_tokens.get(refresh_token)
        if not rt:
            return None
        if rt.expires_at and rt.expires_at < time.time():
            return None
        return rt

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        # rotaciona: invalida o refresh antigo
        self.refresh_tokens.pop(refresh_token.token, None)
        self.store.delete_refresh(refresh_token.token)
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes)

    # --- verificacao de access token (usada pelo /mcp) ----------------------
    async def load_access_token(self, token: str):
        # 1) bearer estatico legado — sempre valido (curl / Claude Desktop)
        if self.static_token and hmac.compare_digest(token, self.static_token):
            return AccessToken(
                token=token, client_id="static-bearer", scopes=[], expires_at=None
            )
        # 2) access token emitido via OAuth
        at = self.access_tokens.get(token)
        if not at:
            return None
        if at.expires_at and at.expires_at < time.time():
            self.access_tokens.pop(token, None)
            self.store.delete_access(token)
            return None
        return at

    async def revoke_token(self, token) -> None:
        tok = getattr(token, "token", None)
        if tok:
            self.access_tokens.pop(tok, None)
            self.refresh_tokens.pop(tok, None)
            self.store.delete_access(tok)
            self.store.delete_refresh(tok)

    # --- helpers -------------------------------------------------------------
    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        now = int(time.time())
        at = AccessToken(
            token=access, client_id=client_id, scopes=scopes or [],
            expires_at=now + _ACCESS_TTL,
        )
        rt = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes or [], expires_at=None,
        )
        self.access_tokens[access] = at
        self.refresh_tokens[refresh] = rt
        self.store.save_access(at)
        self.store.save_refresh(rt)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=_ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    def _gc_pending(self) -> None:
        cutoff = time.time() - _PENDING_TTL
        for k in [k for k, v in self.pending.items() if v[2] < cutoff]:
            self.pending.pop(k, None)


_provider = SingleUserOAuthProvider(MCP_HTTP_TOKEN, MCP_OAUTH_PASSWORD)


# ---------------------------------------------------------------------------
# Rotas custom: /healthz, /consent (gate de senha) e override do metadata AS.
# ---------------------------------------------------------------------------
async def _healthz(_request: Request):
    return JSONResponse({"status": "ok", "service": "copiloto-mcp-http"})


_CONSENT_PAGE = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CoPiloto — Autorizar acesso</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#2b2b2b;color:#f5efe6;
   display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .card{{background:#3b2d24;padding:32px;border-radius:14px;max-width:380px;width:90%;
   box-shadow:0 10px 40px rgba(0,0,0,.4)}}
 h1{{font-size:19px;margin:0 0 6px}} p{{font-size:14px;color:#cbb8a6;margin:0 0 18px}}
 label{{font-size:13px;display:block;margin-bottom:6px}}
 input[type=password]{{width:100%;padding:11px;border-radius:8px;border:1px solid #6b5844;
   background:#2b2b2b;color:#f5efe6;font-size:15px;box-sizing:border-box}}
 button{{margin-top:16px;width:100%;padding:12px;border:0;border-radius:8px;
   background:#a4845f;color:#231a12;font-weight:600;font-size:15px;cursor:pointer}}
 .err{{color:#f0a58a;font-size:13px;margin-top:12px}}
 .app{{color:#a4845f;font-weight:600}}
</style></head><body>
<div class="card">
 <h1>Autorizar CoPiloto</h1>
 <p>O app <span class="app">{app}</span> quer acessar o CoPiloto do Renato.
    Digite a senha para autorizar.</p>
 <form method="post" action="/consent">
  <input type="hidden" name="txn" value="{txn}">
  <label for="pw">Senha</label>
  <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
  <button type="submit">Autorizar</button>
  {err}
 </form>
</div></body></html>"""


async def _consent_get(request: Request):
    txn = request.query_params.get("txn", "")
    name = _provider.pending_client_name(txn)
    if not name:
        return HTMLResponse("<h1>Sessao de autorizacao invalida ou expirada.</h1>",
                            status_code=400)
    return HTMLResponse(_CONSENT_PAGE.format(
        app=html.escape(name), txn=html.escape(txn), err=""))


async def _consent_post(request: Request):
    form = await request.form()
    txn = str(form.get("txn", ""))
    password = str(form.get("password", ""))
    name = _provider.pending_client_name(txn)
    if not name:
        return HTMLResponse("<h1>Sessao de autorizacao invalida ou expirada.</h1>",
                            status_code=400)
    if not password or not hmac.compare_digest(password, MCP_OAUTH_PASSWORD):
        return HTMLResponse(
            _CONSENT_PAGE.format(app=html.escape(name), txn=html.escape(txn),
                                 err='<div class="err">Senha incorreta.</div>'),
            status_code=401,
        )
    try:
        redirect_url = _provider.complete_consent(txn)
    except KeyError:
        return HTMLResponse("<h1>Sessao de autorizacao invalida ou expirada.</h1>",
                            status_code=400)
    return RedirectResponse(url=redirect_url, status_code=302,
                            headers={"Cache-Control": "no-store"})


async def _as_metadata(_request: Request):
    """Override do /.well-known/oauth-authorization-server pra advertir
    `token_endpoint_auth_methods_supported: ["none"]` (client publico + PKCE),
    que e como o claude.ai se registra. O metadata do SDK advertiria
    client_secret_post/basic — cosmeticamente diferente do fluxo publico."""
    base = PUBLIC_BASE_URL
    meta = OAuthMetadata(
        issuer=AnyHttpUrl(base),
        authorization_endpoint=AnyHttpUrl(f"{base}/authorize"),
        token_endpoint=AnyHttpUrl(f"{base}/token"),
        registration_endpoint=AnyHttpUrl(f"{base}/register"),
        revocation_endpoint=AnyHttpUrl(f"{base}/revoke"),
        scopes_supported=None,
        response_types_supported=["code"],
        response_modes_supported=["query"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
        code_challenge_methods_supported=["S256"],
    )
    return JSONResponse(
        meta.model_dump(exclude_none=True, mode="json"),
        headers={"Cache-Control": "public, max-age=3600",
                 "Access-Control-Allow-Origin": "*"},
    )


def build_app():
    """Monta o ASGI app: streamable-http do FastMCP (rota /mcp) com auth OAuth
    nativa do SDK + rotas custom (healthz, consent, override de metadata)."""
    if not MCP_HTTP_TOKEN:
        logger.error(
            "MCP_HTTP_TOKEN nao setada — recusando subir (fail-closed). "
            "Gere um token forte: python -c 'import secrets;print(secrets.token_urlsafe(48))'"
        )
        sys.exit(1)
    if len(MCP_HTTP_TOKEN) < 24:
        logger.error("MCP_HTTP_TOKEN muito curto (<24 chars) — use um token forte.")
        sys.exit(1)
    if len(MCP_OAUTH_PASSWORD) < 8:
        logger.error("MCP_OAUTH_PASSWORD muito curta (<8 chars).")
        sys.exit(1)

    # Transporte HTTP stateless (auto-contido; robusto atras de LB / simples p/ connectors).
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    # DNS-rebinding off: /mcp e protegido por token, nao por Host (dominio publico).
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    # Liga o AS OAuth NATIVO do SDK no MESMO objeto mcp (reusa as 17 tools).
    # issuer_url = base publica; resource_server_url = base -> well-known do
    # protected-resource fica no path "raiz" (/.well-known/oauth-protected-resource).
    mcp.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_BASE_URL),
        resource_server_url=AnyHttpUrl(PUBLIC_BASE_URL),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=None,
    )
    mcp._auth_server_provider = _provider
    mcp._token_verifier = ProviderTokenVerifier(_provider)

    app = mcp.streamable_http_app()

    # Override do metadata do AS (inserido no INICIO pra ganhar da rota do SDK
    # no mesmo path — Starlette casa a primeira rota que match).
    app.router.routes.insert(
        0,
        Route("/.well-known/oauth-authorization-server", _as_metadata,
              methods=["GET", "OPTIONS"]),
    )

    # Rotas custom (paths novos — sem auth; o gate real e /mcp + a senha em /consent).
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/consent", _consent_get, methods=["GET"])
    app.add_route("/consent", _consent_post, methods=["POST"])
    return app


# uvicorn --factory aponta pra isso; rodar direto tambem funciona.
app = build_app()


if __name__ == "__main__":
    host = _env("HOST", "0.0.0.0")
    logger.info(
        "CoPiloto MCP HTTP subindo em %s:%s  (MCP: /mcp · issuer OAuth: %s)",
        host, _PORT, PUBLIC_BASE_URL,
    )
    uvicorn.run(app, host=host, port=_PORT, log_level="info")
