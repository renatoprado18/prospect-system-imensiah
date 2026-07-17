"""
http_server.py — CoPiloto MCP server (transporte HTTP / streamable-http).

Variante REMOTA do `server.py` (stdio). Serve EXATAMENTE as mesmas ~16 tools
(reusa o objeto `mcp` de server.py — zero duplicacao de logica), so que via
HTTP publico. Habilita o CoPiloto no Claude mobile (app) e em connectors do
claude.ai — que herdam da conta.

Como o endpoint e EXPOSTO PUBLICAMENTE e retorna dados pessoais do Renato,
autenticacao por bearer token e OBRIGATORIA: toda request precisa de
`Authorization: Bearer <MCP_HTTP_TOKEN>`. Sem token valido -> 401. Sem a env
`MCP_HTTP_TOKEN` setada o server RECUSA subir (fail-closed).

Rodar local:
    export MCP_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
    export DATABASE_URL=...            # Neon prod (ou local em dev)
    export VOYAGE_API_KEY=...          # opcional (search_memories semantica)
    export CONSELHOOS_DATABASE_URL=... # opcional (get_conselho)
    export RENATO_CONTACT_ID=4734
    python mcp/http_server.py          # escuta 0.0.0.0:$PORT (default 8000)

Endpoint MCP:  http(s)://<host>:<port>/mcp

Deploy: ver mcp/DEPLOY_HTTP.md (servico Railway novo, NAO o intel-api).
"""

import hmac
import logging
import os
import sys

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Reuso total: importa o MESMO objeto FastMCP (com as ~16 tools ja registradas)
# do server stdio. Nada de re-registrar tools aqui — single source of truth.
from server import mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("copilot_mcp.http")


def _env(name: str, default: str = "") -> str:
    """Le env var com strip (Vercel/Railway as vezes colam \\n / espacos)."""
    return (os.getenv(name) or default).strip()


# ---------------------------------------------------------------------------
# Auth — bearer token estatico, fail-closed
# ---------------------------------------------------------------------------
MCP_HTTP_TOKEN = _env("MCP_HTTP_TOKEN")

# Caminhos liberados sem auth. Mantemos MINIMO. O endpoint MCP (/mcp) exige token.
# `/healthz` responde 200 sem auth pra healthcheck do Railway/uptime nao vazar dado.
_PUBLIC_PATHS = {"/healthz"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige `Authorization: Bearer <MCP_HTTP_TOKEN>` em toda request (exceto
    _PUBLIC_PATHS). Comparacao em tempo constante (hmac.compare_digest) pra nao
    vazar o token por timing. Sem header valido -> 401 + WWW-Authenticate."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

        if not token or not hmac.compare_digest(token, MCP_HTTP_TOKEN):
            return JSONResponse(
                {"error": "unauthorized", "detail": "valid bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="copiloto-mcp"'},
            )
        return await call_next(request)


async def _healthz(_request: Request):
    return JSONResponse({"status": "ok", "service": "copiloto-mcp-http"})


def build_app():
    """Monta o ASGI app: streamable-http do FastMCP (rota /mcp) + healthz +
    middleware de auth por cima de tudo."""
    if not MCP_HTTP_TOKEN:
        logger.error(
            "MCP_HTTP_TOKEN nao setada — recusando subir (fail-closed). "
            "Gere um token forte: python -c 'import secrets;print(secrets.token_urlsafe(48))'"
        )
        sys.exit(1)
    if len(MCP_HTTP_TOKEN) < 24:
        logger.error("MCP_HTTP_TOKEN muito curto (<24 chars) — use um token forte.")
        sys.exit(1)

    # Config de transporte HTTP no MESMO objeto mcp (setado antes de construir o app).
    # stateless_http=True: cada request e auto-contida (sem estado de sessao no server)
    # -> mais robusto atras de LB/multi-worker e mais simples pra connectors remotos.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True

    app = mcp.streamable_http_app()  # Starlette app com rota POST/GET /mcp
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware)
    return app


# uvicorn --factory aponta pra isso; rodar direto tambem funciona.
app = build_app()


if __name__ == "__main__":
    host = _env("HOST", "0.0.0.0")
    port = int(_env("PORT", "8000") or "8000")
    logger.info("CoPiloto MCP HTTP subindo em %s:%s  (endpoint MCP: /mcp)", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
