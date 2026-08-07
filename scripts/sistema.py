#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/sistema.html — o que o INTEL está processando, e por quê.

PEDIDO (Renato, 06/08/2026): "como visualizar o modelo de dados e a arquitetura
em tempo real, bem como os raciocínios e inferências realizados, de forma a
enxergar com segurança o que o sistema está processando?"

A DESCOBERTA que define o desenho: o raciocínio JÁ É PERSISTIDO. Cada frente
julgada grava `trajetoria` (os passos da investigação), `nao_consegui_saber` (o
que ficou em aberto, com o motivo) e `fatos_novos`. Numa rodada de hoje foram 9
passos e 5 incertezas declaradas — inclusive "'não respondeu' não é 'não ouviu'".
Não falta instrumentação: falta SUPERFÍCIE. Tudo isso vive num JSONB que ninguém
abre.

Então esta página não inventa métrica nova. Ela mostra o que o sistema já sabe
de si:
  1. MODELO — o banco vivo contra o contrato registrado (divergência = sinal)
  2. CANOS — cada fonte de dado com volume real E cobertura contra a origem
  3. RACIOCÍNIO — a trajetória do agente, com o que ele NÃO conseguiu saber
  4. AGORA — o que rodou nas últimas horas, o que falhou e por quê

O item 2 existe por causa da lição de 05-06/08: sete defeitos em que o mecanismo
pedia, recebia e reportava sucesso sem medir o próprio denominador. Cobertura
contra a fonte é a única leitura que teria pego os sete.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/sistema.html")


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ coleta --

def coletar(cur):
    d = {}

    # 1. MODELO — o verificador é a fonte, não uma query paralela: se ele mudar
    # de critério, a página muda junto. Duas verdades sobre o mesmo assunto
    # divergem em silêncio.
    try:
        r = subprocess.run([f"{ROOT}/.venv/bin/python", f"{ROOT}/scripts/verifica_modelo.py"],
                           capture_output=True, text=True, timeout=90, cwd=ROOT)
        d["modelo_txt"] = (r.stdout or r.stderr or "")[-2500:]
    except Exception as e:
        d["modelo_txt"] = f"(verificador não rodou: {e})"

    cur.execute("SELECT count(*) n FROM information_schema.tables WHERE table_schema='public'")
    d["tabelas"] = cur.fetchone()["n"]

    # 1b. O MODELO PLOTADO, com o movimento das últimas 24h por entidade.
    #
    # Pedido do Renato (06/08): "dá pra plotar o modelo e mostrar nele o que
    # aconteceu no último período?". O verificador respondia "🟢 conforme" —
    # verdadeiro e inútil pra quem quer ver o sistema PENSANDO. Conformidade é
    # sobre a forma; o que muda de hora em hora é o volume que passa por cada
    # entidade. Sem o movimento, o mapa é uma planta baixa; com ele, é o prédio
    # com as luzes acesas.
    d["mapa"] = []
    try:
        sys.path.insert(0, f"{ROOT}/scripts")
        from verifica_modelo import ler_contrato
        contrato = ler_contrato()
    except Exception as e:
        contrato = None
        d["mapa_erro"] = str(e)[:120]
    if contrato:
        # Cada tabela tem sua própria coluna de tempo. Sem esta descoberta, contar
        # "o que entrou em 24h" exigiria hardcode por tabela — e tabela nova
        # entraria como zero, que é o silêncio de sempre.
        # DUAS leituras de tempo, e a diferença entre elas é o ponto:
        #   · ACONTECEU  — a coluna do fato no mundo (timestamp, recebido_em…)
        #   · GRAVADO    — quando a linha entrou no banco (criado_em)
        # Medir só a segunda faz a máquina ouvir o próprio eco: o backfill de
        # grupos de hoje (26.712 mensagens de meses atrás) apareceria como
        # "+40.315 em 24h", como se o mundo tivesse falado 40 mil vezes desde
        # ontem. Quando as duas divergem, a página mostra as duas.
        cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                       WHERE table_schema='public' AND column_name IN
                       ('criado_em','created_at','timestamp','data_criacao','received_at',
                        'started_at','run_at','recebido_em','enviado_em','data_hora','inicio')""")
        tem = {}
        for r in cur.fetchall():
            tem.setdefault(r["table_name"], set()).add(r["column_name"])
        EVENTO = ["timestamp", "data_hora", "inicio", "recebido_em", "received_at",
                  "started_at", "run_at", "data_criacao"]
        GRAVOU = ["criado_em", "created_at"]

        def relogios(t):
            """(coluna do fato, coluna da gravação) — qualquer uma pode faltar."""
            cs = tem.get(t, set())
            ev = next((c for c in EVENTO if c in cs), None)
            gr = next((c for c in GRAVOU if c in cs), None)
            if t == "messages" and "recebido_em" in cs:   # incoming×outgoing em colunas distintas
                ev = "COALESCE(recebido_em, enviado_em)"
            return ev, gr
        for ent, spec in contrato["entidades"].items():
            tabs = []
            for t in spec["tabelas"]:
                nome = t.split(".")[-1]        # copilot.emails -> emails
                ev, gr = relogios(nome)
                alvo = f'"{t.split(".")[0]}"."{nome}"' if "." in t else f'"{nome}"'
                total = novas = gravadas = None
                try:
                    cur.execute(f"SELECT count(*) AS n FROM {alvo}")
                    total = cur.fetchone()["n"]
                    for coluna, destino in ((ev, "ev"), (gr, "gr")):
                        if not coluna:
                            continue
                        expr = coluna if coluna.startswith("COALESCE") else f'"{coluna}"'
                        cur.execute(f"SELECT count(*) AS n FROM {alvo} "
                                    f"WHERE {expr} > now() - interval '24 hours'")
                        n = cur.fetchone()["n"]
                        if destino == "ev":
                            novas = n
                        else:
                            gravadas = n
                    if novas is None:
                        novas = gravadas
                except Exception:
                    # Tabela do contrato que não existe mais (ou schema errado):
                    # o erro aborta a transação inteira no psycopg2 — sem este
                    # rollback, TODA consulta seguinte falharia e o mapa viria
                    # vazio sem dizer por quê.
                    cur.connection.rollback()
                tabs.append({"nome": t, "total": total, "novas": novas,
                             "gravadas": gravadas, "sem_relogio": not (ev or gr)})
            d["mapa"].append({
                "entidade": ent, "esperado": spec["esperado"], "tabelas": tabs,
                "novas": sum(x["novas"] or 0 for x in tabs),
                "gravadas": sum(x["gravadas"] or 0 for x in tabs),
                "total": sum(x["total"] or 0 for x in tabs),
            })

    # 2. CANOS
    cur.execute("""
        SELECT cv.canal, count(*) AS msgs, max(COALESCE(m.recebido_em, m.enviado_em)) AS ultima
        FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
        GROUP BY 1 ORDER BY 2 DESC
    """)
    d["canais"] = cur.fetchall()

    # Canal cujas mensagens são TODAS coladas à mão não é cano — ver render().
    cur.execute("""
        SELECT cv.canal FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
        GROUP BY 1
        HAVING bool_and(COALESCE(m.metadata->>'via', m.metadata->>'source', '') LIKE '%manual%')
    """)
    d["canais_manuais"] = {r["canal"] for r in cur.fetchall()}

    cur.execute("""
        SELECT count(*) AS msgs, max(timestamp) AS ultima,
               count(DISTINCT group_jid) AS grupos FROM group_messages
    """)
    d["grupos"] = cur.fetchone()

    cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE verificado) AS verificados FROM contact_facts")
    d["fatos"] = cur.fetchone()

    # AGENDA como cano. Faltava aqui — e foi por isso que a parada passou
    # despercebida: o cron `sync-calendar` nunca teve agendador (2 runs na vida,
    # o último em 02/05), o calendar só entrava no daily-sync das 5h, e o que o
    # Renato marcava durante o dia ficava invisível por até 24h. Nenhuma
    # superfície mostrava a idade desse cano. O que não aparece não é vigiado.
    cur.execute("""SELECT count(*) AS n, max(last_synced_at) AS ultima,
                          count(*) FILTER (WHERE start_datetime > now()) AS futuros
                     FROM calendar_events""")
    d["agenda"] = cur.fetchone()

    cur.execute("""SELECT max(received_at) AS ultima,
                   count(*) FILTER (WHERE received_at > now() - interval '1 hour') AS ult_hora
                   FROM webhook_audit""")
    d["webhook"] = cur.fetchone()

    # SWEEP DO RACI como cano. Veio parar aqui em 07/08 porque estava no lugar
    # errado: o monitor mandava WhatsApp ao Renato dizendo "o sweep pode ter
    # travado" — sobre um sweep saudável (281 processadas em 7 dias, zero
    # mensagem nova atrasada). O que existe é BACKLOG HISTÓRICO, que não é
    # urgência e não se resolve pelo celular. Aqui a fila aparece como número e
    # o que se vigia é o MOVIMENTO: idade do último processamento.
    cur.execute("""SELECT count(*) FILTER (WHERE gm.raci_processed_at IS NULL) AS fila,
                          max(gm.raci_processed_at) AS ultima
                     FROM group_messages gm
                     JOIN project_whatsapp_groups pwg
                       ON pwg.group_jid = gm.group_jid AND pwg.ativo
                    WHERE gm.from_me = FALSE AND gm.content IS NOT NULL
                      AND length(gm.content) >= 12  -- MIN_TEXT_LEN_FOR_AI
                 """)
    d["raci_sweep"] = cur.fetchone()

    # cobertura contra a FONTE — o que teria pego os 7 defeitos
    d["cobertura"] = []
    base, key = env("EVOLUTION_API_URL"), env("EVOLUTION_API_KEY")
    if base and key:
        cur.execute("""SELECT s.group_jid, s.group_name, count(g.id) AS n
                       FROM social_groups_cache s
                       LEFT JOIN group_messages g ON g.group_jid = s.group_jid
                       WHERE s.sync_enabled GROUP BY 1,2 ORDER BY 3 DESC LIMIT 6""")
        for r in cur.fetchall():
            try:
                req = urllib.request.Request(
                    f"{base.rstrip('/')}/chat/findMessages/rap-whatsapp",
                    data=json.dumps({"where": {"key": {"remoteJid": r["group_jid"]}},
                                     "page": 1, "offset": 1}).encode(),
                    method="POST", headers={"apikey": key, "Content-Type": "application/json"})
                t = json.loads(urllib.request.urlopen(req, timeout=25).read()) \
                    .get("messages", {}).get("total", 0)
            except Exception:
                t = None
            d["cobertura"].append({"nome": r["group_name"], "intel": r["n"], "fonte": t})

    # 3. RACIOCÍNIO
    cur.execute("""SELECT run_date, run_at, payload FROM cos_daily_review
                   ORDER BY id DESC LIMIT 1""")
    d["rodada"] = cur.fetchone()

    # Precisão HISTÓRICA por frente — o único juízo de qualidade que não é meu.
    # Vem do placar que o Renato preencheu; sem ele, "qualidade" viraria o
    # sistema se auto-avaliando, que é o que o gate existe pra evitar.
    cur.execute("""SELECT frente, count(*) AS n,
                          count(*) FILTER (WHERE veredito='certa') AS certas
                   FROM cos_portao_veredito GROUP BY 1""")
    d["placar"] = {r["frente"]: (r["certas"], r["n"]) for r in cur.fetchall()}

    # 3b. PORTÕES AGUARDANDO VEREDITO — 7 dias, não só a última rodada.
    #
    # Por que existe (06/08): a página mostrava o raciocínio da ÚLTIMA rodada e
    # os botões viviam nesses cards. Mas o agente roda 14×/dia e o portão da
    # rodada das 15h não aparece na página gerada às 20h. Medido no dia: **9
    # portões abertos em 05-06/08, e a página oferecia veredito de 1** (11%).
    # O gate dos cockpits pede ≥70% em ≥15 portões; com 11% da amostra
    # alcançável, o placar não fecha por falta de o que marcar — o número
    # ficaria parado e pareceria estabilidade.
    cur.execute("""SELECT run_date, payload FROM cos_daily_review
                   WHERE run_date > CURRENT_DATE - 7 ORDER BY run_at""")
    abertos = {}
    for r in cur.fetchall():
        pay = r["payload"]
        if isinstance(pay, str):
            pay = json.loads(pay)
        for f in (pay.get("frentes") or []):
            pv = f.get("precisa_de_voce") or {}
            if pv.get("sim") and f.get("trajetoria"):
                abertos[(r["run_date"], f.get("frente"))] = {
                    "dia": r["run_date"], "frente": f.get("frente"),
                    "project_id": f.get("project_id"),
                    "o_que": pv.get("o_que") or pv.get("pergunta") or "",
                }
    cur.execute("SELECT run_date, frente FROM cos_portao_veredito "
                "WHERE run_date > CURRENT_DATE - 7")
    ja = {(r["run_date"], r["frente"]) for r in cur.fetchall()}
    d["pendentes"] = [v for k, v in sorted(abertos.items(), reverse=True) if k not in ja]

    # 4. AGORA
    cur.execute("""
        SELECT regexp_replace(path,'^/api/cron/([^?]+).*$','\\1') AS job,
               count(*) AS runs, count(*) FILTER (WHERE status='error') AS erros,
               max(started_at) AS ultimo
        FROM cron_runs WHERE started_at > now() - interval '6 hours'
        GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT 12
    """)
    d["crons"] = cur.fetchall()
    return d



# ------------------------------------------------- qualidade do raciocínio --

# As 8 fontes que o prompt declara ao agente. Se a frente tinha movimento numa
# delas e a trajetória não a menciona, ele julgou sem olhar — é a lacuna que
# interessa, não o total de passos.
FONTES = {
    "tasks":        ("task",),
    "fatos":        ("contact_fact", "fatos"),
    "whatsapp":     ("messages", "whatsapp", "wa "),
    "e-mail":       ("email", "e-mail"),
    "grupo":        ("group_message", "grupo"),
    "notas":        ("nota", "project_note"),
    "memória":      ("system_memories", "memo"),
    "agenda":       ("calendar", "agenda"),
}


def analisar(f):
    """Uso de contexto e sinais de qualidade de UMA frente julgada.

    ⚠️ HEURÍSTICA, e é honesto dizer: detecta a fonte por menção na trajetória
    que o próprio agente escreveu. Ele pode ter consultado sem narrar (falso
    "não tocou") ou citado de passagem sem ler a fundo (falso "tocou"). Serve
    pra levantar a pergunta, não pra fechar o veredito — o veredito é do Renato,
    no placar.
    """
    traj_txt = " ".join(str(t) for t in (f.get("trajetoria") or [])).lower()
    tocou = {nome: any(t in traj_txt for t in termos) for nome, termos in FONTES.items()}
    ids = len(re.findall(r"#\d+", " ".join(str(t) for t in (f.get("trajetoria") or []))))
    datas = len(re.findall(r"\d{2}/\d{2}", traj_txt))
    return {
        "tocou": [k for k, v in tocou.items() if v],
        "nao_tocou": [k for k, v in tocou.items() if not v],
        "ids": ids, "datas": datas,
        "incertezas": len(f.get("nao_consegui_saber") or []),
        "passos": len(f.get("trajetoria") or []),
    }



# O feedback vive em localStorage e sai por copiar-colar. Sem POST de propósito:
# a página roda em file:// e abrir CORS num endpoint de escrita pra economizar um
# clique é troca ruim — mesma decisão do placar e da fila de fatos.
#
# ⚠️ Raw string em constante do módulo, NÃO dentro da f-string do render: as
# chaves do JavaScript colidem com a interpolação, e em 04/08 um `\n` não-raw já
# partiu o script ao meio e derrubou todos os listeners sem erro visível.
JS_FEEDBACK = r"""
<script>
const CHAVE = "sistema-vereditos";
const salvo = JSON.parse(localStorage.getItem(CHAVE) || "{}");

function pintar() {
  let n = 0;
  document.querySelectorAll(".veredito").forEach(v => {
    const e = salvo[v.dataset.uid];
    v.querySelectorAll("button[data-v]").forEach(b =>
      b.setAttribute("aria-pressed", String(!!e && e.v === b.dataset.v)));
    const chk = v.querySelector("[data-raso]");
    if (chk) chk.checked = !!(e && e.raso);
    const ta = v.querySelector(".v-nota");
    // Só escreve na textarea se ela não estiver em foco: sobrescrever o campo
    // enquanto ele digita apaga a frase no meio.
    if (ta && document.activeElement !== ta) ta.value = (e && e.nota) || "";
    if (e && e.v) n++;
  });
  document.getElementById("fb-n").textContent = n;
}

// O comentário salva sozinho, sem botão. Um "salvar" a mais entre pensar e
// registrar é onde o aprendizado se perde.
document.querySelectorAll(".v-nota").forEach(ta => {
  ta.addEventListener("input", () => {
    const uid = ta.closest(".veredito").dataset.uid;
    if (!salvo[uid]) salvo[uid] = { v: "", raso: false };
    salvo[uid].nota = ta.value;
    localStorage.setItem(CHAVE, JSON.stringify(salvo));
    document.getElementById("fb-n").textContent =
      Object.values(salvo).filter(x => x && (x.v || (x.nota || "").trim())).length;
  });
});

document.querySelectorAll(".veredito button[data-v]").forEach(b => {
  b.addEventListener("click", () => {
    const v = b.closest(".veredito"), uid = v.dataset.uid, atual = salvo[uid];
    if (atual && atual.v === b.dataset.v) delete salvo[uid];
    else salvo[uid] = { v: b.dataset.v, raso: !!(atual && atual.raso) };
    localStorage.setItem(CHAVE, JSON.stringify(salvo)); pintar();
  });
});

document.querySelectorAll("[data-raso]").forEach(chk => {
  chk.addEventListener("change", () => {
    const uid = chk.closest(".veredito").dataset.uid;
    if (!salvo[uid]) salvo[uid] = { v: "", raso: false };
    salvo[uid].raso = chk.checked;
    localStorage.setItem(CHAVE, JSON.stringify(salvo)); pintar();
  });
});

document.getElementById("fb-limpar").addEventListener("click", () => {
  Object.keys(salvo).forEach(k => delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("fb-saida").style.display = "none"; pintar();
});

document.getElementById("fb-copiar").addEventListener("click", async () => {
  const L = ["VEREDITOS DO RACIOCINIO (cole no Claude)"];
  let c = 0;
  document.querySelectorAll(".veredito").forEach(v => {
    const e = salvo[v.dataset.uid];
    // Comentário sem veredito TAMBÉM sai: às vezes o julgamento é "não sei
    // dizer se acertou, mas o portão veio no momento errado" — isso é
    // aprendizado, e exigir um rótulo pra registrá-lo perde a observação.
    if (!e || (!e.v && !(e.nota || "").trim())) return;
    c++;
    const p = v.dataset.uid.split("|");
    const rot = e.v ? { certa: "CERTA", errada: "COBROU A TOA", passou: "DEIXOU PASSAR" }[e.v] : "SEM VEREDITO";
    L.push(p[0] + " #" + p[1] + " " + p[2] + " — " + rot
           + (e.raso ? " | RASO: julgou sem olhar tudo" : "")
           + ((e.nota || "").trim() ? " | NOTA: " + e.nota.trim().replace(/\s+/g, " ") : ""));
  });
  const txt = L.join("\n"), out = document.getElementById("fb-saida");
  const btn = document.getElementById("fb-copiar");
  out.value = txt; out.style.display = "block";
  try { await navigator.clipboard.writeText(txt); btn.textContent = "Copiado (" + c + ")"; }
  catch (e) { btn.textContent = "Copie abaixo"; out.select(); }
  setTimeout(() => btn.textContent = "Copiar para o Claude", 1800);
});

pintar();
</script>
"""


# ------------------------------------------------------------------ render --

def render(d):
    ago = datetime.now(timezone.utc)
    p = d["rodada"]["payload"] if d["rodada"] else {}
    if isinstance(p, str):
        p = json.loads(p)
    frentes = [f for f in (p.get("frentes") or []) if f.get("trajetoria")]
    tri = p.get("triagem") or {}

    def tile(v, rot, cor="ink"):
        return (f'<div class="tile" style="--t:var(--{cor})"><b>{esc(v)}</b>'
                f'<span>{esc(rot)}</span></div>')

    # --- mapa do modelo (entidades × movimento em 24h)
    mapa = []
    for e in d.get("mapa", []):
        vivo = e["novas"] > 0
        tabs = "".join(
            f'<li{" class=~morta~" if not (t["novas"] or 0) else ""}>{esc(t["nome"])}'
            f'<b>{"+%d" % t["novas"] if t["novas"] else ("·" if not t["sem_relogio"] else "?")}</b>'
            f'<span>{("{:,}".format(t["total"])).replace(",", ".") if t["total"] is not None else "—"}</span></li>'
            .replace("~", '"')
            for t in e["tabelas"])
        # Só mostra o "gravadas" quando ele DIVERGE do acontecido — aí ele
        # significa alguma coisa (backfill, importação, sync recuperando atraso).
        # replace no NÚMERO, não na frase — senão a vírgula do texto vira ponto.
        eco = (f'<p class="ent-eco">+{("{:,}".format(e["gravadas"])).replace(",", ".")} gravadas '
               f'no mesmo período — a diferença é histórico entrando, não conversa nova</p>'
               if e["gravadas"] > e["novas"] * 1.3 and e["gravadas"] - e["novas"] > 50 else "")
        mapa.append(f"""<div class="ent{'' if vivo else ' ent--parada'}">
      <header><span>{esc(e['entidade'])}</span><b>+{e['novas']}</b></header>
      <p class="ent-tot">{("{:,}".format(e['total'])).replace(",", ".")} no total · {len(e['tabelas'])} tabela(s)</p>
      {eco}
      <ul>{tabs}</ul></div>""")
    if d.get("mapa_erro"):
        mapa.append(f'<p class="nota">(contrato não pôde ser lido: {esc(d["mapa_erro"])})</p>')

    # --- canos
    #
    # `manual` separa cano de registro colado à mão. Sem isso o Instagram
    # aparecia como fonte "parada há 700h" (06/08: o Renato perguntou por quê) —
    # quando na verdade são DUAS mensagens que ele mesmo colou, a DM ao Joe
    # Shoemaker e a resposta. Não existe integração de Instagram no INTEL.
    # Marcar de vermelho um cano que nunca existiu é fabricar um defeito.
    linhas_cano = []
    for c in d["canais"]:
        idade = (ago.replace(tzinfo=None) - c["ultima"]).total_seconds() / 3600 if c["ultima"] else None
        manual = c["canal"] in d.get("canais_manuais", set())
        if manual:
            cor, quando = "faint", "registro manual"
        else:
            cor = "calm" if idade is not None and idade < 6 else ("warn" if idade is not None and idade < 48 else "crit")
            quando = ("há %.0fh" % idade) if idade is not None else "—"
        rot = (f'mensagens · <b>{esc(c["canal"])}</b>'
               + ('<span class="sub"> colado à mão — sem integração</span>' if manual else ''))
        linhas_cano.append(
            f'<tr><td>{rot}</td><td class="n">{c["msgs"]:,}</td>'
            f'<td class="n"><span class="dot dot--{cor}"></span>{quando}</td></tr>'.replace(",", "."))
    g = d["grupos"]
    idg = (ago.replace(tzinfo=None) - g["ultima"]).total_seconds() / 3600 if g["ultima"] else None
    linhas_cano.append(
        f'<tr><td>mensagens de <b>grupo</b> ({g["grupos"]} grupos)</td>'
        f'<td class="n">{g["msgs"]:,}</td><td class="n">'
        f'<span class="dot dot--{"calm" if idg is not None and idg < 6 else "warn"}"></span>'
        f'{("há %.0fh" % idg) if idg is not None else "—"}</td></tr>'.replace(",", "."))
    # Agenda: sincroniza a cada 15min desde 06/08. Vermelho aqui significa que o
    # INTEL está julgando sobre agenda velha — foi o que aconteceu por 3 meses.
    ag = d["agenda"]
    ida = (ago.replace(tzinfo=None) - ag["ultima"]).total_seconds() / 3600 if ag["ultima"] else None
    linhas_cano.append(
        f'<tr><td><b>agenda</b> (Google Calendar) <span class="sub">{ag["futuros"]} eventos futuros</span></td>'
        f'<td class="n">{ag["n"]:,}</td><td class="n">'
        f'<span class="dot dot--{"calm" if ida is not None and ida < 1 else ("warn" if ida is not None and ida < 6 else "crit")}"></span>'
        f'{("há %.0fh" % ida) if ida is not None else "—"}</td></tr>'.replace(",", "."))
    linhas_cano.append(
        f'<tr><td><b>fatos</b> destilados <span class="sub">{d["fatos"]["verificados"]} verificados por você</span></td>'
        f'<td class="n">{d["fatos"]["n"]:,}</td><td class="n">—</td></tr>'.replace(",", "."))
    # RACI: o que se vigia é o MOVIMENTO, não o tamanho da fila. Fila grande com
    # sweep andando é backlog histórico (assunto de sessão); fila com sweep
    # parado é defeito. Só o segundo fica vermelho.
    rs = d["raci_sweep"]
    idr = (ago.replace(tzinfo=None) - rs["ultima"]).total_seconds() / 3600 if rs["ultima"] else None
    # `.replace` só no NÚMERO — aplicá-lo à linha inteira come a vírgula da
    # frase e escreve "histórico. não atraso". O arquivo já avisava disso mais
    # acima; cai-se nele mesmo assim.
    fila_fmt = f"{rs['fila']:,}".replace(",", ".")
    linhas_cano.append(
        f'<tr><td><b>sweep do RACI</b> <span class="sub">{fila_fmt} na fila — histórico, '
        f'não atraso: o fluxo do dia sai em dia</span></td>'
        f'<td class="n">{fila_fmt}</td><td class="n">'
        f'<span class="dot dot--{"calm" if idr is not None and idr < 12 else ("warn" if idr is not None and idr < 24 else "crit")}"></span>'
        f'{("há %.0fh" % idr) if idr is not None else "nunca"}</td></tr>')

    # --- cobertura
    cob = []
    for c in d["cobertura"]:
        if not c["fonte"]:
            cob.append(f'<tr><td>{esc(c["nome"])[:30]}</td><td class="n">{c["intel"]}</td>'
                       f'<td class="n">—</td><td class="n">?</td></tr>')
            continue
        pct = 100 * c["intel"] // max(c["fonte"], 1)
        cor = "calm" if pct >= 65 else ("warn" if pct >= 40 else "crit")
        cob.append(f'<tr><td>{esc(c["nome"])[:30]}</td><td class="n">{c["intel"]:,}</td>'
                   f'<td class="n">{c["fonte"]:,}</td>'
                   f'<td class="n"><span class="dot dot--{cor}"></span>{pct}%</td></tr>'.replace(",", "."))

    # --- raciocínio
    cards = []
    for f in frentes[:6]:
        q = analisar(f)
        cert, tot = d["placar"].get(f.get("frente"), (None, 0))
        pct = (100 * cert // tot) if tot else None
        cor_hist = "calm" if (pct or 0) >= 70 else ("warn" if pct is not None and pct >= 40 else "crit")
        hist = (f'<span class="chip chip--{cor_hist}">precisão histórica {pct}% ({cert}/{tot})</span>'
                if tot else '<span class="chip chip--faint">sem placar ainda</span>')
        # Uma linha de cabeçalho, o resto dobrado.
        #
        # Pedido do Renato (06/08): "não dá pra simplificar a exibição?". Antes,
        # cada frente abria com três blocos de chips e nove passos de trajetória
        # ABERTOS — a página pedia leitura de relatório quando a pergunta dele é
        # de um segundo: acertou ou não? O que ele precisa ver de imediato é o
        # que o agente decidiu e o que ficou faltando olhar; a memória de
        # cálculo fica a um clique.
        faltou = ", ".join(q["nao_tocou"])
        barra = f'''<div class="qual">
      {hist}
      <span class="chip chip--faint">{q['passos']} passos · {q['ids']} ids · {q['datas']} datas</span>
      {'<span class="chip chip--warn">não olhou: ' + esc(faltou) + '</span>'
       if faltou else '<span class="chip chip--calm">olhou as 8 fontes</span>'}
      {'<span class="chip chip--faint">' + str(q['incertezas']) + ' incerteza(s)</span>' if q['incertezas'] else ''}
    </div>'''
        traj = "".join(f'<li>{esc(t)[:260]}</li>' for t in (f.get("trajetoria") or [])[:9])
        nao = "".join(f'<li>{esc(t)[:220]}</li>' for t in (f.get("nao_consegui_saber") or [])[:5])
        fat = "".join(f'<li>{esc((x or {}).get("fato", x))[:150]}</li>'
                      for x in (f.get("fatos_novos") or [])[:4])
        pv = f.get("precisa_de_voce") or {}
        meta = f.get("_meta") or {}
        cards.append(f"""<article class="frente">
  <header><span class="nome">{esc(f.get('frente'))[:52]}</span>
    <span class="lat">{meta.get('duracao_s','?')}s · US${round(meta.get('custo_usd') or 0, 2)}</span></header>
  {barra}
  {'<p class="portao"><b>precisa de você:</b> ' + esc(pv.get('o_que'))[:300] + '</p>' if pv.get('sim') else '<p class="ok">sem portão nesta frente</p>'}
  <details><summary>como chegou aí — {len(f.get('trajetoria') or [])} passos</summary><ol>{traj}</ol></details>
  {'<details><summary>o que NÃO conseguiu saber — ' + str(len(f.get('nao_consegui_saber') or [])) + '</summary><ul class="nao">' + nao + '</ul></details>' if nao else ''}
  {'<details><summary>aprendeu — ' + str(len(f.get('fatos_novos') or [])) + ' fato(s)</summary><ul>' + fat + '</ul></details>' if fat else ''}
  <div class="veredito" data-uid="{esc(d['rodada']['run_date'])}|{f.get('project_id')}|{esc(f.get('frente'))}">
    <span class="v-t">este julgamento</span>
    <button type="button" data-v="certa">Certa</button>
    <button type="button" data-v="errada">Cobrou à toa</button>
    <button type="button" data-v="passou">Deixou passar</button>
    <label class="raso"><input type="checkbox" data-raso> julgou sem olhar tudo</label>
    <textarea class="v-nota" rows="1" placeholder="por quê? — o motivo é o que vira aprendizado no PDCA"></textarea>
  </div>
</article>""")

    # --- portões pendentes de veredito (mesmo data-uid: o JS e o `--gravar` já servem)
    #
    # Fora os que JÁ têm card acima: uid repetido na página faria o botão
    # "Copiar" emitir a mesma linha duas vezes e o contador mentir pra cima.
    ja_na_pagina = {f"{d['rodada']['run_date']}|{f.get('project_id')}|{f.get('frente')}"
                    for f in frentes[:6]} if d["rodada"] else set()
    pendentes = []
    for pd in d["pendentes"]:
        if f"{pd['dia']}|{pd['project_id']}|{pd['frente']}" in ja_na_pagina:
            continue
        pendentes.append(f"""<article class="frente frente--pend">
  <header><span class="nome">{esc(pd['frente'])[:52]}</span>
    <span class="lat">{esc(pd['dia'])}</span></header>
  <p class="portao"><b>pediu de você:</b> {esc(pd['o_que'])[:300]}</p>
  <div class="veredito" data-uid="{esc(pd['dia'])}|{pd['project_id']}|{esc(pd['frente'])}">
    <span class="v-t">este julgamento</span>
    <button type="button" data-v="certa">Certa</button>
    <button type="button" data-v="errada">Cobrou à toa</button>
    <button type="button" data-v="passou">Deixou passar</button>
    <label class="raso"><input type="checkbox" data-raso> julgou sem olhar tudo</label>
    <textarea class="v-nota" rows="1" placeholder="por quê? — o motivo é o que vira aprendizado no PDCA"></textarea>
  </div>
</article>""")

    # --- agora
    crons = []
    for c in d["crons"]:
        idade = (ago.replace(tzinfo=None) - c["ultimo"]).total_seconds() / 60 if c["ultimo"] else 999
        cor = "crit" if c["erros"] else ("calm" if idade < 90 else "warn")
        crons.append(f'<tr><td><span class="dot dot--{cor}"></span>{esc(c["job"])[:30]}</td>'
                     f'<td class="n">{c["runs"]}</td><td class="n">{c["erros"] or ""}</td>'
                     f'<td class="n">{c["ultimo"]:%H:%M}</td></tr>')

    falhas = tri.get("falhas_detalhe") or []
    bloco_falhas = ""
    if falhas:
        li = "".join(f'<li><b>#{esc(x.get("project_id"))}</b> {esc(x.get("frente"))[:34]} — '
                     f'<code>{esc(x.get("erro"))[:110]}</code></li>' for x in falhas)
        bloco_falhas = f'<div class="alerta"><b>{len(falhas)} frente(s) falharam na última rodada</b><ul>{li}</ul></div>'
    elif tri.get("falhas"):
        bloco_falhas = (f'<div class="alerta"><b>{tri["falhas"]} falha(s)</b> — sem detalhe gravado '
                        f'(rodada anterior ao fix de 06/08; a próxima já traz o motivo).</div>')

    modelo_ok = "🟢" in d["modelo_txt"]
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>INTEL — o que o sistema está processando</title>
<style>
:root {{
  --paper:#f4f0e7; --panel:#fbf9f3; --panel-edge:#e4ddcc; --ink:#23211c;
  --ink-soft:#57534a; --ink-faint:#8a8477; --brass:#9c7a33; --brass-soft:#b79a55;
  --crit:#a4321f; --crit-bg:#f0e0d6; --warn:#9a6a13; --warn-bg:#f0e6cf;
  --calm:#4f6b4a; --calm-bg:#e3e9dc;
  --shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 var(--sans);-webkit-font-smoothing:antialiased}}
.wrap{{max-width:60rem;margin:0 auto;padding:2.2rem 1.25rem 4rem}}
.eyebrow{{font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--brass);font-weight:600;margin:0 0 .4rem}}
h1{{font-family:var(--serif);font-weight:400;font-size:clamp(1.6rem,4vw,2.1rem);margin:0;line-height:1.2}}
.filete{{width:3rem;height:2px;background:var(--brass-soft);margin:.9rem 0 1rem}}
.quando{{font-family:var(--mono);font-size:.74rem;color:var(--ink-faint);margin:0 0 1.6rem}}
h2{{font-family:var(--serif);font-weight:400;font-size:1.15rem;margin:2.2rem 0 .3rem}}
.sub-h2{{font-size:.84rem;color:var(--ink-soft);margin:0 0 .9rem}}
.legenda{{font-size:.76rem;color:var(--ink-soft);margin:.15rem 0 1rem;padding:.5rem .7rem;background:var(--panel);border:1px solid var(--panel-edge);border-left:3px solid var(--brass-soft);border-radius:3px}}
.resumo{{display:grid;grid-template-columns:repeat(auto-fit,minmax(7rem,1fr));gap:.55rem;margin:0 0 .5rem}}
.tile{{background:var(--panel);border:1px solid var(--panel-edge);border-radius:4px;padding:.75rem .8rem;box-shadow:var(--shadow);border-top:2px solid var(--t)}}
.tile b{{display:block;font-family:var(--mono);font-size:1.35rem;line-height:1.15;font-variant-numeric:tabular-nums;color:var(--t)}}
.tile span{{font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-faint)}}
table{{width:100%;border-collapse:collapse;font-size:.87rem;background:var(--panel);border:1px solid var(--panel-edge);border-radius:4px;overflow:hidden}}
th{{text-align:left;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-faint);font-weight:600;padding:.45rem .7rem;border-bottom:1px solid var(--panel-edge)}}
td{{padding:.4rem .7rem;border-bottom:1px solid var(--panel-edge)}}
td.n{{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}}
.sub{{display:block;font-size:.72rem;color:var(--ink-faint)}}
.dot{{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;margin-right:.35rem;vertical-align:middle}}
.dot--calm{{background:var(--calm)}} .dot--warn{{background:var(--warn)}} .dot--crit{{background:var(--crit)}}
.frente{{background:var(--panel);border:1px solid var(--panel-edge);border-left:3px solid var(--brass-soft);border-radius:4px;padding:.85rem 1rem;margin:0 0 .6rem;box-shadow:var(--shadow)}}
.frente--pend{{border-left-style:dashed}}
.pend-t{{font-family:var(--serif);font-weight:400;font-size:1.02rem;margin:1.8rem 0 .3rem}}
.h2-sub{{font-size:.8rem;color:var(--ink-faint);font-family:var(--sans)}}
.mapa{{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:.6rem;margin:.2rem 0 .8rem}}
.ent{{background:var(--panel);border:1px solid var(--panel-edge);border-top:3px solid var(--brass);border-radius:4px;padding:.7rem .8rem;box-shadow:var(--shadow)}}
.ent--parada{{border-top-color:var(--panel-edge);opacity:.62}}
.ent header{{display:flex;justify-content:space-between;align-items:baseline;gap:.5rem}}
.ent header span{{font-family:var(--serif);font-size:1rem}}
.ent header b{{font-family:var(--mono);font-size:1.25rem;color:var(--brass)}}
.ent--parada header b{{color:var(--ink-faint)}}
.ent-tot{{margin:.1rem 0 .5rem;font-size:.68rem;color:var(--ink-faint);font-family:var(--mono)}}
.ent-eco{{margin:-.3rem 0 .5rem;font-size:.66rem;color:var(--ink-soft);background:var(--warn-bg);padding:.25rem .4rem;border-radius:3px}}
.ent ul{{list-style:none;margin:0;padding:0;font-family:var(--mono);font-size:.7rem}}
.ent li{{display:flex;gap:.4rem;align-items:baseline;padding:.14rem 0;border-top:1px solid var(--panel-edge)}}
.ent li:first-child{{border-top:0}}
.ent li b{{margin-left:auto;color:var(--brass);font-weight:600}}
.ent li span{{color:var(--ink-faint);min-width:3.6rem;text-align:right}}
.ent li.morta{{color:var(--ink-faint)}}
.ent li.morta b{{color:var(--ink-faint);font-weight:400}}
.verif{{margin:0 0 .4rem}}
.verif summary{{font-size:.8rem;color:var(--ink-soft);cursor:pointer}}
.frente>header{{display:flex;justify-content:space-between;gap:.7rem;align-items:baseline;flex-wrap:wrap;margin-bottom:.4rem}}
.nome{{font-weight:600;font-size:.94rem}}
.lat{{font-family:var(--mono);font-size:.72rem;color:var(--ink-faint)}}
.qual{{display:flex;gap:.35rem;flex-wrap:wrap;margin:.1rem 0 .5rem}}
.chip{{font-family:var(--mono);font-size:.68rem;padding:.12rem .45rem;border-radius:2px;border:1px solid var(--panel-edge);color:var(--ink-soft)}}
.chip--calm{{border-color:var(--calm);color:var(--calm);background:var(--calm-bg)}}
.chip--warn{{border-color:var(--warn);color:var(--warn);background:var(--warn-bg)}}
.chip--crit{{border-color:var(--crit);color:var(--crit);background:var(--crit-bg)}}
.chip--faint{{color:var(--ink-faint)}}
.ctx{{margin:0 0 .6rem;padding:.45rem .6rem;background:var(--paper);border:1px solid var(--panel-edge);border-radius:3px}}
.ctx-t{{font-size:.6rem;letter-spacing:.12em;text-transform:uppercase;color:var(--brass)}}
.fonte{{display:inline-block;font-family:var(--mono);font-size:.7rem;padding:.1rem .4rem;margin:.15rem .2rem 0 0;border-radius:2px}}
.fonte--sim{{background:var(--calm-bg);color:var(--calm)}}
.fonte--nao{{background:var(--crit-bg);color:var(--crit);text-decoration:line-through}}
.risco{{color:var(--crit)}}
.portao{{margin:.2rem 0 .6rem;font-size:.87rem;background:var(--warn-bg);border-radius:3px;padding:.45rem .6rem}}
.ok{{margin:.2rem 0 .6rem;font-size:.83rem;color:var(--ink-faint)}}
details{{margin:.3rem 0}}
summary{{cursor:pointer;font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;color:var(--brass);font-weight:600}}
summary:focus-visible{{outline:2px solid var(--brass);outline-offset:2px}}
details ol,details ul{{margin:.4rem 0 .2rem;padding-left:1.3rem;font-size:.85rem;color:var(--ink-soft)}}
details li{{margin-bottom:.25rem}}
ul.nao li{{color:var(--warn)}}
.veredito{{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;margin-top:.6rem;padding-top:.6rem;border-top:1px solid var(--panel-edge)}}
.v-t{{font-size:.6rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-faint);margin-right:.2rem}}
.veredito button{{font:inherit;font-size:.76rem;padding:.28rem .7rem;cursor:pointer;background:transparent;color:var(--ink-faint);border:1px solid var(--panel-edge);border-radius:2px}}
.veredito button:hover{{border-color:var(--brass-soft);color:var(--ink)}}
.veredito button:focus-visible{{outline:2px solid var(--brass);outline-offset:2px}}
.veredito button[aria-pressed="true"]{{background:var(--brass);border-color:var(--brass);color:var(--panel);font-weight:600}}
.raso{{font-size:.74rem;color:var(--ink-soft);cursor:pointer;display:inline-flex;align-items:center;gap:.25rem}}
.v-nota{{flex-basis:100%;margin-top:.45rem;font:inherit;font-size:.8rem;line-height:1.45;padding:.35rem .5rem;
  border:1px dashed var(--panel-edge);border-radius:3px;background:var(--paper);color:var(--ink);
  resize:vertical;min-height:1.9rem;field-sizing:content}}
.v-nota:focus{{outline:2px solid var(--brass-soft);outline-offset:1px;border-style:solid}}
.v-nota::placeholder{{color:var(--ink-faint)}}
.barra-fb{{position:sticky;bottom:0;margin-top:1.4rem;padding:.8rem 1rem;background:var(--panel);border:1px solid var(--panel-edge);border-radius:4px;display:flex;gap:1rem;justify-content:space-between;align-items:center;flex-wrap:wrap;box-shadow:0 -2px 14px rgba(35,33,28,.08)}}
.barra-fb button{{font:inherit;font-size:.79rem;padding:.42rem .95rem;cursor:pointer;border-radius:2px;border:1px solid var(--brass);background:var(--brass);color:var(--panel);font-weight:600}}
.barra-fb button.ghost{{background:transparent;color:var(--ink-faint);border-color:var(--panel-edge);font-weight:400}}
#fb-saida{{width:100%;margin-top:.6rem;font:12px/1.5 var(--mono);padding:.6rem;border:1px solid var(--panel-edge);border-radius:3px;background:var(--paper);color:var(--ink);display:none}}
.alerta{{background:var(--crit-bg);border:1px solid var(--crit);border-radius:4px;padding:.7rem .9rem;margin:.6rem 0;font-size:.86rem}}
.alerta ul{{margin:.4rem 0 0;padding-left:1.2rem}}
.alerta code{{font-family:var(--mono);font-size:.78rem}}
pre{{background:var(--panel);border:1px solid var(--panel-edge);border-radius:4px;padding:.7rem .9rem;font:12px/1.5 var(--mono);overflow-x:auto;margin:.4rem 0 0}}
.nota{{font-size:.82rem;color:var(--ink-faint);margin:.5rem 0 0}}
</style></head><body>
<div class="wrap">
  <p class="eyebrow">INTEL · observabilidade</p>
  <h1>O que o sistema está processando</h1>
  <div class="filete"></div>
  <p class="quando">gerado {ago:%d/%m/%Y %H:%M} UTC · recarrega sozinho a cada 2 min</p>

  <div class="resumo">
    {tile(d['tabelas'], 'tabelas', 'calm' if modelo_ok else 'crit')}
    {tile(f"{d['fatos']['n']:,}".replace(',','.'), 'fatos', 'ink')}
    {tile(d['webhook']['ult_hora'], 'eventos WA/h', 'calm' if d['webhook']['ult_hora'] else 'warn')}
    {tile(tri.get('ativas','?'), 'frentes ativas', 'ink')}
    {tile(tri.get('julgadas_agora','?'), 'julgadas na rodada', 'ink')}
    {tile(tri.get('falhas', 0), 'falhas', 'crit' if tri.get('falhas') else 'calm')}
  </div>
  <p class="legenda">Destes seis, só <b>dois exigem ação sua</b>: <b>falhas</b> (&gt;0 = algo caiu
  agora) e <b>eventos WA/h</b> em zero por mais de uma hora (o cano da ingestão parou — foi assim
  o incidente de 03/08, 3h caladas). <b>Tabelas</b> só importa se o verificador reprovar;
  <b>fatos</b>, <b>frentes</b> e <b>julgadas</b> são escala, não alarme.</p>
  {bloco_falhas}

  <h2>1 · Modelo de dados <span class="h2-sub">— e o que passou por ele em 24h</span></h2>
  <p class="sub-h2">Cada caixa é uma <b>entidade do contrato</b>; dentro, as tabelas que ela ocupa.
  O número grande é <b>o que entrou nas últimas 24h</b> — é ele que mostra o sistema respirando.
  O total só existe pra dar escala. <b>Caixa apagada = entidade parada há um dia.</b></p>
  <div class="mapa">{''.join(mapa)}</div>
  <details class="verif"><summary>o verificador de conformidade (forma, não movimento)</summary>
  <p class="nota">Compara o banco com o baseline do contrato. <b>Divergência não é erro — é sinal:</b>
  ou foi decisão e o contrato precisa mudar, ou alguém criou estrutura fora do modelo.</p>
  <pre>{esc(d['modelo_txt'])}</pre></details>

  <h2>2 · Canos de entrada</h2>
  <p class="sub-h2">Cada fonte, com volume real e quando entrou a última.
  <b>Cano automático</b> é o que se mede sozinho; <b>registro manual</b> é conversa que você colou
  à mão — não há integração por trás, e cobrar dele "está parado" seria inventar um defeito.</p>
  <table><thead><tr><th>fonte</th><th class="n">registros</th><th class="n">última</th></tr></thead>
  <tbody>{''.join(linhas_cano)}</tbody></table>

  <h2>3 · Cobertura contra a origem</h2>
  <p class="sub-h2">O INTEL tem tudo que a fonte tem? Esta é a leitura que teria pego os
  sete defeitos de 04-06/08 — em todos, o mecanismo pedia, recebia e <b>reportava sucesso</b>
  sem nunca comparar com o outro lado.</p>
  <table><thead><tr><th>grupo</th><th class="n">INTEL</th><th class="n">Evolution</th><th class="n">cobertura</th></tr></thead>
  <tbody>{''.join(cob) or '<tr><td colspan="4">(Evolution não respondeu)</td></tr>'}</tbody></table>
  <p class="nota">O que falta é sticker/reaction/mídia sem texto — não entra nem ao vivo.</p>

  <h2>4 · Raciocínio do agente</h2>
  <p class="sub-h2"><b>Três leituras, não uma.</b> (a) <b>precisão histórica</b> — do placar que
  VOCÊ preencheu, único juízo que não é o sistema se auto-avaliando; (b) <b>contexto tocado</b> —
  quais das 8 fontes ele consultou, e <b class="risco">quais ignorou</b>: julgar sem olhar o
  e-mail foi o que produziu os "sem resposta" falsos; (c) <b>evidência e incerteza</b> — ids e
  datas citados (a regra manda citar) e quantas dúvidas ele declarou. Última rodada
  {esc(d['rodada']['run_at'].strftime('%d/%m %H:%M') if d['rodada'] else '—')} UTC.</p>
  {''.join(cards) or '<p class="nota">Nenhuma frente com trajetória nesta rodada.</p>'}

  <h3 class="pend-t">Outros portões dos últimos 7 dias sem veredito — {len(pendentes)}</h3>
  <p class="sub-h2">O agente roda 14×/dia; a rodada acima é uma delas. Sem esta lista, o
  portão aberto às 15h nunca chegava ao placar — e o gate dos cockpits (≥70% em ≥15 portões)
  ficava parado por falta do que marcar, parecendo estabilidade.</p>
  {''.join(pendentes) or '<p class="nota">Nenhum portão pendente — tudo dos últimos 7 dias já foi marcado.</p>'}

  <div class="barra-fb">
    <span class="progresso"><b id="fb-n">0</b> julgamento(s) avaliado(s)</span>
    <div>
      <button type="button" class="ghost" id="fb-limpar">Limpar</button>
      <button type="button" id="fb-copiar">Copiar para o Claude</button>
    </div>
  </div>
  <textarea id="fb-saida" rows="6" readonly></textarea>

  <h2>5 · O que rodou nas últimas 6h</h2>
  <table><thead><tr><th>job</th><th class="n">runs</th><th class="n">erros</th><th class="n">último</th></tr></thead>
  <tbody>{''.join(crons)}</tbody></table>
</div>@@JS@@</body></html>""".replace("@@JS@@", JS_FEEDBACK)



# ------------------------------------------------------------------ gravar --

ROTULOS = {"CERTA": "certa", "COBROU A TOA": "errada", "DEIXOU PASSAR": "passou"}


def gravar(texto: str) -> int:
    """Aplica o veredito colado da página em `cos_portao_veredito`.

    Formato de cada linha (o botão "Copiar para o Claude" produz assim):
        2026-08-06 #48 Luminosità — CERTA | RASO: julgou sem olhar tudo | NOTA: ...

    O "RASO" vai pra coluna `nota` em vez de virar veredito novo: o CHECK da
    tabela aceita só certa/errada/passou, e mexer nele exigiria migration pra
    guardar um sinal que é ORTOGONAL ao acerto. Um portão pode estar CERTO e
    ainda assim ter sido julgado sem olhar tudo — inclusive é o caso mais
    perigoso, porque acertou por sorte.

    A NOTA (07/08, pedido do Renato: "quero comentar os portões dos últimos 7
    dias pra gerar aprendizado") divide a mesma coluna e vem PRIMEIRO nela: o
    rótulo diz QUANTOS erraram, o comentário diz POR QUÊ — e sem o porquê o PDCA
    vê a precisão cair sem saber o que mudar. Fica por último NA LINHA porque o
    texto livre pode conter `|`, e qualquer campo depois dela seria engolido.

    Linha com comentário e SEM rótulo é lida e avisada, mas não gravada:
    entraria no placar como acerto ou erro, e inventar veredito é pior que
    perder a observação — que segue no clipboard dele.
    """
    linhas = [l.strip() for l in texto.splitlines()
              if re.match(r"^\d{4}-\d{2}-\d{2}\s+#\d+", l.strip())]
    if not linhas:
        sys.exit("nada reconhecido — esperado 'AAAA-MM-DD #id Frente — VEREDITO'")

    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor()
    ok = raso_n = 0
    for l in linhas:
        # A NOTA vem por último de propósito: o texto livre do Renato pode
        # conter `|`, e qualquer campo depois dela seria comido pelo comentário.
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+#(\d+)\s+(.+?)\s+—\s+([A-ZÀ-Ú ]+?)"
                     r"(?:\s*\|\s*RASO:[^|]*)?(?:\s*\|\s*NOTA:\s*(.*))?$", l)
        if not m:
            print(f"  ? não parseou: {l[:70]}")
            continue
        dia, pid, frente, rot, comentario = m.groups()
        raso = "| RASO:" in l
        ver = ROTULOS.get(rot.strip())
        if not ver and rot.strip() != "SEM VEREDITO":
            print(f"  ? veredito desconhecido '{rot.strip()}'")
            continue
        # Comentário e RASO cabem na mesma coluna, mas o comentário vem PRIMEIRO:
        # é o que a pessoa escreveu, e é ele que se lê no PDCA.
        partes = [(comentario or "").strip()] if (comentario or "").strip() else []
        if raso:
            partes.append("[julgou sem olhar todo o contexto disponível]")
        nota = " ".join(partes) or None
        if not ver:
            # Sem rótulo, mas com observação: registrar mesmo assim seria mentir
            # no placar (entraria como acerto ou erro). Sai como aviso pra não
            # sumir em silêncio — o texto está no clipboard dele.
            print(f"  ! sem veredito, só comentário — não entra no placar: "
                  f"{frente.strip()[:40]} — {nota[:60] if nota else ''}")
            continue
        # portao = a frente + o dia: é o que identifica o julgamento avaliado
        # A calibração vigente vai junto: sem ela a linha não diz SOB QUAIS
        # parâmetros o portão foi julgado, e o PDCA agrupa a precisão por
        # deb/teto justamente pra comparar calibrações. Ficava NULL — e o
        # `:3d` em None derrubava o bloco inteiro do relatório.
        deb = os.getenv("DEBOUNCE_MIN") or None
        teto = os.getenv("TETO_DIARIO") or None
        cur.execute("""
            INSERT INTO cos_portao_veredito
              (run_date, project_id, frente, portao, veredito, nota, motor,
               debounce_min, teto_diario)
            VALUES (%s,%s,%s,%s,%s,%s,'agente_local',%s,%s)
            ON CONFLICT (run_date, frente, portao)
            DO UPDATE SET veredito = EXCLUDED.veredito, nota = EXCLUDED.nota,
                          debounce_min = COALESCE(cos_portao_veredito.debounce_min,
                                                  EXCLUDED.debounce_min),
                          teto_diario  = COALESCE(cos_portao_veredito.teto_diario,
                                                  EXCLUDED.teto_diario)
        """, (dia, int(pid), frente.strip(), f"julgamento da rodada de {dia}", ver, nota,
              int(deb) if deb else None, int(teto) if teto else None))
        ok += 1
        raso_n += 1 if nota else 0
    conn.commit()
    print(f"{ok} veredito(s) gravado(s)" + (f" · {raso_n} marcado(s) como RASO" if raso_n else ""))
    if ok:
        print("Rode `scripts/cos_agent/pdca.py` — o bloco 5b já lê estes números.")
    return 0


def main():
    if "--gravar" in sys.argv:
        i = sys.argv.index("--gravar")
        alvo = sys.argv[i + 1] if len(sys.argv) > i + 1 else "-"
        return gravar(sys.stdin.read() if alvo == "-" else open(alvo).read())
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    d = coletar(cur)
    open(SAIDA, "w").write(render(d))
    print(f"→ {SAIDA}")
    os.system(f'open "{SAIDA}"')


if __name__ == "__main__":
    sys.exit(main() or 0)
