# CoPiloto MCP Server (stdio) — MVP

Superfície de trabalho profundo do Renato. Um servidor **MCP local (transporte stdio)**
que expõe o grafo dele (projetos, tasks, docs, contatos), memória semântica, percepção
(cockpit) e ConselhoOS (read-only) para o Claude Desktop / Claude Code / Cursor.

O server **não tem inteligência própria** — quem raciocina é o Claude do outro lado.
Ele só dá acesso a dados (leitura via views `copilot.*`) + escrita controlada e auditada.
**Sem tools de envio (WhatsApp/email) no MVP** — só grava no dado.

Design completo: `docs/MCP_SERVER_DESIGN.md` · Contrato de leitura: `docs/COPILOT_CONTRACT.md`

> **Variante HTTP remota (mobile / connectors claude.ai):** `http_server.py` serve as
> MESMAS tools via `streamable-http` com auth por bearer token. Deploy + registro do
> connector: **`DEPLOY_HTTP.md`**.

## Arquivos

```
mcp/
├── server.py                     # FastMCP stdio — registra as 16 tools
├── db.py                         # camada de dados (conexão própria psycopg2, sem acoplar com o INTEL)
├── requirements.txt              # mcp, psycopg2-binary, httpx
├── migrations/001_mcp_audit_log.sql  # trilha de auditoria de escritas
└── README.md
```

## Tools (16)

**Leitura:** `search_projects`, `get_project`, `search_tasks`, `get_task`,
`search_contacts`, `get_contact`, `get_project_documents`, `get_document`,
`search_memories`, `get_cockpit`, `get_conselho`.

**Escrita (controlada + auditada, sem envio):** `create_task`, `update_task`,
`create_document`, `create_note`, `save_memory`.

## Setup

### 1. Instalar deps

Recomendo um venv dedicado pra não misturar com o INTEL:

```bash
cd /Users/rap/prospect-system/mcp
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Guarde o caminho absoluto do python do venv:
`/Users/rap/prospect-system/mcp/.venv/bin/python`

### 2. Aplicar a migration (uma vez por banco)

```bash
psql "$DATABASE_URL" -f /Users/rap/prospect-system/mcp/migrations/001_mcp_audit_log.sql
```

### 3. Variáveis de ambiente

| Var | Obrigatória | Default | Uso |
|-----|-------------|---------|-----|
| `DATABASE_URL` | sim | `postgresql://rap@localhost/intel` | banco INTEL (reads `copilot.*` + writes) |
| `RENATO_CONTACT_ID` | não | `25613` | single-tenant (assume Renato) |
| `VOYAGE_API_KEY` | não* | — | embeddings pra `search_memories` semântica (senão cai pra keyword) |
| `CONSELHOOS_DATABASE_URL` | não | — | `get_conselho` (read-only). Sem ela, retorna `[]` |

\* Sem `VOYAGE_API_KEY`, o server tenta `~/.voyage_atlas_key` e, se nada, degrada
`search_memories` pra busca por palavra-chave.

> **Local vs. produção:** pra dev aponte `DATABASE_URL` pro Postgres local
> (`postgresql://rap@localhost/intel`). Pra usar contra o Neon de produção, troque
> a string. As escritas são sempre auditadas em `mcp_audit_log`.

### 4a. Claude Desktop

Editar `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "copiloto": {
      "command": "/Users/rap/prospect-system/mcp/.venv/bin/python",
      "args": ["/Users/rap/prospect-system/mcp/server.py"],
      "env": {
        "DATABASE_URL": "postgresql://rap@localhost/intel",
        "RENATO_CONTACT_ID": "25613",
        "VOYAGE_API_KEY": "pa-...cole aqui...",
        "CONSELHOOS_DATABASE_URL": "postgresql://...cole aqui..."
      }
    }
  }
}
```

Reiniciar o Claude Desktop. As 16 tools aparecem no ícone de ferramentas.

### 4b. Claude Code

```bash
claude mcp add copiloto \
  /Users/rap/prospect-system/mcp/.venv/bin/python \
  /Users/rap/prospect-system/mcp/server.py \
  --env DATABASE_URL=postgresql://rap@localhost/intel \
  --env RENATO_CONTACT_ID=25613 \
  --env VOYAGE_API_KEY=pa-... \
  --env CONSELHOOS_DATABASE_URL=postgresql://...
```

Conferir: `claude mcp list`.

## Gotcha importante — colisão de nome `mcp`

Esta pasta se chama `mcp/`, igual ao pacote do SDK. Isso **não** dá problema desde que
você lance o server por **caminho absoluto** (`python /Users/rap/prospect-system/mcp/server.py`)
ou com `cwd` = esta pasta. Nesse caso `sys.path[0]` é a pasta `mcp/` (que tem `server.py`/`db.py`,
mas nenhum subpacote `mcp`), então `import mcp` resolve pro SDK em site-packages e `import db`
resolve pro sibling. **Não** coloque o diretório-pai (`prospect-system/`) no `PYTHONPATH` nem
rode com ele como cwd — aí a pasta `mcp/` vira namespace package e sombreia o SDK. As configs
acima (caminho absoluto do server.py) já evitam isso.

## Exemplo do café (ponta a ponta)

No Claude Desktop/Code, o Renato digita algo como *"pesquisa preços de cafés especiais e
arquiva na Exportação"*. O Claude faz a pesquisa (WebSearch nativo) e chama:

```
create_document(project_id=28, titulo="Pesquisa preços café especial",
                conteudo="<relatório markdown>", tipo="research")
```

Cria a row em `documentos` + vincula ao projeto 28 (`documento_links` entidade `projeto`) +
grava em `mcp_audit_log`. Depois `get_project(28)` mostra o doc.

## O que falta pro Renato rodar

1. **Instalar o SDK** — `mcp` não está no ambiente global (o `import mcp` do repo estava
   sombreando esta pasta). Rodar o passo 1 (venv + `pip install -r requirements.txt`).
2. **Aplicar a migration** no banco alvo (passo 2) — já aplicada no local.
3. **Setar as env vars** e **adicionar ao Claude Desktop/Code** (passos 3 + 4).

## Notas de contrato

- **Reads** usam as views `copilot.*` (tasks, contacts, messages, calendar_events,
  memories, signals, action_proposals).
- **Exceções (sem view ainda):** `search_projects` / `get_project` leem `public.projects`
  direto, e `get_project_documents` / `get_document` leem `public.documentos` +
  `public.documento_links`. Quando existirem `copilot.projects` / `copilot.documents`,
  trocar por elas em `db.py` (backward-compatible).
- **Writes** vão nas tabelas físicas (`tasks`, `documentos`, `documento_links`,
  `project_notes`, `tonia_memories`) com whitelist de campos + audit. Nunca SQL cru do modelo.
