#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/devolutiva.html — o que a sessão fez com cada portão.

PEDIDO (Renato, 08/08/2026): "se dou instruções para os 3 portões, copio e colo
no terminal, depois o terminal me cospe um monte de texto, e fico perdido" e, em
seguida, a solução dele: "seria interessante uma devolutiva em html, assim fica
mais organizado, resolvendo inclusive o copiar e colar (se tiverem 3 drafts em
uma tela) ou revisar".

O DIAGNÓSTICO ERA O OUTPUT, NÃO O INPUT. Escrever a instrução nos três portões
ficou fácil na hora em que a página da tonIAH ganhou um campo por card. O que
quebrava vinha depois: três instruções viram três respostas longas no terminal,
em sequência, e ele perdia a correspondência entre o que pediu e o que voltou —
ainda mais quando o retorno inclui RASCUNHOS, que existem para ser lidos com
calma e não rolados num buffer de terminal.

Então o terminal responde UMA LINHA (`→ devolutiva: open ~/cockpit/devolutiva.html`)
e todo o material vem para cá, onde:

  · a numeração ① ② ③ é a MESMA da tela da tonIAH e da ordem em que ele pediu —
    a correspondência vira visual, em vez de ter que ser reconstruída de memória;
  · cada portão mostra `entendi:` numa linha antes do desfecho. É o ponto de
    correção barato: se eu entendi errado, ele vê ANTES de abrir o rascunho, e
    não depois de já ter lido um texto inteiro que não era o que pediu;
  · cada rascunho tem botão de copiar PRÓPRIO. Com três drafts numa tela, um
    botão só devolveria o problema do copiar-e-colar em outro lugar.

O QUE ELE NÃO FAZ: não envia nada, não fecha task, não grava no banco. É a folha
de retorno de trabalho já feito — o disparo continua sendo dele
([[feedback_markdown_blockquote_copy]]: rascunho é para ele revisar e mandar).

Uso:
    scripts/devolutiva.py < resultado.json     # gera e abre
    scripts/devolutiva.py --quieto < r.json    # sem abrir

Formato de entrada (o que a sessão escreve):
{
  "portoes": [
    {"n": 1, "project_id": 50, "frente": "Máquina de Originação",
     "pedido": "remarca o Richard pra 6ª 14/08",
     "entendi": "remarcar o café do Richard para sexta 14/08, mesma hora",
     "estado": "feito" | "precisa" | "parcial",
     "desfecho": "rascunho de WA pronto — revisa e dispara",
     "drafts": [{"titulo": "WA para Richard", "canal": "whatsapp",
                 "texto": "Richard, bom dia. ..."}],
     "notas": ["a agenda do dia 14 está livre às 15h"]}
  ]
}
"""
import html
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

SAIDA = os.path.expanduser("~/cockpit/devolutiva.html")
BRT = ZoneInfo("America/Sao_Paulo")
NUM = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"]

ESTADOS = {
    "feito":   ("✓", "ok",     "feito"),
    "parcial": ("◐", "parcial", "parcial"),
    "precisa": ("⚠", "warn",   "precisa de você"),
}


def esc(t):
    return html.escape(str(t) if t is not None else "")


CSS = """
:root{
  --charcoal:#2b2925; --ink:#3a362f; --muted:#8a8072;
  --creme:#f6f1e7; --card:#fffdf8; --line:#e5ddcd;
  --dourado:#b8973e; --dourado-soft:#e9dcb8;
  --ok:#4a6035; --ok-bg:#eef3e9;
  --warn:#c0492f; --warn-bg:#fae9e3;
  --parcial:#c69a2e; --parcial-bg:#f9f0d6;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--creme);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  padding:26px 30px 60px}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
  border-bottom:2px solid var(--dourado);padding-bottom:14px;margin-bottom:22px}
h1{font-size:22px;font-weight:700;color:var(--charcoal)}
h1 .dot{color:var(--dourado)}
.meta{margin-left:auto;font-size:13px;color:var(--muted)}
.meta b{color:var(--charcoal)}
.resumo{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px;font-size:13px}
.pill{border-radius:20px;padding:4px 13px;font-weight:600}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.parcial{background:var(--parcial-bg);color:#8a6a12}
.p{background:var(--card);border:1px solid var(--line);border-left-width:4px;
  border-radius:10px;padding:16px 18px;margin-bottom:16px;max-width:900px}
.p.ok{border-left-color:var(--ok)}
.p.warn{border-left-color:var(--warn)}
.p.parcial{border-left-color:var(--parcial)}
.p h2{font-size:16px;color:var(--charcoal);display:flex;align-items:baseline;gap:9px}
.p h2 .n{font-size:19px;color:var(--dourado)}
.p .frente{font-size:11.5px;color:var(--muted);margin:2px 0 0 30px}
.linha{margin:12px 0 0 30px;font-size:14px}
.linha .lbl{display:inline-block;min-width:78px;padding-right:10px;font-size:11px;
  text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700}
.pedido{color:var(--muted);font-style:italic}
.entendi{background:#f4efe4;border-radius:7px;padding:7px 11px;display:block;
  margin-top:5px;border-left:2px solid var(--dourado-soft)}
.desfecho{font-weight:600}
.desfecho.ok{color:var(--ok)} .desfecho.warn{color:var(--warn)}
.desfecho.parcial{color:#8a6a12}
.notas{margin:9px 0 0 30px;font-size:12.5px;color:var(--muted)}
.notas li{margin-left:16px}
.draft{margin:13px 0 0 30px;background:#fbf7ee;border:1px solid var(--line);
  border-radius:9px;overflow:hidden}
.draft-h{display:flex;align-items:center;gap:10px;padding:8px 12px;
  background:#f2ebdc;border-bottom:1px solid var(--line);font-size:12px}
.draft-h .t{font-weight:700;color:var(--charcoal)}
.draft-h .c{color:var(--muted);text-transform:uppercase;font-size:10.5px;letter-spacing:.6px}
.draft-h button{margin-left:auto;font:inherit;font-size:11.5px;font-weight:600;
  background:var(--dourado);color:#fff;border:0;border-radius:6px;
  padding:4px 12px;cursor:pointer}
.draft-h button:hover{background:#a38534}
.draft pre{padding:12px 14px;white-space:pre-wrap;word-wrap:break-word;
  font:13.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;
  color:var(--ink)}
footer{margin-top:26px;color:var(--muted);font-size:11.5px;max-width:900px;line-height:1.6}
"""

JS = """
document.querySelectorAll('[data-copy]').forEach(function(b){
  b.addEventListener('click', function(){
    var pre = document.getElementById(b.getAttribute('data-copy'));
    var txt = pre ? pre.textContent : '';
    var antes = b.textContent;
    function ok(){ b.textContent='copiado ✓'; setTimeout(function(){b.textContent=antes;},1600); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(ok, function(){ sel(pre,b,antes); });
    } else { sel(pre,b,antes); }
  });
});
function sel(pre,b,antes){
  var r=document.createRange(); r.selectNodeContents(pre);
  var s=getSelection(); s.removeAllRanges(); s.addRange(r);
  b.textContent='selecionado — ⌘C'; setTimeout(function(){b.textContent=antes;},2200);
}
"""


def render(d):
    agora = datetime.now(BRT)
    portoes = d.get("portoes") or []

    cont = {"feito": 0, "parcial": 0, "precisa": 0}
    for p in portoes:
        cont[p.get("estado", "feito")] = cont.get(p.get("estado", "feito"), 0) + 1

    pills = []
    for est, n in (("feito", cont["feito"]), ("parcial", cont["parcial"]),
                   ("precisa", cont["precisa"])):
        if n:
            ic, cls, rot = ESTADOS[est]
            pills.append(f'<span class="pill {cls}">{ic} {n} {esc(rot)}</span>')

    blocos, di = [], 0
    for i, p in enumerate(portoes):
        est = p.get("estado", "feito")
        ic, cls, _ = ESTADOS.get(est, ESTADOS["feito"])
        n = p.get("n", i + 1)
        num = NUM[n - 1] if 1 <= n <= len(NUM) else str(n)

        linhas = [f'<div class="p {cls}">',
                  f'<h2><span class="n">{num}</span>{esc(p.get("titulo") or p.get("frente") or "portão")}</h2>']
        sub = " · ".join(x for x in [
            f'#{p["project_id"]}' if p.get("project_id") else "",
            esc(p.get("frente") or "")] if x)
        if sub:
            linhas.append(f'<div class="frente">{sub}</div>')

        if p.get("pedido"):
            linhas.append(f'<div class="linha"><span class="lbl">pediu</span>'
                          f'<span class="pedido">{esc(p["pedido"])}</span></div>')
        if p.get("entendi"):
            linhas.append(f'<div class="linha"><span class="lbl">entendi</span>'
                          f'<span class="entendi">{esc(p["entendi"])}</span></div>')
        linhas.append(f'<div class="linha"><span class="lbl">resultado</span>'
                      f'<span class="desfecho {cls}">{ic} {esc(p.get("desfecho") or "—")}</span></div>')

        if p.get("notas"):
            linhas.append('<ul class="notas">' +
                          "".join(f"<li>{esc(x)}</li>" for x in p["notas"]) + "</ul>")

        for dr in (p.get("drafts") or []):
            di += 1
            pid = f"d{di}"
            linhas.append(
                f'<div class="draft"><div class="draft-h">'
                f'<span class="t">{esc(dr.get("titulo") or "rascunho")}</span>'
                f'<span class="c">{esc(dr.get("canal") or "")}</span>'
                f'<button type="button" data-copy="{pid}">copiar</button></div>'
                f'<pre id="{pid}">{esc(dr.get("texto") or "")}</pre></div>')

        linhas.append("</div>")
        blocos.append("\n".join(linhas))

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Devolutiva dos portões — {agora:%d/%m %H:%M}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Devolutiva <span class="dot">·</span> o que fiz com cada portão</h1>
  <div class="meta">{len(portoes)} portões · <b>{agora:%d/%m %H:%M}</b></div>
</header>

<div class="resumo">{''.join(pills) or '<span class="pill">nada a reportar</span>'}</div>

{chr(10).join(blocos)}

<footer>
  A numeração ① ② ③ é a mesma da tela da tonIAH e da ordem em que você pediu.
  <b>Rascunho aqui é rascunho</b> — nada foi enviado; o disparo é seu, com o botão
  de copiar de cada bloco. Se a linha <b>entendi</b> não bate com o que você quis
  dizer, é ali que a correção sai barata: me diga o número do portão e o que
  mudar.
</footer>
<script>{JS}</script>
</body>
</html>"""


def main():
    bruto = sys.stdin.read()
    try:
        d = json.loads(bruto)
    except Exception as e:
        print(f"✗ JSON inválido: {e}", file=sys.stderr)
        return 1
    open(SAIDA, "w").write(render(d))
    n = len(d.get("portoes") or [])
    print(f"→ devolutiva: open {SAIDA}  ({n} portões)")
    if "--quieto" not in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from cockpit import abrir      # reusa a aba, não multiplica janela
            abrir(SAIDA)
        except Exception:
            subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
