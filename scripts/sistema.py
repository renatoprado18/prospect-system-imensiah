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

    # 2. CANOS
    cur.execute("""
        SELECT cv.canal, count(*) AS msgs, max(COALESCE(m.recebido_em, m.enviado_em)) AS ultima
        FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
        GROUP BY 1 ORDER BY 2 DESC
    """)
    d["canais"] = cur.fetchall()

    cur.execute("""
        SELECT count(*) AS msgs, max(timestamp) AS ultima,
               count(DISTINCT group_jid) AS grupos FROM group_messages
    """)
    d["grupos"] = cur.fetchone()

    cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE verificado) AS verificados FROM contact_facts")
    d["fatos"] = cur.fetchone()

    cur.execute("""SELECT max(received_at) AS ultima,
                   count(*) FILTER (WHERE received_at > now() - interval '1 hour') AS ult_hora
                   FROM webhook_audit""")
    d["webhook"] = cur.fetchone()

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

    # --- canos
    linhas_cano = []
    for c in d["canais"]:
        idade = (ago.replace(tzinfo=None) - c["ultima"]).total_seconds() / 3600 if c["ultima"] else None
        cor = "calm" if idade is not None and idade < 6 else ("warn" if idade is not None and idade < 48 else "crit")
        linhas_cano.append(
            f'<tr><td>mensagens · <b>{esc(c["canal"])}</b></td><td class="n">{c["msgs"]:,}</td>'
            f'<td class="n"><span class="dot dot--{cor}"></span>'
            f'{("há %.0fh" % idade) if idade is not None else "—"}</td></tr>'.replace(",", "."))
    g = d["grupos"]
    idg = (ago.replace(tzinfo=None) - g["ultima"]).total_seconds() / 3600 if g["ultima"] else None
    linhas_cano.append(
        f'<tr><td>mensagens de <b>grupo</b> ({g["grupos"]} grupos)</td>'
        f'<td class="n">{g["msgs"]:,}</td><td class="n">'
        f'<span class="dot dot--{"calm" if idg is not None and idg < 6 else "warn"}"></span>'
        f'{("há %.0fh" % idg) if idg is not None else "—"}</td></tr>'.replace(",", "."))
    linhas_cano.append(
        f'<tr><td><b>fatos</b> destilados <span class="sub">{d["fatos"]["verificados"]} verificados por você</span></td>'
        f'<td class="n">{d["fatos"]["n"]:,}</td><td class="n">—</td></tr>'.replace(",", "."))

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
        usados = "".join(f'<span class="fonte fonte--sim">{esc(x)}</span>' for x in q["tocou"])
        faltou = "".join(f'<span class="fonte fonte--nao">{esc(x)}</span>' for x in q["nao_tocou"])
        barra = f'''<div class="qual">
      {hist}
      <span class="chip">{q['ids']} ids · {q['datas']} datas citadas</span>
      <span class="chip chip--{'calm' if q['incertezas'] else 'warn'}">{q['incertezas']} incerteza(s) declarada(s)</span>
    </div>
    <div class="ctx"><span class="ctx-t">contexto que tocou</span>
      <div>{usados or '<span class="fonte fonte--nao">nenhuma fonte identificada</span>'}</div>
      {'<span class="ctx-t" style="margin-top:.4rem;display:block">NÃO tocou</span><div>' + faltou + '</div>' if faltou else ''}
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
  <details open><summary>como chegou aí — {len(f.get('trajetoria') or [])} passos</summary><ol>{traj}</ol></details>
  {'<details><summary>o que NÃO conseguiu saber — ' + str(len(f.get('nao_consegui_saber') or [])) + '</summary><ul class="nao">' + nao + '</ul></details>' if nao else ''}
  {'<details><summary>aprendeu — ' + str(len(f.get('fatos_novos') or [])) + ' fato(s)</summary><ul>' + fat + '</ul></details>' if fat else ''}
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
  {bloco_falhas}

  <h2>1 · Modelo de dados</h2>
  <p class="sub-h2">O banco vivo contra o contrato registrado. <b>Divergência não é erro — é sinal:</b>
  ou foi decisão e o contrato precisa mudar, ou alguém criou estrutura fora do modelo.</p>
  <pre>{esc(d['modelo_txt'])}</pre>

  <h2>2 · Canos de entrada</h2>
  <p class="sub-h2">Cada fonte, com volume real e quando entrou a última.</p>
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

  <h2>5 · O que rodou nas últimas 6h</h2>
  <table><thead><tr><th>job</th><th class="n">runs</th><th class="n">erros</th><th class="n">último</th></tr></thead>
  <tbody>{''.join(crons)}</tbody></table>
</div></body></html>"""


def main():
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    d = coletar(cur)
    open(SAIDA, "w").write(render(d))
    print(f"→ {SAIDA}")
    os.system(f'open "{SAIDA}"')


if __name__ == "__main__":
    main()
