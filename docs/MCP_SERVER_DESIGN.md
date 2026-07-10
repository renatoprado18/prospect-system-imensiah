# CoPiloto MCP Server — Design (09/07/2026)

## O que é

A **superfície de trabalho profundo** do CoPiloto (F4 do plano de consolidação). Expõe as camadas de **dados** (grafo core + knowledge, via views `copilot`) e a **memória** para uma superfície rica — Claude.ai, Claude Desktop, Claude Code, Cursor — para que o Renato trabalhe projetos/tarefas/objetivos com renderização, pesquisa e documentos, sem a pobreza do chat.

**Princípio-chave:** o MCP server NÃO tem inteligência própria. Ele é um servidor de ferramentas sobre o contrato `copilot` (leitura) + um catálogo de atuadores controlado (escrita). Quem faz o trabalho profundo (pesquisa, draft, análise) é o modelo do Claude.ai/Code (Opus 4.8+). O MCP dá a ele acesso ao grafo + memória do Renato e a capacidade de gravar de volta.

## Decisão de transporte (a bifurcação central)

MCP tem dois transportes:

| | **stdio (local)** | **Streamable HTTP (remoto)** |
|---|---|---|
| Superfície | Claude Desktop, Claude Code, Cursor | Claude.ai web + mobile (Connectors/Integrations) |
| Auth | env local (connection string + keys) na máquina do Renato — sem OAuth | OAuth 2.1 obrigatório (spec MCP) |
| Hosting | roda na máquina do Renato | precisa hospedar (Railway) |
| Esforço | dias | +1 semana (OAuth + deploy) |

**MVP = stdio local.** Renato adiciona o server ao config do Claude Desktop/Code; ele roda local, conecta no Neon (views `copilot`) + delegador. Zero infra de OAuth. Funciona JÁ no Claude Code (esta ferramenta) e no Claude Desktop. O exemplo do café roda ponta a ponta nele.

**Fase 2 = HTTP remoto**, junto da consolidação Railway — desbloqueia Claude.ai web + mobile (trabalhar do celular com interface rica).

## Catálogo de tools (por entidade do grafo core)

Leitura via views `copilot.*` (contrato estável). Escrita via camada controlada (validação + audit, espelhando o padrão `scheduled_actions`). **Sem tool de ENVIO no MVP** — drafts são artefatos que o Renato revisa e dispara (regra "drafts pra revisão, EU disparo").

**Projetos**
- `search_projects(query?, status?)` → lista
- `get_project(id)` → projeto + tasks + notas + documentos
- `create_project(nome, descricao, ...)`
- `update_project(id, campos)`

**Tarefas**
- `search_tasks(project_id?, status?, due_before?, contact_id?)`
- `get_task(id)`
- `create_task(titulo, project_id?, due_date?, ...)`
- `update_task(id, {status, titulo, due_date, project_id, ...})` ← o "corrige a classificação" do exemplo Juliana

**Documentos / Notas** (onde a pesquisa é arquivada)
- `get_project_documents(project_id)`
- `create_document(project_id, titulo, conteudo, tipo)` ← "arquiva a pesquisa de café na Exportação"
- `get_document(id)`
- `create_note(project_id, texto)`

**Contatos**
- `search_contacts(query)`
- `get_contact(id)` → contato + histórico + tags/cargo
- `update_contact(id, campos)`

**Memória** (o que já construímos)
- `search_memories(query, k)` → busca semântica (copilot.memories + system_memories)
- `save_memory(kind, key, value)` → escreve tonia_memories

**Percepção**
- `get_cockpit()` → signals abertos + tasks vencidas + RACI Vallen vencidos + agenda 24h
- `get_open_signals(min_urgencia?)`

**ConselhoOS (read-only)**
- `get_conselho(empresa?)` → reuniões + RACI + decisões (Vallen visível)

**Delegação (opcional MVP)** — no Claude.ai, o próprio Claude É o L3, então delegar é menos necessário. Expor `delegate_dev` só se quiser o executor headless para tarefas fora da sessão.

## Disciplina de leitura/escrita

- **Leitura:** SÓ via views `copilot.*`. Refatorar o INTEL não quebra o MCP.
- **Escrita:** catálogo fechado de atuadores com validação de schema + audit trail (mesma disciplina do `scheduled_actions.py`). Nunca SQL cru.
- **Sem outbound:** o MCP grava no DADO (cria doc, atualiza task) mas NÃO envia (WA/email). Drafts saem como artefatos/documentos pro Renato disparar. Isso respeita a política de voz/revisão e mantém o MCP seguro (blast radius = dados do próprio Renato, não terceiros).
- **Single-tenant:** sem multi-user. O server assume Renato.

## Auth

- **stdio local:** connection string do Neon + INTEL_API_KEY nas env vars do processo do server (na máquina do Renato). Sem auth por request — é local.
- **HTTP remoto (Fase 2):** OAuth 2.1 (a spec MCP exige para remoto). Single-user → um fluxo OAuth simples ou token. Hospedar no Railway com a consolidação.

## O exemplo do café, ponta a ponta (MVP stdio)

No Claude Desktop/Code, Renato digita: *"pesquisa preços de cafés especiais e arquiva na Exportação"*.
1. Claude (Opus 4.8) faz a **pesquisa** (WebSearch nativo — é o L3);
2. Renderiza um **artefato**: tabela de preços por origem/qualidade + fontes;
3. Chama `create_document(project_id=28, titulo="Pesquisa preços café especial", conteudo=<relatório>, tipo="research")`;
4. Confirma arquivado. Depois, `get_project(28)` mostra o doc; a **Tônia** também o vê no projeto (percepção que ligamos).

O fluxo Juliana: `get_cockpit()` → `search_tasks(overdue)` → mostra #999520 → Renato corrige → `update_task(999520, {...})` + drafta o WA num artefato → `create_note(project, draft)`. Sem timeout, editável.

## Tech / onde vive

- **SDK MCP Python** (`mcp` package). Lê views `copilot` + escreve via helpers controlados.
- Vive num módulo focado (novo repo `copilot-mcp` OU `prospect-system/mcp/`). Não importa o monólito INTEL — consome o contrato `copilot` + poucos helpers de escrita.
- stdio primeiro (roda local); Railway HTTP na Fase 2.

## Faseamento + esforço

| Fase | O quê | Esforço |
|---|---|---|
| **1 — stdio MVP** | Server stdio + tools de leitura (projetos/tasks/docs/contatos/memória/percepção/conselho) + escrita segura (create/update task, create doc/note, save_memory). Config Claude Desktop/Code. | ~3-5 dias |
| **2 — HTTP remoto** | OAuth 2.1 + deploy Railway → Claude.ai web + mobile | +1 semana (com consolidação Railway) |
| **3 — richer** | anexos de arquivo, delegate_dev, drafts outbound com gate | depois |

## Relacionados
- `docs/COPILOT_CONTRACT.md` — as views que o MCP lê
- `project_arquitetura_consolidacao_09_07.md` — F4 no plano
- `project_dev_backlog.md` — roadmap
