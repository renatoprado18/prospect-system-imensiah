#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/mapa.html — como o sistema está organizado, e onde ele é frágil.

PEDIDO (Renato, 07/08/2026): "me dá o big picture" — depois do dia em que o
caminho C foi decidido e executado.

POR QUE GERADO E NÃO ESCRITO À MÃO. Um mapa de arquitetura escrito à mão
envelhece exatamente como o cockpit envelhecia: parece atual e não é. E seria
irônico, porque foi este dia que trocou aquela página por um gerador. Aqui a
ESTRUTURA é fixa (o contrato das superfícies, decidido) e os NÚMEROS são vivos —
se a precisão cair ou o backlog crescer, o mapa muda sozinho.

A terceira página gerada de `~/cockpit/`, e ela não viola o contrato C: responde
"como o sistema está organizado?", que não é a pergunta de nenhuma das outras.

    ~/cockpit/index.html    → o que precisa de mim hoje
    ~/cockpit/sistema.html  → o sistema está saudável agora
    ~/cockpit/mapa.html     → como ele está organizado, e onde é frágil
"""
import os
import subprocess
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/mapa.html")


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(n):
    return f"{n:,}".replace(",", ".") if isinstance(n, int) else str(n)


def coletar(cur):
    d = {}
    # --- funil da camada
    cur.execute("""SELECT count(*) n FROM projects WHERE COALESCE(status,'ativo') = 'ativo'""")
    d["frentes_ativas"] = cur.fetchone()["n"]
    cur.execute("""SELECT payload, run_at, EXTRACT(EPOCH FROM (now()-run_at))/3600 h
                     FROM cos_daily_review ORDER BY run_at DESC LIMIT 1""")
    r = cur.fetchone()
    import json as _j
    pay = r["payload"] if r else {}
    if isinstance(pay, str):
        pay = _j.loads(pay)
    frentes = pay.get("frentes") or []
    d["julgadas"] = len(frentes)
    d["portoes"] = len([f for f in frentes if (f.get("precisa_de_voce") or {}).get("sim")])
    d["review_h"] = float(r["h"]) if r and r["h"] is not None else None

    # --- placar (o único juízo externo)
    cur.execute("""SELECT count(*) n, count(*) FILTER (WHERE veredito='certa') certas
                     FROM cos_portao_veredito WHERE COALESCE(origem,'humano')='humano'""")
    p = cur.fetchone()
    d["vereditos"], d["certas"] = p["n"], p["certas"]
    d["precisao"] = (100 * p["certas"] // p["n"]) if p["n"] else None

    # --- fragilidades, com número (sem número é lamento, não diagnóstico)
    cur.execute("""SELECT count(*) n, count(*) FILTER (WHERE contact_id IS NULL) orfas
                     FROM tasks WHERE status IN ('pending','in_progress')""")
    t = cur.fetchone()
    d["tasks"], d["tasks_orfas"] = t["n"], t["orfas"]
    cur.execute("""SELECT count(*) n FROM group_messages gm
                     JOIN project_whatsapp_groups pwg ON pwg.group_jid=gm.group_jid AND pwg.ativo
                    WHERE gm.raci_processed_at IS NULL AND gm.from_me=FALSE
                      AND gm.content IS NOT NULL AND length(gm.content)>=12""")
    d["raci_fila"] = cur.fetchone()["n"]
    cur.execute("SELECT count(*) n FROM system_memories WHERE embedding IS NULL")
    d["embeddings_null"] = cur.fetchone()["n"]
    cur.execute("""SELECT count(*) n FROM check_g_ledger
                    WHERE msg_em > now() - interval '4 days'""")
    d["checkg_janela"] = cur.fetchone()["n"]
    cur.execute("""SELECT count(*) n FROM check_g_ledger
                    WHERE msg_em > now() - interval '4 days' AND decisao='mostrado'""")
    d["checkg_mostrado"] = cur.fetchone()["n"]

    # --- rotas expostas: a fonte é o script, não uma contagem paralela
    try:
        out = subprocess.run([f"{ROOT}/.venv/bin/python", f"{ROOT}/scripts/rotas_sem_auth.py"],
                             capture_output=True, text=True, timeout=90, cwd=ROOT).stdout
        d["rotas"] = int(out.split(":")[1].split()[0])
    except Exception:
        d["rotas"] = None
    return d


SUPERFICIES = [
    ("~/cockpit/index.html", "o que precisa de mim <b>hoje</b>", "a ÚNICA que responde isto",
     "chão (agenda · tarefas · WhatsApp) + o portão da camada", "viva"),
    ("Home INTEL", "quem é essa pessoa, como está esse projeto", "navegação e CRM",
     "perdeu o portão em 07/08 — sem vocabulário de cobrança", "viva"),
    ("tonIAH", "conversar, perguntar, delegar", "o canal do celular",
     "renderiza o MESMO julgamento; sem régua própria desde 07/08", "viva"),
    ("Camada (agente)", "o julgamento e o porquê", "não é superfície: alimenta o cockpit",
     "14 rodadas/dia no Mac · credencial read-only", "viva"),
    ("~/cockpit/sistema.html", "o sistema está saudável agora", "observabilidade",
     "modelo · canos · cobertura · raciocínio · jobs", "viva"),
    ("~/cockpit/mapa.html", "como ele está organizado", "esta página", "estrutura fixa, números vivos", "viva"),
]

CSS = """
:root{--paper:#f4f0e7;--panel:#fbf9f3;--edge:#e4ddcc;--ink:#23211c;--soft:#57534a;
--faint:#8a8477;--brass:#9c7a33;--brass2:#b79a55;--crit:#a4321f;--critbg:#f0e0d6;
--warn:#9a6a13;--warnbg:#f0e6cf;--calm:#4f6b4a;--calmbg:#e3e9dc;
--shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
--serif:Georgia,"Iowan Old Style",serif;--sans:system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:64rem;margin:0 auto;padding:2.4rem 1.3rem 4rem}
.eyebrow{font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--brass);font-weight:600;margin:0 0 .4rem}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(1.7rem,4vw,2.3rem);margin:0;line-height:1.2}
.filete{width:3rem;height:2px;background:var(--brass2);margin:.9rem 0 1rem}
.quando{font-family:var(--mono);font-size:.74rem;color:var(--faint);margin:0 0 2rem}
h2{font-family:var(--serif);font-weight:400;font-size:1.25rem;margin:2.6rem 0 .3rem}
.sub{font-size:.88rem;color:var(--soft);margin:0 0 1.1rem;max-width:62ch}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.6rem;margin:0 0 1rem}
.tile{background:var(--panel);border:1px solid var(--edge);border-top:2px solid var(--t,var(--brass));
border-radius:4px;padding:.8rem .85rem;box-shadow:var(--shadow)}
.tile b{display:block;font-family:var(--mono);font-size:1.5rem;line-height:1.1;color:var(--t,var(--ink));font-variant-numeric:tabular-nums}
.tile span{font-size:.66rem;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
table{width:100%;border-collapse:collapse;font-size:.87rem;background:var(--panel);
border:1px solid var(--edge);border-radius:4px;overflow:hidden}
th{text-align:left;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);
font-weight:600;padding:.5rem .7rem;border-bottom:1px solid var(--edge)}
td{padding:.55rem .7rem;border-bottom:1px solid var(--edge);vertical-align:top}
td.q{font-weight:600}
.mono{font-family:var(--mono);font-size:.8rem}
.funil{display:flex;flex-wrap:wrap;align-items:stretch;gap:.4rem;margin:.4rem 0 .6rem}
.etapa{flex:1 1 8rem;background:var(--panel);border:1px solid var(--edge);border-radius:4px;
padding:.7rem .75rem;box-shadow:var(--shadow);position:relative}
.etapa b{display:block;font-family:var(--mono);font-size:1.35rem;color:var(--brass);line-height:1.1}
.etapa span{font-size:.7rem;color:var(--faint);display:block;margin-top:.15rem}
.etapa i{font-style:normal;font-size:.68rem;color:var(--soft);display:block;margin-top:.35rem;line-height:1.35}
.seta{align-self:center;color:var(--brass2);font-size:1.1rem;flex:0 0 auto}
.frag{background:var(--panel);border:1px solid var(--edge);border-left:3px solid var(--crit);
border-radius:4px;padding:.7rem .9rem;margin:0 0 .5rem;font-size:.87rem}
.frag b{color:var(--crit)}
.frag .n{font-family:var(--mono);font-weight:600}
.ok{border-left-color:var(--calm)}
.ok b{color:var(--calm)}
.nota{font-size:.8rem;color:var(--faint);margin:.6rem 0 0}
.pill{display:inline-block;font-family:var(--mono);font-size:.66rem;padding:.1rem .45rem;border-radius:2px;
background:var(--calmbg);color:var(--calm);margin-left:.3rem}
"""


def render(d):
    ago = datetime.now()
    prec = d["precisao"]
    cor_prec = "calm" if prec and prec >= 70 else "crit"

    linhas = "".join(
        f'<tr><td class="q">{s[0]}</td><td>{s[1]}</td><td class="mono">{s[2]}</td>'
        f'<td style="font-size:.82rem;color:var(--soft)">{s[3]}</td></tr>'
        for s in SUPERFICIES)

    def etapa(n, rot, exp):
        return (f'<div class="etapa"><b>{n}</b><span>{rot}</span><i>{exp}</i></div>')

    funil = (
        etapa(d["frentes_ativas"], "frentes ativas", "projetos vivos no INTEL")
        + '<div class="seta">→</div>'
        + etapa(d["julgadas"], "julgadas", "o agente lê fontes e escreve trajetória")
        + '<div class="seta">→</div>'
        + etapa(d["portoes"], "no portão", "o que ele conclui que precisa de você")
        + '<div class="seta">→</div>'
        + etapa(d["vereditos"], "avaliados", "você marcou certa/errada — juízo externo")
        + '<div class="seta">→</div>'
        + etapa(f"{prec}%" if prec else "—", "precisão", "o gate pedia ≥70% em ≥15")
    )

    frag = [
        ("crit", "A camada erra 1 em 4.", f"{prec}% de precisão em {d['vereditos']} portões avaliados. "
         "Passa no piso, mas um quarto do que ela cobra não presta — e o custo do erro é você "
         "desconfiar do resto."),
        ("crit", "Medir a qualidade ainda depende de você.",
         "A inferência automática foi medida contra os seus vereditos: 80% de concordância com viés "
         "otimista (dizia “certa” onde você disse “cobrou à toa”). Não substitui; no máximo vigia tendência."),
        ("crit", "O julgamento mora no seu Mac.",
         "14 rodadas/dia por launchd. Máquina desligada = sem portão. O resto do cockpit "
         "(agenda, tarefas, WhatsApp) continua, porque não depende da camada."),
        ("crit", f"{d['tasks_orfas']} de {d['tasks']} tarefas abertas sem contato.",
         "Sem <code>contact_id</code> elas não cruzam com WhatsApp nenhum: ficam fora do "
         "“ele já respondeu?” e do check-G."),
        ("crit", f"{num(d['raci_fila'])} mensagens de grupo na fila do RACI.",
         "Backlog histórico (desde 21/12/2025), não atraso: o fluxo do dia sai em dia. "
         "É trabalho de sessão decidir se processa em lote ou aposenta."),
        ("crit", f"{d['embeddings_null']} memórias sem embedding.",
         "Voyage limita a 3 requisições/min. Essas só aparecem em busca por palavra-chave."),
    ]
    frag_html = "".join(
        f'<div class="frag"><b>{esc(t)}</b> <span>{c}</span></div>' for _, t, c in frag)

    idade = (f"{d['review_h']:.0f}h" if d["review_h"] is not None else "—")
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>INTEL — o mapa</title><style>{CSS}</style></head><body>
<div class="wrap">
  <p class="eyebrow">INTEL · arquitetura</p>
  <h1>Como o sistema está organizado</h1>
  <div class="filete"></div>
  <p class="quando">gerado {ago:%d/%m/%Y %H:%M} · estrutura decidida em 07/08 · números ao vivo</p>

  <div class="tiles">
    <div class="tile" style="--t:var(--{cor_prec})"><b>{prec}%</b><span>precisão da camada</span></div>
    <div class="tile"><b>{d['portoes']}</b><span>no portão agora</span></div>
    <div class="tile"><b>{d['julgadas']}</b><span>frentes julgadas</span></div>
    <div class="tile" style="--t:var(--calm)"><b>{d['rotas'] if d['rotas'] is not None else '?'}</b><span>rotas sem auth</span></div>
    <div class="tile"><b>{idade}</b><span>idade do julgamento</span></div>
  </div>

  <h2>1 · Quem responde o quê</h2>
  <p class="sub">Decidido em 07/08 (caminho C), depois de oito dias em que três superfícies
  respondiam “o que precisa de mim hoje” com listas de <b>interseção zero</b>.
  A regra que sobrevive à decisão: superfície nova que “ajude” respondendo o que já é
  respondido recria o problema.</p>
  <table><thead><tr><th>superfície</th><th>pergunta que responde</th><th>papel</th><th>estado</th></tr></thead>
  <tbody>{linhas}</tbody></table>

  <h2>2 · O funil do julgamento</h2>
  <p class="sub">Da frente viva até o número que autoriza decisões. O último passo é o único
  que não é o sistema se auto-avaliando — e por isso é o que trava, quando trava.</p>
  <div class="funil">{funil}</div>
  <p class="nota">O agente roda 14×/dia e só re-julga frente com movimento novo. Desde 07/08,
  quem repete o mesmo pedido 2× seguidas vai pro fim da fila: <b>77% dos portões eram repetição</b>,
  e o teto diário era gasto reconfirmando o já sabido.</p>

  <h2>3 · A triagem do que chega</h2>
  <p class="sub">O WhatsApp que entra passa por gates antes de virar item de cockpit. O ledger
  existe desde 06/08 porque, antes dele, o que era descartado não deixava rastro — e
  “alta precisão” dito sobre o que se vê nunca é afirmação sobre o que se perdeu.</p>
  <div class="funil">
    {etapa(num(d['checkg_janela']), 'avaliadas em 4 dias', 'todo inbound com ficha ou sem')}
    <div class="seta">→</div>
    {etapa(num(d['checkg_mostrado']), 'sobrevivem', 'é isto que chega ao cockpit')}
    <div class="seta">→</div>
    {etapa('5,6%', 'falso negativo', 'medido · e 37,7% não medido, sem ficha pra cruzar')}
  </div>

  <h2>4 · Onde ele é frágil</h2>
  <p class="sub">Com número, porque fragilidade sem número é lamento, não diagnóstico.</p>
  {frag_html}
  <div class="frag ok"><b>O que deixou de ser frágil hoje:</b> <span>rotas que escrevem sem
  credencial (193 → 0, com teto do teste em zero) · o cockpit de frentes, que era HTML à mão e
  agora é gerado a cada 5 min · o placar do gate, que estava crashando em silêncio desde 06/08 ·
  a tonIAH, que servia julgamento velho como atual quando a camada caía.</span></div>

  <h2>5 · O que eu faria a seguir</h2>
  <p class="sub">Atacar a <b>precisão</b>, não mais superfície. Há {d['vereditos']} vereditos com
  comentários seus: dá pra ler o que os erros têm em comum — o caso “isso é RACI da Vallen”
  sugere uma classe inteira (portão sobre ritual recorrente, que tem dia próprio) — e ensinar o
  agente a não abrir esses. É o trabalho que faz o {prec}% subir, e o único que ainda te custa
  alguma coisa.</p>
</div></body></html>"""


def main():
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    open(SAIDA, "w").write(render(coletar(cur)))
    print(f"→ {SAIDA}")
    if "--quieto" not in sys.argv:
        subprocess.run(["open", SAIDA])


if __name__ == "__main__":
    sys.exit(main() or 0)
