# RACI — fonte única: plano de migração

**Decisão do Renato (26/09/2026, reafirmada 05/10):** o RACI tem uma fonte só, e ela é
o INTEL. Este documento é o plano para chegar lá sem quebrar a ata do conselho.

**Tudo aqui foi medido em produção em 05/10/2026.** Onde houver número, ele veio de
query, não de estimativa. Onde não houver, está dito que falta medir.

---

## 1. O que está errado hoje, em números

`get_matrix` (`app/services/raci_matrix.py`) **une as duas fontes na leitura**. O
resultado para o cliente:

| projeto | INTEL | ConselhoOS | abertos exibidos | itens reais | inflação |
|---|---|---|---|---|---|
| 24 Vallen Clinic | 25 | 80 | **39** | ~23 | **+70%** |
| 26 Alba Consultoria | 18 | 27 | **30** | ~18 | maior |

A duplicação é invisível a olho porque **as duas bases usam vocabulário diferente para
a mesma pessoa**: o ConselhoOS escreve "a Gestora" e "a Aptus", o INTEL escreve
"Jéssica" e "Lara".

Consequências que já aconteceram:

- `POST /api/projects/{id}/raci/send-to-group` publica no **grupo de WhatsApp do
  cliente**. Com a duplicação, manda a mesma linha duas vezes ao Conselho Vallen.
  _(Mitigado em 05/10 por um 409 — ver §6, "o que já está no ar".)_
- Sem `CONSELHOOS_DATABASE_URL`, a falha graciosa **engole 4 frentes** exclusivas do
  ConselhoOS, entre elas a **Modelagem Financeira FOCO 1**, insumo do gate de 30/10.
  A RACI de 21/09 saiu por esse caminho e ninguém notou.
- Atualizar um item exige achar o par **a mão** para não atualizar só metade. Em
  05/10 a sessão CoS fez isso em 7 itens; em 2 itens da Alba as cópias estão com
  status divergente **agora**, o que é a prova de que o trabalho manual falha.

---

## 2. A causa-raiz, e ela não é o que o board dizia

### 2.1 O produtor é transcrição manual — não existe cron copiando

| projeto | origem dos itens INTEL |
|---|---|
| 24 Vallen | **25 de 25 `origem='manual'`**, criados em 5 datas (10/08, 31/08, 10/09, 21/09, 25/09) |
| 26 Alba | 12 `raci_grupo_04set` + 6 `ata_alba_07_08` |
| 28 Exportação | 10 `origem='cos'` — ⚠️ **não é ConselhoOS**: o projeto 28 não tem `conselhoos_empresa_id`. Não são duplicatas. |

Nenhum código copia ConselhoOS → `raci_itens` do INTEL.

### 2.2 E a transcrição manual existe por um motivo legítimo

O `conselhoos_raci_sync.sync_raci_to_tasks()` cria **TASKS** a partir do RACI do
ConselhoOS — e tem uma guarda deliberada de 07/06/2026
(`conselhoos_raci_sync.py:328`):

```python
# CoS guard 07/06/2026: skip se Renato NAO esta entre os R.
# Bug original: criava task pra ele mesmo quando outro era R,
# inflando o queue com 30+ itens alheios (Sandra, Amadeo, Thalita...).
# RACI continua visivel no ConselhoOS; so nao polui INTEL pessoal.
```

Medido no cron de hoje: **`skipped_not_renato = 535`**.

**É aqui que o círculo fecha.** O único caminho automatizado para o INTEL é
_task-shaped e Renato-shaped_: por desenho ele recusa o item de que a Sandra, o
Amadeo ou a Thalita são responsáveis. Mas o RACI precisa justamente **de todos**.
Então alguém transcreveu à mão o que o sync se recusava a trazer — e a duplicação
nasceu daí. **A guarda está certa para task e errada para RACI**, e a confusão entre
as duas coisas é o defeito.

### 2.3 O elo não pareia nada, e três enunciados anteriores estavam errados

| enunciado | estado real (medido) |
|---|---|
| _"o elo `intel_task_id` existe e está vazio"_ (board) | errado |
| _"32% de um lado, 0% do outro"_ (prompt CoS, 05/10) | incompleto |
| **a query do ConselhoOS não selecionava `intel_task_id`** | ✅ era isto — o elo era **descartado na leitura**. Corrigido em `5263b43` |

E, mesmo visível, **o elo pareia 0 pares**: ele liga ConselhoOS → **task**, não
ConselhoOS → cópia-INTEL. Logo, "popular `raci_itens.task_id` no lado INTEL" **não é
o primeiro passo** — exigiria resolver o mesmo pareamento que deveria habilitar.

Estado do elo hoje (Vallen): ConselhoOS **35** de 80 · INTEL **0** de 25. Dos 35,
**só 25 tasks ainda existem** — 10 apontam para task apagada (ver §5, dívida).

---

## 3. A correção de escopo: são DOIS FATOS, não duas cópias

"O RACI mora no INTEL" na forma literal quebra coisa que funciona. O ConselhoOS **não
é só empresas**: são 14 tabelas, `empresas → reuniões → (raci_itens, decisões, temas,
pautas)`, e **100% dos 122 `raci_itens` têm `reuniao_id`**. O app
(`~/conselhoOS`) renderiza:

- `src/app/empresas/[id]/reunioes/[reuniaoId]/page.tsx` → `<RaciPanel>`, **o RACI é
  uma aba da reunião**, com contador;
- `src/app/empresas/[id]/page.tsx` e `src/app/dashboard/page.tsx` → leem o RACI;
- `src/app/api/reunioes/[reuniaoId]/ata/send-email/route.ts` → anexa
  `raciSheetDriveId`: **o RACI sai como planilha junto com a ata para o conselho**.

Mover a tabela apaga três telas e o anexo da ata. E há a razão que não é técnica: é a
deliberação de um colegiado de uma empresa **que não é do Renato** — a mesma razão
pela qual `delete_item` já é INTEL-only de propósito.

**Então a separação é por NATUREZA do fato:**

| | o fato | onde mora | muda? |
|---|---|---|---|
| **Ata** | "a reunião de 09/09 deliberou X, sob Y" | ConselhoOS, preso a `reuniao_id` | não — é registro histórico |
| **Execução** | "X está aberto, com Y, vence em Z" | **INTEL, por projeto — FONTE ÚNICA** | sim, todo dia |

Ligados por um **ponteiro**, nunca por cópia. O padrão já existe no repo:
`tasks.conselhoos_raci_id` (43 de 1.421 tasks).

---

## 4. O plano, em 6 passos

O mapa `empresa → projeto` é **1:1** em todas as 4 empresas vinculadas (Vallen→24,
Alba→26, Despertar→25, AP Conselhos→36), então não há ambiguidade sobre onde o item
nasce. `_find_or_create_project` já resolve isso.

### Passo 1 — DDL: o ponteiro (reversível)

```sql
-- migration 084
ALTER TABLE raci_itens ADD COLUMN IF NOT EXISTS conselhoos_raci_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS idx_raci_itens_cos_id
    ON raci_itens(conselhoos_raci_id) WHERE conselhoos_raci_id IS NOT NULL;
```

O índice **único parcial** é o que torna a importação idempotente e impede que uma
segunda rodada do sync crie a segunda cópia. Reversão: `DROP COLUMN`.

### Passo 2 — o sync passa a criar `raci_item`, SEM o filtro de Renato

Em `conselhoos_raci_sync.py`, um método novo `sync_raci_to_intel_raci()`:

- lê todos os `raci_itens` do ConselhoOS da empresa;
- faz `INSERT ... ON CONFLICT (conselhoos_raci_id) DO UPDATE` no `raci_itens` do
  INTEL, com `origem='conselhoos'` e `conselhoos_raci_id` setado **no nascimento**;
- **não aplica a guarda `skipped_not_renato`** — ela continua valendo para
  `sync_raci_to_tasks`, que é outra coisa. ⚠️ Esta é a mudança que resolve a
  causa-raiz: trazer os 535 itens de terceiros que hoje são transcritos à mão.
- escreve de volta no ConselhoOS? **Não.** O ponteiro novo mora no INTEL; o
  `intel_task_id` do lado COS continua servindo ao fluxo de task.

Com isso o elo nasce **100% preenchido** e o pareamento deixa de existir como
problema — não há duas linhas para parear.

### Passo 3 — o pareamento único (passo HUMANO, não automatizável)

Antes de ligar o passo 2 em produção, as linhas manuais que já existem precisam
receber o `conselhoos_raci_id` do gêmeo, **senão a importação cria a segunda cópia de
cada uma**.

- **~17 pares na Vallen e ~12 na Alba.** A detecção por texto normalizado
  (`_chave_dedup`, já no ar) resolve **9 na Vallen e 10 na Alba** automaticamente.
- O **resíduo (~8 + ~2)** é o de vocabulário divergente e **exige olho humano**.
- ⛔ **Não deduplicar por similaridade.** Medido no próprio conjunto: "Regra de
  repasse da **Dra. Daniela**" × "Acordo de repasse da **Dra. Sayonê**" dá **0.58** —
  duas médicas, dois contratos. Qualquer corte que pegue "a Gestora" × "Jéssica"
  passa por cima desse 0.58 e **funde contrato de cliente**.
- Entregável: um script `scripts/raci_parear.py` que **propõe** os pares (os exatos
  já casados, o resíduo ordenado por similaridade apenas para ORDENAR a revisão) e
  grava só o que for confirmado. `--dry-run` por default.

Os **3 itens só-INTEL** da Vallen (esteticista, Dra. Sayonê, Dra. Camila) **não têm
par e não devem ter**: são execução que nunca passou por conselho. Ficam como
`origem='manual'`, sem ponteiro — e isso é o estado correto, não um resíduo.

### Passo 4 — `get_matrix` lê só o INTEL

Em `raci_matrix.py`, `_fetch_conselhoos` sai do caminho de leitura. O que permanece:

- `duplicatas` e a guarda do 409 **ficam**, como rede. Depois do passo 3 elas devem
  acusar **zero** — e se acusarem algo, é sinal de que o passo 2 regrediu.
- **O front se resolve sozinho, e isso é verificável** (`rap_projeto_raci.html`):
  `temConselho` (linha 496) já é derivado de `dados.fontes`, então a legenda
  _"Itens marcados **ConselhoOS** gravam no ConselhoOS (não aqui)… remover item de
  conselho só de lá"_ **desaparece** quando a fonte sai, e o selo por linha (555)
  deixa de ser renderizado. Nada a editar na tela — **mas confirmar visualmente**,
  porque legenda que sobrevive à mudança vira instrução errada numa tela que grava
  em banco de cliente.
- ⚠️ **Escrita:** `update_item` hoje é write-through (`_update_conselhoos` quando o
  uid é `conselhoos:*`). Com fonte única, editar execução grava no INTEL; o que **não
  pode** acontecer é a ata do ConselhoOS passar a ser reescrita pelo INTEL.

### Passo 5 — validar pelo EFEITO, nos dois sistemas

```bash
# INTEL: a inflação acabou
curl -s https://intel.almeida-prado.com/api/projects/24/raci | python3 -c "
import json,sys; d=json.load(sys.stdin)
ab=[i for i in d['itens'] if i.get('status_efetivo')!='concluido']
print('abertos:', len(ab), '— esperado ~23, era 39')
print('duplicatas:', d['duplicatas_total'], '— esperado 0')"
```

E, no ConselhoOS, **abrir a aba RACI de uma reunião da Vallen e confirmar que ela
continua populada**. Este segundo check não é formalidade: é o que distingue "unifiquei
a execução" de "quebrei a ata do cliente".

### Passo 6 — só então religar o `send-to-group`

Validar contra **grupo de teste**, nunca contra o Conselho Vallen.

---

## 5. Dívida que este plano NÃO resolve (e é melhor dizer)

- **10 `intel_task_id` órfãos** na Vallen apontam para task que não existe mais. A
  importação precisa tolerá-los sem quebrar — e alguém precisa decidir se são para
  limpar.
- **`raci_itens.task_id` do INTEL fica 0%.** O ponteiro novo é
  `conselhoos_raci_id`; o `task_id` continua sendo outra coisa (elo para a task de
  execução do Renato) e segue vazio. Não confundir os dois.
- **`wa_attachments.extraction_cost_usd` e `raci_itens.task_id`** são, hoje, colunas
  com escritor e sem leitor. Não preencher por preencher.

## 6. O que já está no ar (05/10, `5263b43` + `89ca08e`)

Não faz parte da migração — é a rede que segura até ela acontecer:

- `send-to-group` devolve **409** quando há duplicata, citando quantas e quantas estão
  com status divergente. Override explícito: `confirmar_duplicatas=true`. **Provado em
  prod** com `group_jid` falso (409 antes do 404 do grupo).
- `create_item` recusa criar o que já existe no ConselhoOS (`permitir_duplicata=true`
  libera). **As duas abstenções são opostas de propósito:** no envio, não poder checar
  **bloqueia**; na criação, **libera** — travar o trabalho do dia porque um banco que
  não é nosso caiu seria pior.
- O resumo voltou a mostrar movimento: `movimento.rotulo` = _"30 atrasados, dos quais
  17 com movimento"_. Antes, `em_andamento` era **estruturalmente 0** porque
  `status_efetivo` converte todo vencido em `atrasado`.
- `intel_task_id` passou a ser lido na matriz.

## 7. A pergunta que é do Renato, não do código

**Os 40 registros em `pessoas` do ConselhoOS acessam as telas?** Não há tabela de
`users`/`sessions` no banco do ConselhoOS (14 tabelas, nenhuma de auth), o que sugere
operação de um só usuário — mas `pessoas` tem `email`, `cargo`, `papel` e `ativo`, e a
ata sai por e-mail com a planilha do RACI anexa.

**Se conselheiro ou cliente abre aquele painel**, a aba de RACI da reunião tem de
continuar servindo — o que este plano preserva, e o literal ("mover a tabela") não.
Se for operação só do Renato, há uma simplificação possível no passo 4 que não vale a
pena desenhar antes da resposta.

---

**Relacionados:** `app/services/raci_matrix.py` · `app/services/conselhoos_raci_sync.py` ·
`docs/CONSELHOOS_SYNC_TASK_QUEUE.md` · `docs/CONSELHOOS_ANALISE.md` ·
`tests/test_raci_duas_fontes.py` · memória `feedback_intel_conselhoos_sync_lacuna`
