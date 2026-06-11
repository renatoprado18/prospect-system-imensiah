# Playbook — Reuniões de Conselho

Pipeline executável de preparação **pré-reunião** e processamento **pós-reunião** para conselhos onde Renato atua (Vallen, Alba, Despertar futuro). Reaproveita o trabalho consolidado nas sessões de 12/05 e 09/06 de 2026.

**Como usar:** abrir sessão Claude Code, dizer "vamos preparar o conselho da Vallen do dia X" — o assistente lê este playbook, a memória `project_vallen_conselho_workflow.md` e a pasta da reunião em `conselho/<empresa>/<data>/`. Os passos abaixo são checklist + comandos prontos.

---

## Estrutura de pastas

```
conselho/
  vallen/
    2026-06-09/           # reunião realizada
      README.md           # contexto: hora, participantes, links Drive
      dossie.md
      pauta.md
      devolutivas/
        marketing.md
        proposta_remuneracao_medicos.md
      ata.md
      raci_diff.json      # antes/depois das mudancas
      follow_ups.md       # acoes pos-reuniao + status
    2026-07-14/           # proxima
      README.md           # bootstrap minimo
```

Convenção: data no formato `YYYY-MM-DD` (dia da reunião).

---

## Pipeline PRÉ-reunião (5-7 dias antes)

### 1 · Sync RACI ↔ WhatsApp do grupo da empresa

```bash
# verifica updates do grupo desde a última reunião
# (grupo Conselho Vallen: 120363408325592607@g.us)
python3 -m app.services.raci_smart_updates process_week_for_empresa \
  --empresa <UUID-empresa> --group-jid <JID@g.us> --days 30
```

Saída: tabela de updates aplicados/pulados. Renato confirma antes de aplicar.

### 2 · Coleta de comunicação

Para grupos Vallen, dados ficam **só em Neon prod** — não no DB local. Conectar via `DATABASE_URL` direto pra análises de mensagens.

### 3 · Análise financeira (se aplicável)

Quando a empresa entregar DMR/Faturamento/Indicações novos:

1. Baixar PDFs do Drive da empresa
2. Extrair via Read tool
3. Cruzar com transcripts de reuniões anteriores
4. **Sinal vermelho:** discrepâncias de KPI entre fontes (caixa vs consolidado vs procedimento)

### 4 · Análise de atas anteriores

Últimas 3 atas (`reunioes.ata_md`). Identificar:
- Itens recorrentes adiados (3+ reuniões = padrão estrutural, não execução)
- Riscos sem dono RACI explícito
- Decisões tomadas que ainda não viraram ação

Para transcripts grandes, usar `Agent(subagent_type=general-purpose)`.

### 5 · Itens eventuais que entram na pauta

Sempre que aparecer algum dos abaixo nas semanas anteriores, **devem virar bloco de pauta + devolutiva técnica do conselheiro**:

| Item | Quem traz | Devolutiva esperada |
|---|---|---|
| **Relatório de Marketing mensal** | Renata Comin (Fors) | análise de métricas, ROI, decisões pendentes (ex: tráfego pago sem gestor, continuidade de campanhas underperforming) |
| **Proposta de remuneração de médicos** / parceria | Amadeo (Aptus) | análise estratégica (alinhamento ao PE), risco (4 vetores: tributário, vínculo, regulatório, operacional), jurídica (cláusulas faltantes) |
| **Revisão de contrato** (cessão de espaço, etc.) | Advogado/Amadeo | análise tridimensional (estratégica + risco + jurídica). Gera `.docx` revisado com redlines |
| **Plano operacional** (campanhas, treinamentos) | Amadeo/Renata | desconfiar de incentivos auto-prescritivos. Pedir contraditório quando prestador desenha incentivos da própria área |
| **Indicação de novo conselheiro / sócio** | Presidente do conselho | leitura de perfil + sinal "sim/não/abstenção" + e-mail privado de voto |

Para cada um: **gerar devolutiva em `conselho/<empresa>/<data>/devolutivas/<tema>.md`** + linkar no dossiê.

### 6 · Geração de Pauta + Dossiê

```python
# preencher reunioes.pauta_md e reunioes.dossie_md no ConselhoOS (PG Neon)
# converter pra .docx via:
python3 scripts/ata_to_docx.py --reuniao-id <UUID>
# upload pra Drive na pasta da empresa, registrar em documentos
```

**Verificar paralelismo** com outra sessão Claude — pode ter pre-preenchido `pauta_md`. Guard:
```sql
WHERE pauta_md IS NULL OR LENGTH(pauta_md)=0
```

### 7 · Compartilhamento Drive

Sempre incluir **email de trabalho + gmail pessoal** dos participantes. Lista canônica em `project_vallen_conselho_workflow.md`. Padrão: novos arquivos = share como **commenter** (exceto Renato = writer/owner).

---

## Pipeline DURANTE a reunião (presencial)

Plano B (sem diarização Fathom):

1. Renato grava com Voice Memos no iPhone — anuncia gravação
2. Exporta `.m4a` → bot do INTEL via WA → transcreve via Whisper Groq (worker Railway)
3. Salvar `/tmp/reuniao_<data>.txt`
4. Rodar:
   ```bash
   ~/prospect-system/.venv/bin/python3 \
     ~/prospect-system/scripts/processar_reuniao_presencial.py \
     --transcricao /tmp/reuniao_X.txt \
     --reuniao-id <UUID-ConselhoOS> [--dry-run]
   ```
5. Saídas: ata + JSON de updates RACI pra Renato aprovar

Plano A (Fathom integration):
- Webhook `new-meeting-content-ready` já ingere automaticamente
- Tool `import_fathom_meeting` cobre fallback se reunião não está no INTEL

---

## Pipeline PÓS-reunião (até 48h depois)

### 1 · Gerar ata

A partir de transcrição (Fathom share link ou colagem manual):
```python
# Claude gera ata_md a partir de prompt estruturado
# salva em reunioes.ata_md (ConselhoOS) + arquivo .md local
```

Conferir antes de compartilhar — Renato revisa no Drive Google Doc.

### 2 · Updates RACI

A partir da mesma transcrição:
- Marcar concluídos
- Atualizar status (pendente → em_andamento, etc)
- **Estender prazos** quando combinado (não criar item duplicado)
- Criar novos itens decididos
- **Validar UUIDs** (regex + check em `raci_itens.id`) — bug 09/06: smart_updates alucina

Sempre gerar `raci_diff.json` em `conselho/<empresa>/<data>/`:
```json
{
  "concluidos": [{"id": "...", "acao": "...", "razao": "..."}],
  "atualizados": [{"id": "...", "antes": {...}, "depois": {...}}],
  "novos": [{"acao": "...", "responsavel_r": "...", "prazo": "..."}]
}
```

### 3 · Envio do RACI ao grupo

Formato C (forma compacta validada nas sessões 12/05 e 09/06):
```
📋 *RACI Semanal — <Empresa>* _<data>_

📝 *Resumo da reunião <data>:* ...

✅ *Concluídos esta semana (N):*
1. [acao] — [contexto curto]
...

🆕 *Novos itens (N):*
1. [acao] — R: [pessoa] | A: [pessoa] | Prazo: [data]
...

🔄 *Em andamento (N):* ...

⚠️ *Atrasadas (N):* ...
```

**Sempre preview pra Renato no DM antes de mandar no grupo.**

### 4 · Cooldown 72h pra "Atrasadas"

Se item vencido tem update_recente (<72h), reclassificar de **urgente** → **atrasada_com_movimento**. Lógica em `app/services/raci_weekly_report.py:format_raci_whatsapp`.

### 5 · Follow-ups individuais

Para itens onde Renato é R (responsável de execução):
- Criar drafts de emails/WAs em `conselho/<empresa>/<data>/follow_ups.md`
- Aplicar após confirmação do Renato (gmail draft ou send, WA via copy/paste ou send via `rap-whatsapp` instance)
- Notificar grupo quando concluir (msg curta com status RACI updated)

---

## Gotchas (de sessões anteriores)

| Gotcha | Mitigação |
|---|---|
| `get_valid_token` compara `datetime.now()` local com `expires_at` UTC | Refresh manual via `oauth2.googleapis.com/token` |
| ConselhoOS em repo separado `/Users/rap/conselhoOS` | Conectar via `CONSELHOOS_DATABASE_URL` env var no INTEL |
| `.docx` no Drive: PATCH `/upload/drive/v3/files/{id}?uploadType=media` mantém file_id e link | Útil para artefatos que evoluem (dossie v1 → v2) |
| Smart_updates alucina UUIDs e item_ids | Regex + valid_ids check (drop hallucinated) |
| Outra sessão Claude pode estar mexendo no mesmo conselho | `git diff` antes de commit + guard `WHERE pauta_md IS NULL` |
| `raci_status` enum: só `pendente/em_andamento/concluido/atrasado` (sem "cancelado") | Marcar como concluído com nota "cancelado por decisão" |
| `reuniao_status` enum: `agendada/em_andamento/concluida/cancelada` (sem "realizada") | Usar "concluida" |
| Audio worker `INTEL_BOT_INSTANCE` hardcoded | Para audios fora bot, usar `/transcribe-raw` no worker Railway |
| Mensagens da Thalita densas em 1-3min = sinal de crise/decisão | Propor call automaticamente |

---

## Sinais operacionais que devem virar proposta de ação

Triggers que o bot/CoS deve detectar proativamente entre reuniões:

- "cirurgia segunda/terça/..." + nome de funcionária chave → risco operacional, propor call de contingência
- Anexo de CV + indicação ("Recebi do X") → cruzar com processos seletivos ativos
- Discrepância de KPI em material novo → bloco de pauta automático
- Item RACI vencido + 0 movimento em 30 dias → sinalizar risco estrutural (não execução)
- Decisão crítica adiada em 3+ reuniões → bloco "decisão pendente" obrigatório na próxima

---

## Convenções de nomenclatura

| Artefato | Padrão |
|---|---|
| Pasta da reunião | `conselho/<empresa>/<YYYY-MM-DD>/` |
| Pauta | `pauta.md` (versão Drive: `<Empresa>_Pauta_Conselho_<data>.md`) |
| Dossiê | `dossie.md` |
| Devolutivas | `devolutivas/<tema>.md` (tema em snake_case) |
| Ata | `ata.md` |
| RACI diff | `raci_diff.json` |
| Follow-ups | `follow_ups.md` |
| README da reunião | `README.md` (links Drive + contexto curto) |

---

## Memórias relacionadas

- `project_vallen_conselho_workflow.md` — workflow base + emails + gotchas técnicos
- `project_cos_config.md` — prioridades estratégicas (Vallen é Frente 4)
- `feedback_billable_time.md` — deslocamento e eventos sociais não são billable
- `project_conselhoos_ata_raci.md` — pipeline geração ata + RACI tracking
