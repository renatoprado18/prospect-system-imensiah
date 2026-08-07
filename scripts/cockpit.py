#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/index.html — o cockpit de frentes e tarefas do Renato.

DECISÃO QUE ORIGINOU (Renato, 06/08/2026): promover esta página de HTML escrito
à mão pela sessão CoS a GERADOR. O defeito era estrutural: escrita à mão, ela
retrata o INSTANTE em que a CoS a escreveu. Tarefa fechada, WhatsApp novo,
agenda bloqueada às 13h — nada disso aparece até alguém reescrever o arquivo. Foi
literalmente a reclamação dele naquele dia ("bloqueei uma agenda e nada
apareceu"). O par `~/cockpit/sistema.html` (`scripts/sistema.py`) já era gerado;
esta é a irmã que faltava.

A ORDEM ERA DURA e foi respeitada: o gerador só depois do sync de calendar
consertado (feito em 06/08, `d314e25` — roda a cada 15 min). Um cockpit "ao
vivo" lendo agenda de 24h atrás é PIOR que o snapshot honesto, porque mente com
cara de atual.

O DESENHO QUE IMPORTA — duas camadas, e a costura entre elas:

  · O CHÃO é gerado (aqui): tasks, vencidas, agenda, check-G, frescor dos canos.
    É o que uma query sabe responder e nenhuma pessoa deveria estar transcrevendo
    à mão.
  · O JULGAMENTO é da CoS: o que é compromisso e o que é ruído, o "por que isto
    importa hoje". Isso uma query não sabe.

Se o julgamento morasse no HTML, a primeira regeneração o apagaria — e o gerador
teria trocado um defeito (página velha) por outro pior (página fresca e burra).
Então ele mora num overlay, `~/cockpit/curadoria.json`, e é fundido na
renderização. O gerador cria o esqueleto do arquivo se ele não existir.

E a curadoria carrega a PRÓPRIA IDADE na tela. Uma nota escrita há três dias
sobre uma tarefa de hoje é exatamente o tipo de coisa que parece atual e não é —
o defeito que este gerador nasceu pra matar não pode voltar pela porta do
overlay.

O QUE ELE NÃO FAZ: não escreve no banco, não fecha task, não notifica. A página
SINALIZA ("houve ato depois — confira") e nunca conclui: dar por resolvido o que
não foi resolvido é o erro caro, e desfazer o dado por concluído nunca é ação
automática ([[feedback_cos_autonomy_policy]]).

Uso:  ./cockpit.py            # gera e abre
      ./cockpit.py --quieto   # gera sem abrir (pra cron/launchd)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/index.html")
CURADORIA = os.path.expanduser("~/cockpit/curadoria.json")
BRT = ZoneInfo("America/Sao_Paulo")

# A ficha do próprio Renato. Sem esta exclusão, a mensagem que a CAMADA manda
# PRA ELE conta como "ato dele" e a página anuncia que ele já respondeu quando
# quem falou foi a máquina — a máquina ouvindo o próprio eco
# ([[feedback_maquina_ouve_o_proprio_eco]], 4 casos em 2 dias de agosto).
SQL_EU = "(SELECT contact_id FROM users WHERE id = 1)"

DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def data_br(d):
    return f"{d.day:02d}/{d.month:02d}"


# ------------------------------------------------------------------ coleta --

def coletar(cur, hoje):
    """Tudo que a página mostra, numa passada. `hoje` vem em BRT — nunca do
    banco: `CURRENT_DATE` no Neon é UTC, e depois das 21h BRT ele já virou o dia
    seguinte. O cockpit erraria o "hoje" toda noite."""
    d = {"hoje": hoje}
    fim = hoje + timedelta(days=8)

    # --- AGENDA -------------------------------------------------------------
    # ⚠️ start_datetime NÃO é UTC: é gravado na timezone do campo `timezone`
    # (BRT, na prática). Converter subtrai 3h e mostra reunião no horário errado
    # — já aconteceu ([[feedback_calendar_events_tz]]). Lê-se raw.
    cur.execute("""
        SELECT e.id, e.summary, e.start_datetime, e.end_datetime, e.all_day, e.location,
               e.conference_url, e.timezone, e.contact_id, c.nome AS contato,
               e.last_synced_at
          FROM calendar_events e
          LEFT JOIN contacts c ON c.id = e.contact_id
         WHERE e.start_datetime >= %s AND e.start_datetime < %s
           AND COALESCE(e.status, 'confirmed') <> 'cancelled'
         ORDER BY e.start_datetime
    """, (hoje, fim))
    eventos = cur.fetchall()
    d["aniversarios"] = [e for e in eventos if "nivers" in (e["summary"] or "")]
    d["eventos"] = [e for e in eventos if "nivers" not in (e["summary"] or "")]

    # --- TAREFAS ------------------------------------------------------------
    # O LATERAL com LIMIT 1 é deliberado: cruzar `atos_que_resolvem` com tasks
    # sem limite dá produto cartesiano (medido em 06/08). Um ato — o mais
    # recente — basta pra levantar a pergunta, que é tudo que a página faz.
    cur.execute(f"""
        SELECT t.id, t.titulo, t.data_vencimento, t.prioridade, t.status, t.contexto,
               t.on_hold_since, t.on_hold_reason, t.data_criacao,
               t.project_id, p.nome AS projeto, p.icone,
               t.contact_id, ct.nome AS contato,
               ato.quando AS ato_quando, ato.quem AS ato_quem, ato.canal AS ato_canal,
               left(ato.evidencia, 150) AS ato_evidencia
          FROM tasks t
          LEFT JOIN projects p  ON p.id = t.project_id
          LEFT JOIN contacts ct ON ct.id = t.contact_id
          LEFT JOIN LATERAL (
                SELECT a.quando, a.quem, a.canal, a.evidencia
                  FROM atos_que_resolvem a
                 WHERE a.pessoa_id = t.contact_id
                   AND t.contact_id <> {SQL_EU}
                   AND a.quando > GREATEST(t.data_criacao, now() - interval '5 days')
                 ORDER BY a.quando DESC LIMIT 1
          ) ato ON true
         WHERE t.status IN ('pending', 'in_progress', 'on_hold')
         ORDER BY t.data_vencimento NULLS LAST, t.prioridade DESC
    """)
    d["tasks"] = cur.fetchall()

    # --- CHECK-G: o inbound que sobreviveu à triagem -------------------------
    # Fonte é o LEDGER, não uma query paralela — se o gate mudar, esta página
    # muda junto. Duas verdades sobre o mesmo funil divergem em silêncio (a
    # razão de o `sistema.py` chamar o verificador em vez de reimplementá-lo).
    #
    # E vem o FIO, não uma amostra: mostrar uma mensagem por contato — preferindo
    # a que tem anexo — foi o que escondeu o pedido do Orestes em 06/08. O pedido
    # real costuma ser o irmão do anexo ([[feedback_inbound_ancora_nao_e_a_mensagem_acionavel]]).
    cur.execute("""
        SELECT l.contact_id, c.nome AS contato, count(*) AS n,
               max(l.msg_em) AS ultima,
               (array_agg(l.trecho ORDER BY l.msg_em DESC))[1:3] AS fio,
               bool_or(l.desfecho = 'agiu') AS agiu_depois
          FROM check_g_ledger l
          LEFT JOIN contacts c ON c.id = l.contact_id
         WHERE l.decisao = 'mostrado'
           AND l.msg_em > now() - interval '3 days'
           AND COALESCE(l.desfecho, '') <> 'virou_task'
           -- O que ele já disse que não precisa de ação não volta amanhã (066).
           -- `dispensado_em` é decisão dele; `desfecho` é observação do sistema,
           -- e o cron reescreve o segundo a cada rodada.
           AND l.dispensado_em IS NULL
         GROUP BY 1, 2 ORDER BY 4 DESC
    """)
    d["checkg"] = cur.fetchall()

    cur.execute("SELECT contact_id FROM users WHERE id = 1")
    r = cur.fetchone()
    d["eu_contact_id"] = r["contact_id"] if r else None

    # --- O PORTÃO DA CAMADA --------------------------------------------------
    # A peça que faltava (07/08, passo 3 do caminho C). Este cockpit nasceu de
    # manhã lendo tasks, agenda e WhatsApp — o CHÃO — e o julgamento do agente,
    # que roda 14×/dia e tem 75% de precisão medida, só aparecia na tonIAH. Com
    # o contrato novo (esta é a única superfície que responde "o que precisa de
    # mim hoje"), deixar o portão de fora seria eleger a superfície e tirar dela
    # justamente o que a camada produz de mais valioso: o porquê.
    cur.execute("""SELECT run_at, payload,
                          EXTRACT(EPOCH FROM (now() - run_at))/3600 AS horas
                     FROM cos_daily_review ORDER BY run_at DESC LIMIT 1""")
    d["review"] = cur.fetchone()

    # Portão que ele já resolveu HOJE não volta pra tela. O `/api/cos/portao-action`
    # grava um `project_note` tipo `portao_acao`, e a camada só o lê na PRÓXIMA
    # rodada — até lá o cockpit continuaria pedindo o que ele acabou de fazer.
    # Foi exatamente a reclamação de hoje sobre o placar ("dou feedback e
    # continua lá"); aqui a correção nasce junto com o recurso.
    cur.execute("""SELECT DISTINCT project_id FROM project_notes
                    WHERE tipo = 'portao_acao'
                      AND criado_em > (now() AT TIME ZONE 'UTC')::date""")
    d["portao_resolvido"] = {r["project_id"] for r in cur.fetchall()}

    # --- FRESCOR DOS CANOS ---------------------------------------------------
    # A razão de existir desta faixa: por 3 meses o INTEL julgou sobre agenda de
    # até 24h atrás e NENHUMA superfície mostrava isso. Uma página que se diz ao
    # vivo tem obrigação de declarar a idade do que está lendo.
    cur.execute("""
        SELECT
          (SELECT max(last_synced_at) FROM calendar_events)                        AS agenda,
          (SELECT max(m.criado_em) FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
            WHERE cv.canal = 'whatsapp')                                           AS whatsapp,
          (SELECT max(m.criado_em) FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
            WHERE cv.canal = 'email')                                              AS email,
          (SELECT max(timestamp) FROM group_messages)                              AS grupos,
          (SELECT max(avaliado_em) FROM check_g_ledger)                            AS checkg,
          (SELECT max(run_at) FROM cos_daily_review)                               AS camada
    """)
    d["canos"] = cur.fetchone()
    return d


def carregar_curadoria():
    """Overlay da CoS. Ausente ou quebrado, a página sai só com o chão — nunca
    deixa de sair: perder o cockpit inteiro por causa de uma vírgula no JSON
    seria trocar o defeito por um pior."""
    if not os.path.exists(CURADORIA):
        esqueleto = {
            "_leia": "Camada de JULGAMENTO da CoS. O chão (tasks/agenda/check-G) vem do banco "
                     "e é regerado; o que você escrever aqui sobrevive. Chaves: 'task:999741', "
                     "'evento:6054', 'checkg:26691'.",
            "atualizado_em": None,
            "nota_do_dia": "",
            "itens": {},
            "blocos": [],
        }
        try:
            open(CURADORIA, "w").write(json.dumps(esqueleto, ensure_ascii=False, indent=2))
        except Exception:
            pass
        return esqueleto, None
    try:
        c = json.load(open(CURADORIA))
    except Exception as e:
        return {"_erro": f"{type(e).__name__}: {e}", "itens": {}, "blocos": []}, None
    quando = c.get("atualizado_em")
    try:
        quando = datetime.fromisoformat(quando) if quando else None
    except Exception:
        quando = None
    return c, quando


# ------------------------------------------------------------- classificação --

def hora_evt(e):
    return "dia todo" if e["all_day"] else f"{e['start_datetime']:%H:%M}"


def separar(d):
    """Distribui as tarefas nos baldes da página.

    `on_hold` é tratado como a convenção manda ([[feedback_aguardar_terceiro_on_hold]]):
    aguardar terceiro NÃO é dívida enquanto está dentro do prazo — vira cobrança
    depois de 7 dias. Misturar os dois faz a lista de vencidas gritar sobre coisa
    que está correndo bem, e uma lista que grite à toa deixa de ser lida.
    """
    hoje, sep = d["hoje"], {"hoje": [], "semana": [], "vencidas": [], "aguardando": [],
                            "sem_data": [], "andamento": []}
    limite = hoje + timedelta(days=8)
    for t in d["tasks"]:
        venc = t["data_vencimento"].date() if t["data_vencimento"] else None
        if t["status"] == "on_hold":
            # `on_hold_since` é UTC naive; `datetime.now()` é BRT. Subtrair os
            # dois tirava 3h e produzia "mais antigo -1d" numa task posta em
            # espera hoje — apareceu na hora em que a visão por pessoa passou a
            # mostrar a idade. Compara-se em UTC, e nunca abaixo de zero.
            idade = max(0, (datetime.now(timezone.utc).replace(tzinfo=None)
                            - t["on_hold_since"]).days) if t["on_hold_since"] else 0
            (sep["vencidas"] if idade > 7 else sep["aguardando"]).append({**t, "atraso": idade})
            continue
        if t["status"] == "in_progress":
            # EM ANDAMENTO NÃO É VENCIDA (07/08): "incomoda, não está vencida",
            # sobre a task de despesas que ele tinha acabado de tocar. Prazo
            # estourado numa coisa que está em curso não é dívida — é estimativa
            # errada. Chamar de vencida transforma trabalho em curso em cobrança.
            sep["andamento"].append(t)
        elif venc is None:
            sep["sem_data"].append(t)
        elif venc < hoje:
            sep["vencidas"].append({**t, "atraso": (hoje - venc).days})
        elif venc == hoje:
            sep["hoje"].append(t)
        elif venc < limite:
            sep["semana"].append(t)
    return sep


# ---------------------------------------------------------------------- CSS --
# Fora da f-string de propósito: as chaves do CSS colidem com a interpolação, e
# duplicá-las (`{{`) num bloco deste tamanho é fonte garantida de erro mudo.
CSS = """
:root {
  --paper:#f4f0e7; --panel:#fbf9f3; --panel-edge:#e4ddcc; --ink:#23211c;
  --ink-soft:#57534a; --ink-faint:#8a8477; --brass:#9c7a33; --brass-soft:#b79a55;
  --crit:#a4321f; --crit-bg:#f0e0d6; --warn:#9a6a13; --warn-bg:#f0e6cf;
  --calm:#4f6b4a; --calm-bg:#e3e9dc;
  --shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--sans); line-height:1.5; -webkit-font-smoothing:antialiased; }
.wrap { max-width:880px; margin:0 auto; padding:40px 22px 72px; }
.mast { border-bottom:2px solid var(--ink); padding-bottom:16px; }
.mast-top { display:flex; justify-content:space-between; align-items:baseline; gap:16px; flex-wrap:wrap; }
.brand { font-family:var(--serif); font-size:clamp(30px,6vw,46px); font-weight:700; letter-spacing:-.01em; line-height:1; }
.brand em { font-style:italic; color:var(--brass); }
.stamp { font-family:var(--mono); font-size:12px; color:var(--ink-faint); text-align:right; white-space:nowrap; }
.stamp b { color:var(--ink-soft); font-weight:600; }
.canos { display:flex; flex-wrap:wrap; gap:6px 14px; margin:12px 0 0; font-family:var(--mono); font-size:11px; color:var(--ink-faint); }
.canos b { font-weight:600; color:var(--ink-soft); }
.canos .cano { display:inline-flex; align-items:center; gap:5px; }
.dot { width:7px; height:7px; border-radius:50%; flex:none; background:var(--calm); }
.dot.warn { background:var(--warn); } .dot.crit { background:var(--crit); }
.alerta-cano { margin:12px 0 0; padding:9px 12px; background:var(--crit-bg); border:1px solid var(--crit); border-radius:8px; font-size:13px; }
.nota-cos { margin:16px 0 0; padding:11px 14px; background:var(--panel); border:1px solid var(--panel-edge); border-left:3px solid var(--brass); border-radius:8px; font-size:13.5px; }
.nota-cos .quem { font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.1em; color:var(--brass); display:block; margin-bottom:4px; }
.velha { color:var(--crit); font-weight:600; }
.summary { display:flex; flex-wrap:wrap; gap:10px; margin:18px 0 34px; }
.stat { flex:1 1 120px; background:var(--panel); border:1px solid var(--panel-edge); border-radius:10px; padding:12px 14px; box-shadow:var(--shadow); }
.stat .n { font-family:var(--serif); font-size:30px; font-weight:700; line-height:1; font-variant-numeric:tabular-nums; }
.stat .l { text-transform:uppercase; letter-spacing:.1em; font-size:10px; color:var(--ink-faint); margin-top:6px; font-weight:600; }
.stat.hot .n { color:var(--crit); }
section { margin-bottom:34px; }
.head { display:flex; align-items:baseline; gap:12px; margin-bottom:14px; }
.head h2 { font-family:var(--serif); font-size:21px; font-weight:700; margin:0; letter-spacing:-.01em; }
.head .rule { flex:1; height:1px; background:var(--panel-edge); }
.head .tag { font-family:var(--mono); font-size:11px; color:var(--ink-faint); }
.lead { font-size:13.5px; color:var(--ink-soft); margin:-6px 0 16px; max-width:64ch; }
.items { display:flex; flex-direction:column; gap:8px; }
.item { display:grid; grid-template-columns:4px 1fr auto; gap:0 14px; background:var(--panel); border:1px solid var(--panel-edge); border-radius:10px; overflow:hidden; box-shadow:var(--shadow); }
.item .stripe { grid-row:1 / -1; background:var(--calm); }
.item.p-crit .stripe { background:var(--crit); }
.item.p-warn .stripe { background:var(--warn); }
.item .body { padding:13px 0 13px 2px; min-width:0; }
.item .title { font-size:14.5px; font-weight:600; line-height:1.35; }
.item .why { font-size:12.5px; color:var(--ink-soft); margin-top:4px; max-width:66ch; }
.item .meta { display:flex; flex-wrap:wrap; gap:6px 10px; margin-top:9px; align-items:center; }
.item .aside { padding:13px 14px 13px 0; display:flex; flex-direction:column; align-items:flex-end; gap:7px; justify-content:center; text-align:right; }
.id { font-family:var(--mono); font-size:11px; color:var(--ink-faint); }
.chip { font-size:10.5px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; padding:2px 8px; border-radius:999px; background:var(--paper); border:1px solid var(--panel-edge); color:var(--ink-soft); white-space:nowrap; }
.chip.ato { background:var(--warn-bg); border-color:var(--warn); color:var(--warn); text-transform:none; letter-spacing:0; font-weight:600; }
.chip.cos { background:var(--calm-bg); border-color:var(--calm); color:var(--calm); }
  .chip.velho { background:var(--crit-bg); border-color:var(--crit); color:var(--crit); }
  .why.cos-diz { border-left:2px solid var(--calm); padding-left:8px; margin-top:6px; }
.due { font-family:var(--mono); font-size:11px; color:var(--ink-soft); white-space:nowrap; }
.due.over { color:var(--crit); font-weight:600; }
.prio { font-family:var(--mono); font-size:10.5px; font-weight:700; padding:2px 7px; border-radius:6px; background:var(--calm-bg); color:var(--calm); }
.prio.crit { background:var(--crit-bg); color:var(--crit); }
.prio.warn { background:var(--warn-bg); color:var(--warn); }
.hora { font-family:var(--mono); font-weight:700; font-size:13px; color:var(--brass); }
.dia-h { font-family:var(--serif); font-size:15px; font-weight:700; margin:16px 0 8px; color:var(--ink-soft); text-transform:capitalize; }
.dia-h:first-child { margin-top:0; }
.compact { display:flex; flex-direction:column; gap:7px; }
.row { display:flex; gap:12px; align-items:baseline; padding:9px 13px; background:var(--panel); border:1px solid var(--panel-edge); border-radius:9px; box-shadow:var(--shadow); flex-wrap:wrap; }
.row .dot { align-self:center; background:var(--brass-soft); }
.row .txt { font-size:13.5px; flex:1 1 60%; min-width:0; }
.row .txt b { font-weight:600; }
.row .note { font-size:12px; color:var(--ink-soft); font-family:var(--mono); }
.pessoa { background:var(--panel); border:1px solid var(--panel-edge); border-radius:10px; padding:11px 14px; box-shadow:var(--shadow); }
  .pessoa--orfa { border-style:dashed; opacity:.85; }
  .pessoa header { display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .pessoa header b { font-size:14px; }
  .pessoa header span { font-family:var(--mono); font-size:11px; color:var(--ink-faint); }
  .pessoa ul { margin:7px 0 0; padding-left:16px; font-size:12.5px; color:var(--ink-soft); }
  .pessoa li { margin-bottom:3px; }
  .pessoa li i { font-style:normal; color:var(--ink-faint); }
  .fio { margin:6px 0 0; padding-left:14px; border-left:2px solid var(--panel-edge); font-size:12.5px; color:var(--ink-soft); }
.fio div { padding:1px 0; }
.passou { font-size:12px; color:var(--ink-faint); margin:10px 0 0; padding-top:8px; border-top:1px dashed var(--panel-edge); }
  .vazio { font-size:13px; color:var(--ink-faint); font-style:italic; padding:10px 2px; }
details.dobra { margin-top:10px; }
details.dobra summary { cursor:pointer; font-size:12px; font-family:var(--mono); color:var(--brass); }
details.dobra .compact { margin-top:8px; }
footer { margin-top:44px; padding-top:18px; border-top:1px solid var(--panel-edge); font-size:12px; color:var(--ink-faint); }
footer code { font-family:var(--mono); font-size:11px; color:var(--brass); }
footer .limites { margin-top:8px; }
footer .limites b { color:var(--ink-soft); }
@media (max-width:560px){ .item{grid-template-columns:4px 1fr;} .item .aside{grid-column:2;padding:0 14px 13px 2px;align-items:flex-start;text-align:left;flex-direction:row;flex-wrap:wrap;} }
.anot { margin-top:10px; }
.row .anot { flex-basis:100%; }
.anot-toggle { font:inherit; font-size:11px; color:var(--ink-faint); background:transparent; border:1px dashed var(--panel-edge); border-radius:6px; padding:2px 9px; cursor:pointer; }
.anot-toggle:hover { border-color:var(--brass-soft); color:var(--ink-soft); }
.anot-toggle.on { border-style:solid; border-color:var(--brass); color:var(--brass); background:var(--panel); font-weight:600; }
.anot-panel { margin-top:8px; padding:9px 10px; border:1px solid var(--panel-edge); border-radius:8px; background:var(--paper); }
.anot-tags { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:7px; }
.anot-tags button { font:inherit; font-size:11px; padding:3px 11px; border-radius:999px; border:1px solid var(--panel-edge); background:var(--panel); color:var(--ink-soft); cursor:pointer; }
.anot-tags button:hover { border-color:var(--brass-soft); }
.anot-tags button[aria-pressed="true"] { background:var(--brass); border-color:var(--brass); color:var(--panel); font-weight:600; }
.anot-panel textarea { width:100%; font:12.5px/1.5 var(--sans); padding:7px 9px; border:1px solid var(--panel-edge); border-radius:6px; background:var(--panel); color:var(--ink); resize:vertical; }
.anot-panel textarea:focus { outline:2px solid var(--brass-soft); outline-offset:1px; }
.barra-fb { position:sticky; bottom:0; margin-top:22px; padding:12px 16px; background:var(--panel); border:1px solid var(--panel-edge); border-radius:10px; display:flex; gap:14px; justify-content:space-between; align-items:center; flex-wrap:wrap; box-shadow:0 -2px 16px rgba(35,33,28,.09); }
.barra-fb .progresso { font-size:13px; color:var(--ink-soft); }
.barra-fb .progresso b { font-family:var(--serif); font-size:19px; color:var(--brass); }
.barra-fb .acoes { display:flex; gap:9px; }
.barra-fb button { font:inherit; font-size:13px; padding:7px 15px; cursor:pointer; border-radius:7px; border:1px solid var(--brass); background:var(--brass); color:var(--panel); font-weight:600; }
.barra-fb button.ghost { background:transparent; color:var(--ink-faint); border-color:var(--panel-edge); font-weight:400; }
#fb-saida { width:100%; margin-top:10px; font:12px/1.55 var(--mono); padding:10px; border:1px solid var(--panel-edge); border-radius:7px; background:var(--paper); color:var(--ink); display:none; }
"""

# O JS de anotações veio da v0 escrita à mão e é preservado inteiro: ele funciona,
# o Renato já o usa, e o uid é hash do TEXTO do item — então continua casando
# item a item mesmo com a página regerada, desde que o título não mude.
#
# ⚠️ Raw string em constante do módulo, NUNCA dentro da f-string do render: as
# chaves do JavaScript colidem com a interpolação, e em 04/08 um `\n` não-raw
# partiu um script ao meio e derrubou todos os listeners sem erro visível.
JS = r"""
<script>
const CHAVE = "cockpit-index-anotacoes";
const salvo = JSON.parse(localStorage.getItem(CHAVE) || "{}");
const ROT = { feito:"Feito", adiado:"Adiado", cancelado:"Cancelado", nota:"Nota" };

function hash(s){ let h=0; for(let i=0;i<s.length;i++){ h=(h*31+s.charCodeAt(i))|0; } return "u"+(h>>>0).toString(36); }
function labelDe(el){ const t = el.querySelector(".title") || el.querySelector(".txt") || el; return t.textContent.replace(/\s+/g," ").trim().slice(0,96); }
function marcado(e){ return !!(e && (e.s || (e.txt||"").trim())); }
function persist(){ localStorage.setItem(CHAVE, JSON.stringify(salvo)); pintar(); }

document.querySelectorAll("[data-annot] .item, [data-annot] .row").forEach(el => {
  const label = labelDe(el);
  const uid = hash(label);
  el.dataset.uid = uid; el.dataset.label = label;
  const host = el.querySelector(".body") || el;
  const box = document.createElement("div");
  box.className = "anot";
  box.innerHTML =
    '<button type="button" class="anot-toggle">✎ anotar</button>' +
    '<div class="anot-panel" hidden>' +
      '<div class="anot-tags">' +
        '<button type="button" data-s="feito">Feito</button>' +
        '<button type="button" data-s="adiado">Adiar</button>' +
        '<button type="button" data-s="cancelado">Cancelar</button>' +
        '<button type="button" data-s="nota">Nota</button>' +
      '</div>' +
      '<textarea rows="2" placeholder="ex.: ficou pra próxima semana"></textarea>' +
    '</div>';
  host.appendChild(box);

  const panel = box.querySelector(".anot-panel");
  const ta = box.querySelector("textarea");
  const e = salvo[uid];
  if (e) { ta.value = e.txt || ""; if (marcado(e)) panel.hidden = false; }

  box.querySelector(".anot-toggle").addEventListener("click", () => { panel.hidden = !panel.hidden; });
  box.querySelectorAll(".anot-tags button").forEach(b =>
    b.addEventListener("click", () => {
      const cur = salvo[uid] || {};
      const ns = cur.s === b.dataset.s ? "" : b.dataset.s;
      salvo[uid] = { s: ns, txt: ta.value, label };
      persist();
    }));
  ta.addEventListener("input", () => {
    const cur = salvo[uid] || {};
    salvo[uid] = { s: cur.s || "", txt: ta.value, label };
    persist();
  });
});

function pintar(){
  let n = 0;
  document.querySelectorAll("[data-uid]").forEach(el => {
    const e = salvo[el.dataset.uid];
    const box = el.querySelector(".anot"); if (!box) return;
    const toggle = box.querySelector(".anot-toggle");
    const on = marcado(e);
    toggle.classList.toggle("on", on);
    toggle.textContent = on ? ("✎ " + (e.s ? ROT[e.s] : "anotado")) : "✎ anotar";
    box.querySelectorAll(".anot-tags button").forEach(b =>
      b.setAttribute("aria-pressed", String(!!e && e.s === b.dataset.s)));
    if (on) n++;
  });
  document.getElementById("fb-n").textContent = n;
}

document.getElementById("fb-limpar").addEventListener("click", () => {
  Object.keys(salvo).forEach(k => delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.querySelectorAll(".anot textarea").forEach(t => t.value = "");
  document.getElementById("fb-saida").style.display = "none";
  pintar();
});

document.getElementById("fb-copiar").addEventListener("click", async () => {
  const L = ["ANOTAÇÕES DO COCKPIT (frentes/tarefas) — cole no Claude"];
  let c = 0;
  document.querySelectorAll("[data-uid]").forEach(el => {
    const e = salvo[el.dataset.uid]; if (!e) return;
    const txt = (e.txt || "").trim();
    if (!e.s && !txt) return;
    c++;
    const st = e.s ? ROT[e.s].toUpperCase() : "NOTA";
    L.push("• " + el.dataset.label + " — " + st + (txt ? ": " + txt : ""));
  });
  const txt = L.join("\n");
  const out = document.getElementById("fb-saida");
  const btn = document.getElementById("fb-copiar");
  out.value = c ? txt : "(nada anotado ainda)";
  out.style.display = "block";
  try { await navigator.clipboard.writeText(txt); btn.textContent = "Copiado (" + c + ")"; }
  catch (err) { btn.textContent = "Copie abaixo"; out.select(); }
  setTimeout(() => btn.textContent = "Copiar para o Claude", 1800);
});

pintar();

// Reload na MESMA aba: pega a próxima geração, mas NUNCA enquanto você anota
// (textarea em foco ou painel aberto) e preservando o scroll — senão a página
// pula pro topo no meio da leitura.
const _sc = sessionStorage.getItem("cockpit-scroll");
if (_sc) window.scrollTo(0, parseInt(_sc, 10));
setInterval(() => {
  const digitando = document.activeElement && document.activeElement.closest(".anot");
  const painelAberto = document.querySelector(".anot-panel:not([hidden])");
  if (!digitando && !painelAberto) {
    sessionStorage.setItem("cockpit-scroll", String(window.scrollY));
    location.reload();
  }
}, 60000);
</script>
"""


# ------------------------------------------------------------------- render --

def render(d, cur_cos, cos_em):
    agora = datetime.now(BRT)
    hoje = d["hoje"]
    sep = separar(d)
    itens_cos = (cur_cos or {}).get("itens") or {}

    def cos(chave):
        return itens_cos.get(chave) or {}

    def prio_cls(p):
        return "crit" if (p or 0) >= 8 else ("warn" if (p or 0) >= 6 else "")

    def chip_ato(t):
        """O sinal 'ele já agiu' — como PERGUNTA, nunca como veredito.

        Nasce da reclamação recorrente do Renato (5 falhas medidas em 06/08): o
        sistema só sabe o que a camada cria e é cego ao que ele faz direto, então
        oferece o que já foi feito. Mas o contrário — a página CONCLUIR que está
        resolvido e sumir com o item — seria pior: some com a tarefa viva.
        Sinaliza, e quem decide é ele.
        """
        if not t.get("ato_quando"):
            return ""
        quem = "a Andressa" if t["ato_quem"] == "executor" else "você"
        canal = {"whatsapp": "WhatsApp", "email": "e-mail", "agenda": "agenda",
                 "whatsapp_grupo": "grupo"}.get(t["ato_canal"], t["ato_canal"] or "")
        return (f'<span class="chip ato" title="{esc(t["ato_evidencia"])}">'
                f'{quem} mexeu nisso {data_br(t["ato_quando"])} · {esc(canal)}</span>')

    def item(t, quando_txt, cls=""):
        c = cos(f"task:{t['id']}")
        if c.get("ocultar"):
            return ""
        prio = c.get("prio") or t["prioridade"] or 5
        porque = c.get("porque") or t["contexto"] or ""
        proj = f'<span class="chip">{esc(t["projeto"])[:34]}</span>' if t["projeto"] else ""
        pessoa = f'<span class="chip">{esc(t["contato"])[:26]}</span>' if t["contato"] else ""
        marca_cos = '<span class="chip cos">CoS</span>' if c.get("porque") else ""
        return f"""<div class="item {cls}">
        <div class="stripe"></div>
        <div class="body">
          <div class="title">{esc(t['titulo'])}</div>
          {f'<div class="why">{esc(porque)}</div>' if porque else ''}
          <div class="meta">{proj}{pessoa}{marca_cos}{chip_ato(t)}<span class="id">#{t['id']}</span></div>
        </div>
        <div class="aside"><span class="due{' over' if cls == 'p-crit' else ''}">{esc(quando_txt)}</span>
          <span class="prio {prio_cls(prio)}">P{prio}</span></div>
      </div>"""

    def evento(e):
        c = cos(f"evento:{e['id']}")
        if c.get("ocultar"):
            return ""
        passou = (not e["all_day"]) and e["start_datetime"] < agora.replace(tzinfo=None)
        local = e["location"] or ("remoto" if e["conference_url"] else "")
        porque = c.get("porque") or ""
        return f"""<div class="item">
        <div class="stripe"></div>
        <div class="body">
          <div class="title"><span class="hora">{hora_evt(e)}</span> &nbsp;{esc(e['summary'])}</div>
          {f'<div class="why">{esc(porque)}</div>' if porque else ''}
          <div class="meta">{f'<span class="chip">{esc(local)[:40]}</span>' if local else ''}
            {f'<span class="chip">{esc(e["contato"])[:26]}</span>' if e['contato'] else ''}
            {'<span class="chip">já passou</span>' if passou else ''}
            {'<span class="chip cos">CoS</span>' if porque else ''}</div>
        </div>
        <div class="aside"><span class="due">agenda</span></div>
      </div>"""

    # --- O PORTÃO (julgamento da camada) -------------------------------------
    # Idade POR FRENTE, não da rodada: o `fundir` do agente preserva a frente
    # não re-julgada marcando `_meta.herdado`, então uma rodada de 10 minutos
    # atrás pode carregar um julgamento de anteontem. Mostrar a idade da rodada
    # seria vender foto velha como atual — exatamente o defeito que a tonIAH
    # tinha e que consertei hoje.
    portao_html, portao_aviso, n_portao = "", "", 0
    rev = d.get("review")
    if rev:
        pay = rev["payload"]
        if isinstance(pay, str):
            try:
                pay = json.loads(pay)
            except ValueError:
                pay = {}
        horas_rev = float(rev["horas"]) if rev["horas"] is not None else None
        if horas_rev is not None and horas_rev > 20:
            portao_aviso = (
                f'<div class="alerta-cano">A camada não julga há <b>{horas_rev/24:.0f} dia(s)</b>. '
                f'O portão abaixo é o último que ela produziu — trate como histórico, não como hoje. '
                f'O resto desta página (agenda, tarefas, WhatsApp) não depende dela.</div>')
        linhas = []
        for f in (pay.get("frentes") or []):
            pv = f.get("precisa_de_voce") or {}
            if not pv.get("sim"):
                continue
            if f.get("project_id") in (d.get("portao_resolvido") or set()):
                continue          # ele já marcou feito hoje
            c = cos(f"frente:{f.get('project_id')}")
            if c.get("ocultar"):
                continue
            meta = f.get("_meta") or {}
            idade = ""
            if meta.get("julgado_em"):
                try:
                    q = datetime.fromisoformat(str(meta["julgado_em"]).replace("Z", "+00:00"))
                    h = (datetime.now(timezone.utc) - q).total_seconds() / 3600
                    idade = (f'<span class="chip">julgado há {h:.0f}h</span>' if h < 24
                             else f'<span class="chip velho">julgado há {h/24:.0f}d</span>')
                except Exception:
                    pass
            n_portao += 1
            linhas.append(f"""<div class="item p-crit">
        <div class="stripe"></div>
        <div class="body">
          <div class="title">{esc(f.get('frente'))}</div>
          <div class="why">{esc(pv.get('o_que') or pv.get('pergunta') or '')}</div>
          {f'<div class="why cos-diz">{esc(c["porque"])}</div>' if c.get("porque") else ''}
          <div class="meta">{idade}{'<span class="chip cos">CoS</span>' if c.get('porque') else ''}
            <span class="id">#{f.get('project_id')}</span></div>
        </div>
        <div class="aside"><span class="due">portão</span></div>
      </div>""")
        portao_html = "".join(linhas)

    # --- HOJE ---------------------------------------------------------------
    # O QUE JÁ PASSOU SAI DA FRENTE (07/08). Ele marcou como "FEITO" as quatro
    # reuniões do dia que já tinham acontecido — não porque houvesse algo a
    # fazer nelas, mas porque estavam ocupando a tela. Compromisso das 10h às
    # 17h não é informação: é entulho entre ele e o que ainda falta. Fica numa
    # linha só, porque sumir de vez tiraria a memória do dia.
    ev_hoje = [e for e in d["eventos"] if e["start_datetime"].date() == hoje]
    agora_naive = agora.replace(tzinfo=None)
    ev_passados = [e for e in ev_hoje
                   if not e["all_day"] and e["start_datetime"] < agora_naive]
    ev_futuros = [e for e in ev_hoje if e not in ev_passados]
    hoje_html = "".join([evento(e) for e in ev_futuros]
                        + [item(t, "hoje", "p-crit") for t in sep["hoje"]])
    passados_html = ""
    if ev_passados:
        itens = " · ".join(f"{e['start_datetime']:%H:%M} {esc(e['summary'])[:38]}"
                           for e in ev_passados)
        passados_html = (f'<p class="passou">✓ já passou hoje — {itens}</p>')

    # --- ESTA SEMANA (por dia) ----------------------------------------------
    dias = {}
    for e in d["eventos"]:
        if e["start_datetime"].date() > hoje:
            dias.setdefault(e["start_datetime"].date(), []).append(("e", e))
    for t in sep["semana"]:
        dias.setdefault(t["data_vencimento"].date(), []).append(("t", t))
    semana_html = ""
    for dia in sorted(dias):
        linhas = "".join(evento(x) if k == "e" else item(x, "vence") for k, x in dias[dia])
        semana_html += (f'<div class="dia-h">{DIAS_SEMANA[dia.weekday()]} · {dia.day} {MESES[dia.month-1]}</div>'
                        f'<div class="items">{linhas}</div>')

    # --- VENCIDAS -----------------------------------------------------------
    # Corta em 8 e DIZ quantas ficaram na dobra. Truncar calado seria a mesma
    # classe de defeito que a página combate: a lista pareceria completa.
    vencidas = sorted(sep["vencidas"], key=lambda x: (-(x["prioridade"] or 0), -x["atraso"]))
    venc_html = "".join(item(t, f"{t['atraso']}d atrás", "p-crit") for t in vencidas[:8])
    venc_resto = ""
    if len(vencidas) > 8:
        venc_resto = (f'<details class="dobra"><summary>+ {len(vencidas)-8} vencidas de prioridade menor'
                      f'</summary><div class="items">'
                      + "".join(item(t, f"{t['atraso']}d atrás", "p-crit") for t in vencidas[8:])
                      + "</div></details>")

    # --- AGUARDANDO TERCEIRO, POR PESSOA ------------------------------------
    # Pedido dele (07/08): "seria interessante ter uma visão por pessoa (quem
    # estou aguardando), consolidando as tarefas". A lista plana respondia "o
    # que está parado"; agrupada por pessoa responde "a quem eu cobro" — que é
    # a pergunta que se resolve com UMA mensagem em vez de cinco.
    #
    # Ordena por quantidade: três coisas paradas com a mesma pessoa é uma
    # conversa só, e é a que rende mais.
    # A ficha DELE não é um terceiro. Tarefa vinda do Fathom é ligada ao
    # participante da reunião — que é ele — e caía aqui como "aguardando", ou
    # seja, ele esperando ele mesmo. Mesma família do eco da camada: quando o
    # sistema encontra o próprio dono do outro lado da relação, não é relação.
    por_pessoa = {}
    for t in sep["aguardando"]:
        quem = t["contato"]
        if t.get("contact_id") == d.get("eu_contact_id"):
            quem = "— sem terceiro definido"
        por_pessoa.setdefault(quem or "— sem pessoa na ficha", []).append(t)
    aguard = ""
    for pessoa, ts in sorted(por_pessoa.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        # A ficha órfã não é detalhe de exibição: task sem `contact_id` não
        # cruza com WhatsApp nenhum, então ela nunca fecha sozinha. Aqui isso
        # fica visível em vez de virar uma linha a mais.
        orfa = pessoa.startswith("—")
        itens = "".join(
            f'<li>{esc(t["titulo"])[:88]}'
            f'{" <i>— " + esc(t["on_hold_reason"])[:70] + "</i>" if t["on_hold_reason"] else ""}'
            f' <span class="id">#{t["id"]} · {t["atraso"]}d</span></li>'
            for t in sorted(ts, key=lambda x: -x["atraso"]))
        mais_velho = max(t["atraso"] for t in ts)
        aguard += (
            f'<div class="pessoa{" pessoa--orfa" if orfa else ""}">'
            f'<header><b>{esc(pessoa)}</b>'
            f'<span>{len(ts)} item(ns) · mais antigo {mais_velho}d</span></header>'
            f'<ul>{itens}</ul></div>')

    # --- CHEGOU NO WHATSAPP -------------------------------------------------
    # Some sozinha quando zera — o zero-state que, escrito à mão, dependia de
    # alguém lembrar de apagar a seção.
    wa = []
    visiveis = [g for g in d["checkg"] if not cos(f"checkg:{g['contact_id']}").get("ocultar")]
    escondidos = max(0, len(visiveis) - 12)
    for g in visiveis[:12]:
        fio = "".join(f"<div>· {esc((m or '').replace(chr(10), ' ')[:130])}</div>" for m in (g["fio"] or []))
        wa.append(f'<div class="row"><span class="dot warn"></span><span class="txt">'
                  f'<b>{esc(g["contato"] or "sem ficha")}</b> — {g["n"]} mensagem(ns), última {data_br(g["ultima"])} '
                  f'{g["ultima"]:%H:%M}{" · você já respondeu depois" if g["agiu_depois"] else ""}'
                  f'<div class="fio">{fio}</div></span>'
                  f'<span class="note">#{g["contact_id"] or "—"}</span></div>')

    # --- SEM DATA (dobra) ---------------------------------------------------
    por_proj = {}
    for t in sep["sem_data"]:
        por_proj.setdefault(t["projeto"] or "sem projeto", []).append(t)
    sem_data = "".join(
        f'<div class="row"><span class="dot"></span><span class="txt"><b>{esc(p)}</b> — '
        + "; ".join(esc(x["titulo"])[:70] for x in ts[:4])
        + (f" <i>(+{len(ts)-4})</i>" if len(ts) > 4 else "")
        + f'</span><span class="note">{len(ts)}</span></div>'
        for p, ts in sorted(por_proj.items(), key=lambda kv: -len(kv[1])))

    # --- ANIVERSÁRIOS -------------------------------------------------------
    aniv = "".join(
        f'<div class="row"><span class="dot"></span><span class="txt">'
        f'<b>{esc(a["summary"])}</b></span><span class="note">{data_br(a["start_datetime"])}</span></div>'
        for a in d["aniversarios"])

    # --- BLOCOS LIVRES DA CoS -----------------------------------------------
    blocos = ""
    for b in (cur_cos or {}).get("blocos") or []:
        linhas = "".join(
            f'<div class="row"><span class="dot"></span><span class="txt">{esc(l.get("texto"))}</span>'
            f'<span class="note">{esc(l.get("ref") or "")}</span></div>'
            for l in b.get("linhas") or [])
        blocos += (f'<section data-annot><div class="head"><h2>{esc(b.get("titulo"))}</h2>'
                   f'<div class="rule"></div><span class="tag">curadoria CoS</span></div>'
                   f'<div class="compact">{linhas}</div></section>')

    # --- FRESCOR DOS CANOS --------------------------------------------------
    LIMITES = {"agenda": (1, 6), "whatsapp": (2, 12), "email": (3, 24),
               "grupos": (12, 48), "checkg": (26, 72), "camada": (4, 24)}
    ROTULO = {"agenda": "agenda", "whatsapp": "WhatsApp", "email": "e-mail",
              "grupos": "grupos", "checkg": "check-G", "camada": "camada"}
    canos, frios = [], []
    for k, (ok, mau) in LIMITES.items():
        ts = d["canos"][k]
        if not ts:
            canos.append(f'<span class="cano"><span class="dot crit"></span>{ROTULO[k]} <b>nunca</b></span>')
            frios.append(ROTULO[k])
            continue
        # Nem toda coluna de tempo do banco é naive: `cos_daily_review.run_at` é
        # TIMESTAMPTZ e as outras não. Normaliza pra UTC naive antes de subtrair.
        if ts.tzinfo:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        h = (datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds() / 3600
        cls = "" if h < ok else ("warn" if h < mau else "crit")
        if cls == "crit":
            frios.append(ROTULO[k])
        rot = f"{h*60:.0f}min" if h < 1 else f"{h:.0f}h"
        canos.append(f'<span class="cano"><span class="dot {cls}"></span>{ROTULO[k]} <b>{rot}</b></span>')
    alerta = ""
    if frios:
        alerta = (f'<div class="alerta-cano"><b>Esta página está lendo dado velho de: '
                  f'{esc(", ".join(frios))}.</b> O que você fez por esses canais pode não estar aqui — '
                  f'foi exatamente assim que a agenda ficou 3 meses parada sem ninguém ver.</div>')

    # --- NOTA DA CoS + IDADE DA CURADORIA -----------------------------------
    nota = ""
    if (cur_cos or {}).get("_erro"):
        nota = (f'<div class="alerta-cano">A curadoria da CoS não pôde ser lida '
                f'(<code>{esc(cur_cos["_erro"])}</code>) — a página saiu só com o chão do banco.</div>')
    elif (cur_cos or {}).get("nota_do_dia"):
        idade = ""
        if cos_em:
            dh = (agora.replace(tzinfo=None) - cos_em.replace(tzinfo=None)).total_seconds() / 3600
            idade = (f'<span class="velha">escrita há {dh/24:.0f} dia(s) — pode estar desatualizada</span>'
                     if dh > 20 else f"escrita há {dh:.0f}h")
        nota = (f'<div class="nota-cos"><span class="quem">leitura da CoS · {idade}</span>'
                f'{esc(cur_cos["nota_do_dia"])}</div>')

    # Conta o que FALTA, não o que o dia teve: o statcard existe pra decidir,
    # não pra relatar. Somar reunião das 10h às 17h infla o número que ele usa
    # pra saber se ainda tem dia pela frente.
    n_hoje = len(ev_futuros) + len(sep["hoje"])
    return f"""<!doctype html>
<html lang="pt-BR" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cockpit tonIAH · Renato</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <header class="mast">
    <div class="mast-top">
      <div class="brand">Cockpit <em>tonIAH</em></div>
      <div class="stamp"><b>{DIAS_SEMANA[hoje.weekday()].capitalize()} · {hoje.day:02d} {MESES[hoje.month-1]} {hoje.year} · {agora:%Hh%M}</b><br>
        gerado do banco · atualiza a cada 1 min</div>
    </div>
    <div class="canos">{''.join(canos)}</div>
  </header>
  {alerta}
  {nota}

  <div class="summary">
    <div class="stat{' hot' if n_portao else ''}"><div class="n">{n_portao}</div><div class="l">No portão</div></div>
    <div class="stat{' hot' if n_hoje else ''}"><div class="n">{n_hoje}</div><div class="l">Falta hoje</div></div>
    <div class="stat"><div class="n">{len(sep['semana']) + len([e for e in d['eventos'] if e['start_datetime'].date() > hoje])}</div><div class="l">Próximos 8 dias</div></div>
    <div class="stat{' hot' if sep['vencidas'] else ''}"><div class="n">{len(sep['vencidas'])}</div><div class="l">Vencidas</div></div>
    <div class="stat"><div class="n">{len(d['checkg'])}</div><div class="l">Chegou no WA</div></div>
    <div class="stat"><div class="n">{len(sep['aguardando'])}</div><div class="l">Aguardando terceiro</div></div>
  </div>

  {portao_aviso}
  {f'''<section data-annot>
    <div class="head"><h2>O portão</h2><div class="rule"></div><span class="tag">julgamento da camada · {n_portao}</span></div>
    <p class="lead">O que a camada concluiu que precisa de você — com o porquê dela. É a única
    parte desta página que é <b>juízo</b>, não fato: precisão medida de 75%, então leia como
    recomendação forte, não como ordem.</p>
    <div class="items">{portao_html}</div>
  </section>''' if portao_html else ''}

  <section data-annot>
    <div class="head"><h2>Hoje</h2><div class="rule"></div><span class="tag">agenda + vence hoje</span></div>
    <div class="items">{hoje_html or '<p class="vazio">Nada mais na agenda e nada vencendo hoje.</p>'}</div>
    {passados_html}
  </section>

  {f'''<section data-annot>
    <div class="head"><h2>Chegou no WhatsApp</h2><div class="rule"></div><span class="tag">72h · triado pelo check-G</span></div>
    <p class="lead">O fio, não uma amostra: até 3 mensagens por pessoa. O pedido real costuma ser
    a mensagem ao lado do anexo, não o anexo.
    {f"Mostrando as 12 conversas mais recentes — <b>outras {escondidos} ficaram de fora desta lista</b>." if escondidos else ""}</p>
    <div class="compact">{''.join(wa)}</div>
  </section>''' if wa else ''}

  {f'''<section data-annot>
    <div class="head"><h2>Em andamento</h2><div class="rule"></div><span class="tag">você já começou</span></div>
    <div class="items">{''.join(item(t, "em curso") for t in sep["andamento"])}</div>
  </section>''' if sep["andamento"] else ''}

  {f'''<section data-annot>
    <div class="head"><h2>Vencidas</h2><div class="rule"></div><span class="tag">decidir: fazer ou baixar</span></div>
    <div class="items">{venc_html}</div>{venc_resto}
  </section>''' if venc_html.strip() else ''}

  <section data-annot>
    <div class="head"><h2>Próximos 8 dias</h2><div class="rule"></div><span class="tag">por dia</span></div>
    {semana_html or '<p class="vazio">Semana livre.</p>'}
  </section>

  {f'''<section data-annot>
    <div class="head"><h2>Aguardando terceiro</h2><div class="rule"></div><span class="tag">não é dívida sua</span></div>
    <p class="lead">Bola com outra pessoa e ainda dentro do prazo. Passou de 7 dias, sobe pra Vencidas.</p>
    <div class="compact">{aguard}</div>
  </section>''' if aguard else ''}

  {blocos}

  {f'''<section>
    <div class="head"><h2>Aniversários</h2><div class="rule"></div><span class="tag">próximos 8 dias</span></div>
    <div class="compact">{aniv}</div>
  </section>''' if aniv else ''}

  <section>
    <div class="head"><h2>Sem data</h2><div class="rule"></div><span class="tag">{len(sep['sem_data'])} tarefas · por projeto</span></div>
    <details class="dobra"><summary>abrir — tarefa sem vencimento não aparece em nenhuma lista de prazo</summary>
      <div class="compact">{sem_data or '<p class="vazio">Nenhuma.</p>'}</div>
    </details>
  </section>

  <footer>
    <p>Gerado de <code>Neon</code> por <code>scripts/cockpit.py</code> em {agora:%d/%m/%Y %H:%M} BRT.
    Página local <code>~/cockpit/index.html</code> · custo de API do INTEL: <code>zero</code> ·
    par: <code>sistema.html</code> (observabilidade).</p>
    <p class="limites"><b>O que esta página NÃO sabe:</b> nada aqui é julgamento — a leitura da CoS
    entra por <code>~/cockpit/curadoria.json</code> e vem datada. Horário de evento é o do Google,
    <b>sem conversão</b> (o storage não é UTC; converter já mostrou reunião 3h adiantada).
    O chip “mexeu nisso” diz que <b>houve conversa depois</b>, não que está resolvido.
    Tarefa sem <code>contact_id</code> não cruza com WhatsApp nenhum — hoje são
    {len([t for t in d['tasks'] if not t['contact_id']])} de {len(d['tasks'])}.</p>
  </footer>

  <div class="barra-fb">
    <span class="progresso"><b id="fb-n">0</b> anotação(ões) — marque nos itens acima e copie</span>
    <div class="acoes">
      <button type="button" class="ghost" id="fb-limpar">Limpar</button>
      <button type="button" id="fb-copiar">Copiar para o Claude</button>
    </div>
  </div>
  <textarea id="fb-saida" rows="6" readonly></textarea>

</div>@@JS@@</body>
</html>""".replace("@@JS@@", JS)


def main():
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    d = coletar(cur, datetime.now(BRT).date())
    cur_cos, cos_em = carregar_curadoria()
    open(SAIDA, "w").write(render(d, cur_cos, cos_em))
    print(f"→ {SAIDA}")
    if "--quieto" not in sys.argv:
        subprocess.run(["open", SAIDA])


if __name__ == "__main__":
    sys.exit(main() or 0)
