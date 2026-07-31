# Camada CoS como agente — executor local

Substitui, para as frentes que se mexeram, o julgamento por **prompt único sobre
janela fixa** por um **agente que decide o que pesquisar**. Roda no Mac do
Renato, via `launchd`, no Claude Code (assinatura Max) — custo de API zero.

## Por que existe

A camada antiga monta um pacote fixo de contexto: 21 dias de DM, 40 mensagens,
5 memórias, 2500 chars por nota, 12 dias de grupo. Sete cortes, cada um um lugar
onde a resposta pode estar uma linha adiante.

O caso que decidiu (30/07): a data de início das aulas da FAAP estava **40
caracteres depois** do corte de nota. A camada relatou, honestamente, *"dado
incompleto, a data não pode ser afirmada"* — sobre informação que existia no
mesmo campo, quatro palavras à frente. Um teste com três agentes de verdade
(frentes #38, #52, #47) achou o dado, recusou repetir uma alucinação que a
versão fixa cometera, e não regrediu no caso de controle.

## Quem pode o quê

| | lê | escreve |
|---|---|---|
| **agente** (`claude -p`, subprocesso) | tudo, via `cos_agent_ro` | **nada** — negado pelo Postgres |
| **runner** (`run.py`, determinístico) | — | só `cos_daily_review` e `cron_heartbeats` |

A credencial de escrita (`COS_OWNER_URL`) é removida do ambiente do subprocesso.
O agente roda com `Write`/`Edit` bloqueados e teto de turnos.

**Limite honesto:** numa máquina de usuário único não há isolamento total — um
agente determinado poderia procurar credencial em disco. Isto protege contra o
modo de falha REAL (o agente resolver "consertar" algo que achou), não contra um
agente hostil.

## Triagem

Só frentes com movimento vão ao agente; as paradas herdam o estado anterior.
Medido em 31/07: **17 de 31 frentes sem movimento em 26h**.

⚠️ **`tasks.atualizado_em` não é sinal de movimento.** O `daily-sync` carimba a
coluna em toda task que puxa. Medido: das 75 "movimentações" que a primeira
versão da triagem via, **4 eram reais** — 95% eco da máquina. O sinal honesto é
`atualizado_em > last_synced_at`, a mesma guarda que o `tasks_sync` usa.

## Se a máquina dormir

| | dorme | acorda pela rede |
|---|---|---|
| na tomada | **nunca** (`sleep 0`) | sim (`womp 1`) |
| na bateria | em **1 minuto** | não |

O `launchd` **não acorda** a máquina. Para o job disparar com ela dormindo:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 07:10:00
```

Sem isso, viajar com o laptop = dia sem julgamento.

## Como se sabe que não rodou

O runner bate ponto em `cron_heartbeats` (`job_id=cos-agent-local`,
`source=mac-local`) e o `monitor-cron-health`, que já roda de hora em hora no
servidor, acusa a ausência. **A detecção mora no servidor** — um vigia que mora
na máquina que caiu não vigia nada.

O **resultado** vai para o Neon: o tonIAH e a home leem de lá. O que é local é só
a execução, não o dado.

## Uso

```bash
set -a; . ~/.cos-agent/env; set +a

./run.py --dry-run        # mostra a triagem, não gasta agente nem grava
./run.py --frente 49      # uma frente (teste)
./run.py --limit 3        # teto de frentes na rodada
./run.py                  # rodada completa
```

## Instalar o agendamento

```bash
cp com.almeidaprado.cos-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.almeidaprado.cos-agent.plist
launchctl kickstart -k gui/$(id -u)/com.almeidaprado.cos-agent   # disparo manual
launchctl bootout gui/$(id -u)/com.almeidaprado.cos-agent        # desligar
```

Logs em `~/.cos-agent/run.log` e `run.err`.

## Credenciais

`~/.cos-agent/env` (chmod 600, **fora do repo**):

```bash
export COS_RO_URL='postgres://cos_agent_ro:...'   # lê tudo, não escreve nada
export COS_OWNER_URL='postgres://neondb_owner:...' # só o runner
```

O papel `cos_agent_ro` foi criado em 31/07 com `SELECT` em `public` e `copilot`,
mais `ALTER DEFAULT PRIVILEGES` para tabelas futuras. Verificado: `UPDATE`,
`INSERT`, `DELETE`, `CREATE TABLE` e `DROP TABLE` todos negados.

## Coexistência com a camada antiga

Os dois motores gravam em `cos_daily_review`; o campo `motor` distingue
(`agente_local` × ausente). O cron do Railway roda 10:00 UTC, este roda 07:12
BRT — 12 minutos de folga de propósito, para não gravarem no mesmo minuto com a
última ganhando por acaso. **Enquanto os dois convivem, o portão que vale é o da
linha mais recente.** Desligar um dos dois é decisão do Renato, depois do placar.
