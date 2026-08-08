---
name: cos
description: Abertura de sessao no PAPEL CoS (executivo — Chief of Staff). Marca a sessao como cos, le o board executivo + topo do dev, mostra "onde paramos + proximos passos" e roda checks dinamicos (feedback WA, tasks overdue). Papel executivo — NAO toca git/deploy/Neon-DDL. Par de /dev; ambos fechados por /fim.
---

# Skill: /cos

Abertura estruturada de sessao no **papel CoS (executivo)**. Substitui o antigo `/abre`. Roda no INICIO da sessao pra nao comecar do zero. Par simetrico do `/dev`; o `/fim` le o marcador e fecha o board certo.

**Papel CoS:** executivo/leitura + INSERTs executivos (tasks/project_notes/WA/Drive). **NAO toca git/deploy/Neon-DDL** (isso e da sessao Dev — ver `session_locks.md` + `feedback_parallel_sessions`).

## Etapas (executar em ordem)

### 0. Marcar o papel + pintar a janela (PRIMEIRO, sempre)

**(a) Marcador** — gravar o papel no scratchpad DESTA sessao pra o `/fim` saber que caminho fechar. O diretorio esta no system prompt ("Scratchpad Directory"):

```bash
echo "cos" > "$SCRATCHPAD_DIR/session_role"   # $SCRATCHPAD_DIR = o Scratchpad Directory do system prompt
```
Sobrescrever se ja existir com outro valor (o papel e desta sessao).

**(b) Cor de fundo = ROXO ametista `#45327A`** (distingue a janela CoS da Dev, que e grafite). Terminal.app usa AppleScript; outros terminais usam OSC 11:

```bash
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  osascript -e 'tell application "Terminal" to set background color of front window to {17733, 12850, 31354}'
else
  printf '\033]11;#45327A\007'
fi
```

### 1. Ler os boards — executivo INTEIRO + topo do dev

```
Read /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/project_cos_status.md   (INTEIRO)
Read /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/session_locks.md         (estado dos locks + ultimo log)
```

Do `project_dev_backlog.md` ler **so o topo** (header + "Em curso") — contexto do que a Dev esta fazendo, sem detalhe tecnico. Nao ler o backlog P1-P3 inteiro.

- `project_cos_status.md` = cockpit executivo (compromissos 14d, decisoes CoS, monitoramento, fechados). **Dono de escrita = CoS.**
- `session_locks.md` = canal CoS⇄Dev. Ver se a Dev esta com git/deploy/neon e ha quanto tempo.

**Depois de ler, conferir se coube numa leitura:**

```bash
python3 /Users/rap/prospect-system/scripts/verifica_boards.py --quiet
```

Acima do teto a leitura foi **truncada** e o cockpit do dia sai apoiado num pedaco do board, sem aviso (em 30/07 uma abertura leu 22% do arquivo). O `project_cos_status.md` e **teu** — se ele acusar, consolidar vem ANTES de montar o cockpit: compromisso com data vencida e decisao ja batida descem pro `project_cos_status_historico.md`. Board da Dev acima do teto nao se reescreve; deixa recado no `session_locks`.

### 2. Abrir com "ONDE PARAMOS + PROXIMOS PASSOS" (a dor real do Renato)

ANTES do cockpit de hoje, mostrar 2 blocos curtos extraidos dos boards:

```
## 📍 Onde paramos
[2-3 linhas: resumir o HEADER do cos_status (ultima atualizacao) — o essencial, NAO colar o paragrafo inteiro. + 1 linha do estado dos locks se a Dev estiver mid-flight.]

## ▶️ Proximos passos
[Os bloqueantes/compromissos que a ultima sessao deixou registrado — da secao "Proximos 14 dias" (itens vencidos ou de hoje) + "Decisoes pendentes" com gate.]
```

### 3. Rodar checks dinamicos em paralelo

**REGRA CENTRAL DO COCKPIT (aprovado 03/07/26):**
- So mostra o que exige input do Renato **HOJE**.
- **Zero-state nao vira linha** (count=0 → secao inteira some).
- **Futuro nao aparece ate virar hoje.**
- Cockpit cabe em ~10 linhas na maioria dos dias.

**A. Feedback WA pendente (OBRIGATORIO por CLAUDE.md):**
```sql
SELECT id, tipo, conteudo, criado_em FROM system_feedback WHERE status = 'pending' ORDER BY criado_em DESC
```
Via Neon (`DATABASE_URL_UNPOOLED`). So exibir se count > 0.

**B. Memos drift (.md local vs DB):**
```bash
ls /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/*.md | grep -v MEMORY.md | wc -l
```
Comparar com `SELECT COUNT(*) FROM system_memories WHERE fonte='claude_code_migration'`. So exibir se diff > 0.

**C. Compromissos de HOJE** (nao amanha, nao semana): filtrar `cos_status` "Proximos 14 dias" por `data = HOJE` + cruzar Google Calendar. So exibir se ha.

**D. Tasks INTEL overdue:**
```sql
SELECT id, titulo, data_vencimento FROM tasks WHERE status='pending' AND data_vencimento < NOW()::date ORDER BY data_vencimento LIMIT 10
```
So exibir se count > 0.

**E. Cost tracker (se ultima sessao >24h):**
```sql
SELECT provider, amount_usd, max(fetched_at)::date atualizado
FROM platform_costs WHERE period_start = date_trunc('month', CURRENT_DATE)::date
GROUP BY provider, amount_usd ORDER BY amount_usd DESC
```
So alertar se acima do baseline (~US$76/mes). Medido em 08/08: US$25,71 no mes.

⚠️ **Consertado em 08/08 — estava quebrado desde sempre.** A query anterior filtrava
`data_referencia = CURRENT_DATE - 1`, e essa coluna **nao existe**: `platform_costs` e
**mensal por provider** (`period_start`/`period_end`), nao diaria. O erro de SQL nao
aparecia como erro — a secao simplesmente nao saia, e secao ausente aqui le-se como
"custo normal". Alerta que falha calado e pior que alerta nenhum, porque certifica o
que nunca olhou ([[feedback_filtro_vocabulario_errado_falha_calado]]).

**F. Reconciliacao conversa×tasks (PROTECAO anti cos_action_blindness):**
O sistema so reconcilia quando o BOT age; quando o Renato age direto ou uma nota e criada, a task fica aberta = drift. Cruzar tasks × atividade real (~96h):

⚠️ **NAO diga "WA" ao mostrar isto.** WhatsApp e e-mail moram na MESMA `messages`, separados por `conversations.canal` — F1 nunca filtrou canal, entao **sempre leu os dois**. Medido em 08/08: das 33 tasks candidatas, **11 casavam por e-mail**. Anunciar "parece resolvida no WA" manda o Renato conferir no WhatsApp uma conversa que aconteceu no e-mail; ele nao acha, e passa a desconfiar do check inteiro. **Mostre o canal que casou** — e por isso a query abaixo tem `canais`.
```sql
-- (F1) task pending cujo CONTATO teve conversa recente — WhatsApp E/OU e-mail
SELECT t.id, t.titulo, c.nome, max(m.criado_em) ultima,
       string_agg(DISTINCT cv.canal,',') canais, string_agg(DISTINCT m.direcao,',') dirs
FROM tasks t JOIN messages m ON m.contact_id=t.contact_id
JOIN conversations cv ON cv.id=m.conversation_id
LEFT JOIN contacts c ON c.id=t.contact_id
WHERE t.status='pending' AND m.criado_em > NOW() - INTERVAL '96 hours'
GROUP BY t.id, t.titulo, c.nome ORDER BY ultima DESC;
-- (F2) task pending cujo PROJETO teve nota nova recente
SELECT t.id, t.titulo, t.project_id, max(pn.criado_em) ultima
FROM tasks t JOIN project_notes pn ON pn.project_id=t.project_id
WHERE t.status='pending' AND pn.criado_em > NOW() - INTERVAL '96 hours'
GROUP BY t.id, t.titulo, t.project_id ORDER BY ultima DESC;
```
**(F3) QUEM FECHA NEM SEMPRE E O RENATO — nem a contraparte (06/08).** A
Andressa escreveu *"Esta sim. Paguei hoje."* (05/08 16:48) e a task do G100
(#999562) seguiu aberta; o Renato perguntou DUAS vezes no cockpit por que. Dois
furos somados: a task estava com `contact_id` NULL, e mesmo linkada apontaria
pro contato do G100 — a contraparte — enquanto quem paga e a **executora**.
```sql
-- atos de executor (Andressa/Priscila/Piccino, via tonha_role_contacts) que
-- declaram execucao. `declara_execucao` ORDENA, nao filtra: o regex de verbo
-- acerta ~15% e confunde "paguei" com "vou transferir".
SELECT papel, quando, evidencia FROM atos_que_resolvem
WHERE quem='executor' AND quando > NOW() - INTERVAL '7 days'
ORDER BY declara_execucao DESC, quando DESC LIMIT 20;
```
⚠️ **NAO cruze isto automaticamente com a lista de tasks abertas** — testei: da
produto cartesiano, 12 tasks casando com a mesma mensagem que nem era execucao.
Le a lista, reconhece o que fecha o que, e confirma com o Renato.

📉 **E saiba o limite:** so **49% das tasks abertas tem `contact_id`** (eram 37%
antes de 06/08; 14 foram ligadas pelo titulo com `scripts/tasks_liga_contato.py`).
Task sem contato e invisivel pra F1, F2, F3 e pro check G. Quando criar ou fechar
task, LINKE ao contato — e as 60 orfas seguem fora de qualquer cruzamento.

**(F4) A TASK QUE ESPERA TERCEIRO NAO ESTA EM `pending` — ela esta `on_hold` (08/08).**
F1/F2/F3 filtram `status='pending'`, e a regra de 04/08 ([[feedback_aguardar_terceiro_on_hold]])
manda parquear como `on_hold` exatamente a task que aguarda retorno. Resultado: **as 24
`on_hold` ficavam 100% fora do check F** — e sao justamente aquelas cuja condicao de saida
e "o terceiro respondeu". Nao era o e-mail que faltava no cruzamento: era o *status*.
Medido em 08/08: **16 das 24 tinham incoming nos ultimos 14 dias** — o retorno chegou e
ninguem viu. (Sao 100% linkadas a contato, ao contrario das `pending`, so 46%.)

```sql
-- on_hold cujo RETORNO chegou depois do parqueio E que ele ainda nao respondeu
WITH ult AS (
  SELECT t.id AS task_id, t.titulo, t.contact_id, t.on_hold_since,
         max(m.criado_em) FILTER (WHERE m.direcao='incoming') AS ult_in,
         max(m.criado_em) FILTER (WHERE m.direcao='outgoing') AS ult_out
  FROM tasks t JOIN messages m ON m.contact_id = t.contact_id
  WHERE t.status = 'on_hold'
  GROUP BY t.id, t.titulo, t.contact_id, t.on_hold_since
)
SELECT u.task_id, u.titulo, c.nome, u.on_hold_since::date parqueada, u.ult_in::date retorno,
       (SELECT cv.canal FROM messages m JOIN conversations cv ON cv.id=m.conversation_id
         WHERE m.contact_id=u.contact_id AND m.criado_em=u.ult_in LIMIT 1) canal
FROM ult u JOIN contacts c ON c.id=u.contact_id
WHERE u.ult_in > COALESCE(u.on_hold_since, '-infinity'::timestamp)
  AND (u.ult_out IS NULL OR u.ult_out < u.ult_in)
ORDER BY u.ult_in DESC;
```
⚠️ **As duas condicoes do WHERE sao a regra — nao as simplifique.** Sem `ult_out < ult_in`
a query devolve **8** candidatas em vez de **3**, e o extra e falso positivo do tipo que
queima confianca: a #999738 (Daniela) aparecia porque ela respondeu 07/08 10:07 — mas o
Renato **respondeu de volta as 20:42**, entao a bola voltou pra ela e a task segue esperando
com razao. Incoming nao significa "chegou a vez dele"; significa isso **so se ele nao falou
depois**. Mesma inversao de bola medida no board hunt em 07/08.

⚠️ **Nao trate `on_hold` vencida como atraso.** Vencimento passado e o normal nesse estado
(regra 2 do prompt do agente). O que F4 detecta nao e o prazo — e o **fim da condicao do
parqueio**. Oferecer: "o retorno que ela esperava chegou (canal, data) — reabre ou fecha?".

Calibracao (13/07): **verbo de acao** (FUP/contatar/planejar/revisar/enviar) → o entregavel E A ACAO; se a acao foi feita (outgoing do Renato, nota criada, gate batido) = **RESOLVIDA, procurar a evidencia ativamente e fechar** (NAO devolver pro Renato quando a evidencia existe — [[feedback_nao_perguntar_age]]). **"Aguardar X retornar"** so fecha com `incoming` do terceiro. Ignorar tasks dev/backlog. So exibir se houver candidatas; NAO fechar auto — Renato confirma.

**G. Inbound orfao (WA/email que devia virar task/evento e nao virou):**
Fecha o buraco do inbound do Renato que e capturado+extraido mas nao tem cano pra acao. **Substitui parcialmente o `detector_relacionamento` desligado 20/07** — e **NAO religa signals** (respeita a decisao "ii" do Renato: zero notificacao nova, so aparece quando ele abre `/cos`). **Limite honesto: e session-bound** — se o Renato nao abrir `/cos` num dia, o inbound daquele dia espera ate a proxima abertura. Read-only, roda na abertura como A-F. Janela 4 dias:
```sql
WITH att AS (   -- anexos-documento (imagem = ~90% ruido, fora); join por external_id
  SELECT message_id, bool_or(kind IN ('pdf','docx','xlsx','doc','xls')) has_doc,
         max(kind) kind, max(original_filename) fname
  FROM wa_attachments WHERE criado_em > NOW() - INTERVAL '5 days' GROUP BY message_id
),
lastdir AS (    -- de quem esta a bola: ultima msg de cada contato
  SELECT DISTINCT ON (contact_id) contact_id, direcao FROM messages
  WHERE contact_id IS NOT NULL AND criado_em > NOW() - INTERVAL '14 days'
  ORDER BY contact_id, criado_em DESC
),
dispensadas AS ( -- ⛔ O QUE ELE JA MANDOU IGNORAR NAO VOLTA (fix 07/08)
  -- A migration 066 criou `dispensado_em` e o cockpit passou a le-lo, mas ESTA
  -- query nunca leu: a decisao dele existia gravada no banco e o check
  -- re-oferecia assim mesmo. Ele reagiu na abertura de 07/08 — 6 itens ja
  -- tratados, "alguns varias vezes". Nao era falta de dado, era um consumidor
  -- que nunca nasceu. Sao 104 dispensas gravadas contra 85 mostrados.
  SELECT message_id FROM check_g_ledger WHERE dispensado_em IS NOT NULL
),
qual AS (
  -- ⚠️ NAO FILTRA MAIS POR VOCABULARIO. Ate 05/08 havia aqui um regex
  -- ('reuni|agend|proposta|prazo...') e o que nao casava SUMIA: zero linha,
  -- zero erro. Medido no dia: das 24 mensagens com a bola no Renato em 4 dias,
  -- **4 passavam e 20 eram descartadas em silencio (83%)**. Entre as cortadas:
  -- Jose Horacio Halfeld ("Avise quando puder falar do projeto do Judo" —
  -- frente #7), Glaucia Parizotto (consulta trabalhista da Vallen), Luguitec
  -- ("vamos realizar a descupinizacao?"), e tres contatos abrindo conversa.
  --
  -- O fix NAO e acrescentar palavra no regex (decisao do Renato, 05/08):
  -- e gato-e-rato infinito, e cada palavra que falta vira silencio de novo.
  -- Traz TUDO que esta com a bola nele e deixa o JULGAMENTO com quem tem
  -- contexto — voce. Volume real: ~5/dia. Nao e enxurrada.
  --
  -- `ack` marca encerramento obvio ("ok", "feito", "obrigado", so emoji) pra
  -- voce REBAIXAR, nao pra sumir: a lista mostra tudo, ordenada.
  SELECT m.id, m.contact_id, m.criado_em, m.conteudo,
         COALESCE(a.has_doc,false) has_doc, a.fname,
         (length(btrim(m.conteudo)) <= 22
          AND btrim(lower(m.conteudo)) ~ '^(ok|okay|blz|beleza|feito|certo|certinho|combinado|indo|vou|sim|nao|não|valeu|obrigad[oa]|grato|perfeito|show|joia|jóia|👍|🙏|😉|🤝|❤️|\W)+$') AS ack
  FROM messages m LEFT JOIN att a ON a.message_id = m.external_id
  WHERE m.direcao='incoming' AND m.contact_id IS NOT NULL
    AND m.criado_em > NOW() - INTERVAL '4 days'
    AND m.id NOT IN (SELECT message_id FROM dispensadas)   -- ⛔ ele ja disse "ignora"
    -- Remetente que ele ja classificou como cold-seller ou notificacao
    -- automatica: e a NATUREZA de quem manda, nao o assunto — por isso pode ser
    -- gate sem cair no gato-e-rato de vocabulario que foi removido em 05/08.
    -- Quando ele diz "ignora, e cold", a CoS marca a tag NA HORA, sem clique
    -- dele ([[feedback_ferramenta_nao_vira_tarefa_do_renato]]).
    AND NOT EXISTS (SELECT 1 FROM contacts ct2 WHERE ct2.id = m.contact_id
                      AND (ct2.tags::text ILIKE '%cold-seller%'
                        OR ct2.tags::text ILIKE '%notifica%autom%'))
),
cand AS (
  SELECT q.* FROM qual q
  JOIN lastdir l ON l.contact_id=q.contact_id AND l.direcao='incoming'   -- GATE: bola com o Renato (ele ainda nao respondeu por ultimo)
  WHERE (
      q.has_doc
      -- Sem `text_hit`: o que decide agora e ser inbound com a bola nele.
      -- Fica so o corte de RUIDO COMERCIAL, que e sobre a NATUREZA do
      -- remetente e nao sobre o assunto do Renato — promo nunca vira acao.
      OR (length(q.conteudo) <= 600
          AND q.conteudo !~* 'desconto|promo[çc]|black friday|imperd|clique aqui|link na bio|inscri[çc]|newsletter|cancelar|no-?reply|unsubscribe')
  )
  AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.contact_id=q.contact_id AND t.data_criacao > q.criado_em)          -- GATE: ja virou task (criada depois)
  AND NOT EXISTS (SELECT 1 FROM calendar_events e WHERE e.contact_id=q.contact_id AND e.criado_em > q.criado_em)   -- GATE: ja virou evento
  AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.contact_id=q.contact_id AND t.status='completed' AND t.atualizado_em > q.criado_em)  -- GATE (fix 29/07): task DO PROPRIO contato (casada por contact_id) foi CONCLUIDA depois da msg = ja tratado. Preciso: so olha as tasks DELE, nao de colega de projeto. Pegou a Priscila (contrato #999678 completed) DEPOIS de linkar #999678 a ficha #4734 — o defeito era o contact_id NULL, nao o gate.
),
agg AS (
SELECT c.contact_id, ct.nome, max(c.criado_em) ultima, bool_or(c.has_doc) tem_doc,
       max(c.fname) FILTER (WHERE c.has_doc) anexo,
       bool_and(c.ack) so_ack,   -- true = tudo que ele mandou foi "ok/feito/obrigado": REBAIXE (nao suma)
       -- ⚠️ 06/08: uma amostra so NAO basta — LEIA `fio`, nao `amostra`.
       -- `msgs` e o DENOMINADOR: quantas incoming distintas o contato mandou
       -- na janela. Medido no dia: 62 mensagens em 20 contatos — a amostra
       -- unica mostrava 20 delas (**32%**), e 14 dos 20 contatos tinham mais
       -- de uma. O caso que expos isso: o Orestes mandou 4 PDFs de comprovante
       -- (04/08 ~15h) e as 21:07 um WA separado com o pedido real ("voce
       -- precisa conversar com a Daniela... zerar estas despesas"). O
       -- ORDER BY has_doc DESC elegeu o PDF, a triagem leu so o anexo e
       -- concluiu "nada a fazer". Quem pegou foi o Renato.
       count(*) AS msgs,
       (array_agg(c.conteudo ORDER BY c.criado_em DESC))[1:3] AS fio,   -- ate 3 ultimas, recente primeiro
       (array_agg(c.conteudo ORDER BY (c.has_doc)::int DESC, c.criado_em DESC))[1] amostra
FROM cand c LEFT JOIN contacts ct ON ct.id=c.contact_id
GROUP BY c.contact_id, ct.nome
)
SELECT a.*,
       -- 🔀 O FIO NAS DUAS DIRECOES (fix 07/08). O `fio` acima so tem INCOMING —
       -- e um monologo do contato. Lendo so ele, o que ELE respondeu no meio da
       -- conversa fica invisivel, e o item reaparece como se ninguem tivesse
       -- feito nada. Aqui vem a conversa como ela e: quem falou, quando, o que.
       f2.conversa,
       -- E o que ele fez DEPOIS da ultima msg, por qualquer canal (view 062):
       -- e-mail, grupo, RSVP de convite. "Ele ja fez isso?" tem UMA fonte.
       ato.o_que AS ato_depois, ato.canal AS ato_canal, ato.quando AS ato_quando
FROM agg a
LEFT JOIN LATERAL (
    SELECT array_agg(x.linha ORDER BY x.quando DESC) AS conversa FROM (
        SELECT (CASE WHEN m2.direcao='incoming' THEN '← ' ELSE '→ ' END)
               || to_char(m2.criado_em,'DD/MM HH24:MI') || ' ' || left(m2.conteudo,110) AS linha,
               m2.criado_em AS quando
          FROM messages m2
         WHERE m2.contact_id = a.contact_id
           AND m2.criado_em > NOW() - INTERVAL '6 days'
         ORDER BY m2.criado_em DESC LIMIT 6
    ) x
) f2 ON true
LEFT JOIN LATERAL (
    SELECT o_que, canal, quando FROM acao_do_renato
     WHERE pessoa_id = a.contact_id AND quando > a.ultima
     ORDER BY quando DESC LIMIT 1
) ato ON true
ORDER BY a.so_ack ASC,    -- o que PEDE algo primeiro; encerramento no fim
         a.tem_doc DESC, a.ultima DESC;
```
**`ato_depois` PREENCHIDO = ele ja agiu depois da ultima mensagem do contato.**
Nao suprime sozinho (a acao pode ser sobre outro assunto), mas **rebaixa**: leia
`conversa` antes de oferecer. Oferecer o que ele acabou de fazer foi o defeito de
06/08, cinco vezes numa sessao ([[feedback_checar_acao_renato_antes_de_oferecer]]).
**LER O FIO, NAO A ANCORA.** `msgs > 1` significa que ha conversa: o pedido
acionavel pode ser o irmao da mensagem que aparece em `amostra` — anexo nao e
o assunto, e o envelope. Com `msgs > 3` o `fio` tambem esta truncado: abra a
conversa do contato antes de julgar. Ver [[feedback_inbound_ancora_nao_e_a_mensagem_acionavel]].
**INCLUI se:** (a) tem anexo pdf/docx/xlsx (NAO imagem — imagem e ~90% ruido), OU (b) o texto casa o regex de convite/pedido E e curto (<=280).
**GATES anti-ruido (CRITICO — Renato ODEIA firehose, alta precisao > recall):**
- **Bola com o Renato** (`lastdir=incoming`): a ULTIMA msg do contato e incoming → ele ainda nao fechou o loop. Isso deixa passar o caso "respondeu no WA mas nao criou o evento" (ex.: "Consigo sim" no chat ≠ evento na agenda). Se o contato mandou algo DEPOIS que o Renato ja respondeu por ultimo (bola=outgoing), sai — sinal comportamental de "ja tratou".
- **Ja virou task/evento** (`NOT EXISTS` task/event criado depois da msg) → ja tem cano, sai.
- **Ja foi RESOLVIDO depois da msg (fix 29/07):** UM gate novo fecha o falso-positivo que re-levantava item ja tratado (o Renato reclamou "de novo" — [[feedback_cos_action_blindness]]). O gate antigo so olhava se nascia task *depois* da msg; a task de arranque costuma nascer *antes* do anexo do desfecho chegar, entao o PDF ressurgia como orfao. O novo: **task DO PROPRIO contato (`t.contact_id=q.contact_id`) `completed` com `atualizado_em > msg`** = ja tratado. **Por que casar por `contact_id` e nao por projeto/nome:** testei as duas versoes amplas contra prod e as DUAS mataram a Thalita (item legitimo — ela perguntou do proprio contrato) porque ela e da equipe Vallen e aparece em qualquer nota de estado do #24; gatear por "atividade no projeto do contato" e grosso demais. So a task DELE e sinal limpo. **Licao de dado (nao de query):** o caso Priscila so passou porque a task do desfecho (#999678, "Arranque Priscila") estava com **`contact_id` NULL** — nao linkada a ficha #4734. Corrigido o dado, o gate pega. **Convencao que isso impoe:** ao fechar uma task de arranque/desfecho, LINKE ao contato (`contact_id`), senao o gate nao a ve. **Residual conhecido (aceito):** resolucao registrada SO como nota (sem fechar task linkada) OU discutida e nunca gravada (ex.: "Fam Faria" do Jose Olimpio, falado fora do sistema) nao tem como ser suprimida por query — o gate amplo de nota resolveria mas ao custo de matar item legitimo, entao fica de fora. **COMO LER O RESULTADO (05/08):** a lista agora traz TUDO com a bola nele — o
julgamento e teu, nao do SQL. Para cada linha, decida com o CONTEXTO (quem e a
pessoa, que frente, o que estava pendente), nao pelo texto isolado:
- `ack=true` (ok/feito/obrigado/so emoji) → encerramento; **rebaixe**, nao suma.
  Mas leia junto: "Ok" depois de uma pergunta aberta ainda pode exigir resposta.
- Pedido de contato sem palavra-chave ("Avise quando puder falar do projeto X",
  "Tudo bem? Sumido") → **e acao**. Foi exatamente isso que o regex antigo
  cortava.
- **Na duvida, MOSTRE e diga que esta em duvida.** A regra do Renato (05/08):
  *na incerteza, PERGUNTAR — nunca descartar calado.* Um item a mais na lista
  custa 3 segundos de leitura; um pedido perdido custa a relacao.

✅ **LEDGER NO AR (06/08).** O que o check descarta passou a ficar registrado —
`check_g_ledger`, gravado por cron diario (9h UTC), independente de voce abrir a
`/cos`. Leia o estado com:
```bash
curl -s -H "X-API-Key: $INTEL_API_KEY" "https://intel.almeida-prado.com/api/cos/check-g-ledger?dias=14"
```
Primeira medicao: **falso negativo 5,6%** sobre o que deu pra medir, e **37,7%
NAO MEDIDO** (mensagem sem ficha nao tem `contact_id` pra cruzar — sao todas
e-mail, newsletter/boleto). O numero do buraco vem sempre junto do resultado, de
proposito: taxa sem denominador foi o que produziu os sete defeitos de 04-06/08.
O funil de uma janela de 4 dias: **328 incoming → 71 sobrevivem aos gates**.

⚠️ **Ainda session-bound na EXIBICAO** (o ledger grava sozinho, mas quem julga e
voce). Se ele nao abrir `/cos` por 5 dias, o inbound daqueles dias nao evapora
mais — fica marcado como nao-avaliado —, mas segue sem julgamento. A janela e
de 4 dias. E nada registra o que foi dispensado, entao **o falso negativo segue
imensuravel**: nao da pra saber o que a triagem deixou passar. O fix completo e
um **ledger de "dispensado" por mensagem** (Dev) — registrado no `session_locks`.
- **Texto-so <=280 chars** → derruba forward motivacional/politico e thread de email quotado (450-10k chars). Anexo-documento ignora o length (PDF e PDF).
- **Promo obvio** (regex negativo) e **imagem** (fora do `att`) → ruido social/marketing.
- Msg WA e 1:1 (tabela `messages`, nao `group_messages`) → sem ruido de grupo.
- **So exibir se count>0** (zero-state some, como A-F). **NAO cria task/evento auto** — Renato confirma ou ignora; o proprio ato calibra (passive labeling, sem clique de aprovacao em serie — [[feedback_passive_labeling_over_manual]]).
- Colunas reais: `tasks.data_criacao` (nao criado_em), `calendar_events.criado_em`, `wa_attachments.message_id = messages.external_id`.
- **Validado 21/07 contra prod:** 5 candidatos limpos — pega os 2 ancoras (Eli #26440 "consegue participar hoje 16:30" + Baeta #17305 PDF "INFORMACOES COMPLEMENTARES") + 3 legitimos (Sandra "vir presencial amanha?", Wanelise penhora PDFs, Alessandro "vc me liga? NDA"); NAO vaza os forwards (Lilian/Francisco), a mass-invite (Sanfer) nem os threads de email (Adriana/Orestes).

**H. RACI dos conselhos — SO NA SEGUNDA-FEIRA (decisao do Renato, 29/07/26):**

O preview semanal por WhatsApp foi **desligado** em 29/07. Motivo: desde 28-29/07 a pagina `/projetos/{id}/raci` mostra a matriz, deixa editar na fonte e tem botao **"Enviar no grupo"** — o preview virou segunda copia do mesmo texto, e chegava **truncado** (a Evolution corta em 4.096 chars em silencio; os previews de 13, 20 e 27/07 tinham 5.243, 5.866 e 4.669). Quem lembra agora e esta skill.

**Checar o dia ANTES de mostrar** (nunca assumir — [[feedback_verificar_dia_semana]]):
```bash
date "+%u %A %d/%m/%Y"   # 1 = segunda
```
**So se for segunda (1)**, achar os conselhos com grupo vinculado:
```sql
SELECT p.id, p.nome, g.group_name
FROM projects p
JOIN project_whatsapp_groups g ON g.project_id = p.id AND g.ativo
WHERE p.empresa_id IS NOT NULL AND p.status = 'ativo'
ORDER BY p.nome;
```
Contar os itens **pelo endpoint**, nao por SQL: a matriz vive em DOIS bancos (o `raci_itens` do INTEL tem zero linhas nos conselhos — todas vem do ConselhoOS), e contar so o lado INTEL diria "0 itens" pra Vallen, que tem 61. O endpoint e publico e ja soma as fontes:
```bash
for p in <ids>; do curl -s "https://intel.almeida-prado.com/api/projects/$p/raci" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('resumo') or {}; \
print(sum(v for k,v in r.items() if k!='concluido'))"; done
```
**Omitir conselho sem item em aberto** (zero-state nao vira linha — regra do cockpit). Ex. medido em 29/07: Vallen 11 abertos, Alba 8, **Despertar 0** (o RACI dele foi zerado a pedido do Renato) → so Vallen e Alba entram. Mostrar assim:

```
## 📋 Segunda — mandar o RACI dos conselhos
- Vallen Clinic → grupo Conselho Vallen: https://intel.almeida-prado.com/projetos/24/raci
- Alba Consultoria → grupo Conselho Consultivo ALBA: .../projetos/26/raci
[so os que tem grupo vinculado]
_Abrir, revisar a matriz, "Enviar no grupo" (o texto e editavel antes de sair)._
```

**Nao disparar nada** — a CoS lembra e da o link; quem manda e o Renato, pela pagina. **A Governanca Jabo NAO entra aqui**: o preview dela por WhatsApp continua ligado (segunda 8h BRT), porque o RACI do Jabo sao as *tasks* do #28 e a pagina nao as le (zero `raci_itens`). Se um dia a matriz do #28 passar a ler as tasks, este bloco absorve o Jabo e o preview morre tambem.

### 4. Apresentar cockpit — secoes vazias SOMEM

```
## 🎯 Hoje ([DATA])
[compromissos de HOJE + decisoes com gate = HOJE]

## 🚨 Atencao
[APENAS itens > 0: feedback WA, memos drift, tasks overdue, custo anomalo]

## 🔄 Parecem resolvidas — fecho? (check F, so se houver)
[id · titulo · **canal que casou (WA/e-mail/nota)** · evidencia de 1 linha — Renato confirma]
[F4 vem rotulada a parte: "o retorno chegou" — task `on_hold` cuja espera acabou]

## 📎 Chegou no WA/email e nao virou nada — vira task/evento? (check G, so se houver)
[contato · 1 linha do que pediu · anexo? — Renato confirma ou ignora (o ato calibra)]

## 📋 Segunda — mandar o RACI dos conselhos (check H, SO na segunda)
[projeto · grupo · link direto pra pagina do RACI — ele revisa e dispara de la]

## 🤔 Proposta pra sessao
[1-2 frentes: compromisso proximo, ou algo do cos_status]
```

Nao mostrar: "amanha", decisoes futuras (so gate HOJE), zero-states. **Excecao:** se o user pediu review estrategica/planejamento de semana, ai traz futuro.

### 5. Aguardar direcao do user

NAO comecar a implementar antes do user responder. `/cos` apresenta, nao decide.

## Principio: "ele ja fez isso?" tem UMA fonte

Antes de oferecer QUALQUER coisa — rascunho, task, cobranca, item de check F/G,
proposta pra sessao — pergunte a `acao_do_renato`:

```sql
SELECT canal, o_que, quando, left(evidencia,120) FROM acao_do_renato
WHERE pessoa_id = <id> AND quando > '<data do pedido>' ORDER BY quando DESC LIMIT 5;
```

Ela une o que ele fez DIRETO em qualquer canal: WhatsApp e e-mail enviados,
mensagem no grupo, **e RSVP de convite** — este ultimo nao era lido por consulta
nenhuma antes de 06/08. Nao decide nada: mostra o ato, com data e evidencia; o
julgamento de se aquilo cumpre o pedido continua seu.

### O outro lado: rascunho pendente NAO e e-mail enviado

`acao_do_renato` so mostra o rascunho que **virou envio** (`status='sent'`). O
inverso — o rascunho criado e que **ninguem mandou** — tem tabela (`email_drafts`,
061), funcao (`pending_drafts`) e endpoint (`GET /api/admin/email-drafts/pending`)
desde 06/08. Sem ler isto, "esta pronto no rascunho" e *suposicao* — foi o que
produziu o caso de 04/08 (e-mail ao pai relatado como pronto no rascunho quando
ele ja tinha enviado as 16h29).

🚨 **CRIE O RASCUNHO PELO PROXY DO INTEL, NAO PELO MCP DO GMAIL.** Este e o passo
que faz o resto existir. Medido em 08/08: o cano funciona ponta a ponta (registra,
lista, e reconcilia casando por `thread_id` mesmo com o message id trocado) e a
tabela esta **VAZIA** — zero linhas em dois dias. Nao e bug do cano: e que o
rascunho vinha nascendo pelo **MCP do Gmail**, que fala direto com o Google e
**nunca passa pelo INTEL**. Produtor fora do processo nao aparece em grep nenhum
([[feedback_consumidor_externo_invisivel_ao_grep]]) — e um loop cujo produtor real
esta fora dele nunca fecha, por mais correto que seja por dentro.

```bash
curl -s -X POST https://intel.almeida-prado.com/api/admin/gmail-proxy \
  -H "X-API-Key: $INTEL_API_KEY" -H "Content-Type: application/json" \
  -d '{"account":"professional","action":"create_draft",
       "params":{"to":"...","subject":"...","body":"...","contact_id":123,
                 "source":"cos_skill","thread_id":"<se for resposta>"}}'
```
Passe `contact_id` e `thread_id` sempre que souber: sem `contact_id` o rascunho
nao liga a pessoa nenhuma nos cruzamentos, e sem `thread_id` o Gmail abre thread
nova e a reconciliacao perde a ancora. O rascunho sai igual no Gmail dele — a
unica diferenca e que passa a existir para o sistema.

**Antes de afirmar QUALQUER coisa sobre um e-mail que a camada montou:**
```sql
SELECT id, subject, to_emails, source, criado_em, thread_id
FROM email_drafts WHERE status='pending' ORDER BY criado_em DESC LIMIT 20;
```
Como ler o resultado:
- **linha presente** = o rascunho existe e **NAO foi enviado**. Diga "esta no
  rascunho, falta voce mandar" — nunca "mandei" nem "foi enviado".
- **linha ausente** para uma thread que voce esperava = **tres** coisas possiveis,
  nao uma: nunca houve rascunho · ele ja foi reconciliado como enviado · ou o
  rascunho foi criado por fora do INTEL (MCP do Gmail) e o sistema nunca soube.
  Enquanto a tabela estiver vazia, o terceiro caso e o mais provavel. Confirme em
  `acao_do_renato` antes de afirmar; **ausencia nao prova envio**.
- Rascunho pendente **ha varios dias** e sinal pra oferecer ("segue parado no
  rascunho desde X — mando, reescrevo ou descarto?"), nao pra cobrar.

⚠️ **A ancora e `thread_id`, nunca o message id** — o Gmail troca o id da
mensagem ao enviar, e casar por ele falha calado (zero linhas, zero erro).

Existe porque em 06/08 foram **cinco falhas numa sessao so**, todas oferecendo o
que ele ja tinha feito — Michele, Orestes, parabens ao Marson, RSVP da Phisalia,
itens do check-G ja engajados. Nenhuma exigia dado novo. Ver
[[feedback_checar_acao_renato_antes_de_oferecer]].

⚠️ **Medicao honesta (06/08):** nos itens do check G de hoje ela nao suprimiu
nada — o gate `lastdir=incoming` ja cobria esses casos. Onde ela morde e no
**portao** (o agente propondo o que ele fez) e no **RSVP**, que era invisivel.
Nao infle o valor dela: consulte e siga.

## Principio: consultar antes de perguntar

Antes de perguntar algo que o SISTEMA sabe (composicao de grupo WA, estado de task, cargo de contato, se msg foi respondida), CONSULTAR primeiro (`messages`, `group_messages`, `tasks`, `project_notes`, `contacts`). So perguntar o que exige julgamento do Renato. Ver `feedback_cos_action_blindness`, `feedback_timeline_triage`, `feedback_vallen_grupo_inclui_aptus`.

## Principio: instrucao de portao volta em DEVOLUTIVA, nao em parede de texto

Quando o Renato cola instrucoes vindas da tela da tonIAH (blocos `PORTAO ① · #id
· frente` + `→ instrucao`), o retorno tem contrato fixo. **Nao e preferencia de
estilo — e o conserto de um problema medido** (08/08/2026, palavras dele): *"se
dou instrucoes para os 3 portoes, copio e colo no terminal, depois o terminal me
cospe um monte de texto, e fico perdido"*.

**No terminal, UMA LINHA POR PORTAO, na MESMA ordem e numeracao que ele mandou:**

```
① Richard × Edu Marson   entendi: remarcar o Richard p/ 6ª 14/08, mantendo o Edu
                         ✓ rascunho pronto
② Francine               entendi: voce ja alinhou — e pra encerrar, nao cobrar
                         ✓ baixado, task #999689 fechada
③ Curitiba 12/08         entendi: recusar e converter em pedido de apresentacao
                         ⚠ confirma o enquadramento antes

→ devolutiva: open ~/cockpit/devolutiva.html
```

A numeracao correspondente e o que dispensa reconstruir de memoria o que voltou
de qual pedido. **O `entendi:` vem ANTES do desfecho** de proposito: e o ponto de
correcao barato — se eu entendi errado, ele ve na hora, e nao depois de ler um
rascunho inteiro que nao era o que pediu.

**Todo o material longo vai pra devolutiva** (`scripts/devolutiva.py`, gera
`~/cockpit/devolutiva.html`): rascunhos com botao de copiar POR bloco, notas,
evidencia. Terminal nao e lugar de revisar texto de mensagem — com tres drafts
vira scroll, e foi ele quem propos a saida: *"seria interessante uma devolutiva
em html... resolvendo inclusive o copiar e colar (se tiverem 3 drafts em uma
tela) ou revisar"*.

**⛔ Nunca no terminal:** SQL, log, o caminho ate a resposta, ou o texto integral
de um rascunho. Rascunho vira draft/devolutiva — [[feedback_entrega_visual_html_local]].

## Quando NAO usar

- Sessoes one-shot ("rapido — me mostra X").
- Continuacao de sessao recente (<2h) — o papel ja esta marcado.
- Quando o user ja deu prompt detalhado com escopo claro.

## Erro comum

Nao confundir com `/init` (cria CLAUDE.md) nem com `/papel-cos` (triagem on-demand de pendencias). `/cos` LE state + marca papel, nao cria nem triaga.
