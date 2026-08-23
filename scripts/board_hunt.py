#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/board_hunt.html — o funil de originação de conselhos, vivo.

PEDIDO (CoS, 07/08/2026, task #999763). O v0 desta página foi escrito à mão e
serve de contrato visual: fases em colunas, card por frente, cor pela
temperatura, mapa de conectores embaixo. Este script mantém o desenho e troca a
fonte — a página passa a se recalcular a cada 5 min pelo `launchd`.

POR QUE ISTO NÃO É COSMÉTICA. A informação principal desta tela é a PASSAGEM DO
TEMPO: "3 dias sem troca", "57 dias parado". Escrita à mão, ela continua dizendo
"3 dias" na semana seguinte — e numa tela de temperatura isso não é ficar
desatualizado, é mentir com cara de medida. Já há consumidor com hora marcada: a
retro da Máquina de Originação (#50) roda toda sexta 9h30 em cima deste funil.

O QUE É HUMANO mora em `board_hunt_frentes` (migration 067): fase, canal âncora,
originador, piso, nota. O QUE DERIVA é recalculado aqui e nunca gravado: dias sem
troca, temperatura, bola, próximo passo. Gravar o derivado seria refazer o
defeito do v0 numa tabela em vez de num HTML.

A RÉGUA (pedida pela CoS, com uma precedência que ela não especificou e eu
resolvi — está anotada em `temperatura()` junto com o efeito medido).

O CASAMENTO COM A AGENDA foi o osso da frente. O pedido dizia "próximo passo =
próximo calendar_event por contact_id", mas **7 dos 341 eventos têm contact_id**
(2%): a coluna nasceria vazia e o alerta "⚠ sem próximo passo" acusaria frentes
que TÊM reunião marcada — alarme falso em massa, a classe de defeito do
[[feedback_filtro_vocabulario_errado_falha_calado]]. A saída é casar também pelo
nome no título do evento, sem virar chute: um token casa sozinho só quando é RARO
no próprio CRM (`marson` está em 2 fichas, `rodrigo` em 115). Raridade é medida,
não adivinhada. A cobertura de cada caminho aparece no rodapé da página: régua
que não se mede vira régua em que se acredita.

O TERCEIRO CAMINHO — TASK (23/08/26, pedido do Renato). O alarme media mais
estreito que a regra que ele vigia: a regra do ritual (nota #50) tem três
destinos — *marcar*, *tocar* e *baixar consciente* — e só o primeiro vira
`calendar_event`. Medido no dia: **11 frentes acusadas "sem próximo passo", 10
com ato marcado em task pra semana seguinte**. O alarme acusava quem estava em
dia — a forma mais cara de errar numa tela de cobrança, porque ensina a ignorá-la.
Agora conta também `tasks` com vencimento à frente, e `on_hold` vira estado
próprio ("⏸ em espera") em vez de virar acusação. Ficou **1**: a Premix, cuja
única task mora em outro projeto — e está certo que apareça. Ver `proxima_task`
pro corte estrito e por que ele PERDE casos de propósito.

DUAS SAÍDAS. `board_hunt.html` é a tela de trabalho — tudo, com a telemetria da
régua à vista. `board_hunt_resumo.html` é uma folha A4 pra mandar a terceiros (o
pedido de 12/08 foi o pai): as mesmas frentes e datas, **sem as notas internas** —
resumir é escolher o que o leitor precisa, não encolher a fonte. Ver `render_resumo`.

Uso:  ./board_hunt.py            # gera as duas e abre a tela
      ./board_hunt.py --quieto   # gera sem abrir (pra launchd)
      ./board_hunt.py --pdf      # gera + converte o resumo em PDF (pra WhatsApp)
"""
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/board_hunt.html")
SAIDA_RESUMO = os.path.expanduser("~/cockpit/board_hunt_resumo.html")
PDF_RESUMO = os.path.expanduser("~/Downloads/board_hunt_resumo.pdf")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CANAIS = os.path.expanduser("~/cockpit/board_hunt_canais.json")
BRT = ZoneInfo("America/Sao_Paulo")

META_ASSENTOS = 2          # meta do Board Hunt 2026: 2 assentos remunerados
QUENTE_ATE = 14            # dias
MORNO_ATE = 45             # dias
RARO_ATE = 3               # nº de fichas no CRM abaixo do qual um sobrenome casa sozinho
JANELA_AGENDA = 45         # dias à frente que a página varre por "próximo passo"
NOTA_NO_CARD = 260         # caracteres de nota visíveis antes do "nota completa"

FASES = [(1, "Prospecção / Rede"), (2, "Aproximação"), (3, "Reunião / Descoberta"),
         (4, "Avaliação / Proposta"), (5, "Negociação"), (6, "Assento fechado")]

# Canais que ainda não produziram frente. É JULGAMENTO (quem vale acionar), não
# dado — então mora num overlay, como a curadoria do cockpit de frentes: a
# regeneração não pode apagá-lo. Semeado na primeira execução com a lista da
# nota #50.
CANAIS_SEED = [
    {"nome": "Lilian Schiavo", "nota": "CAMBRAPER / OBME / G100 — intros OBME em curso (Ornare, Deôla, Aços)"},
    {"nome": "Orestes (pai)", "nota": "Virtus BR — ponte seletiva"},
    {"nome": "Eleazar de Carvalho Filho", "nota": "ex-BNDES / Virtus"},
    {"nome": "Pretola + Panico (Orbiz)", "nota": "M&A — canal advisor"},
    {"nome": "Waldemar Lobo", "nota": "ASSESPRO — pool ~130 associadas"},
]

_VAZIAS = {"filho", "junior", "neto", "santos", "silva", "souza", "prado", "almeida"}


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sem_acento(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def tokens(nome):
    """Palavras de ≥4 letras de um nome, sem acento e sem os sobrenomes de bacia
    (`Silva`, `Filho`): esses casariam com meio CRM e não identificam ninguém."""
    return [p for p in re.findall(r"[a-z]{4,}", sem_acento(nome)) if p not in _VAZIAS]


def data_br(d):
    return f"{d.day:02d}/{d.month:02d}"


def _curto(t, n):
    """Corta no espaço, não no meio da palavra: `conversa (Consel` lido por um
    terceiro parece dado corrompido, não texto abreviado."""
    t = (t or "").strip()
    if len(t) <= n:
        return t
    corte = t[:n]
    if " " in corte[n // 2:]:
        corte = corte[:corte.rindex(" ")]
    return corte.rstrip(" ,;·-") + "…"


# ------------------------------------------------------------------ coleta --

def coletar(cur, hoje):
    d = {"hoje": hoje}

    cur.execute("""
        SELECT f.id, f.nome, f.subtitulo, f.fase, f.status, f.nota, f.piso_alvo,
               f.project_id, p.nome AS projeto,
               f.contato_id, c.nome AS contato,
               f.originador_contact_id, o.nome AS originador, f.originador_rotulo
          FROM board_hunt_frentes f
          LEFT JOIN projects p ON p.id = f.project_id
          LEFT JOIN contacts c ON c.id = f.contato_id
          LEFT JOIN contacts o ON o.id = f.originador_contact_id
         ORDER BY f.fase, f.id
    """)
    frentes = [dict(r) for r in cur.fetchall()]
    d["frentes"] = frentes

    ancoras = sorted({f["contato_id"] for f in frentes if f["contato_id"]})

    # --- ÚLTIMO TOQUE por âncora --------------------------------------------
    # DM e grupo na mesma pergunta: responder no grupo e deixar a DM muda é
    # comportamento normal dele, e cobrar por DM o que foi respondido no grupo já
    # aconteceu ([[feedback_cross_group_before_send]]). O canal vai pro card —
    # frente aquecida só por conversa de grupo não é a mesma coisa que troca 1:1,
    # e quem olha precisa poder ver a diferença.
    d["toques"] = {}
    if ancoras:
        cur.execute("""
            SELECT DISTINCT ON (t.cid) t.cid, t.direcao, t.quando, t.canal, t.trecho
              FROM (
                    SELECT m.contact_id AS cid, m.direcao,
                           COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS quando,
                           COALESCE(cv.canal, 'whatsapp') AS canal,
                           left(m.conteudo, 120) AS trecho
                      FROM messages m
                      LEFT JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE m.contact_id = ANY(%s)
                    UNION ALL
                    SELECT g.contact_id,
                           CASE WHEN g.from_me THEN 'outgoing' ELSE 'incoming' END,
                           COALESCE(g.timestamp, g.criado_em), 'grupo',
                           left(g.content, 120)
                      FROM group_messages g
                     WHERE g.contact_id = ANY(%s)
              ) t
             WHERE t.quando IS NOT NULL
             ORDER BY t.cid, t.quando DESC
        """, (ancoras, ancoras))
        d["toques"] = {r["cid"]: dict(r) for r in cur.fetchall()}

    # --- AGENDA -------------------------------------------------------------
    # start_datetime NÃO é UTC — é gravado na timezone do próprio evento (BRT na
    # prática). Converter subtrai 3h e mostra a reunião na hora errada
    # ([[feedback_calendar_events_tz]]). Lê-se raw.
    cur.execute("""
        SELECT id, summary, start_datetime, contact_id, all_day
          FROM calendar_events
         WHERE start_datetime >= %s AND start_datetime < %s
           AND COALESCE(status, 'confirmed') <> 'cancelled'
           AND COALESCE(summary, '') NOT ILIKE '%%nivers%%'
         ORDER BY start_datetime
    """, (datetime.now(BRT).replace(tzinfo=None), datetime.now(BRT).replace(tzinfo=None)
          + timedelta(days=JANELA_AGENDA)))
    d["eventos"] = [dict(r) for r in cur.fetchall()]

    # --- RARIDADE dos sobrenomes --------------------------------------------
    # Quantas fichas do CRM contêm cada token dos nomes envolvidos. É o que separa
    # "Marson" (2 fichas — identifica) de "Rodrigo" (115 — não identifica), e o
    # que impede a agenda de casar "Reunião: Srs. Rodrigo Sá" com o Rodrigo
    # Romero. Medido no corpus, não estimado.
    alvos = set()
    for f in frentes:
        alvos.update(tokens(f["contato"] or ""))
        alvos.update(tokens(f["nome"]))
    d["raridade"] = {}
    if alvos:
        cur.execute("""
            SELECT t.tok, count(c.id) AS n
              FROM unnest(%s::text[]) AS t(tok)
              LEFT JOIN contacts c
                     ON translate(lower(c.nome),
                                  'áàãâäéèêëíìîïóòõôöúùûüç',
                                  'aaaaaeeeeiiiiooooouuuuc') LIKE '%%' || t.tok || '%%'
             GROUP BY t.tok
        """, (sorted(alvos),))
        d["raridade"] = {r["tok"]: r["n"] for r in cur.fetchall()}

    # --- TAREFA COM DATA, o segundo destino da regra ------------------------
    # A regra do ritual (nota #50, 07/08) dá TRÊS destinos válidos a uma frente:
    # *marcar* (vira evento), *tocar* (WhatsApp/e-mail com data combinada) e
    # *baixar consciente* (on_hold). Só o primeiro virava `calendar_event` — os
    # outros dois viram TASK, e a página não as olhava. Efeito medido em 23/08:
    # o painel acusava 11 frentes "sem próximo passo" e 10 delas tinham ato
    # marcado em task pra semana seguinte. O alarme acusava quem estava em dia,
    # que é a forma mais cara de errar numa tela de cobrança.
    #
    # O CASAMENTO É ESTRITO — `contact_id` E `project_id` iguais aos da frente.
    # Só `contact_id` seria frouxo: a task #999870 ("Voltar ao Monforte") aponta
    # pro contato da Premix mas mora no projeto 65, e entraria como próximo passo
    # de uma frente que não é a dela. Preferi PERDER a Premix (ela aparece como
    # sem próximo passo, e está certo: não há ato agendado no projeto dela) a
    # calar o alarme com evidência de outro projeto. Alarme que emudece pelo
    # motivo errado é pior que o barulhento ([[feedback_guarda_abstencao_vira_fabrica]]).
    #
    # ⚠️ Não desambigua frentes que compartilham contato E projeto: MilClean (#6)
    # e Eduardo Marson (#7) são cid=18707/pid=60 as duas, e recebem a mesma task.
    # A agenda já tem exatamente esse comportamento pelo mesmo motivo — não
    # inventei desempate sem dado que o sustente.
    d["tasks"], d["esperas"] = {}, {}
    chaves = {(f["contato_id"], f["project_id"]) for f in frentes
              if f["contato_id"] and f["project_id"]}
    if chaves:
        cids = sorted({c for c, _ in chaves})
        pids = sorted({p for _, p in chaves})
        cur.execute("""
            SELECT id, titulo, contact_id, project_id, data_vencimento,
                   status, on_hold_since, on_hold_reason
              FROM tasks
             WHERE contact_id = ANY(%s) AND project_id = ANY(%s)
               AND status IN ('pending', 'in_progress', 'on_hold')
             ORDER BY data_vencimento NULLS LAST, id
        """, (cids, pids))
        limite = hoje + timedelta(days=JANELA_AGENDA)
        for r in cur.fetchall():
            t = dict(r)
            k = (t["contact_id"], t["project_id"])
            if k not in chaves:
                continue
            if t["status"] == "on_hold":
                # Espera consciente não é próximo passo — é o oposto, e a tela
                # tem que saber a diferença. Sem data no futuro, de propósito:
                # a #999851 está em hold com vencimento 18/08, no PASSADO, e
                # chamá-la de "próximo passo" seria mentir com cara de medida.
                d["esperas"].setdefault(k, t)
            elif t["data_vencimento"] and hoje <= t["data_vencimento"].date() < limite:
                d["tasks"].setdefault(k, t)   # a mais próxima: query já ordenada

    cur.execute("SELECT max(last_synced_at) AS agenda FROM calendar_events")
    d["frescor_agenda"] = cur.fetchone()["agenda"]

    # A cobertura do caminho oficial ("por contact_id"), medida — não citada de
    # memória. É ela que justifica existir um segundo caminho de casamento, e uma
    # justificativa que envelhece sem avisar é a mesma armadilha da página velha:
    # se o sync melhorar, este número tem que cair sozinho no rodapé.
    cur.execute("""SELECT count(*) AS total,
                          count(contact_id) AS com_contato
                     FROM calendar_events""")
    r = cur.fetchone()
    d["agenda_ligada_pct"] = round(100.0 * r["com_contato"] / r["total"], 1) if r["total"] else 0.0
    return d


# ------------------------------------------------------------------- régua --

def proximo_passo(frente, d):
    """Próxima reunião da frente. Devolve (evento, como_casou) ou (None, None).

    Dois caminhos, nesta ordem: `contact_id` no evento (certo, mas só 2% dos
    eventos têm) e o nome no título. O segundo só aceita casamento que
    IDENTIFICA: dois tokens do nome, ou um token raro no CRM. Um token comum
    sozinho nunca casa — foi o que impediu "Rodrigo Sá" de virar reunião do
    Rodrigo Romero."""
    cid = frente["contato_id"]
    for e in d["eventos"]:
        if cid and e["contact_id"] == cid:
            return e, "id"

    alvo = tokens(frente["contato"] or "") + tokens(frente["nome"])
    if not alvo:
        return None, None
    for e in d["eventos"]:
        titulo = sem_acento(e["summary"] or "")
        achados = {t for t in alvo if t in titulo}
        if len(achados) >= 2 or any(d["raridade"].get(t, 99) <= RARO_ATE for t in achados):
            return e, "nome"
    return None, None


def proxima_task(frente, d):
    """Tarefa com data marcada da frente, ou None.

    Terceiro caminho, e o mais fraco dos três de propósito: só vale quando a
    agenda não respondeu. Reunião marcada é compromisso com terceiro; task é
    compromisso só com ele — quando as duas existem, a agenda manda.

    Fica FORA da folha A4 (`render_resumo`). O título de uma task é caderno
    interno ("Mandar o 'oi' ao Marcelo — preparar o texto antes"), e o resumo é
    peça que sai pra terceiros; foi por vazar um título assim que o PDF de 12/08
    precisou de conserto. Na peça externa, frente sem reunião continua lendo
    "sem data marcada" — o que é verdade: task não é reunião."""
    return _por_chave(frente, d["tasks"])


def espera_consciente(frente, d):
    """Task em `on_hold` da frente — o terceiro destino da regra do ritual."""
    return _por_chave(frente, d["esperas"])


def _por_chave(frente, balde):
    """Lookup por (contato, projeto), com a guarda contra a chave meia-vazia.

    Frente sem `project_id` não pode casar: a chave `(cid, None)` acharia
    qualquer task daquele contato sem projeto, que é exatamente o casamento
    frouxo que o corte estrito existe pra impedir. Hoje `coletar()` já não gera
    essas chaves — a guarda está aqui porque isso é invariante de OUTRA função,
    e a próxima frente cadastrada sem projeto não pode depender disso."""
    cid, pid = frente["contato_id"], frente["project_id"]
    if not cid or not pid:
        return None
    return balde.get((cid, pid))


def temperatura(dias, bola, tem_proximo):
    """Quente ≤14d · morno 15–45d · frio >45d, com a bola rebaixando na faixa do
    meio.

    A CoS pediu "morno: 15–45d **ou** bola c/ terceiro" e "frio: >45d **ou**
    parado contigo", sem dizer o que ganha quando as duas cláusulas discordam — e
    elas discordam no caso mais comum do funil: troca de ontem com a bola no
    outro lado. Tomei a precedência mais defensável: **o tempo manda**, porque é
    o que a tela existe pra mostrar; a bola só decide na faixa morna, onde ela é
    a diferença real entre "esperando alguém" e "parado comigo".

    Conferido contra as 10 frentes que a CoS classificou à mão: bate em 9. A
    única diferença é o Ariolino, que ela deixou frio (57 dias) e a régua vê
    morno — porque o Renato mandou WhatsApp pra ele HOJE, depois de ela escrever
    a página. O v0 envelheceu em oito horas; é literalmente o defeito que este
    gerador nasceu pra matar."""
    if dias is None:
        return "indefinido"
    if dias > MORNO_ATE:
        return "frio"
    if dias <= QUENTE_ATE:
        # Troca fresca, mas esperando o outro sem data marcada: não é quente, é
        # torcida. Com reunião na agenda, segue quente — há para onde ir.
        return "morno" if (bola == "terceiro" and not tem_proximo) else "quente"
    return "frio" if bola == "voce" else "morno"


def avaliar(d):
    hoje = d["hoje"]
    cobertura = {"id": 0, "nome": 0, "task": 0, "sem": 0}
    for f in d["frentes"]:
        t = d["toques"].get(f["contato_id"])
        f["toque"] = t
        f["dias"] = (hoje - t["quando"].date()).days if t else None
        # Última mensagem enviada = a vez é do outro; recebida = a vez é dele.
        # ⚠️ Cortesia de encerramento ("Gratos", "Recebido, até 2ª") inverte isso
        # sem que a vez tenha mudado — por isso o card mostra a EVIDÊNCIA (trecho
        # + data) junto da tag, em vez de só afirmar de quem é a bola.
        f["bola"] = None if not t else ("terceiro" if t["direcao"] == "outgoing" else "voce")

        ev, como = proximo_passo(f, d)
        f["evento"], f["evento_como"] = ev, como
        # Task só é consultada quando a agenda não respondeu — a precedência está
        # em `proxima_task`. `f["task"]` é campo PRÓPRIO, nunca `f["evento"]`:
        # tudo que consome `evento` (barra de próximos passos, folha A4, PDF)
        # continua vendo só agenda, e a task não escapa pra peça externa.
        f["task"] = None if ev else proxima_task(f, d)
        f["espera"] = espera_consciente(f, d) if not (ev or f["task"]) else None
        cobertura[como or ("task" if f["task"] else "sem")] += 1

        if f["status"] == "hold":
            f["temp"] = "hold"
        elif f["status"] == "prova":
            f["temp"] = "prova"
        else:
            # Ato marcado em task conta como "tem para onde ir" tanto quanto
            # reunião: era o que fazia troca fresca com a bola no outro lado cair
            # pra morno mesmo tendo toque agendado pra amanhã.
            f["temp"] = temperatura(f["dias"], f["bola"], bool(ev or f["task"]))

    d["cobertura"] = cobertura
    ativas = [f for f in d["frentes"] if f["status"] == "ativo"]
    d["kpis"] = {
        "assentos": sum(1 for f in d["frentes"] if f["status"] == "fechado"),
        "funil": len(ativas),
        "quentes": sum(1 for f in ativas if f["temp"] == "quente"),
        # "Sem próximo passo" passa a significar o que a regra sempre disse: nem
        # reunião marcada, nem ato com data, nem espera assumida. Frente em hold
        # declarado sai da conta por já ser um destino válido.
        "sem_prox": sum(1 for f in ativas
                        if not (f["evento"] or f["task"] or f["espera"])),
        "em_espera": sum(1 for f in ativas if f["espera"]),
    }
    return d


def carregar_canais():
    if not os.path.exists(CANAIS):
        json.dump(CANAIS_SEED, open(CANAIS, "w"), ensure_ascii=False, indent=2)
        return CANAIS_SEED
    try:
        return json.load(open(CANAIS))
    except Exception:
        return CANAIS_SEED


# ------------------------------------------------------------------ render --

CSS = """
  :root{
    --charcoal:#2b2925; --ink:#3a362f; --muted:#8a8072;
    --creme:#f6f1e7; --card:#fffdf8; --line:#e5ddcd;
    --dourado:#b8973e; --dourado-soft:#e9dcb8;
    --quente:#c0492f; --quente-bg:#fae9e3;
    --morno:#c69a2e; --morno-bg:#f9f0d6;
    --frio:#3f6f92; --frio-bg:#e3edf4;
    --hold:#9a9184; --hold-bg:#eeeae1;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--creme);color:var(--ink);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:26px 30px 60px}
  header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
    border-bottom:2px solid var(--dourado);padding-bottom:14px;margin-bottom:6px}
  h1{font-size:23px;font-weight:700;color:var(--charcoal);letter-spacing:.2px}
  h1 .dot{color:var(--dourado)}
  .meta{margin-left:auto;font-size:13px;color:var(--muted)}
  .meta b{color:var(--charcoal)}
  .metabar{display:flex;gap:22px;flex-wrap:wrap;margin:14px 0 20px;font-size:13.5px}
  .metabar .kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:9px 16px}
  .metabar .kpi b{font-size:19px;color:var(--charcoal);display:block;font-weight:700}
  .metabar .kpi span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);
    margin-bottom:18px;align-items:center}
  .chip{display:inline-flex;align-items:center;gap:6px}
  .sw{width:11px;height:11px;border-radius:50%}
  /* Faixas empilhadas por fase, não colunas. O kanban de 7 colunas fixas media
     1.708px e o `overflow-x` jogava fora da tela justamente Negociação, Assento
     fechado e On-hold — as três que decidem ("ficou difícil visualizar", Renato,
     12/08). Empilhado, o funil se lê de cima pra baixo e o grid usa a largura
     que existir, seja notebook ou monitor. */
  .board{display:flex;flex-direction:column;gap:13px}
  .col{background:transparent;border-top:1px solid var(--line);padding-top:9px}
  .col > h2{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;
    color:var(--charcoal);padding:0 2px 9px;display:flex;align-items:center;gap:9px}
  .col > h2 .fase{background:var(--dourado);color:#fff;font-size:10.5px;width:18px;height:18px;
    border-radius:50%;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}
  .col > h2 .n{color:var(--muted);font-weight:600;margin-left:auto;text-transform:none;
    letter-spacing:0;font-size:11.5px}
  .col.hold{border-top-style:dashed}
  .col.hold > h2{color:var(--hold)}
  .col.hold > h2 .fase{background:var(--hold)}
  .col.vazia > h2{opacity:.5}
  /* `align-items:start`: sem isso o grid estica todo card à altura do maior da
     faixa, e uma nota longa infla os vizinhos com espaço vazio. */
  .stack{display:grid;grid-template-columns:repeat(auto-fill,minmax(248px,1fr));gap:10px;
    align-items:start}
  .vazio{color:var(--muted);font-size:12px;padding:4px}
  .cardw{background:var(--card);border:1px solid var(--line);border-left-width:4px;
    border-radius:9px;padding:11px 12px;box-shadow:0 1px 2px rgba(80,66,40,.05)}
  .cardw.quente{border-left-color:var(--quente)}
  .cardw.morno{border-left-color:var(--morno)}
  .cardw.frio{border-left-color:var(--frio)}
  .cardw.hold,.cardw.prova{border-left-color:var(--hold);opacity:.85}
  .cardw.indefinido{border-left-color:var(--line);border-left-style:dashed}
  .cardw .t{font-weight:700;color:var(--charcoal);font-size:14.5px;line-height:1.25}
  .cardw .sub{color:var(--muted);font-size:11.5px;margin-top:1px}
  .cardw .n{font-size:12.5px;color:var(--ink);margin-top:7px}
  /* A nota é o caderno da operação e cresce sem teto — há frentes com 1.500
     caracteres. Aberta por padrão, uma faixa passava de uma tela de altura e o
     problema do scroll horizontal virava scroll vertical. Fica o começo; o resto
     abre com um clique (`<details>` nativo, sem JS). */
  .cardw details{margin-top:5px}
  .cardw details summary{font-size:11.5px;color:var(--dourado);cursor:pointer;
    list-style:none;font-weight:600}
  .cardw details summary::-webkit-details-marker{display:none}
  .cardw details summary::before{content:"▸ ";font-size:10px}
  .cardw details[open] summary::before{content:"▾ "}
  .cardw details .n{margin-top:5px;padding-top:6px;border-top:1px dotted var(--line)}
  .tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px;font-size:11px}
  .tag{background:var(--dourado-soft);color:#6b551d;border-radius:20px;padding:2px 8px;
    white-space:nowrap}
  .tag.temp-quente{background:var(--quente-bg);color:var(--quente)}
  .tag.temp-morno{background:var(--morno-bg);color:#8a6a12}
  .tag.temp-frio{background:var(--frio-bg);color:var(--frio)}
  .tag.temp-indefinido{background:#f0ece3;color:var(--muted)}
  .tag.bola{background:#eee7d8;color:var(--ink);cursor:help}
  .tag.grupo{background:#e7eef0;color:#42646e}
  .nextbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;background:var(--card);
    border:1px solid var(--line);border-radius:11px;padding:11px 16px;margin-bottom:20px;font-size:13px}
  .nextbar .nl{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dourado);
    font-weight:700;margin-right:4px}
  .nextbar .ne{background:#eef3e9;color:#4a6035;border-radius:7px;padding:4px 10px}
  .nextbar .ne b{color:#33471f}
  .nextbar .ne.dim{background:transparent;color:var(--muted)}
  .prox{margin-top:9px;font-size:12px}
  .prox .ok{color:#4a6035;font-weight:600}
  .prox .warn{color:var(--quente);font-weight:700}
  .prox .sem{color:var(--muted);font-weight:600}
  .prox .hold{color:var(--hold);font-weight:600}
  .orig{margin-top:26px;background:var(--card);border:1px solid var(--line);border-radius:11px;
    padding:16px 20px;max-width:640px}
  .orig h3{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--charcoal);
    margin-bottom:12px}
  .orig .row{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:13.5px}
  .orig .row .nm{width:180px;color:var(--ink)}
  .orig .bar{height:9px;background:var(--dourado);border-radius:5px;min-width:9px}
  .orig .row .c{color:var(--muted);font-size:12px}
  footer{margin-top:26px;color:var(--muted);font-size:11.5px;line-height:1.6;max-width:820px}
  footer b{color:var(--ink)}
"""


def card(f):
    temp = f["temp"]
    tags = []

    if f["dias"] is not None:
        t = f["toque"]
        ev = f"{t['direcao'] == 'outgoing' and 'você enviou' or 'ele enviou'} em " \
             f"{data_br(t['quando'].date())} · {t['canal']}: {t['trecho'] or ''}"
        cls = temp if temp in ("quente", "morno", "frio") else "indefinido"
        tags.append(f'<span class="tag temp-{cls}" title="{esc(ev)}">{f["dias"]}d</span>')
        if t["canal"] == "grupo":
            tags.append('<span class="tag grupo" title="a temperatura veio de conversa '
                        'em grupo, não de troca 1:1">via grupo</span>')
    elif f["status"] not in ("hold", "prova"):
        tags.append('<span class="tag temp-indefinido" title="sem contato âncora declarado '
                    "em board_hunt_frentes — nada de onde derivar temperatura"
                    '">sem âncora</span>')

    if f["bola"] and f["status"] == "ativo":
        quem = "você" if f["bola"] == "voce" else (f["contato"] or "o outro lado").split()[0]
        tags.append(f'<span class="tag bola" title="derivado da direção da última mensagem — '
                    f'cortesia de encerramento pode inverter isto">bola: {esc(quem)}</span>')

    orig = f["originador"] or f["originador_rotulo"]
    if orig:
        tags.append(f'<span class="tag">↳ {esc(orig)}</span>')

    if f["status"] in ("hold", "prova"):
        prox = ""
    elif f["evento"]:
        e = f["evento"]
        quando = data_br(e["start_datetime"].date())
        hora = "" if e["all_day"] else f" {e['start_datetime']:%H}h"
        # `~` = casou pelo nome no título, não pelo contato ligado ao evento. É a
        # diferença entre "é esta reunião" e "provavelmente é esta" — e ela tem
        # que estar na tela, não só no rodapé.
        titulo = (e["summary"] or "")[:34].strip()
        marca = "" if f["evento_como"] == "id" else " ~"
        prox = (f'<div class="prox"><span class="ok" title="{esc(e["summary"] or "")}">'
                f'▸ próximo: {quando}{hora} · {esc(titulo)}{marca}</span></div>')
    elif f["task"]:
        # Marca diferente do `▸` de propósito: reunião é compromisso com outra
        # pessoa, task é compromisso só com ele. A tela que trata as duas como a
        # mesma coisa deixa de mostrar quem confirmou presença e quem só anotou.
        t = f["task"]
        quando = data_br(t["data_vencimento"].date())
        prox = (f'<div class="prox"><span class="ok" title="{esc(t["titulo"] or "")}">'
                f'◦ ato: {quando} · {esc((t["titulo"] or "")[:34].strip())} ⋯</span></div>')
    elif f["espera"]:
        t = f["espera"]
        desde = f' desde {data_br(t["on_hold_since"].date())}' if t["on_hold_since"] else ""
        motivo = _curto(t["on_hold_reason"] or t["titulo"] or "", 40)
        prox = (f'<div class="prox"><span class="hold" title="{esc(t["on_hold_reason"] or "")}">'
                f'⏸ em espera{desde} · {esc(motivo)}</span></div>')
    else:
        prox = '<div class="prox"><span class="warn">⚠ sem próximo passo</span></div>'

    nota = (f["nota"] or "").strip()
    if len(nota) <= NOTA_NO_CARD:
        bloco_nota = f'<div class="n">{esc(nota)}</div>' if nota else ""
    else:
        bloco_nota = (f'<div class="n">{esc(_curto(nota, NOTA_NO_CARD))}</div>'
                      f'<details><summary>nota completa ({len(nota)} caracteres)</summary>'
                      f'<div class="n">{esc(nota)}</div></details>')

    return f"""<div class="cardw {temp}">
      <div class="t">{esc(f['nome'])}</div>
      <div class="sub">{esc(f['subtitulo'] or '')}</div>
      {bloco_nota}
      {prox}
      <div class="tags">{''.join(tags)}</div>
    </div>"""


def render(d, canais):
    agora = datetime.now(BRT)
    k = d["kpis"]

    colunas = []
    for num, nome in FASES:
        itens = [f for f in d["frentes"] if f["fase"] == num and f["status"] != "hold"]
        corpo = "".join(card(f) for f in itens) or '<div class="vazio">—</div>'
        n = f'{len(itens)} frente{"s" if len(itens) != 1 else ""}' if itens else "vazia"
        colunas.append(f'<div class="col{"" if itens else " vazia"}">'
                       f'<h2><span class="fase">{num}</span>{nome}<span class="n">{n}</span></h2>'
                       f'<div class="stack">{corpo}</div></div>')
    hold = [f for f in d["frentes"] if f["status"] == "hold"]
    corpo = "".join(card(f) for f in hold) or '<div class="vazio">—</div>'
    n = f'{len(hold)} frente{"s" if len(hold) != 1 else ""}' if hold else "vazia"
    colunas.append(f'<div class="col hold{"" if hold else " vazia"}">'
                   f'<h2><span class="fase">⏸</span>On-hold<span class="n">{n}</span></h2>'
                   f'<div class="stack">{corpo}</div></div>')

    # Barra de próximos passos: os eventos que as frentes casaram, em ordem.
    # Só frentes ATIVAS: a reunião mensal da Vallen é compromisso de cliente, não
    # passo do funil de caça — na barra de "o que vem" ela empurraria para baixo
    # a reunião que de fato move um assento.
    casados, vistos = [], set()
    for f in sorted((x for x in d["frentes"] if x["evento"] and x["status"] == "ativo"),
                    key=lambda x: x["evento"]["start_datetime"]):
        e = f["evento"]
        if e["id"] in vistos:
            continue
        vistos.add(e["id"])
        hora = "" if e["all_day"] else f" {e['start_datetime']:%H}h"
        casados.append(f'<span class="ne">{data_br(e["start_datetime"].date())}{hora} · '
                       f'<b>{esc(f["nome"])}</b></span>')
    nextbar = ("".join(casados) if casados else
               '<span class="ne dim">nenhuma reunião de frente nos próximos '
               f'{JANELA_AGENDA} dias</span>')

    # Originadores: contagem REAL das frentes que cada um produziu.
    contagem = {}
    for f in d["frentes"]:
        nome = f["originador"] or f["originador_rotulo"]
        if not nome or nome == "relação direta":
            continue
        contagem.setdefault(nome, []).append(f["nome"])
    ativos = sorted(contagem.items(), key=lambda kv: -len(kv[1]))
    maxn = max((len(v) for _, v in ativos), default=1)
    linhas = "".join(
        f'<div class="row"><span class="nm">{esc(n)}</span>'
        f'<span class="bar" style="width:{len(fs) / maxn * 200:.0f}px"></span>'
        f'<span class="c">{len(fs)} frente{"s" if len(fs) > 1 else ""} · {esc(" · ".join(fs))}</span></div>'
        for n, fs in ativos)
    dormentes = "".join(
        f'<div class="row"><span class="nm" style="color:var(--quente);font-weight:600">'
        f'{esc(c["nome"])}</span><span class="c">{esc(c.get("nota", ""))}</span></div>'
        for c in canais)

    cob = d["cobertura"]
    # ⚠️ `last_synced_at` é UTC — ao contrário de `start_datetime`, que é BRT
    # ingênuo. Exibi-lo cru punha a página anunciando um sync às 23:15 quando
    # eram 20:24: o carimbo de frescor, que existe justamente pra denunciar dado
    # velho, mostrando o futuro.
    if d["frescor_agenda"]:
        s = d["frescor_agenda"].replace(tzinfo=ZoneInfo("UTC")).astimezone(BRT)
        fresco = f"agenda sincronizada {data_br(s.date())} {s:%H:%M}"
    else:
        fresco = "agenda sem carimbo de sync"

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Board Hunt 2026 — Pipeline</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Board Hunt 2026 <span class="dot">·</span> Pipeline de originação de conselhos</h1>
  <div class="meta">gerado <b>{agora:%d/%m %H:%M}</b> · {esc(fresco)}<br>
    <a href="board_hunt_resumo.html" style="color:var(--dourado)">↗ versão de 1 folha, pra enviar</a>
  </div>
</header>

<div class="metabar">
  <div class="kpi"><b>{k['assentos']} / {META_ASSENTOS}</b><span>Assentos remunerados · meta dez/26</span></div>
  <div class="kpi"><b>{k['funil']}</b><span>Frentes no funil</span></div>
  <div class="kpi"><b>{k['quentes']}</b><span>Quentes (≤{QUENTE_ATE}d)</span></div>
  <div class="kpi" style="border-color:var(--quente)"><b style="color:var(--quente)">{k['sem_prox']}</b><span>⚠️ Sem próximo passo</span></div>
</div>

<div class="nextbar"><span class="nl">Próximos passos agendados</span>{nextbar}</div>

<div class="legend">
  <span class="chip"><span class="sw" style="background:var(--quente)"></span>Quente — troca ≤{QUENTE_ATE}d</span>
  <span class="chip"><span class="sw" style="background:var(--morno)"></span>Morno — {QUENTE_ATE + 1}–{MORNO_ATE}d, ou esperando o outro sem data</span>
  <span class="chip"><span class="sw" style="background:var(--frio)"></span>Frio — &gt;{MORNO_ATE}d, ou parado contigo</span>
  <span class="chip"><span class="sw" style="background:var(--hold)"></span>On-hold</span>
</div>

<div class="board">{''.join(colunas)}</div>

<div class="orig">
  <h3>Originadores — quem abre as portas</h3>
  {linhas}
  <h3 style="margin:16px 0 10px;color:var(--quente)">Canais a ativar — 0 frentes ainda</h3>
  {dormentes}
</div>

<footer>
  Página <b>gerada</b> a cada 5 min (<code>scripts/board_hunt.py</code>) — fase, âncora,
  originador e nota vêm de <code>board_hunt_frentes</code>; dias, temperatura, bola e
  próximo passo são recalculados a cada rodada e não ficam gravados.<br>
  <b>Cobertura do "próximo passo"</b>: {cob['id']} frente(s) casada(s) pelo <code>contact_id</code>
  do evento, {cob['nome']} pelo nome no título (marcadas com <b>~</b>), <b>{cob['task']} por tarefa
  com data</b> (<b>◦</b>), {cob['sem']} sem nenhum dos três nos próximos {JANELA_AGENDA} dias.
  O casamento por nome só aceita dois tokens do nome ou um sobrenome raro no CRM — <b>só
  {d['agenda_ligada_pct']}% dos eventos da agenda têm contato ligado</b>, então sem esse segundo
  caminho a coluna inteira ficaria vazia.<br>
  <b>Tarefa como próximo passo</b> (23/08): a regra do ritual dá três destinos — <i>marcar</i>,
  <i>tocar</i> e <i>baixar consciente</i>; só o primeiro vira evento. O casamento com task exige
  <code>contact_id</code> <b>e</b> <code>project_id</code> iguais aos da frente: mais estrito que
  o da agenda de propósito, porque calar o alarme com evidência de outro projeto é pior que
  mantê-lo aceso. Tarefa <b>não</b> entra na folha de 1 página — lá "sem data marcada" continua
  significando <i>sem reunião</i>.<br>
  Canais dormentes moram em <code>~/cockpit/board_hunt_canais.json</code> (julgamento — a
  regeneração não os apaga).
</footer>
</body>
</html>"""


# ---------------------------------------------------------------- resumo A4 --
# Segunda saída, pedida pelo Renato em 12/08: uma folha pra mandar ao pai (Orestes)
# no WhatsApp. Ele cobra a RENDA ([[feedback_board_hunt_e_a_renda]]) — o que ele
# precisa ver é onde a caça está e o que tem data marcada.
#
# O QUE FICA DE FORA, de propósito: as `nota` das frentes. Elas são o caderno
# interno da operação — ID de mensagem, "bola com o Marcelo", NDA lido errado,
# correção de rumo. Resumir não é encolher a fonte: é escolher o que o leitor
# precisa. Também saem a bola, o marcador `~` de casamento por nome, a cobertura
# da régua e os canais dormentes — telemetria de quem MANTÉM a página, não de
# quem a lê. O que entra: fase, o que é a frente, última troca, próxima reunião.

CSS_RESUMO = """
  @page{size:A4 portrait;margin:14mm 13mm}
  :root{--charcoal:#2b2925;--ink:#3a362f;--muted:#7d7466;--line:#ded5c4;
    --dourado:#b8973e;--quente:#c0492f;--morno:#a8801d;--frio:#3f6f92;--hold:#9a9184}
  *{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  body{background:#fff;color:var(--ink);
    font:11px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:22px 26px;max-width:820px;margin:0 auto}
  header{border-bottom:2px solid var(--dourado);padding-bottom:9px;margin-bottom:12px}
  h1{font-size:16.5px;font-weight:700;color:var(--charcoal);letter-spacing:.2px}
  .sub{font-size:10px;color:var(--muted);margin-top:3px}
  .kpis{display:flex;gap:8px;margin-bottom:11px}
  .kpis div{flex:1;border:1px solid var(--line);border-radius:7px;padding:6px 9px}
  .kpis b{display:block;font-size:16px;font-weight:700;color:var(--charcoal);line-height:1.1}
  .kpis span{font-size:8.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  h2{font-size:9.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--dourado);
    font-weight:700;margin:11px 0 5px;padding-bottom:3px;border-bottom:1px solid var(--line)}
  h2 .leg{float:right;color:var(--muted);text-transform:none;letter-spacing:0;font-weight:400;
    font-size:9px}
  .ag{display:flex;flex-wrap:wrap;gap:6px}
  .ag span{border:1px solid var(--line);border-radius:5px;padding:3px 8px;font-size:10.5px}
  .ag b{color:var(--charcoal)}
  table{width:100%;border-collapse:collapse;page-break-inside:auto}
  tr{page-break-inside:avoid}
  th{font-size:8.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);
    text-align:left;font-weight:600;padding:0 6px 4px 0}
  td{padding:3px 6px 3px 0;border-top:1px solid #efe9dc;vertical-align:top;line-height:1.3}
  .fase td{border-top:1px solid var(--line);padding-top:7px;font-size:9px;font-weight:700;
    text-transform:uppercase;letter-spacing:.6px;color:var(--charcoal)}
  .nm{font-weight:700;color:var(--charcoal);width:36%}
  .nm em{display:block;font-weight:400;font-style:normal;color:var(--muted);font-size:9.5px}
  .st{width:17%;white-space:nowrap}
  .px{width:47%}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
  .q{background:var(--quente)} .m{background:var(--morno)}
  .f{background:var(--frio)} .h{background:var(--hold)}
  .ok{color:#4a6035;font-weight:600} .warn{color:var(--quente)}
  footer{margin-top:11px;padding-top:6px;border-top:1px solid var(--line);
    font-size:8.5px;color:var(--muted);line-height:1.45}
  @media print{body{padding:0}}
"""

_TEMP_CLS = {"quente": "q", "morno": "m", "frio": "f",
             "hold": "h", "prova": "h", "indefinido": "h"}


def linha_resumo(f):
    cls = _TEMP_CLS.get(f["temp"], "h")
    if f["status"] == "hold":
        quando = "em espera"
    elif f["status"] == "prova":
        quando = "referência"
    elif f["dias"] is None:
        quando = "—"
    elif f["dias"] == 0:
        quando = "hoje"
    else:
        quando = f'há {f["dias"]} dia' + ("s" if f["dias"] > 1 else "")

    e = f["evento"]
    # Frente em espera ou de referência não tem "próximo passo" — o compromisso da
    # Vallen é de cliente, e mostrá-lo aqui punha a peça anunciando reunião de
    # caça onde não há. Reordenado depois que o PDF de 12/08 exibiu justamente o
    # evento "[Interno] ..." da Vallen como próximo passo do funil.
    if f["status"] in ("hold", "prova") or not e:
        prox = "—" if f["status"] in ("hold", "prova") else '<span class="warn">sem data marcada</span>'
    elif "[interno]" in (e["summary"] or "").lower():
        # Título de bloqueio interno não descreve nada pra quem lê de fora — a data
        # é verdadeira, o rótulo é de uso doméstico.
        hora = "" if e["all_day"] else f" {e['start_datetime']:%H}h"
        prox = f'<span class="ok">▸ {data_br(e["start_datetime"].date())}{hora} · compromisso agendado</span>'
    else:
        hora = "" if e["all_day"] else f" {e['start_datetime']:%H}h"
        prox = (f'<span class="ok">▸ {data_br(e["start_datetime"].date())}{hora} · '
                f'{esc(_curto(e["summary"], 42))}</span>')
    sub = f'<em>{esc(_curto(f["subtitulo"], 44))}</em>' if f["subtitulo"] else ""
    return (f'<tr><td class="nm">{esc(f["nome"])}{sub}</td>'
            f'<td class="st"><span class="dot {cls}"></span>{esc(quando)}</td>'
            f'<td class="px">{prox}</td></tr>')


def render_resumo(d):
    agora = datetime.now(BRT)
    k = d["kpis"]
    ativas = [f for f in d["frentes"] if f["status"] == "ativo"]
    com_data = sum(1 for f in ativas if f["evento"])

    corpo = []
    for num, nome in FASES:
        itens = [f for f in d["frentes"] if f["fase"] == num and f["status"] != "hold"]
        if not itens:
            continue
        corpo.append(f'<tr class="fase"><td colspan="3">{num} · {nome}</td></tr>')
        corpo += [linha_resumo(f) for f in itens]
    hold = [f for f in d["frentes"] if f["status"] == "hold"]
    if hold:
        corpo.append('<tr class="fase"><td colspan="3">Em espera</td></tr>')
        corpo += [linha_resumo(f) for f in hold]

    # Só reuniões de frente ATIVA, na ordem em que chegam — é o que responde
    # "está andando?" sem precisar ler o funil inteiro.
    vistos, ag = set(), []
    for f in sorted((x for x in ativas if x["evento"]),
                    key=lambda x: x["evento"]["start_datetime"]):
        e = f["evento"]
        if e["id"] in vistos:
            continue
        vistos.add(e["id"])
        hora = "" if e["all_day"] else f" {e['start_datetime']:%H}h"
        ag.append(f'<span>{data_br(e["start_datetime"].date())}{hora} · <b>{esc(f["nome"])}</b></span>')
    agenda = "".join(ag) or '<span style="color:var(--muted)">nenhuma reunião marcada</span>'

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Board Hunt 2026 — resumo</title>
<style>{CSS_RESUMO}</style>
</head>
<body>
<header>
  <h1>Busca de assentos em conselho — resumo</h1>
  <div class="sub">Renato de Faria e Almeida Prado · situação em {agora:%d/%m/%Y}, {agora:%H:%M}
    · meta: {META_ASSENTOS} assentos remunerados até dezembro/2026</div>
</header>

<div class="kpis">
  <div><b>{k['assentos']} de {META_ASSENTOS}</b><span>Assentos fechados</span></div>
  <div><b>{len(ativas)}</b><span>Frentes ativas</span></div>
  <div><b>{k['quentes']}</b><span>Com conversa nos últimos {QUENTE_ATE} dias</span></div>
  <div><b>{com_data}</b><span>Com reunião marcada</span></div>
</div>

<h2>Próximas reuniões</h2>
<div class="ag">{agenda}</div>

<h2>As frentes, por estágio da conversa
  <span class="leg"><span class="dot q"></span>conversa recente
    <span class="dot m" style="margin-left:9px"></span>aguardando retorno
    <span class="dot f" style="margin-left:9px"></span>parada</span></h2>
<table>
  <tr><th>Frente</th><th>Última troca</th><th>Próximo passo agendado</th></tr>
  {''.join(corpo)}
</table>

<footer>
  Quadro gerado pelo sistema a partir das conversas e da agenda — os prazos e as datas
  são os reais, não estimativas. &ldquo;Última troca&rdquo; conta os dias desde a
  mensagem mais recente com cada interlocutor. Estágios 1 a 6: da rede até o assento
  fechado.
</footer>
</body>
</html>"""


def gerar_pdf():
    """HTML → PDF por Chrome headless (não há wkhtmltopdf/weasyprint na máquina —
    [[reference_pdf_onbrand]]). Fora da rodada de 5 min de propósito: abrir o
    Chrome 288×/dia pra um PDF que ninguém pediu é queimar bateria."""
    import subprocess
    if not os.path.exists(CHROME):
        print(f"⚠️  Chrome não encontrado em {CHROME} — PDF não gerado")
        return None
    r = subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={PDF_RESUMO}", f"file://{SAIDA_RESUMO}"],
                       capture_output=True, timeout=90)
    if r.returncode != 0 or not os.path.exists(PDF_RESUMO):
        print(f"⚠️  Chrome falhou ({r.returncode}): {r.stderr.decode()[:200]}")
        return None
    return PDF_RESUMO


def main():
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    d = avaliar(coletar(cur, datetime.now(BRT).date()))
    open(SAIDA, "w").write(render(d, carregar_canais()))
    open(SAIDA_RESUMO, "w").write(render_resumo(d))
    print(f"→ {SAIDA}  ({d['kpis']['funil']} frentes · {d['kpis']['quentes']} quentes · "
          f"{d['kpis']['sem_prox']} sem próximo passo)")
    print(f"→ {SAIDA_RESUMO}  (1 folha A4, sem as notas internas)")
    if "--pdf" in sys.argv:
        pdf = gerar_pdf()
        if pdf:
            print(f"→ {pdf}")
            if "--quieto" not in sys.argv:
                import subprocess
                subprocess.run(["open", "-R", pdf])
        return 0 if pdf else 1
    if "--quieto" not in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from cockpit import abrir          # reusa a aba em vez de abrir outra
            abrir(SAIDA)
        except Exception:
            import subprocess
            subprocess.run(["open", SAIDA])


if __name__ == "__main__":
    sys.exit(main() or 0)
