# Roadmap — Integração INTEL ↔ ConselhoOS (lado INTEL)

> Itens que vivem **neste repo** (INTEL / prospect-system). Pra os itens que vivem no ConselhoOS, ver `conselhoOS/docs/ROADMAP_INTEGRACAO_INTEL.md`.

---

## Contexto

Sessão de 12/05/2026 (preparação da reunião do Conselho Vallen 13/05) consumiu 6h de trabalho manual em chat fazendo coisas que deveriam ser pipelines. Diagnóstico cheio: ver o documento espelho no ConselhoOS.

Este roadmap divide o trabalho em 5 camadas. O INTEL ganha capacidade de **captação + síntese + escrita** no banco do ConselhoOS via `CONSELHOOS_DATABASE_URL` (env var já existe — usada por `app/services/conselhoos_sync.py` para leitura).

## Princípio organizador

- **INTEL = captação + inteligência analítica + bot.** Recebe mensagens, faz síntese via Claude, escreve em ambos os bancos.
- **ConselhoOS = governança formal.** Verdade institucional dos atos formais.

INTEL é o "cérebro analítico" do conselheiro; ConselhoOS é o "sistema de registro oficial". Esta separação vale ser preservada.

---

## P1 — Crítico (próximas 4 semanas)

### 1.1 · Extrator de updates RACI a partir de mensagens WhatsApp

**Problema:** Updates de Renata/Amadeo no grupo Conselho Vallen chegam em texto livre formato carta ("📋 RACI Vallen — Update semana 04-10/05 \n 1. Tráfego pago → proposta enviada..."). O `services/raci_weekly_report.py` espera regex "3 concluído" e ignora 95% do que entra. Renato precisa ler manualmente e atualizar via SQL.

**Proposta:**
- Novo módulo `app/services/raci_message_parser.py`
- Pipeline:
  1. Hook em `group_messages` insert: se `group_jid` pertence a um Conselho (lookup em `project_whatsapp_groups` cruzado com `conselhoos_links` ou tabela nova `conselho_grupos`), enfileira mensagem para análise
  2. Worker (Railway) lê mensagem, busca contexto: lista atual de `raci_itens` em aberto da empresa (via `CONSELHOOS_DATABASE_URL`)
  3. Chama Claude com schema RACI no system prompt + mensagem + lista de itens, retorna JSON estruturado: `[{raci_item_id, suggested_status, suggested_nota, suggested_prazo, confidence, excerpt}]`
  4. Escreve cada sugestão em `raci_update_suggestions` no ConselhoOS (tabela nova — ver lado ConselhoOS 1.1)
  5. Notifica Renato via push/dashboard se `confidence` alta ou via fila silenciosa se baixa
- Suporte a mensagens "RACI report" inteiras (lista) e mensagens pontuais ("@Renata acabou de mandar a proposta")

**Complexidade:** média-alta. Worker novo + integração Claude + cross-DB. Reaproveita pattern do audio worker e do bot.

**Onde:**
- Service: `app/services/raci_message_parser.py`
- Worker handler no Railway worker repo (welcoming-consideration)
- Endpoint INTEL `/api/raci/extract` que enfileira

**Dependência:** ConselhoOS 1.1 (tabela `raci_update_suggestions`).

### 1.2 · Auto-anexar documentos do WhatsApp ao ConselhoOS

**Problema:** PDFs financeiros (DMR, Faturamento, Indicações), contratos, planos chegam no grupo da empresa. Renato baixa, sobe no Drive, anexa no ConselhoOS. 4 passos manuais por documento.

**Proposta:**
- Hook em `group_messages` insert quando `message_type='document'` e `group_jid` é de um Conselho:
  1. Baixa o documento via Evolution API
  2. Sobe pro Drive na pasta da empresa (lookup mapa empresa→folder_id)
  3. Insere em `documentos` no ConselhoOS com `empresa_id` correto + `drive_file_id` + tipo inicial `outros`
  4. Dispara classificador Claude: lê primeiras páginas, classifica tipo (`financeiro`/`contrato`/`planejamento`/`outros`), extrai `resumo` e `topicos`
  5. Atualiza `documentos` com classificação + resumo
- Notifica Renato no dashboard pra revisar classificação se confidence baixa

**Complexidade:** média. Hook + Drive upload (já existe em `integrations/google_drive.py`) + classificador.

**Onde:**
- Hook em `app/services/realtime_analyzer.py` (já é nosso ponto central de processamento de mensagens novas)
- Service `app/services/conselhoos_document_importer.py` novo
- Classificador reusa pattern de `app/services/auto_tags.py`

**Dependência:** mapa `empresa → drive_folder_id` precisa estar disponível. Hoje a folder ID está hardcoded no script (`1vtJXh4uzfiK0X_MWB3KLNad23TwNY87S` pra Vallen). Sugestão: campo `drive_documentos_folder_id` em `empresas` no ConselhoOS.

### 1.3 · Auto-criar reuniões a partir do Google Calendar

**Problema:** Reuniões do Conselho são criadas no Google Calendar mas precisam ser importadas manualmente no ConselhoOS (vimos na sessão: "Acabei de importar pro ConselhoOS").

**Proposta:**
- Cron diário lê eventos Calendar com pattern `Conselho [Empresa]` ou `[Empresa] - Reunião de Conselho`
- Faz match com `empresas` no ConselhoOS por nome (fuzzy)
- Cria entry em `reunioes` se não existir (data, calendar_event_id, calendar_link, status='agendada', título derivado do evento)
- Idempotente

**Complexidade:** baixa. Cron novo + 1 query Calendar API + 1 insert.

**Onde:**
- `app/services/conselhoos_calendar_sync.py` novo
- Cron em `vercel.json`

**Dependência:** independente.

### 1.5 · Captura de reunião presencial (Voice Memos → diarização → ata)

**Problema:** Fathom só captura reuniões online. A reunião do Conselho Vallen de 13/05 (e provavelmente Alba) acontecem presencialmente. Hoje não há pipeline pra transformar gravação local em transcrição estruturada + ata. Esta sessão revelou a lacuna ao preparar a reunião sem ter como capturá-la.

**Proposta — pipeline completo:**
1. **Captura:** iPhone Voice Memos no centro da mesa. Gravação `.m4a` (Voice Memos comprime razoavelmente; alternativa: app gravador com bitrate maior pra salas mais ruidosas).
2. **Upload:** envio pra bot do INTEL via WhatsApp com legenda `/captura_reuniao [reuniao_id]` (ID da reunião no ConselhoOS). Bot reconhece comando + áudio anexo.
3. **Diarização:** audio worker no Railway (já existe pra Whisper) recebe áudio. Pipeline novo:
   - `pyannote.audio` (modelo open source HuggingFace) identifica segmentos por falante anônimo (SPEAKER_00, SPEAKER_01, ...)
   - Whisper Groq transcreve cada segmento mantendo timestamps
   - Junta em transcrição estruturada `[HH:MM:SS] SPEAKER_XX: texto`
4. **Identificação de falantes:** Claude recebe transcrição diarizada + lista de `pessoas` da empresa (consultando ConselhoOS) + título da reunião como contexto. Mapeia SPEAKER_XX → "Dra. Thalita Mendes" etc., baseado em conteúdo (autoapresentação, papéis mencionados, vocabulário). Quando confidence < threshold, marca falante como `?` para revisão manual.
5. **Persistência:** transcrição final escrita em `reunioes.transcricao` no ConselhoOS via `CONSELHOOS_DATABASE_URL`.
6. **Geração de ata:** dispara pipeline existente (Railway → Claude → `ata_md` → upload Drive como `.docx`).
7. **Extração de updates RACI:** transcrição passa pelo extrator (item 1.1) — RACI atualizado automaticamente a partir das decisões da própria reunião.

**Complexidade:** alta. Adiciona `pyannote.audio` como dependência (uso de CPU intenso, ~1× tempo de áudio em hardware modesto; existe versão GPU opcional). Whisper Groq continua rápido e barato. Identificação Claude é leve.

**Custos estimados:** Whisper Groq pra reunião de 90 min: ~US$ 0,03. Claude pra identificação + ata: ~US$ 0,40. Total ~US$ 0,50 por reunião.

**Onde:**
- Audio worker (Railway, repo separado `welcoming-consideration`): adicionar `pyannote.audio` em requirements; pipeline em `worker/diarize_and_transcribe.py` (novo)
- Bot: nova tool `captura_reuniao_presencial(reuniao_id, audio_url)` em `app/services/intel_bot.py`
- Service INTEL: `app/services/reuniao_presencial_pipeline.py` orquestra os passos
- Endpoint INTEL: `/api/reunioes/[id]/captura` aceita audio_url, enfileira processamento

**Dependência:** independente, mas casa com 1.1 (extrator RACI) e 2.2 (gerador de dossiê) — toda a inteligência acumulada da reunião alimenta o ciclo seguinte.

**Plano B (zero esforço, disponível hoje):** sem diarização. Voice Memos → enviar pro bot → bot transcreve via Whisper Groq simples sem speaker detection → você cola texto em `reunioes.transcricao` manualmente. Ata gerada pelo Claude infere falantes pelo conteúdo (~70% acerto). Suficiente como muleta enquanto 1.5 completo não está pronto.

**Considerações de governança:**
- Ética: antes de gravar, anunciar aos participantes (já é prática usual)
- LGPD: dados de saúde da Vallen ficam no pipeline próprio (INTEL + Railway + Neon) — não em SaaS terceiro tipo Otter/Plaud
- Retenção: definir política de exclusão da gravação `.m4a` após N dias (transcrição é o artefato útil)

### 1.4 · Fix do refresh de token Google (TZ bug)

**Problema:** `app/integrations/google_drive.py:44` compara `datetime.now()` (naive local) com `expires_at` (naive UTC) — causa 401 intermitente. Pegamos nesta sessão.

**Proposta:**
- Usar `now_utc()` do `app/services/tz.py` em vez de `datetime.now()`
- Garantir que `expires_at` é gravado como tz-aware UTC
- Adicionar buffer de 5 min antes do expiry (refresh proativo)

**Complexidade:** trivial. 5 linhas.

**Onde:** `app/integrations/google_drive.py` + `app/integrations/google_contacts.py` (mesmo pattern).

**Dependência:** independente. Pode ser drive-by next session.

---

## P2 — Importante (próximos 2 meses)

### 2.1 · Gerador automático de pauta na véspera

**Problema:** Hoje pauta é construída sob demanda. Cada reunião custa 30-60 min manual.

**Proposta:**
- Cron 24h antes de cada `reunioes.data` (lookup em ConselhoOS via `CONSELHOOS_DATABASE_URL`)
- Pipeline pra cada reunião:
  1. Lê `temas_anuais` do mês conforme `pautas_anuais` da empresa
  2. Lê `raci_itens` em aberto/atrasados
  3. Cross-reference últimas 3 atas pra carry-over (itens recorrentes adiados)
  4. Lista documentos novos desde a última reunião
  5. Claude gera `pauta_md` rascunho
  6. Escreve em `reunioes.pauta_md`
  7. Gera `.docx` via `scripts/ata_to_docx.py` (ou consolida com ConselhoOS — ver decisão técnica)
  8. Sobe pro Drive, insere em `documentos`
  9. Notifica Renato via WhatsApp + dashboard

**Complexidade:** alta. Múltiplas queries cross-DB + Claude + geração docx + Drive.

**Onde:**
- `app/services/conselhoos_pauta_generator.py`
- Cron em `vercel.json` ou Railway worker se exceder 60s

**Dependência:** ConselhoOS 2.3 (carry-over) seria sinérgico.

### 2.2 · Gerador automático de dossiê na véspera

**Problema:** Esta sessão inteira foi essencialmente isso, à mão.

**Proposta:**
- Cron 12h antes da reunião:
  1. Análise financeira: lê documentos `tipo='financeiro'` da empresa anexos à reunião; baixa PDFs via Drive; extrai texto; Claude gera análise (resultado, volume, indicações, top categorias)
  2. Status RACI: lê todos os itens da empresa; agrupa por status
  3. Cross-reference WhatsApp: pega mensagens do grupo Conselho desde a última reunião; Claude resume "o que o conselho precisa saber"
  4. PE: lê `planejamentos_estrategicos` (após ConselhoOS 3.2) para ancorar análise
  5. Itens recorrentes: queries cross-ata
  6. Decisões pendentes: lê `decisoes` com `status='pendente'`
  7. Claude monta `dossie_md` consolidando os blocos
  8. Escreve em `reunioes.dossie_md` + gera `.docx` + sobe pro Drive
  9. Notifica Renato

**Complexidade:** alta. Tudo que esta sessão fez automatizado.

**Onde:**
- `app/services/conselhoos_dossie_generator.py`
- Railway worker (provavelmente excede 60s pelo volume de Claude calls)

**Dependência:** itens financeiros classificados (1.2), PE tipado (ConselhoOS 3.2), reuniões importadas (1.3).

### 2.3 · Devolutiva técnica automática a planos de prestadores

**Problema:** Quando Aptus apresenta plano (Comissionamento, Crescimento Médicos), conselheiro precisa fazer análise crítica. Foi 30 min de leitura + análise nesta sessão.

**Proposta:**
- Quando documento `tipo='plano'` é anexado a uma reunião e categorizado pelo classificador (1.2):
  1. Claude lê o plano
  2. Carrega contexto: PE da empresa, últimas decisões, RACI atual, ata anterior
  3. Gera draft de "Devolutiva do Conselheiro" em formato carta `.docx`
  4. Anexa à reunião como informativo
  5. Notifica Renato pra revisar e enviar

**Complexidade:** média. Prompt template + Claude + geração docx (carta com header/footer formal).

**Onde:**
- `app/services/conselheiro_devolutiva.py`

**Dependência:** 1.2 (classificador de documentos identificar tipo `plano`).

### 2.4 · Bot WhatsApp ganha ferramentas de escrita no ConselhoOS

**Problema:** Hoje `app/services/intel_bot.py` tem tool `query_conselhoos` (só leitura). Tudo que precisei fazer nesta sessão foi via SQL ou scripts. Bot podia mediar.

**Proposta:** Novas tools no bot:
- `conselhoos_update_raci(raci_item_id, status, nota, prazo)` — para correções pontuais via chat
- `conselhoos_add_decision(reuniao_id, titulo, descricao, urgencia)` — registra decisão formal
- `conselhoos_attach_doc(documento_id, reuniao_id)` — vincula doc existente a reunião
- `conselhoos_create_meeting(empresa_id, data, titulo, calendar_link)` — cria reunião nova (alternativa ao 1.3 automático)
- `conselhoos_get_dossie(reuniao_id)` — retorna dossiê pra Renato consultar via WA durante reunião
- `conselhoos_update_dossie(reuniao_id, section, new_md)` — edição pontual via chat

**Complexidade:** baixa-média por tool (estrutura já existe em `intel_bot.py`).

**Onde:** `app/services/intel_bot.py` + system prompt update.

**Dependência:** independente.

---

## P3 — Médio prazo (3-6 meses)

### 3.1 · Detector de KPI divergente

**Problema:** Nesta sessão pegamos 3 leituras de "Faturamento Vallen abril" (R$ 88K, R$ 95K, R$ 98K). Conselho gastou tempo de pauta resolvendo.

**Proposta:**
- Quando mensagens em grupo Conselho mencionam valores financeiros (regex + Claude detector), comparar com KPI canônico da empresa (ConselhoOS 2.4)
- Se divergência > X%, sinalizar em fila "alinhamento de KPI pendente"
- Renato vê no dashboard "antes da reunião, alinhar: alguém disse R$ 98K, outro R$ 88K — qual é o oficial?"

**Complexidade:** média. Regex + Claude detector + comparação.

**Onde:** `app/services/kpi_divergence_detector.py` + cron.

**Dependência:** ConselhoOS 2.4 (KPI canônico tipado).

### 3.2 · Análise de transcripts pós-reunião (riscos órfãos)

**Problema:** Riscos mencionados em ata mas que nunca viram RACI. Esta sessão mapeou 10 deles pra Vallen lendo transcripts à mão.

**Proposta:**
- Quando `reunioes.transcricao` é atualizada (via Fathom webhook), worker dispara análise Claude:
  - Extrai menções de risco/preocupação/promessa não-formalizada
  - Cruza com `raci_itens` da empresa
  - Identifica órfãos (mencionados mas sem dono)
  - Escreve em `riscos_orfãos` no ConselhoOS (ver lado ConselhoOS 3.1)
- Renato revisa lista no dashboard, transforma em RACI com 1 clique

**Complexidade:** média. Trigger + Claude + cross-reference.

**Onde:** `app/services/transcript_risk_extractor.py`

**Dependência:** ConselhoOS 3.1 (tabela `riscos_orfãos`).

### 3.3 · Detector de padrões de adiamento por pessoa

**Problema:** Sabemos qualitativamente que "Renata atrasa". Sistemático seria mensurar.

**Proposta:**
- Cron mensal calcula por `pessoas`:
  - Itens RACI onde a pessoa é R, agrupado por (mês criado, mês concluído)
  - Tempo médio de cumprimento
  - Taxa de atraso vs prazo original
  - Patterns ("itens com 'aguardando X' são Y% mais lentos")
- Escreve em tabela `padroes_pessoas` no ConselhoOS
- Dashboard mostra ao Renato como tooltip ao atribuir nova ação

**Complexidade:** média-alta. Análise estatística + storage + UI.

**Dependência:** dados históricos (já temos para Vallen desde Mar/26).

---

## P4 — Longo prazo (6+ meses)

### 4.1 · Sync bidirecional RACI ↔ tasks
`raci_itens.intel_task_id` já existe mas é leitura unidirecional. Tornar bidirecional: completar task no INTEL → fechar RACI.

### 4.2 · Centro de operação do conselheiro
Dashboard INTEL com aba "Conselheiro" agregando todas as empresas: próximas reuniões, status dossiês, mensagens pendentes, decisões aguardando follow-up.

### 4.3 · Bot WhatsApp atende perguntas do conselho via comando
"/conselho Vallen status" → resume status atual da empresa via WA pra Renato consultar mesmo fora de aplicativo.

---

## Decisões de governança técnica

Antes de implementar:

1. **Quem gera `.docx`?** Hoje há duplicação (INTEL `scripts/ata_to_docx.py` vs ConselhoOS `lib/ata-generator.ts`). Consolidar — sugestão: ConselhoOS gera (proximidade da UI), INTEL chama via endpoint `/api/docx/generate`.

2. **Quem chama Claude pra cada coisa?** Sugestão:
   - **INTEL** chama Claude pra captação (extração de updates RACI, classificação de docs, devolutivas, dossie generator) — porque é fluxo assíncrono, leve em Railway worker
   - **ConselhoOS** chama Claude pra sob demanda interativo (gerar ata da reunião que acabou, ajustes manuais) — síncrono na UI

3. **Estado canônico** — quando uma ação acontece em ambos os sistemas, ConselhoOS sempre ganha (é a verdade formal). INTEL escreve no ConselhoOS via SQL direto (mais simples) ou via API endpoint dele (mais idiomático). Decisão: começar com SQL direto (como esta sessão fez), migrar pra API quando criar UI no ConselhoOS pra esse tipo de update.

4. **Schema de empresas e folder Drive** — mapa hardcoded hoje. Adicionar `drive_documentos_folder_id` em `empresas` (ConselhoOS) e ler de lá ao subir docs.

---

## Mapa de itens cruzados com ConselhoOS

| Item INTEL | Item ConselhoOS correspondente |
|---|---|
| 1.1 — Extrator de updates RACI | ConselhoOS 1.1 — Tabela `raci_update_suggestions` + UI de revisão |
| 1.2 — Auto-anexar documentos | ConselhoOS — campo `drive_documentos_folder_id` em `empresas` |
| 1.3 — Auto-criar reuniões | (puramente INTEL) |
| 1.4 — Fix TZ token Google | (puramente INTEL) |
| 1.5 — Captura de reunião presencial | (escreve em `reunioes.transcricao` + dispara ata existente) |
| 2.1 — Gerador de pauta | ConselhoOS 2.3 — Carry-over de pendências |
| 2.2 — Gerador de dossiê | ConselhoOS 3.2 — PE tipado |
| 2.3 — Devolutiva automática | (puramente INTEL, mas grava doc no ConselhoOS) |
| 2.4 — Bot WA escreve no ConselhoOS | (puramente INTEL) |
| 3.1 — Detector KPI divergente | ConselhoOS 2.4 — KPI canônico |
| 3.2 — Riscos órfãos | ConselhoOS 3.1 — Tabela `riscos_orfãos` |
| 3.3 — Padrões por pessoa | ConselhoOS — tabela `padroes_pessoas` |

---

## Quick wins identificados nesta sessão (drive-by)

Itens curtos que valem fazer assim que tocar a vizinhança:

- **1.4 — Fix TZ token Google** — 5 linhas, melhora ergonomia em toda chamada Drive
- **`scripts/ata_to_docx.py`** está marcado como dono de geração de docx no INTEL — vale renomear/comentar pra deixar claro que gera **qualquer markdown formal** (foi usado nesta sessão pra gerar dossiê, não só ata)
- **Adicionar memória pra Claude do INTEL** sobre o workflow Vallen — já feito (`memory/project_vallen_conselho_workflow.md`)

---

*Documento criado em 12/05/2026 pelo Claude (sessão de prep da reunião Vallen 13/05) após Renato pedir reflexão arquitetural. Vai evoluir.*
