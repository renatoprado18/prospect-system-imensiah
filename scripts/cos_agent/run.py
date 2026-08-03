#!/usr/bin/env python3
"""
Camada CoS como AGENTE — executor local (31/07/2026).

POR QUE EXISTE. Até aqui a camada era um prompt único sobre um pacote fixo de
contexto: 7 janelas de tamanho arbitrário (21 dias de DM, 40 mensagens, 5
memórias, 500 chars por nota...). Cada janela é um lugar onde a resposta pode
estar uma linha depois do corte, e ninguém saberia. Medido em 30/07: a data de
início das aulas da FAAP estava **40 caracteres depois** do corte de nota, e a
camada relatou honestamente "dado incompleto, não posso afirmar" sobre uma
informação que existia no mesmo campo. Um teste com três agentes de verdade
(frentes #38, #52, #47) achou o dado, não repetiu uma alucinação que a versão
fixa cometeu, e não regrediu no caso de controle.

ARQUITETURA — quem pode o quê:
  - O AGENTE (`claude -p`, subprocesso) recebe SÓ `COS_RO_URL`, a credencial
    `cos_agent_ro`, que **não consegue escrever** (provado: UPDATE/INSERT/DELETE/
    CREATE/DROP todos negados pelo Postgres, não por instrução no prompt).
    Tools restritas a leitura; Write/Edit bloqueados.
  - O RUNNER (este arquivo, determinístico) é quem escreve, e só sabe fazer
    UMA coisa: gravar o payload em `cos_daily_review`. O agente nunca vê a
    credencial de escrita.
  Limite honesto: numa máquina de usuário único não há isolamento total — um
  agente determinado poderia procurar credencial em disco. Isto protege contra
  o modo de falha REAL (o agente "consertar" algo que achou), não contra um
  agente hostil.

TRIAGEM. Medido em 31/07: **14 das 31 frentes ativas não tiveram movimento
nenhum em 24h**, e a camada antiga julgava todas com o mesmo custo. Aqui só
quem se mexeu vai pro agente; o resto herda o estado da rodada anterior. É o
que faz agente custar o mesmo que prompt único, gastando onde importa.

BATIMENTO. Roda na máquina do Renato, que dorme fora da tomada. Sem sinal, uma
rodada que não aconteceu é invisível — o portão de ontem fica de pé parecendo o
de hoje. Por isso o runner registra `cron_heartbeats`, e o `monitor-cron-health`
que já existe no servidor acusa a ausência. O servidor detecta a falta da
máquina.

Uso:
  ./run.py                 # rodada normal (triagem + agentes)
  ./run.py --dry-run       # mostra o plano, não chama agente nem escreve
  ./run.py --limit 3       # teto de frentes nesta rodada
  ./run.py --frente 38     # uma frente específica (teste)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
JOB_ID = "cos-agent-local"
MAX_QUERIES = 20          # teto de consultas por frente, dito ao agente
MAX_TURNS = 40            # teto duro do CLI — mata a toca de coelho
AGENT_TIMEOUT_S = 420     # por frente
PARALELO = 3              # frentes simultâneas

# --- Controles do modo horário (31/07) --------------------------------------
# Sem eles, "rodar de hora em hora" é uma fábrica de custo:
#
# DEBOUNCE — uma conversa de WhatsApp ao vivo faz a frente "se mexer" a cada
# rodada. Sem intervalo mínimo, uma tarde de troca com o Piccino re-julgaria a
# Reorg 8 vezes pra dizer quase a mesma coisa. 90 min deixa a frente respirar
# sem perder o dia.
# 03/08 — 90 → 60. Medido no 1º PDCA de 7 dias: a frente #5 foi barrada **6×**
# (ficou quente o dia todo e o portão dela envelheceu justo quando mais andava).
# 90 min protegia contra repetição, mas cegava a frente mais ativa. Decisão do
# Renato. Alternativa que o PDCA sugeriu e ele NÃO escolheu: exceção pra
# prioridade >=8 — fica pro próximo ciclo se 60 não bastar.
DEBOUNCE_MIN = 60
# TETO DIÁRIO — o limite real não é dinheiro (no Max não é cobrança), é a
# CAPACIDADE que o Renato usa pra trabalhar no terminal. Medido: US$1,10
# nocionais por frente (18 julgamentos = US$19,73 em 31/07, primeiro dia).
# 31/07 → 18→28: o PDCA do 1º dia mostrou o orçamento drenando às 18h com a
# janela indo até 21h; as duas últimas rodadas do dia ficaram cegas. 28 cobre a
# demanda medida (~18 em 7h de operação) com folga pro dia inteiro, que ainda
# NÃO foi medido — o agente só entrou às 14h. Estourou, a rodada não julga e DIZ
# quantas frentes ficaram de fora — silenciar seria repetir o teto de WhatsApp
# que nunca segurou nada.
# 03/08 — 28 → 36. Medido: 01/08 e 02/08 fecharam **100% (28/28)** e **12
# frentes ficaram sem julgamento por teto**. O dia inteiro ainda não cabia — em
# 31/07 eu tinha subido de 18 pra 28 sobre meio dia de dados, e o primeiro dia
# completo mostrou que a folga era menor do que parecia. Decisão do Renato.
TETO_DIARIO = 36
# MÁXIMO POR RODADA — quantas frentes uma única rodada pode julgar. Era o
# literal `3` espalhado pelo código, e em 31/07 ele foi o gargalo REAL: as
# rodadas de 15h/16h/17h tinham 6, 6 e 5 frentes elegíveis e cortaram 3, 3 e 2 —
# com o teto diário ainda em 6/18, 9/18 e 12/18. O corte era deste parâmetro e
# vinha rotulado como "teto diário", mandando o PDCA acusar o número errado.
# 3→5 porque nenhuma rodada do dia passou de 6 elegíveis: com 5 o atraso some
# quase todo, e o que sobra a rodada seguinte pega 1h depois.
MAX_POR_RODADA = 5
# RE-JULGAMENTO POR IDADE (01/08) — o 2º gatilho da triagem. Sem ele, frente
# parada nunca voltava ao agente, e frente parada é a que gera portão de
# cobrança. Só entra se sobrar espaço na rodada DEPOIS das com movimento: é
# preenchimento de folga, não competição. Com o consumo em ~36% do teto, cabe.
# Quem está no portão tem prazo menor porque o custo de errar é maior — cobrar
# alguém que já respondeu gasta credibilidade com terceiro, não só tempo.
REJULGA_APOS_H = 10        # frente comum: uma vez por dia útil de janela
REJULGA_PORTAO_H = 4       # no portão: reconfere ~3x ao longo do dia
# JANELA — julgar às 3 da manhã não serve a ninguém e gasta igual.
HORA_INICIO, HORA_FIM = 7, 21   # BRT


def _env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        sys.exit(f"[cos-agent] {name} ausente — veja {BASE}/env.example")
    return v


def _conn(url: str):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


# --------------------------------------------------------------------------
# 1. Triagem — quem se mexeu desde a última rodada
# --------------------------------------------------------------------------
def triar(ro_url: str, desde_horas: int = 26) -> list[dict]:
    """Frentes ativas COM movimento na janela. 26h e não 24 de propósito: a
    rodada diária tem jitter, e uma janela justa perde o que aconteceu na borda
    do dia anterior — perder movimento é pior que reprocessar uma frente.

    ⚠️ `tasks.atualizado_em` NÃO É SINAL DE MOVIMENTO. O `daily-sync` carimba a
    coluna em toda task que puxa do Google, a cada rodada. Medido em 31/07: o
    sync das 11:37:32 deixou 14 tasks da Vallen com `atualizado_em` 11:37:36-37.
    Contando assim, esta triagem via 75 "movimentos" de task nas frentes ativas
    e **só 4 eram reais** — 95% de eco da máquina. Uma triagem que lê o próprio
    sync como atividade manda tudo pro agente e a economia evapora, que é o
    contrário do motivo dela existir.

    O sinal honesto é `atualizado_em > last_synced_at`: mudança local mais nova
    que a última sincronização. É a MESMA guarda que o `tasks_sync` usa pra
    decidir que o local vence o Google — reusar em vez de inventar um critério
    novo. `last_synced_at IS NULL` = task que nunca foi ao Google, logo qualquer
    toque nela é local."""
    sql = """
        SELECT p.id, p.nome, p.prioridade,
               (SELECT count(*) FROM tasks t
                 WHERE t.project_id = p.id
                   AND t.atualizado_em > NOW() - make_interval(hours => %(h)s)
                   AND (t.last_synced_at IS NULL
                        OR t.atualizado_em > t.last_synced_at)) AS n_tasks,
               (SELECT count(*) FROM project_notes n
                 WHERE n.project_id = p.id AND n.tipo <> 'estado_cos'
                   AND n.criado_em > NOW() - make_interval(hours => %(h)s)) AS n_notas,
               (SELECT count(*) FROM message_project_links l
                  JOIN messages m ON m.id = l.message_id
                 WHERE l.project_id = p.id
                   AND COALESCE(m.enviado_em, m.recebido_em)
                       > NOW() - make_interval(hours => %(h)s)) AS n_msgs,
               (SELECT count(*) FROM project_whatsapp_groups g
                  JOIN group_messages gm ON gm.group_jid = g.group_jid
                 WHERE g.project_id = p.id AND g.ativo
                   AND gm.timestamp > NOW() - make_interval(hours => %(h)s)) AS n_grupo
        FROM projects p
        WHERE p.status = 'ativo'
        ORDER BY p.prioridade DESC NULLS LAST, p.id
    """
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute(sql, {"h": desde_horas})
        linhas = [dict(r) for r in cur.fetchall()]
    for f in linhas:
        f["movimento"] = f["n_tasks"] + f["n_notas"] + f["n_msgs"] + f["n_grupo"]
    return linhas


# --------------------------------------------------------------------------
# 2. O agente — um subprocesso por frente, sem credencial de escrita
# --------------------------------------------------------------------------
def julgar(frente: dict, ro_url: str) -> dict:
    prompt = (BASE / "prompt_frente.md").read_text(encoding="utf-8")
    prompt = (prompt
              .replace("{PROJECT_ID}", str(frente["id"]))
              .replace("{PROJECT_NAME}", frente["nome"] or "")
              .replace("{HOJE}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
              .replace("{MAX_QUERIES}", str(MAX_QUERIES)))

    # SÓ a credencial de leitura entra no ambiente do agente. A de escrita
    # existe apenas neste processo.
    env = {k: v for k, v in os.environ.items() if k != "COS_OWNER_URL"}
    env["COS_RO_URL"] = ro_url

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(MAX_TURNS),
        "--allowedTools", "Bash", "Read", "Grep", "Glob",
        "--disallowedTools", "Write", "Edit", "MultiEdit", "NotebookEdit",
        "--add-dir", "/Users/rap/.claude/projects/-Users-rap-prospect-system/memory",
    ]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=AGENT_TIMEOUT_S, env=env,
                           cwd=str(BASE))
    except subprocess.TimeoutExpired:
        return {"project_id": frente["id"], "frente": frente["nome"],
                "error": f"timeout > {AGENT_TIMEOUT_S}s"}
    dur = int(time.monotonic() - t0)

    if r.returncode != 0:
        return {"project_id": frente["id"], "frente": frente["nome"],
                "error": f"exit {r.returncode}: {(r.stderr or '')[:300]}"}

    # O CLI devolve um envelope; o julgamento é o `result` dentro dele.
    try:
        env_json = json.loads(r.stdout)
        texto = env_json.get("result") or ""
        custo = env_json.get("total_cost_usd")
    except json.JSONDecodeError:
        texto, custo = r.stdout, None

    ini, fim = texto.find("{"), texto.rfind("}") + 1
    if ini < 0 or fim <= ini:
        return {"project_id": frente["id"], "frente": frente["nome"],
                "error": "resposta não-JSON", "raw": texto[:300]}
    try:
        d = json.loads(texto[ini:fim])
    except json.JSONDecodeError as e:
        return {"project_id": frente["id"], "frente": frente["nome"],
                "error": f"JSON inválido: {e}"}

    pdv = d.get("precisa_de_voce") or {}
    agora = datetime.now(timezone.utc).isoformat()
    return {
        "project_id": frente["id"],
        "frente": frente["nome"],
        "julgado_em": agora,
        "estado": (d.get("estado") or "").strip(),
        "movimento": (d.get("movimento") or "").strip(),
        "trava": (d.get("trava") or "").strip(),
        "precisa_de_voce": {"sim": bool(pdv.get("sim")),
                            "o_que": (pdv.get("o_que") or "").strip()},
        "vigilias": [v for v in (d.get("vigilias") or []) if v][:2],
        "nota": (d.get("nota") or "").strip(),
        # a trajetória É a auditoria — sem ela não dá pra saber de onde veio o
        # julgamento, que é a pergunta que originou esta frente inteira
        "trajetoria": d.get("trajetoria") or [],
        "nao_consegui_saber": d.get("nao_consegui_saber") or [],
        "_meta": {"duracao_s": dur, "custo_usd": custo, "motor": "agente_local",
                  "julgado_em": agora},
    }


# --------------------------------------------------------------------------
# 3. Escrita — o único caminho de gravação, e ele só sabe fazer isto
# --------------------------------------------------------------------------
def _mexeu_depois(ro_url: str, project_id: int, quando) -> bool:
    """Houve movimento nesta frente DEPOIS do instante `quando`?

    A triagem geral responde "mexeu na janela"; esta responde "mexeu desde que
    eu a julguei". Sem a segunda, uma frente que andou às 9h volta pro agente às
    10h, 11h, 12h... dizendo a mesma coisa e gastando o teto. Espelha as mesmas
    quatro fontes da triagem, inclusive a guarda contra o eco do `daily-sync`."""
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute("""
            SELECT
              EXISTS (SELECT 1 FROM tasks t
                       WHERE t.project_id = %(p)s AND t.atualizado_em > %(q)s
                         AND (t.last_synced_at IS NULL OR t.atualizado_em > t.last_synced_at))
              OR EXISTS (SELECT 1 FROM project_notes n
                          WHERE n.project_id = %(p)s AND n.tipo <> 'estado_cos'
                            AND n.criado_em > %(q)s)
              OR EXISTS (SELECT 1 FROM message_project_links l
                           JOIN messages m ON m.id = l.message_id
                          WHERE l.project_id = %(p)s
                            AND COALESCE(m.enviado_em, m.recebido_em) > %(q)s)
              OR EXISTS (SELECT 1 FROM project_whatsapp_groups g
                           JOIN group_messages gm ON gm.group_jid = g.group_jid
                          WHERE g.project_id = %(p)s AND g.ativo AND gm.timestamp > %(q)s)
              AS mexeu
        """, {"p": project_id, "q": quando.replace(tzinfo=None)})
        return bool(cur.fetchone()["mexeu"])


def julgamentos_de_hoje(ro_url: str) -> int:
    """Quantas frentes já foram julgadas pelo agente hoje (BRT). Alimenta o teto
    diário. Conta pelo payload, não por linha: uma rodada pode julgar várias."""
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM((payload->'triagem'->>'julgadas_agora')::int), 0) AS n
            FROM cos_daily_review
            WHERE payload->>'motor' = 'agente_local'
              AND (run_at AT TIME ZONE 'America/Sao_Paulo')::date
                  = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
        """)
        return cur.fetchone()["n"] or 0


def ultimo_julgamento_por_frente(anterior: dict) -> dict:
    """project_id -> quando o AGENTE julgou aquela frente pela última vez.

    É o que transforma "mexeu nas últimas 26h" em "mexeu desde que EU a julguei".
    Sem isso, no modo horário a mesma frente que andou de manhã seria re-julgada
    a cada hora até a meia-noite — 14 vezes pra dizer o mesmo. O timestamp
    sobrevive à herança porque `fundir` preserva o `_meta` da frente herdada."""
    out = {}
    for f in (anterior.get("frentes") or []):
        ts = (f.get("_meta") or {}).get("julgado_em")
        if ts:
            out[f.get("project_id")] = ts
    return out


def herdar(ro_url: str) -> dict:
    """Último payload gravado, de qualquer motor. É a base sobre a qual esta
    rodada escreve.

    POR QUE ISTO EXISTE. O agente julga só quem se mexeu — por desenho. Sem
    herdar, cada rodada gravaria uma foto PARCIAL, e como todo consumidor
    (tonIAH, `/cockpit`, o abridor do chat) lê `ORDER BY run_at DESC LIMIT 1`,
    a foto parcial vira a oficial. Aconteceu no 1º teste: uma rodada de UMA
    frente virou a linha mais recente e o cockpit do Renato ficou com 1 frente
    e portão vazio. Não é artefato de teste — é o comportamento normal deste
    motor, e sem a herança ele encolheria a visão todo dia."""
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute("SELECT payload FROM cos_daily_review ORDER BY run_at DESC LIMIT 1")
        row = cur.fetchone()
    if not row or not row.get("payload"):
        return {}
    p = row["payload"]
    return p if isinstance(p, dict) else json.loads(p)


def fundir(anterior: dict, novos: list[dict], triagem: dict) -> dict:
    """Estado anterior + o que esta rodada re-julgou. Frente não re-julgada
    mantém o debriefing de ontem, MARCADO como herdado — para o consumidor (e o
    Renato) distinguirem "julgado agora" de "estava assim e não se mexeu".
    Silenciar essa diferença seria vender foto velha como atual."""
    por_id = {}
    for d in (anterior.get("frentes") or []):
        d = dict(d)
        meta = dict(d.get("_meta") or {})
        meta["herdado"] = True
        meta["herdado_de"] = anterior.get("run_at")
        d["_meta"] = meta
        por_id[d.get("project_id")] = d
    for d in novos:                      # o julgamento fresco sobrescreve
        por_id[d["project_id"]] = d

    frentes = list(por_id.values())
    precisa = [{"frente": d["frente"], "project_id": d["project_id"],
                "o_que": (d.get("precisa_de_voce") or {}).get("o_que", "")}
               for d in frentes if (d.get("precisa_de_voce") or {}).get("sim")]
    vigilias = [{"frente": d["frente"], "project_id": d["project_id"], "item": v}
                for d in frentes for v in (d.get("vigilias") or [])]
    cobertas = [d["frente"] for d in frentes
                if not (d.get("precisa_de_voce") or {}).get("sim")]
    return {"frentes": frentes, "precisa": precisa,
            "vigilias": vigilias, "cobertas": cobertas}


def gravar(owner_url: str, payload: dict) -> int:
    with _conn(owner_url) as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO cos_daily_review (run_date, n_frentes, n_precisa, payload)
               VALUES ((NOW() AT TIME ZONE 'America/Sao_Paulo')::date, %s, %s, %s)
               RETURNING id""",
            (payload["n_frentes"],
             len(payload["placar"]["precisa_de_voce"]),
             json.dumps(payload, ensure_ascii=False)),
        )
        rid = cur.fetchone()["id"]
        c.commit()
    return rid


def bater_ponto(owner_url: str, status: int, dur_ms: int) -> None:
    """Sem isto, uma rodada que NÃO aconteceu é invisível: o portão de ontem
    fica de pé parecendo o de hoje. Com isto, o `monitor-cron-health` do
    servidor acusa a ausência — o servidor detecta a falta da máquina."""
    try:
        with _conn(owner_url) as c, c.cursor() as cur:
            # append-only: a tabela não tem unique em job_id, e o monitor lê o
            # MAX(fired_at) por job. `source` distingue esta máquina do worker
            # Railway — se um dia os dois baterem o mesmo job, dá pra separar.
            cur.execute("""
                INSERT INTO cron_heartbeats (job_id, fired_at, http_status, duration_ms, source)
                VALUES (%s, NOW(), %s, %s, 'mac-local')
            """, (JOB_ID, status, dur_ms))
            c.commit()
    except Exception as e:
        print(f"[cos-agent] batimento falhou (não fatal): {e}", file=sys.stderr)


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="teto de frentes")
    ap.add_argument("--frente", type=int, default=0, help="só esta frente")
    ap.add_argument("--horario", action="store_true",
                    help="modo horário: só quem mexeu DESDE o próprio julgamento, "
                         "com debounce, teto diário e janela de horas")
    a = ap.parse_args()

    ro_url = _env("COS_RO_URL")
    owner_url = _env("COS_OWNER_URL")
    t0 = time.monotonic()
    motivos: dict = {}

    from datetime import timedelta
    agora = datetime.now(timezone.utc)
    hora_brt = (agora - timedelta(hours=3)).hour

    if a.horario and not (HORA_INICIO <= hora_brt < HORA_FIM):
        print(f"[cos-agent] {hora_brt}h BRT — fora da janela {HORA_INICIO}-{HORA_FIM}h; nada a fazer")
        return 0

    anterior = herdar(ro_url)
    ja_hoje = julgamentos_de_hoje(ro_url) if a.horario else 0
    # NÃO retornamos aqui quando o teto estourou. Até 31/07 retornávamos, e o
    # efeito era um teto que corta CEGO: ele dizia que tinha batido, mas não
    # dizia o que deixou passar — as rodadas das 19h e 20h daquele dia não
    # registraram nada. A triagem é só SQL (não gasta agente), então rodamos
    # sempre e deixamos o corte cair em `cabe`, que agora sabe distinguir quem
    # barrou. Sem isso, "o teto custou N julgamentos" é indemonstrável.

    todas = triar(ro_url)
    if a.frente:
        alvo = [f for f in todas if f["id"] == a.frente]
    elif a.horario:
        # O critério que faz o modo horário existir: movimento DESDE o último
        # julgamento DAQUELA frente — não "nas últimas 26h", que re-julgaria a
        # mesma coisa a cada hora. Mais o debounce, que impede uma conversa ao
        # vivo de consumir o teto sozinha.
        ultimos = ultimo_julgamento_por_frente(anterior)
        alvo = []
        # Por que cada frente NÃO foi julgada. Sem isto o PDCA não consegue
        # separar "o debounce está apertado" de "não há movimento" de "o teto
        # cortou" — e ajustar parâmetro sem saber qual deles mordeu é chute.
        motivos = {"sem_movimento": 0, "debounce": 0, "movimento_ja_visto": 0,
                   "cortada_pelo_teto": 0, "cortada_pelo_limite_rodada": 0}
        barradas_debounce = []
        for f in todas:
            if f["movimento"] <= 0:
                motivos["sem_movimento"] += 1
                continue
            ts = ultimos.get(f["id"])
            if ts:
                try:
                    quando = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    quando = None
                if quando and (agora - quando) < timedelta(minutes=DEBOUNCE_MIN):
                    motivos["debounce"] += 1
                    barradas_debounce.append(f["id"])
                    continue          # julgada agora há pouco; deixa respirar
                if quando and not _mexeu_depois(ro_url, f["id"], quando):
                    motivos["movimento_ja_visto"] += 1
                    continue          # o movimento é o mesmo que ela já viu
            alvo.append(f)
        alvo = sorted(alvo, key=lambda f: -f["movimento"])
        # DOIS cortes distintos, contados separadamente. Até 31/07 os dois caíam
        # em `cortada_pelo_teto`, e o PDCA acusava o teto diário quando o
        # gargalo era o limite por rodada — subir o teto não teria mudado nada.
        # Quem barra primeiro é quem leva a culpa: o orçamento do dia (`resta`)
        # ou a fatia da rodada (`por_rodada`).
        elegiveis = len(alvo)
        resta = max(0, TETO_DIARIO - ja_hoje)
        por_rodada = a.limit or MAX_POR_RODADA
        cabe = min(por_rodada, resta)
        excedente = max(0, elegiveis - cabe)
        if resta < por_rodada:
            # O orçamento do dia é mais apertado que a fatia da rodada: o teto
            # diário é o culpado por tudo que passou de `resta`.
            motivos["cortada_pelo_teto"] = excedente
        else:
            motivos["cortada_pelo_limite_rodada"] = excedente
        alvo = alvo[:cabe]

        # --- 2º GATILHO: IDADE (01/08) ------------------------------------
        # Até aqui só MOVIMENTO mandava frente ao agente, e isso deixava um
        # buraco exatamente onde mais dói: a frente PARADA nunca era re-julgada.
        # Só que portão de cobrança ("ninguém respondeu, cobre") nasce justamente
        # de frente parada — então o julgamento mais arriscado do sistema era o
        # único que envelhecia sem ninguém conferir.
        #
        # Caso que provou (01/08): a #50 pedia "mandar o FUP pro Amirilian
        # agora". O FUP tinha sido enviado em 28/07 11:15, o Renato insistiu em
        # 31/07 14:19, o Eduardo respondeu às 15:14 que NÃO levou ao conselho e
        # o Renato encerrou às 17:42. Nada disso chegou ao portão: sem
        # movimento contado, a frente nunca voltou ao agente e o julgamento
        # velho seguiu sendo servido como se fosse de hoje.
        #
        # Quem está NO PORTÃO tem prazo menor de propósito: pedir algo ao
        # Renato cria a obrigação de reconferir que o pedido ainda faz sentido.
        if len(alvo) < cabe:
            no_portao = {p.get("project_id")
                         for p in ((anterior.get("placar") or {}).get("hoje") or [])}
            ja_no_alvo = {f["id"] for f in alvo}
            velhas = []
            for f in todas:
                if f["id"] in ja_no_alvo:
                    continue
                ts = ultimos.get(f["id"])
                # Rank 0 = está NO PORTÃO. Vem antes de tudo, inclusive de
                # frente nunca julgada com prioridade maior: o portão é o que o
                # Renato lê e executa, então é onde um julgamento velho vira
                # ação errada. (Primeira versão deste bloco empatava os dois
                # ranks e a #44 — no portão, prioridade 7 — perdia lugar pra
                # três frentes de prioridade 8 que ninguém estava lendo.)
                if f["id"] in no_portao:
                    idade = 1e9 if not ts else None
                    if ts:
                        try:
                            idade = (agora - datetime.fromisoformat(
                                ts.replace("Z", "+00:00"))).total_seconds() / 3600
                        except ValueError:
                            idade = 1e9
                    if idade >= REJULGA_PORTAO_H:
                        velhas.append((0, -(f["prioridade"] or 0), f, idade))
                    continue
                if not ts:
                    # Nunca julgada por este motor (herdada da API): julgamento
                    # sem dono, mas fora do portão ninguém age sobre ele agora.
                    velhas.append((1, -(f["prioridade"] or 0), f, 1e9))
                    continue
                try:
                    quando = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                horas = (agora - quando).total_seconds() / 3600
                if horas >= REJULGA_APOS_H:
                    velhas.append((2, -(f["prioridade"] or 0), f, horas))
            # portão → nunca julgada → velha; dentro de cada, prioridade e idade
            velhas.sort(key=lambda x: (x[0], x[1], -x[3]))
            escolhidas = [v[2] for v in velhas[: cabe - len(alvo)]]
            alvo += escolhidas
            motivos["rejulgada_por_idade"] = len(escolhidas)
            motivos["_idade_ids"] = [f["id"] for f in escolhidas]
        else:
            motivos["rejulgada_por_idade"] = 0

        motivos["_debounce_ids"] = barradas_debounce
        motivos["_teto_usado"] = f"{ja_hoje}/{TETO_DIARIO}"
        motivos["_elegiveis"] = elegiveis
        motivos["_max_por_rodada"] = por_rodada
    else:
        alvo = [f for f in todas if f["movimento"] > 0]
        if a.limit:
            alvo = sorted(alvo, key=lambda f: -f["movimento"])[:a.limit]
    paradas = [f for f in todas if f["movimento"] == 0]

    print(f"[cos-agent] {len(todas)} frentes ativas · {len(alvo)} pro agente · "
          f"{len(paradas)} sem movimento (herdam o estado anterior)")
    for f in alvo:
        print(f"   #{f['id']:<4} {(f['nome'] or '')[:48]:48} "
              f"tasks={f['n_tasks']} notas={f['n_notas']} msgs={f['n_msgs']} grupo={f['n_grupo']}")
    if a.dry_run:
        print("[cos-agent] dry-run — nada executado, nada gravado")
        return 0
    if not alvo:
        barradas = motivos.get("cortada_pelo_teto", 0)
        if not barradas:
            # Rodada estéril de verdade: não havia trabalho. Barata (só SQL) e
            # não merece linha no banco — gravar a foto herdada de novo só
            # encheria `cos_daily_review` de cópias idênticas.
            print("[cos-agent] nada se mexeu; rodada encerrada sem gastar agente")
            bater_ponto(owner_url, 200, int((time.monotonic() - t0) * 1000))
            return 0
        # Aqui HAVIA trabalho e o teto do dia recusou. Isso é um evento que o
        # PDCA precisa contar, então esta rodada grava (foto herdada + motivos)
        # mesmo sem julgar nada. É a diferença entre "não tinha o que fazer" e
        # "tinha, e o parâmetro impediu".
        print(f"[cos-agent] TETO DIÁRIO atingido ({ja_hoje}/{TETO_DIARIO} julgamentos hoje) "
              f"— {barradas} frente(s) com movimento novo ficaram SEM julgamento nesta "
              f"rodada. A foto anterior segue valendo.")
        triagem = {"ativas": len(todas), "com_movimento": 0,
                   "paradas": len(paradas), "falhas": 0,
                   "modo": "horario" if a.horario else "lote",
                   "nao_julgadas": motivos}
        m = fundir(anterior, [], triagem)
        prio = {f["id"]: (f["prioridade"] or 0) for f in todas}
        hoje = sorted(m["precisa"], key=lambda p: -prio.get(p["project_id"], 0))[:3]
        rid = gravar(owner_url, {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "n_frentes": len(m["frentes"]),
            "motor": "agente_local",
            "triagem": {**triagem, "julgadas_agora": 0,
                        "herdadas": len(m["frentes"])},
            "custo_usd": None,
            "frentes": m["frentes"],
            "placar": {"hoje": hoje,
                       "esta_semana": [p for p in m["precisa"] if p not in hoje],
                       "precisa_de_voce": m["precisa"], "vigilias": m["vigilias"],
                       "cobertas": m["cobertas"]},
        })
        print(f"[cos-agent] gravado cos_daily_review id={rid} · 0 julgadas "
              f"(teto) + {len(m['frentes'])} herdadas")
        bater_ponto(owner_url, 200, int((time.monotonic() - t0) * 1000))
        return 0

    with ThreadPoolExecutor(max_workers=PARALELO) as ex:
        debriefs = list(ex.map(lambda f: julgar(f, ro_url), alvo))

    ok = [d for d in debriefs if not d.get("error")]
    ruim = [d for d in debriefs if d.get("error")]
    for d in ruim:
        print(f"[cos-agent] #{d['project_id']} falhou: {d['error']}", file=sys.stderr)

    # Funde com a foto anterior: quem não se mexeu mantém o julgamento de ontem.
    # Sem isto o payload sairia PARCIAL e viraria o oficial (ver `herdar`).
    triagem = {"ativas": len(todas), "com_movimento": len(alvo),
               "paradas": len(paradas), "falhas": len(ruim),
               "modo": "horario" if a.horario else "lote",
               "nao_julgadas": motivos}
    m = fundir(anterior, ok, triagem)   # `anterior` já foi lido no topo

    # O corte de ≤3 fica com o mesmo critério de sempre; ordenar por prioridade
    # do projeto é determinístico e não gasta uma 2ª chamada de LLM.
    prio = {f["id"]: (f["prioridade"] or 0) for f in todas}
    hoje = sorted(m["precisa"], key=lambda p: -prio.get(p["project_id"], 0))[:3]
    esta_semana = [p for p in m["precisa"] if p not in hoje]

    custo = sum((d.get("_meta", {}).get("custo_usd") or 0) for d in ok)
    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "n_frentes": len(m["frentes"]),
        "motor": "agente_local",
        "triagem": {**triagem, "julgadas_agora": len(ok),
                    "herdadas": len(m["frentes"]) - len(ok)},
        "custo_usd": round(custo, 4) if custo else None,
        "frentes": m["frentes"],
        "placar": {"hoje": hoje, "esta_semana": esta_semana,
                   "precisa_de_voce": m["precisa"], "vigilias": m["vigilias"],
                   "cobertas": m["cobertas"]},
    }
    rid = gravar(owner_url, payload)
    dur = int((time.monotonic() - t0) * 1000)
    bater_ponto(owner_url, 200 if not ruim else 207, dur)
    print(f"[cos-agent] gravado cos_daily_review id={rid} · "
          f"{len(ok)} julgadas agora + {len(m['frentes']) - len(ok)} herdadas "
          f"= {len(m['frentes'])} frentes · {len(hoje)} no portão · "
          f"{dur/1000:.0f}s · US${custo:.3f} (nocional; no Max não é cobrança)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
