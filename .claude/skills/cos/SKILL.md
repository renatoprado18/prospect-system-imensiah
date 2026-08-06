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
SELECT SUM(amount_usd) FROM platform_costs WHERE data_referencia = CURRENT_DATE - 1
```
So alertar se acima do baseline.

**F. Reconciliacao conversa×tasks (PROTECAO anti cos_action_blindness):**
O sistema so reconcilia quando o BOT age; quando o Renato age direto no WA/e-mail ou uma nota e criada, a task fica aberta = drift. Cruzar tasks abertas × atividade real (~96h):
```sql
-- (F1) task ABERTA cujo TERCEIRO teve atividade recente — WhatsApp E E-MAIL.
--
-- Ate 06/08 este cruzamento era `JOIN messages m ON m.contact_id = t.contact_id`
-- e so via WhatsApp na pratica. Tres furos medidos no caso #999695 ("FUP
-- Piccino", espera de joao@piccino.com.br):
--   (a) 81% dos e-mails tem `messages.contact_id` NULL (1.436 de 1.769) —
--       o e-mail JA esta na base desde jul/2025, o cruzamento e que nao o via;
--   (b) FICHA IRMA: a task aponta pra ficha #2869 e a thread esta na #2858, as
--       duas com o mesmo endereco (56 enderecos em >1 ficha, 133 fichas);
--   (c) task SEM `contact_id` (73 das 119 abertas) sumia inteira do gate.
-- Agora casa por IDENTIDADE: ficha + fichas irmas + endereco citado no texto da
-- task quando nao ha ficha. Em codigo o mesmo cano vive em
-- `services/task_reconciler.py` (funcoes `_task_scope`/`_scope_where`), que usa
-- os primitivos de `services/contact_identity.py` — NAO reescrever `ILIKE '%x%'`.
WITH aberta AS (   -- `on_hold` entra: task parqueada cujo terceiro respondeu e o caso classico da bola que voltou
  SELECT id, titulo, status, contact_id,
         coalesce(titulo,'') || ' ' || coalesce(descricao,'') AS txt
  FROM tasks WHERE status IN ('pending','on_hold')
),
dono AS (   -- e-mail do proprio Renato nunca e "o terceiro respondeu" (a maquina ouvindo o proprio eco)
  SELECT unnest(ARRAY['renato@almeida-prado.com','renato.almeida.prado@gmail.com']) em
),
ident AS (
  SELECT a.id tid, lower(btrim(e->>'email')) em
    FROM aberta a JOIN contacts c ON c.id = a.contact_id
    CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(c.emails)='array' THEN c.emails ELSE '[]'::jsonb END) e
  UNION      -- sem ficha: o endereco que a propria descricao registrou ("E-mail ENVIADO ao Piccino (joao@piccino.com.br)")
  SELECT a.id, lower((regexp_matches(a.txt,
         '[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', 'g'))[1])
    FROM aberta a WHERE a.contact_id IS NULL
),
alvo AS (SELECT * FROM ident WHERE em <> '' AND em NOT IN (SELECT em FROM dono)),
fichas AS (   -- ficha da task + IRMAS (mesmo endereco em outra ficha)
  SELECT a.id tid, a.contact_id cid FROM aberta a WHERE a.contact_id IS NOT NULL
  UNION
  SELECT al.tid, c.id FROM alvo al JOIN contacts c ON EXISTS (
    SELECT 1 FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(c.emails)='array' THEN c.emails ELSE '[]'::jsonb END) e
     WHERE lower(btrim(e->>'email')) = al.em)
),
ev AS (   -- `from` vem ora puro ora "Nome <endereco>", e `to` ora array ora string: a regex normaliza os tres
  SELECT m.contact_id, m.direcao, COALESCE(m.enviado_em, m.recebido_em, m.criado_em) ts,
         COALESCE(cv.canal,'whatsapp') canal,
         ARRAY(SELECT DISTINCT lower(x[1]) FROM regexp_matches(coalesce(m.metadata->>'from',''),
               '[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', 'g') x) frm,
         ARRAY(SELECT DISTINCT lower(x[1]) FROM regexp_matches(coalesce(m.metadata->>'to',''),
               '[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', 'g') x) too
  FROM messages m LEFT JOIN conversations cv ON cv.id = m.conversation_id
  WHERE COALESCE(m.enviado_em, m.recebido_em, m.criado_em) > NOW() - INTERVAL '96 hours'
)
SELECT a.id, a.status, a.titulo,
       string_agg(DISTINCT coalesce(ct.nome, e.frm[1]), ', ') quem,
       max(e.ts) ultima,
       string_agg(DISTINCT e.canal||'/'||e.direcao, ',') sinais   -- 'email/incoming' = a bola VOLTOU por e-mail
FROM aberta a
JOIN ev e ON (
     EXISTS (SELECT 1 FROM fichas f WHERE f.tid = a.id AND f.cid = e.contact_id)
     -- incoming casa pelo `from`, outgoing pelo `to`: sem essa separacao um
     -- e-mail de OUTRA pessoa com o contato em copia contaria como resposta dele
  OR (e.canal='email' AND EXISTS (SELECT 1 FROM alvo al WHERE al.tid = a.id
        AND (CASE WHEN e.direcao='incoming' THEN e.frm ELSE e.too END) && ARRAY[al.em])))
LEFT JOIN contacts ct ON ct.id = e.contact_id
GROUP BY a.id, a.status, a.titulo
ORDER BY ultima DESC;
-- (F2) task pending cujo PROJETO teve nota nova recente
SELECT t.id, t.titulo, t.project_id, max(pn.criado_em) ultima
FROM tasks t JOIN project_notes pn ON pn.project_id=t.project_id
WHERE t.status='pending' AND pn.criado_em > NOW() - INTERVAL '96 hours'
GROUP BY t.id, t.titulo, t.project_id ORDER BY ultima DESC;
```
Calibracao (13/07): **verbo de acao** (FUP/contatar/planejar/revisar/enviar) → o entregavel E A ACAO; se a acao foi feita (outgoing do Renato, nota criada, gate batido) = **RESOLVIDA, procurar a evidencia ativamente e fechar** (NAO devolver pro Renato quando a evidencia existe — [[feedback_nao_perguntar_age]]). **"Aguardar X retornar"** so fecha com `incoming` do terceiro. Ignorar tasks dev/backlog. So exibir se houver candidatas; NAO fechar auto — Renato confirma.

Como ler as colunas novas (06/08):
- **`sinais` com `email/incoming`** = a bola voltou POR E-MAIL. Era o caso invisivel: o #999695 esperava resposta do Piccino e nenhum check a alcancava.
- **`status='on_hold'`** so interessa quando ha `incoming`: parqueada sem resposta e espera em curso, nao candidata a fechar. O `sweep_on_hold` do `task_reconciler` ja a reabre sozinho quando o terceiro responde (por WhatsApp OU e-mail) — se ela aparecer aqui com `incoming`, e porque a reconciliacao ainda nao rodou no dia.
- **`quem` com mais de um nome** = a task cita mais de uma pessoa (a #999735 cita o Nick e a Fran em copia). Conferir se quem respondeu foi de fato **o esperado** antes de propor fechar.
- Task alcancada so pelo endereco no texto e task com **dado faltando**: aproveitar e **linkar o `contact_id`** ([[feedback_check_g_task_link_convention]]). Medido 06/08: 72 das 119 abertas nao tem ficha NEM endereco no texto — para essas, este check continua cego.

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
)
SELECT c.contact_id, ct.nome, max(c.criado_em) ultima, bool_or(c.has_doc) tem_doc,
       max(c.fname) FILTER (WHERE c.has_doc) anexo,
       bool_and(c.ack) so_ack,   -- true = tudo que ele mandou foi "ok/feito/obrigado": REBAIXE (nao suma)
       (array_agg(c.conteudo ORDER BY (c.has_doc)::int DESC, c.criado_em DESC))[1] amostra
FROM cand c LEFT JOIN contacts ct ON ct.id=c.contact_id
GROUP BY c.contact_id, ct.nome
ORDER BY bool_and(c.ack) ASC,    -- o que PEDE algo primeiro; encerramento no fim
         bool_or(c.has_doc) DESC, ultima DESC;
```
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

⚠️ **Ainda session-bound e ainda sem ledger.** Se ele nao abrir `/cos` por 5
dias, o inbound daqueles dias evapora sem nunca ter sido avaliado — a janela e
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

## 🔄 Parecem resolvidas no WA/nota — fecho? (check F, so se houver)
[id · titulo · evidencia de 1 linha — Renato confirma]

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

## Principio: consultar antes de perguntar

Antes de perguntar algo que o SISTEMA sabe (composicao de grupo WA, estado de task, cargo de contato, se msg foi respondida), CONSULTAR primeiro (`messages`, `group_messages`, `tasks`, `project_notes`, `contacts`). So perguntar o que exige julgamento do Renato. Ver `feedback_cos_action_blindness`, `feedback_timeline_triage`, `feedback_vallen_grupo_inclui_aptus`.

## Quando NAO usar

- Sessoes one-shot ("rapido — me mostra X").
- Continuacao de sessao recente (<2h) — o papel ja esta marcado.
- Quando o user ja deu prompt detalhado com escopo claro.

## Erro comum

Nao confundir com `/init` (cria CLAUDE.md) nem com `/papel-cos` (triagem on-demand de pendencias). `/cos` LE state + marca papel, nao cria nem triaga.
