---
name: dev
description: Abertura de sessao no PAPEL Dev (codigo). Marca a sessao como dev, le o roadmap dev + topo do executivo + session_locks, checa saude tecnica (git/deploy/health), pega os locks e propoe frente tecnica. Par de /cos; ambos fechados por /fim.
---

# Skill: /dev

Abertura estruturada de sessao no **papel Dev (codigo)**. Espelho tecnico do `/cos`. Roda no INICIO da sessao pra retomar de onde a ultima parou. O `/fim` le o marcador e fecha o board certo.

**Papel Dev:** dona de **git-tree + deploy + Neon-DDL**. **So a Dev pusha `main`** (Railway auto-deploy). Dono unico por recurso; coordena com a sessao CoS via `session_locks.md` + `feedback_parallel_sessions`.

## Etapas (executar em ordem)

### 0. Marcar o papel + pintar a janela (PRIMEIRO, sempre)

**(a) Marcador** — gravar o papel no scratchpad DESTA sessao pra o `/fim` saber que caminho fechar. O diretorio esta no system prompt ("Scratchpad Directory"):

```bash
echo "dev" > "$SCRATCHPAD_DIR/session_role"   # $SCRATCHPAD_DIR = o Scratchpad Directory do system prompt
```
Sobrescrever se ja existir com outro valor.

**(b) Cor de fundo = GRAFITE `#16161A`** (distingue a janela Dev da CoS, que e roxo). Terminal.app usa AppleScript; outros terminais usam OSC 11:

```bash
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  osascript -e 'tell application "Terminal" to set background color of front window to {5654, 5654, 6682}'
else
  printf '\033]11;#16161A\007'
fi
```

### 1. Ler os boards — roadmap dev INTEIRO + topo do executivo + locks

```
Read /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/project_dev_backlog.md   (INTEIRO: Em curso + P1-P3 + Fechado dev + monitoramento tecnico)
Read /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/session_locks.md          (estado dos locks + Log)
```

Do `project_cos_status.md` ler **so o topo** (header + "Decisoes pendentes") — contexto executivo do que importa pro Renato, sem o historico executivo inteiro.

- `project_dev_backlog.md` = roadmap dev. **Dono de escrita = Dev.**
- `session_locks.md` = canal CoS⇄Dev. Ver o que a CoS esta com/tocando e ha quanto tempo.

### 2. Checar SAUDE TECNICA (antes de mexer em qualquer coisa)

Rodar em paralelo:

```bash
cd /Users/rap/prospect-system && git status -sb && git log --oneline -5   # working tree limpo? branch? ultimos commits
python3 /Users/rap/prospect-system/scripts/verifica_modelo.py --quiet      # o banco ainda bate com o contrato do modelo?
python3 /Users/rap/prospect-system/scripts/verifica_boards.py --quiet     # os boards que acabei de ler cabem numa leitura?
```

- **Working tree:** limpo? Se houver mudancas nao-commitadas, investigar de quem sao (outra sessao? — `git diff` antes de qualquer `git add`, ver `feedback_parallel_sessions`).
- **Conformidade do modelo:** o `verifica_modelo.py` compara o banco com o baseline declarado em `project_modelo_rede_entidades.md` (10 entidades, 3 grupos, 7 decisoes de 03/08). **Divergencia nao e erro — e sinal.** Tabela nova numa entidade, era morta recebendo escrita, total fora do registrado: ou foi decisao (e o memo precisa ser atualizado) ou alguem criou estrutura fora do modelo. **Levar pro bloco "Proximos passos" da Etapa 3.** Verde = seguir. Se o script FALHAR (memo mudou de formato), tratar como bloqueio: sem baseline nao ha verificacao, e um verificador calado certifica conformidade que nunca checou.
- **Teto dos boards:** o `verifica_boards.py` mede os arquivos que a Etapa 1 acabou de ler. **Acima do teto, a leitura foi TRUNCADA** — e a sessao segue decidindo sobre um pedaco do conteudo achando que viu tudo (em 30/07 o `/dev` leu 22% do backlog sem nenhum sinal). Ele sai com exit 1 de proposito: **consolidar vem ANTES de propor frente**, senao a frente e escolhida sobre memoria incompleta. Recado atendido desce pro `_historico.md`. Board de OUTRO papel acima do teto (ex.: a CoS com o `project_cos_status.md`) nao se reescreve — deixa recado no `session_locks`.
- **Deploy/health:** se a frente tocar prod, checar deploy Railway + health endpoint antes de mexer. Gate Railway / status dos crons se relevante (`/scheduler-status`, cron_runs).
- **Locks:** conferir no `session_locks.md` se git/deploy/neon estao livres. Se a CoS estiver com algum recurso, respeitar. Se um lock estiver idle >3h, pode retomar deixando nota no Log.

### 2b. Fila de delegacoes da Tonia (F-C C1)

A Tonia enfileira delegacoes de coding (comando do Renato no WhatsApp/chat: "investiga o bug X", "analisa o codigo Y") em `tonia_dev_delegations`. Antes do C1 o resultado so voltava pro WhatsApp e **morria ali** — a sessao Dev nunca via. Agora o `/dev` puxa as ACIONAVEIS:

```bash
INTEL_API_KEY=$(grep -m1 '^INTEL_API_KEY=' /Users/rap/prospect-system/.env | cut -d= -f2- | tr -d '[:space:]')
curl -s -H "X-API-Key: $INTEL_API_KEY" https://intel.almeida-prado.com/api/dev/delegations | python3 -m json.tool
```

Buckets (o endpoint ja ordena por prioridade de acao):
- **`needs_review`** — delegacao `edit` que terminou com diff (`has_changes=true`, tem `branch`). Trabalho pronto esperando a **dona do merge**: revisar o diff → mergear ou descartar. **Prioridade 1.**
- **`failed`** — `error`/`timeout` nos ultimos 14d. Investigar a causa.
- **`in_progress`** — `queued`/`running`. So informar que ha trabalho rolando (nao agir).

Levar as `needs_review` + `failed` pro bloco "Proximos passos" da Etapa 3. Endpoint le Neon prod read-only (INTEL nao escreve em `tonia_*`). Resposta com `note: indisponivel` ou lista vazia = sem delegacoes / tabela ausente no alvo — seguir normal.

### 2c. Propostas do Playbook Andressa (regras Jabo aguardando aprovacao)

Reunioes Fathom do projeto Jabo #28 disparam extracao de regras operacionais duraveis (feature `PLAYBOOK_AUTO_EXTRACT`, ligada) → cada regra vira uma PROPOSTA que o Renato aprova pelo WhatsApp ("aprovo playbook") ou pela UI. O `/dev` mostra as pendentes so pra VISIBILIDADE (a sessao Dev NAO aplica — o gate e do Renato):

```bash
curl -s https://intel.almeida-prado.com/api/playbook/proposals | python3 -m json.tool
```

`{"pending": [...]}` — cada item e um `project_note` com `id` (o **PLAYBOOK-N**), `titulo`, `metadata.rules` (as regras extraidas). Lista vazia = nada esperando. **Se houver pendentes ha varios dias**, mencionar no bloco "Proximos passos" (o Renato pode ter esquecido de aprovar — o merge no Google Doc so acontece com o "aprovo playbook" dele). Nunca aplicar pela sessao Dev.

### 3. Abrir com "ONDE PARAMOS + PROXIMOS PASSOS" (a dor real do Renato)

ANTES de propor frente, mostrar 2 blocos curtos:

```
## 📍 Onde paramos
[2-3 linhas: header do dev_backlog (ultima atualizacao) + o item de "Em curso" + estado dos locks/deploy. O essencial, nao o board inteiro.]

## ▶️ Proximos passos (dev)
[Bloqueantes tecnicos que a ultima sessao deixou: gate pendente (ex: passo 5 cutover), P1 aberto, shadow aguardando flip de flag + data. + delegacoes acionaveis da fila da Tonia (needs_review/failed do passo 2b).]
```

### 4. Pegar os locks e logar no session_locks

Se a frente da sessao vai tocar git/deploy/Neon-DDL, **pegar o lock** ANTES de comecar: editar `session_locks.md` — marcar o recurso como **Dev**, com "desde" (data/hora) e a frente. Adicionar 1 linha no Log ("Dev pegou git/deploy pra frente X").

Ver o `DB_TARGET protocol` (`reference_db_target_protocol`): alvo declarado, nunca deduzido. Prod da maquina local = `DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1`.

### 5. Propor frente tecnica + aguardar direcao

Propor 1-2 frentes baseadas em "Em curso" + P1 do backlog. NAO comecar a implementar antes do user confirmar a frente (a menos que o user ja tenha dado escopo claro no prompt de abertura).

## Regras de coordenacao (feedback_parallel_sessions)

- **So a Dev pusha `main`.** Se a CoS precisar de um commit, a Dev faz.
- **Dono unico por recurso.** git-tree, deploy, neon-ddl: um dono por vez, declarado no `session_locks`.
- **git diff antes de git add** — outra sessao pode ter mexido no repo.
- **Push:** `--no-verify` quando o pre-push hook (sync tasks/notes/milestones) atrapalhar; `.python-version=3.12` senao Railpack usa 3.13 e pydantic quebra (ver `reference_railway_projects`).

## Quando NAO usar

- Sessoes one-shot ("rapido — roda esse script").
- Continuacao de sessao dev recente (<2h) — papel ja marcado, locks ja pegos.
- Tarefa puramente executiva (use `/cos`).

## Erro comum

Nao confundir com `/cos` (executivo, nao toca repo). `/dev` e dona do codigo/deploy/Neon-DDL e coordena locks.
