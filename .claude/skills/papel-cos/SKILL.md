---
name: papel-cos
description: Triagem on-demand das pendencias CoS (action_proposals + tasks + system_feedback). Classifica em 3 buckets (auto BG / propor pra voce / silenciar), executa BG cap 3, devolve cockpit estruturado. Manual, sem cron. Skill v1 — escopo limitado, sem auto-dismiss stale, recomendacoes hibridas (template + LLM).
---

# Skill: /papel-cos

Triagem estruturada das pendencias CoS sob demanda. Gera um sweep_id, classifica cada item em 3 buckets, executa bucket=auto em BG (cap 3 paralelos, timebox 60s) e devolve cockpit pro Renato decidir o bucket=propose.

NAO substitui Tonha (Patrol). Coexiste:
- Tonha = autonoma 24/7, output via WA (hoje OFF no experimento 27/06-04/07)
- papel-cos = on-demand terminal, inbox estruturado pra triagem rapida

## Quando usar

- "/papel-cos" no inicio de sessao apos /abre, pra triagem do dia
- Quando sentir backlog de propostas/tasks crescendo
- Substituto manual da Tonha durante experimento OFF

## Quando NAO usar

- Em loop (custo Sonnet escala)
- Pra delegacao especifica (use Agent direto)
- Pra status simples (use /abre)

## Etapas (executar em ordem)

### 1. Gerar sweep_id

```bash
SWEEP_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
echo "Sweep $SWEEP_ID"
```

Guardar pra usar no INSERT de cos_actions_log.

### 2. Rodar 3 queries em paralelo (Bash)

Usar `psql` direto no Neon (action_proposals/tasks/system_feedback ficam stale no local — ver `feedback_neon_source_of_truth_proposals.md`).

```bash
DB="$DATABASE_URL_UNPOOLED"  # de .env
```

**Q1 — action_proposals pending:**
```sql
SELECT id, action_type, contact_id, title, urgency, confidence, action_params,
       (criado_em AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::timestamp AS criado_brt,
       EXTRACT(EPOCH FROM NOW() - criado_em)/3600 AS idade_horas
FROM action_proposals
WHERE status='pending'
ORDER BY urgency='high' DESC, criado_em DESC
LIMIT 50;
```

**Q2 — tasks vencendo/vencidas (cap 10 + total pra "mais N"):**

Primeiro o cap pra cockpit:
```sql
SELECT id, titulo, descricao, prioridade, contexto, project_id, ai_generated,
       conselhoos_raci_id IS NOT NULL AS is_raci,
       (data_vencimento AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::timestamp AS due_brt,
       EXTRACT(EPOCH FROM NOW() - data_vencimento)/86400 AS dias_atraso
FROM tasks
WHERE status='pending'
  AND (data_vencimento < NOW() + INTERVAL '3 days'
       OR (data_vencimento IS NULL AND data_criacao < NOW() - INTERVAL '7 days'))
ORDER BY data_vencimento NULLS LAST, prioridade
LIMIT 10;
```

Depois total pra footer "+ N tasks":
```sql
SELECT COUNT(*) FROM tasks
WHERE status='pending'
  AND (data_vencimento < NOW() + INTERVAL '3 days'
       OR (data_vencimento IS NULL AND data_criacao < NOW() - INTERVAL '7 days'));
```

Se total > 10, mostrar "+ N tasks omitidas — `/papel-cos tasks-all` pra ver todas" (comando v2).

**Q3 — system_feedback pending:**
```sql
SELECT id, tipo, conteudo,
       (criado_em AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::timestamp AS criado_brt,
       EXTRACT(EPOCH FROM NOW() - criado_em)/86400 AS idade_dias
FROM system_feedback
WHERE status='pending'
ORDER BY criado_em DESC;
```

### 3. Classificar via heuristica hibrida

**Catalogo de templates** (action_type → bucket). Cobertura ~95% dos casos:

| action_type | Bucket | Reason |
|---|---|---|
| `update_contact_phone` | auto | template: update DB reversivel |
| `update_contact_email` | auto | template: update DB reversivel |
| `pending_response` | propose | gera draft msg sua |
| `follow_up` / `follow_up_alert` | propose | gera draft cobranca |
| `linkedin_job_change` | propose | parabens sua assinatura |
| `create_meeting` / `meeting_request` | propose | bloqueia agenda |
| `cancel_event` / `reschedule_event` | propose | mexe agenda |
| `financial_alert` / `urgent_alert` / `complaint_alert` / `operational_risk` | propose | decisao importante |
| `opportunity_alert` / `news_alert` | propose | decisao de acao |
| `introduction_request` | propose | voce medeia |
| `review_decision` / `weekly_editorial` / `system_improvement` / `operational_task` | propose | decisao |
| (default desconhecido) | propose | safe default |

**Tasks** (sem `assignee` no schema — usar contexto + tags + titulo):

| Padrao | Bucket | Reason |
|---|---|---|
| `conselhoos_raci_id IS NOT NULL` | propose | RACI cliente-conselho, voce decide |
| `titulo ~ 'Verificar\|Conferir\|Buscar\|Pesquisar'` | auto | research read-only |
| `titulo ~ 'Enviar\|Responder\|Escrever\|Draftar'` | propose | comunicacao externa |
| `contexto='personal'` | propose | personal sempre voce |
| `ai_generated=true AND prioridade > 5` | propose (LLM judge) | rever se ainda valido |
| `project_id IS NULL AND contexto IS NULL` | propose | orfa, perguntar destino |
| `data_vencimento` em sab/dom | propose | ajustar dia util |
| (default) | propose | safe default |

**system_feedback** — V1 sempre `propose`. Tipos `bug`/`melhoria` exigem voce priorizar.

**LLM judge** (so pra casos ambiguos — cap 5 chamadas/sweep): Quando heuristica diz `auto` mas confidence < 0.6, pergunta LLM "essa acao tem efeito externo ou comunica com terceiro?" — yes → upgrade pra propose.

### 4. INSERT em cos_actions_log

Pra cada item classificado, INSERT (use `$DB`):

```sql
INSERT INTO cos_actions_log
  (sweep_id, source_table, source_id, source_summary, bucket, bucket_reason,
   action_type, action_params, rollback_hint, status)
VALUES
  ('{SWEEP_ID}', 'action_proposals', {id}, '{title} ({action_type})',
   '{bucket}', 'template:{action_type}',
   '{action_type}', '{params_json}'::jsonb,
   '{rollback_sql}', 'pending');
```

`rollback_hint` exemplo: `UPDATE action_proposals SET status='pending' WHERE id={id}` (se a acao auto resolveu/dismissed).

### 5. Fan-out BG (so bucket=auto, max 3 paralelos)

Pra cada row auto, spawn Agent em paralelo (single message com multiplos Agent calls):

- `Explore` pra tasks de research (titulos "Verificar...")
- `general-purpose` pra acoes c/ side-effect (UPDATE contacts, etc)

Cada agent recebe instrucao com:
- O que fazer (1 frase)
- ID + sweep_id pra UPDATE row apos
- Como gravar resultado: `UPDATE cos_actions_log SET status='done', result=..., finished_em=NOW() WHERE id={row_id}`

**Timebox 60s.** Apos isso, query `cos_actions_log WHERE sweep_id=$SWEEP_ID AND status='running'` — o que sobrou vira `Watching`.

### 6. Output cockpit

Formato fixo (markdown, ~30 linhas):

```
🎛️ Papel CoS — sweep {timestamp BRT} — id: {sweep_id:8}

✅ BG (X done, Y running):
  - [action_proposals#123] update phone Marson → updated to +55 11 9... ✓
  - 🔄 [tasks#456] Verificar status JD Gestora (ETA ~30s)

🤔 Decidir (N):
  1. **[action_proposals#789]** pending_response Reginaldo Jabô (28h sem resposta)
     → [a] drafto cobranca leve  [b] espero  [c] cobra Juliana
     → recomendo: [b] — ele responde 2-3d normalmente
  2. ...

🔕 Silenciadas (N): action_proposals=0, tasks=0, system_feedback=0
  (V1 ainda nao silencia; tudo desconhecido vai pra propor)

⏳ Watching (Y): rever c/ /papel-cos status {sweep_id:8}
```

### 7. Aguardar direcao do user

NAO executar acoes do bucket=propose sem confirmacao. Apresenta opcoes, espera resposta.

## Recomendacoes (templates fixos vs LLM)

**Template fixo** pra tipos recorrentes:
- `update_contact_phone/email` → recomendo: [a] aplicar agora (BG ja preparou)
- `linkedin_job_change` → recomendo: [a] drafto parabens curto
- `meeting_request` → recomendo: checar calendar antes (memo `checar_agenda_antes_propor`)

**LLM gera recomendacao** pros demais (~Sonnet, 1 call por item, cap 5):
- Contexto: title + description + ai_reasoning + idade
- Output: 1 linha "recomendo: [letra] porque {motivo curto}"

## Custos esperados (v1)

- 3 queries Neon: ~200ms total
- Classificacao heuristica: free (regras Bash/Python)
- LLM judge (ambiguos): ~5 Sonnet calls × ~$0.002 = $0.01/sweep
- Recomendacoes LLM (propose itens): ~10 calls × $0.003 = $0.03/sweep
- Agents BG (auto): variavel, ~$0.05 cada × 3 = $0.15/sweep cap
- **Total cap: ~$0.20/sweep.** Se invocar 3x/dia = ~$0.60/dia.

## Limitacoes V1 conhecidas

- **Sem auto-dismiss stale.** Itens >14d ficam aparecendo (waiting volume real pra calibrar).
- **Sem comando `/papel-cos revert`.** Rollback = copy/paste do `rollback_hint`.
- **Catalogo de templates pode ficar desalinhado** se novos action_type aparecerem — sweep avisa "tipo desconhecido X".
- **Bucket auto vai ficar quase vazio em v1** (so update_contact_phone/email no catalogo). Valor v1 = triagem estruturada, nao delegacao.

## V2 (futuro, depois de 3-5 sweeps de uso real)

- `/papel-cos revert {action_id}` ou `/papel-cos revert sweep:{sweep_id}`
- `/papel-cos status {sweep_id}` pra checar BG ainda rodando
- Auto-dismiss stale com cutoff calibrado
- Trigger por evento (sweep silencioso ao abrir Claude Code) se valor provar
- Expandir escopo: WA incoming sem resposta, email professional, calendar conflict

## Memos relevantes (consultar se precisar contexto)

- `feedback_neon_source_of_truth_proposals.md` — sempre query Neon, nao local
- `feedback_query_tz_brt.md` — converter UTC→BRT em SQL pra exibir
- `feedback_verificar_dia_semana.md` — checar `date` antes de afirmar dia
- `feedback_checar_agenda_antes_propor.md` — calendar MCP pra eventos <7d
- `feedback_markdown_blockquote_copy.md` — drafts via revisao + Evolution
- `feedback_lingua_portugues_correto.md` — drafts externos: "para", "está", "Abraço"
- `feedback_voice_cortesia.md` — "grato"/"agradeço", nunca "obrigado"
- `feedback_nao_perguntar_age.md` — paralelizar 3+ frentes claras
- `project_tonha_experiment_27_06.md` — Tonha OFF ate 04/07
