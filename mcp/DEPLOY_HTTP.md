# CoPiloto MCP — Servidor HTTP remoto (deploy + connector)

Habilita as ~17 tools do CoPiloto no **Claude mobile (app)** e em **connectors do
claude.ai**. O celular NAO roda processo stdio local → precisa de um endpoint MCP
servido por HTTP, publico e autenticado.

- **Entrypoint:** `mcp/http_server.py` (transporte `streamable-http` do FastMCP).
- **Reuso total:** importa o objeto `mcp` de `mcp/server.py` — as MESMAS tools do stdio,
  zero duplicacao de logica.
- **Auth:** `Authorization: Bearer <MCP_HTTP_TOKEN>` em TODA request. Sem token valido
  → **401**. Sem a env `MCP_HTTP_TOKEN` o server **recusa subir** (fail-closed).
- **Endpoint MCP:** `https://<dominio>/mcp` · **Healthcheck (sem auth):** `/healthz`.

> ⚠️ Este e um **servico Railway NOVO** — NAO mexer no `intel-api` existente.
> O deploy real e coordenado pela sessao dona do lock de deploy. Este doc e o runbook.

---

## 0. Gerar o token (forte, rotacionavel)

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Guarde o valor. Ele vai na env `MCP_HTTP_TOKEN` do Railway **e** no connector do
claude.ai (passo 3). **Nunca** commitar. Para **rotacionar**: gere um novo, atualize a
env no Railway (redeploy) e atualize o token no connector.

---

## 1. Criar o servico Railway (root = `mcp/`)

O repo ja tem `mcp/Procfile` (`web: python http_server.py`), `mcp/.python-version`
(`3.12` — senao Railpack usa 3.13 e pode quebrar dep) e `mcp/requirements.txt`.

**Via dashboard (recomendado):**
1. Railway → projeto existente (mesmo do `intel-api`/`tonia`/`worker`) → **+ New** →
   **GitHub Repo** → escolher o repo `prospect-system`.
2. No servico criado → **Settings**:
   - **Root Directory:** `mcp`  ← faz o build rodar dentro de `mcp/` (acha Procfile,
     .python-version e requirements.txt).
   - **Start Command:** deixar vazio (usa o Procfile) OU setar `python http_server.py`.
   - **Watch Paths** (opcional): `mcp/**` — so redeploy quando `mcp/` mudar.
3. **Networking → Generate Domain** → gera `https://<algo>.up.railway.app`.
   O endpoint MCP publico e `https://<algo>.up.railway.app/mcp`.

**Via CLI** (se preferir; NAO rodar sem coordenar o lock de deploy):
```bash
railway service create copiloto-mcp-http
# setar root dir = mcp nas settings do servico (dashboard), depois:
railway up   # <- so a sessao dona do lock de deploy roda isto
```

Railway injeta `$PORT` automaticamente; `http_server.py` ja escuta `0.0.0.0:$PORT`.

---

## 2. Variaveis de ambiente (Railway → Variables)

| Var | Obrigatoria | Valor | Uso |
|-----|-------------|-------|-----|
| `MCP_HTTP_TOKEN` | **sim** | token do passo 0 (>=24 chars) | auth bearer; sem ela o server nao sobe |
| `DATABASE_URL` | **sim** | Neon prod (mesma do INTEL) | reads `copilot.*` + writes auditadas |
| `VOYAGE_API_KEY` | nao* | chave Voyage | `search_memories` semantica (senao cai p/ keyword) |
| `CONSELHOOS_DATABASE_URL` | nao | Neon do ConselhoOS | `get_conselho` (read-only; senao retorna vazio) |
| `RENATO_CONTACT_ID` | nao | `4734` | single-tenant (default 25613 no codigo) |

Copie `DATABASE_URL`, `VOYAGE_API_KEY`, `CONSELHOOS_DATABASE_URL` do `.env` do projeto
(ou das envs do `intel-api`). `RENATO_CONTACT_ID=4734`.

> Atencao whitespace: o codigo ja da `.strip()` nas envs (Railway/Vercel as vezes colam `\n`).

---

## 3. Registrar o connector no claude.ai (OAuth 2.1 — o celular herda da conta)

A UI de custom connector do claude.ai **so oferece OAuth** (nao ha campo de bearer
estatico). Desde o commit de OAuth, o server e um **Authorization Server OAuth 2.1**
single-user (suporte NATIVO do SDK `mcp`): DCR + PKCE + pagina de consentimento com
senha. O app mobile herda os connectors da conta claude.ai — registra-se uma vez na web:

1. Abrir **claude.ai** (web) logado na conta do Renato.
2. **Settings** (menu do perfil) → **Connectors**.
3. **Add custom connector** (ou "Browse connectors" → "Add custom connector").
4. Preencher **APENAS**:
   - **Name:** `CoPiloto Renato`
   - **Remote MCP server URL:** `https://<dominio-railway>/mcp`  ← inclui o `/mcp`.
   - **SEM token** — a UI faz OAuth sozinha.
5. **Add / Connect**. O claude.ai descobre o AS (well-known), **se registra sozinho**
   (Dynamic Client Registration) e **abre uma pagina no navegador** pedindo a senha.
6. Na pagina **"Autorizar CoPiloto"**, digite a senha (`MCP_OAUTH_PASSWORD`) → **Autorizar**.
   O browser volta pro claude.ai, que troca o code por token e conclui o handshake —
   aparecem as 17 tools.
7. No **app mobile**: abrir uma conversa → menu de connectors → habilitar **CoPiloto Renato**.

> **Bearer estatico ainda vale** pra Claude Desktop/Code/curl: `MCP_HTTP_TOKEN` continua
> aceito como `Authorization: Bearer <token>` direto no `/mcp` (o verificador aceita AMBOS:
> token OAuth emitido OU o estatico).
>
> **Estado OAuth e em memoria** — apos um redeploy, clients/tokens zeram; o claude.ai
> re-faz o DCR e o Renato re-autoriza com a senha uma vez. Esperado, nao-bloqueante.

### Envs OAuth (Railway → Variables)
| Var | Obrigatoria | Uso |
|-----|-------------|-----|
| `MCP_OAUTH_PASSWORD` | recomendada | senha da pagina de consentimento (fallback = `MCP_HTTP_TOKEN`) |
| `MCP_PUBLIC_URL` | recomendada | base publica = issuer OAuth (ex `https://<dominio>`); fallback deriva de `RAILWAY_PUBLIC_DOMAIN` |

---

## 4. Smoke test pos-deploy (do seu terminal)

```bash
BASE=https://<dominio-railway>
TOKEN=<MCP_HTTP_TOKEN>

# healthz sem auth -> 200
curl -s -o /dev/null -w "healthz %{http_code}\n" $BASE/healthz

# /mcp SEM token -> 401
curl -s -o /dev/null -w "noauth %{http_code}\n" -X POST $BASE/mcp \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# initialize COM token -> 200 + serverInfo
curl -s -X POST $BASE/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Esperado: `healthz 200`, `noauth 401`, initialize retorna
`"serverInfo":{"name":"copiloto-renato",...}`.

---

## Notas de arquitetura

- **stateless_http=True + json_response=True** (`http_server.py`): cada request e
  auto-contida (sem estado de sessao no server) → robusto atras de LB/multi-worker e
  simples p/ connectors remotos. Nao precisa de `mcp-session-id`.
- **Auth em tempo constante** (`hmac.compare_digest`) p/ nao vazar o token por timing.
- **`/healthz`** e o unico path sem auth (nao vaza dado; so `{"status":"ok"}`).
- **Sem tools de envio** (WhatsApp/email) — igual ao stdio; so le/escreve no dado, tudo
  auditado em `mcp_audit_log`.
- **Escala:** 1 worker uvicorn e suficiente (single-tenant). Se precisar de mais,
  stateless_http ja permite multi-worker sem sticky sessions.
