# Contrato de Leitura `copilot.*` — Tonia / CoPiloto

**Migration:** `scripts/migrations/043_copilot_views.sql` · **Criado:** 09/07/2026

Schema `copilot` no Neon compartilhado INTEL ↔ Tonia. Contém **somente views** com colunas explícitas sobre as tabelas físicas do INTEL.

## Regras do contrato

1. **Consumidores externos (Tonia) leem SÓ `copilot.*`** — nunca tabelas físicas (`public.*`). Qualquer query da Tonia contra `public.*` é bug.
2. **Colunas nunca são removidas nem renomeadas — só adicionadas.** Adição de coluna no fim da view é backward-compatible; remoção/rename é breaking change e exige coordenação com o repo da Tonia.
3. **Mudança no schema físico exige atualizar a view no MESMO commit.** Se um ALTER em `public.*` quebrar uma view `copilot.*`, o commit não está completo.

Convenção de timestamps: todos `TIMESTAMP` naive em **UTC**, **exceto** `copilot.calendar_events.start_datetime/end_datetime` que são **BRT naive** (exceção histórica).

---

## copilot.signals

Origem: `public.signals` (migration 026). Sinais estruturados emitidos por detectores deterministas.

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | bigint | PK do sinal |
| `tipo` | text | Tipo lógico do sinal (ex: `raci_vencido`, `post_sem_imagem`) |
| `urgencia` | integer | 1–10 (10 = ação imediata) |
| `contexto` | jsonb | Payload completo pra decidir sem lookup adicional |
| `detector` | text | Detector que emitiu (ex: `detector_conselhos`) |
| `status` | text | `open` / `resolved` / `expired` / `dismissed` |
| `criado_em` | timestamp | Criação (UTC naive) |
| `atualizado_em` | timestamp | Última atualização (UTC naive, trigger) |

## copilot.tasks

Origem: `public.tasks`.

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK da tarefa |
| `titulo` | text | Título |
| `descricao` | text | Descrição livre |
| `status` | text | `pending` / `completed` / `cancelled` / etc |
| `prioridade` | integer | Default 5 (menor = mais prioritário) |
| `due_date` | timestamp | Vencimento (físico: `data_vencimento`) |
| `completed_at` | timestamp | Conclusão (físico: `data_conclusao`) |
| `contact_id` | integer | FK → `copilot.contacts.id` (nullable) |
| `project_id` | integer | FK → projects (nullable; projects não exposto ainda) |
| `source_table` | text | Tabela de origem quando gerada por automação |
| `source_id` | integer | ID na tabela de origem |
| `criado_em` | timestamp | Criação (físico: `data_criacao`) |
| `atualizado_em` | timestamp | Última atualização |

## copilot.contacts

Origem: `public.contacts`. Só o essencial — scoring interno, sync Google, enriquecimento LinkedIn bruto, endereços e dossiês **não** são expostos.

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK do contato |
| `nome` | text | Nome completo |
| `apelido` | text | Apelido/como chamar |
| `empresa` | text | Empresa atual |
| `cargo` | text | Cargo atual |
| `emails` | jsonb | Array de emails (`["a@b.com", ...]`) |
| `telefones` | jsonb | Array de telefones |
| `circulo` | integer | Círculo de relacionamento 1–5 (1 = mais íntimo) |
| `tags` | jsonb | Array de tags livres |
| `health_score` | integer | Saúde do relacionamento 0–100 (recalc 2x/dia, pode ficar stale) |
| `ultimo_contato` | timestamp | Última interação registrada (UTC naive) |
| `resumo_ai` | text | Resumo AI do contato |
| `criado_em` | timestamp | Criação |
| `atualizado_em` | timestamp | Última atualização |

## copilot.messages

Origem: `public.messages` LEFT JOIN `public.conversations` (canal vive na conversa).

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK da mensagem |
| `conversation_id` | integer | FK da thread (nullable) |
| `contact_id` | integer | FK → `copilot.contacts.id` |
| `canal` | text | `whatsapp` / `email` / etc (da conversa; NULL se sem thread) |
| `direction` | text | `outgoing` = Renato enviou · `incoming` = contato enviou (físico: `direcao`) |
| `content` | text | Corpo em texto (físico: `conteudo`) |
| `timestamp` | timestamp | `COALESCE(enviado_em, recebido_em, criado_em)` — UTC naive |

## copilot.emails

Origem: `public.email_triage` LEFT JOIN `public.messages` + `public.conversations`. Superfície **email-cêntrica** (`copilot.messages` mistura email+WA e não traz assunto/remetente/triagem). Base = `email_triage` (cada linha = 1 email triado). Corpo capturado com fallback HTML→texto (migration 048 + `email_triage.extract_email_body`).

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK da triagem (`email_triage.id`) |
| `message_id` | integer | FK → `copilot.messages.id` / `public.messages` |
| `contact_id` | integer | FK → `copilot.contacts.id` (nullable) |
| `account_email` | text | Conta Google que recebeu (ex.: `renato@almeida-prado.com`) |
| `from_email` | text | Remetente (de `messages.metadata.from`) |
| `from_name` | text | Nome do remetente (de `messages.metadata.from_name`) |
| `subject` | text | Assunto (de `messages.metadata.subject`, fallback `conversations.assunto`) |
| `content` | text | Corpo em texto — text/plain, ou HTML→texto se HTML-only (físico: `conteudo`) |
| `has_body` | boolean | `false` = corpo não capturado (raro pós-048; email HTML-only antigo) |
| `direction` | text | `incoming` (email recebido). Físico: `direcao` |
| `priority` | integer | 1-10 (círculo 1 = 10, 2 = 8, etc) |
| `classification` | text | `urgent` / `must_read` / `important` / `archive_proposed` / etc |
| `needs_attention` | boolean | Marcado pela triagem |
| `status` | text | `pending` / `approved` / `dismissed` / `archive_proposed_shadow` / etc |
| `account_type` | text | `professional` / `personal` |
| `timestamp` | timestamp | `COALESCE(enviado_em, recebido_em, criado_em)` — UTC naive |
| `criado_em` | timestamp | Quando a triagem foi criada (UTC naive) |

## copilot.calendar_events

Origem: `public.calendar_events`. **Atenção:** horários em **BRT naive** (exceção à convenção UTC).

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK do evento |
| `titulo` | text | Título (físico: `summary`) |
| `start_datetime` | timestamp | Início — **BRT naive** |
| `end_datetime` | timestamp | Fim — **BRT naive** |
| `all_day` | boolean | Evento de dia inteiro |
| `attendees` | jsonb | Array de participantes (objetos do Google Calendar) |
| `location` | text | Local |
| `status` | text | `confirmed` / `tentative` / `cancelled` |
| `contact_id` | integer | FK → `copilot.contacts.id` (nullable) |
| `criado_em` | timestamp | Criação (UTC naive) |
| `atualizado_em` | timestamp | Última atualização (UTC naive) |

## copilot.memories

Origem: `public.system_memories` (migrations 003/006). Memórias do INTEL não atreladas a contato.

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK da memória |
| `name` | text | Título (físico: `titulo`) |
| `content` | text | Conteúdo completo (físico: `conteudo`) |
| `type` | text | `decisao` / `compromisso` / `padrao` / `reflexao` / `sintese_diaria` / etc (físico: `tipo`) |
| `tags` | jsonb | Array de tags livres |
| `source` | text | `chat` / `whatsapp` / `sintese` / `manual` (físico: `fonte`) |
| `embedding` | vector(1024) | Embedding voyage-4-lite, cosine (nullable) |
| `created_at` | timestamp | Criação (físico: `criado_em`) |
| `updated_at` | timestamp | Última atualização (físico: `atualizado_em`) |

Nota: não existe coluna física `description`/`metadata` — o mais próximo é `content` (texto completo) e `tags`.

## copilot.action_proposals

Origem: `public.action_proposals`. Propostas do INTEL proativo (auto-dedup contato+tipo em 24h, auto-resolve on reply).

| Coluna | Tipo | Semântica |
|---|---|---|
| `id` | integer | PK da proposta |
| `tipo` | text | Tipo da ação (físico: `action_type`) |
| `contact_id` | integer | FK → `copilot.contacts.id` (nullable) |
| `status` | text | `pending` / `accepted` / `rejected` / `executed` / `expired` |
| `confidence` | double precision | Confiança do modelo 0–1 |
| `urgency` | text | `low` / `medium` / `high` |
| `title` | text | Título pra exibição |
| `description` | text | Descrição pra exibição |
| `payload` | jsonb | Parâmetros da ação (físico: `action_params`) |
| `criado_em` | timestamp | Criação (UTC naive) |

---

## Aliases (nome físico → nome do contrato)

| View | Físico | Contrato |
|---|---|---|
| tasks | `data_vencimento` | `due_date` |
| tasks | `data_conclusao` | `completed_at` |
| tasks | `data_criacao` | `criado_em` |
| messages | `direcao` | `direction` |
| messages | `conteudo` | `content` |
| messages | `enviado_em/recebido_em/criado_em` (coalesce) | `timestamp` |
| messages | `conversations.canal` (join) | `canal` |
| calendar_events | `summary` | `titulo` |
| memories | `titulo` | `name` |
| memories | `conteudo` | `content` |
| memories | `tipo` | `type` |
| memories | `fonte` | `source` |
| memories | `criado_em` / `atualizado_em` | `created_at` / `updated_at` |
| action_proposals | `action_type` | `tipo` |
| action_proposals | `action_params` | `payload` |
