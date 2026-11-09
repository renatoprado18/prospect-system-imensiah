---
name: sys
description: Abertura de sessao no PAPEL Sys (dreno de modo degradado). Enquanto o canario de saldo Anthropic esta aceso, drena a fila de trabalho LLM diferido (llm_defer_jobs) respondendo os prompts pela assinatura em vez da API. Terceiro papel, ao lado de /cos e /dev; nao toca git/deploy/Neon-DDL. Pareado com /loop pra continuar drenando.
---

# Skill: /sys

Abertura de sessao no **papel Sys**. Terceiro papel, ao lado de `/cos` (executivo) e
`/dev` (codigo). Existe por um motivo so e tem prazo de validade:

> **Enquanto a conta Anthropic esta fora, as funcoes LLM do INTEL param. Esta sessao
> responde os prompts que elas deixaram de fazer.**

Em 28/10/26 o saldo zerou e o sistema passou **12 dias** com toda funcao LLM parada —
`task_reconciler` (41% dos fechamentos de task), triagem, visao, briefing, tonIAH.
Antes da migration 089 o julgamento que nao acontecia **nao deixava rastro**: cada
sitio de `messages.create` falhava por si e o efeito aparecia no board executivo
como "326 tasks atrasadas", sem nada apontando a causa.

## ⚠️ Leia isto antes de rodar

**Isto e MODO DEGRADADO, nao caminho normal.** Rotear a carga LLM de producao por
uma sessao de assinatura e um dreno enquanto a conta esta fora — nao e arquitetura.
A camada LLM inteira roda num baseline de ~US$ 76/mes, menos que a assinatura: nao
ha economia nenhuma em manter isto ligado, so divida tecnica.

**A acao nº 1 continua sendo recarregar o saldo em `console.anthropic.com`.** Quando
ele volta, `check_anthropic_credit_balance` apaga o flag e a fila **deixa de receber
trabalho sozinha** — ninguem precisa lembrar de desligar nada.

## Etapas

### 0. Marcar o papel + pintar a janela

**(a) Marcador** — pra o `/fim` saber que caminho fechar:

```bash
echo "sys" > "$SCRATCHPAD_DIR/session_role"   # $SCRATCHPAD_DIR = o Scratchpad Directory do system prompt
```

**(b) Cor de fundo = VERMELHO ESCURO `#3D0A0A`** (distingue da CoS roxa e da Dev grafite
— cor de "sistema em modo degradado", de proposito; vermelho pedido pelo Renato em
09/11/26, no lugar do ambar que nao lia como alarme). O badge `Sys` da statusline
(`~/.claude/statusline_papel.sh`) usa o mesmo vermelho:

```bash
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  osascript -e 'tell application "Terminal" to set background color of front window to {15677, 2570, 2570}'
else
  printf '\033]11;#3D0A0A\007'
fi
```

**(c) Registrar a frente** (quadro vivo do `session_locks.md`):

```bash
cd /Users/rap/prospect-system && python3 scripts/frentes.py claim sys "Dreno LLM diferido"
```

### 1. Espiar a fila — e LER OS DOIS NUMEROS JUNTOS

```bash
INTEL_API_KEY=$(grep -m1 '^INTEL_API_KEY=' /Users/rap/prospect-system/.env | cut -d= -f2- | tr -d '[:space:]')
curl -s -H "X-API-Key: $INTEL_API_KEY" https://intel.almeida-prado.com/api/sys/llm-jobs | python3 -m json.tool
```

🔴 **`pendentes: 0` tem DOIS significados opostos, e confundi-los e o modo de falha
desta skill:**

| `canario_aceso` | `pendentes` | Leitura | O que fazer |
|---|---|---|---|
| `false` | 0 | 🟢 **Saude.** A conta voltou, nada a diferir | **Encerrar o papel.** Avisar que o saldo voltou e sugerir `/dev` ou `/cos` |
| `true` | 0 | ⏳ **Normal na maior parte do dia** — ver o quadro abaixo | Conferir se o cron de hoje JA passou. Se passou e a fila esta vazia, o trabalho dele ja foi drenado |
| `true` | >0 | ▶️ **Trabalho esperando** | Drenar (etapa 2) |
| `false` | >0 | ⏭️ Sobra de episodio anterior | Drenar o que der; o resto expira em 12h sozinho |

🔴 **O PRODUTOR RODA 1×/DIA, e isto muda como se le a fila vazia.** O
`task-reconciler` e disparado por `CronTrigger(hour=15, minute=0)` UTC = **12h BRT**
(`workers/audio-transcriber/main.py:480`). Entao **fila vazia com canario aceso é o
estado normal nas outras ~23 horas** — nao e produtor parado.

A primeira versao desta tabela dizia "🔴 produtor nao esta enfileirando, investigar",
e estava errada: mandaria toda sessao `/sys` cacar um defeito inexistente, que e
exatamente como um alarme ensina a ser ignorado. **Fila vazia so e suspeita se o cron
de hoje JA passou e a fila nunca teve nada** — aí sim vale olhar `cron_runs` /
`/scheduler-status`.

**O que segue valendo:** nunca reporte "nada a fazer" sem ter olhado o
`canario_aceso`. As aberturas de 04/10 e 05/10 passaram verdes com 30h de LLM parado
porque nenhum verificador olhava LLM.

⚠️ **E A 089 COBRE 1 DAS 5 FUNCOES.** `llm_defer.enqueue` tem **um** sitio de
chamada: `task_reconciler.py:673`. Triagem, visao, briefing e tonIAH **nao
enfileiram** — seguem falhando cada uma por si, sem rastro, enquanto o canario
estiver aceso. Foi escopo declarado do primeiro corte (FEATURES 20.7), nao descuido;
mas **nao anuncie "o sistema esta drenado" com base numa fila vazia**: o que esta
coberto e o reconciler.

### 2. Drenar: pegar, responder, devolver

Pegue em lotes pequenos (3 por vez) pra nao estourar o contexto da janela:

```bash
curl -s -X POST -H "X-API-Key: $INTEL_API_KEY" \
  "https://intel.almeida-prado.com/api/sys/llm-jobs/claim?limit=3" | python3 -m json.tool
```

Cada job vem com `prompt`, `model`, `max_tokens` e `instrucoes`. Para cada um:

1. **Leia o prompt e responda como o modelo responderia** — so o JSON que ele pede,
   sem comentario, sem cerca de codigo, sem preambulo.
2. 🔴 **A CITACAO E CONFERIDA.** O prompt do reconciler exige
   `evidencia_trecho` **copiado literalmente** de uma das mensagens M1..Mn. O
   aplicador roda `_evidencia_confere` contra o texto exato que esta no prompt — se
   voce parafrasear, juntar pedacos de mensagens diferentes ou inventar, o veredito
   e **DESCARTADO** e nada fecha. Isso e a guarda funcionando, nao um erro seu; mas
   fazer de proposito seria fabricar prova.
3. **Na duvida, `done: false`.** A barra e 0.85 e o prompt diz isso com todas as
   letras. Fechar task errada custa mais que nao fechar.
4. Devolva:

```bash
curl -s -X POST -H "X-API-Key: $INTEL_API_KEY" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"answer": sys.stdin.read()}))' <<'JSON'
{"done": false, "confidence": 0.2, "reason": "..."}
JSON
)" "https://intel.almeida-prado.com/api/sys/llm-jobs/<ID>/answer" | python3 -m json.tool
```

A resposta do endpoint diz o que **de fato** aconteceu: `fechados`, `fecharia`
(kill-switch off), `sem_evidencia` (veredito descartado pela guarda). **Relate o que
o endpoint devolveu, nunca o que voce julgou** — o que vale e o efeito.

### 3. Repetir e, no fim, fechar com numeros

Voltar a etapa 1 ate `pendentes: 0`. Depois reportar, em 3 linhas: quantos jobs
respondidos · quantas tasks **fechadas de fato** · quantos vereditos descartados
pela guarda de citacao (se houver muitos, o problema esta nas suas respostas, nao
na guarda).

### 4. Manter drenando: **`/sys` sozinho nao basta**

🔴 **Uma sessao nao age sozinha — ela precisa de um turno.** Aberta e parada, a `/sys`
drena uma vez e depois fica ali *parecendo de pe e sem fazer nada*, que e pior que
nada: le-se como coberto. O que faz o papel funcionar e o par com `/loop`:

```
intel sys loop        # /loop 4h /sys
```

⚠️ **A CADENCIA UTIL E A DO PRODUTOR, NAO A DO LOOP.** Com o cron rodando 1×/dia
(15h UTC), um `/loop 15m /sys` gira **~80 vezes vazio** entre uma run e outra —
achado da sessao `/sys` de 09/11, e foi por isso que o default virou **4h**. Uma
passada do `/sys` ja drena a fila INTEIRA (etapa 3 repete ate `pendentes: 0`), entao
o loop nao serve pra acelerar o dreno: serve so pra pegar a run do dia seguinte.

Quando o `canario_aceso` virar `false`, **encerrar o loop** e dizer que a conta
voltou.

## Limites do papel — o que a /sys NAO faz

- ❌ **Nao toca git, deploy nem Neon-DDL.** Esses sao da `/dev`. Se achar um bug no
  dreno, **deixe recado no `session_locks.md`** pra Dev — nao conserte aqui.
- ❌ **Nao escreve board.** Nem `project_cos_status.md` nem `project_dev_backlog.md`.
  O `/fim` do papel `sys` nao fecha board nenhum: solta a frente e relata.
- ❌ **Nao decide nada de negocio.** Responder o prompt do reconciler nao e opinar
  sobre a task: e dizer se as mensagens provam que ela foi feita.
- ❌ **Nao contorna kill-switch.** Se `task_reconciler_enabled` esta `off`, a resposta
  e julgada e relatada e **nada fecha** — o gate e do Renato.

## Quando NAO usar

- **Canario apagado** (conta com saldo). Nao ha fila; use `/dev` ou `/cos`.
- Sessao de codigo (use `/dev`) ou executiva (use `/cos`).

## Erro comum

Reportar "fila vazia, tudo em ordem" sem olhar o `canario_aceso`. Fila vazia com o
canario aceso significa que o **produtor** parou — e esse e um problema maior que o
que esta skill existe pra drenar.
