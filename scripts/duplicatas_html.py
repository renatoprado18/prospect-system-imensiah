"""Template da revisao de duplicatas. Tokens do ~/cockpit do Renato."""

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
body { margin:0; background:var(--paper); color:var(--ink); font:16px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased; }
.wrap { max-width:50rem; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
.eyebrow { font-size:.67rem; letter-spacing:.2em; text-transform:uppercase;
  color:var(--brass); font-weight:600; margin:0 0 .5rem; }
h1 { font-family:var(--serif); font-weight:400; font-size:clamp(1.6rem,4vw,2.2rem);
  line-height:1.2; margin:0; text-wrap:balance; }
.filete { width:3rem; height:2px; background:var(--brass-soft); margin:1rem 0 1.2rem; }
.intro { color:var(--ink-soft); max-width:37rem; margin:0 0 .5rem; font-size:.93rem; }
.intro b { color:var(--ink); }
.resumo { display:grid; grid-template-columns:repeat(auto-fit,minmax(6.5rem,1fr));
  gap:.55rem; margin:1.8rem 0 .7rem; }
.tile { background:var(--panel); border:1px solid var(--panel-edge); border-radius:4px;
  padding:.8rem .85rem; box-shadow:var(--shadow); border-top:2px solid var(--t); }
.tile b { display:block; font-family:var(--mono); font-size:1.5rem; line-height:1.1;
  font-variant-numeric:tabular-nums; color:var(--t); }
.tile span { font-size:.68rem; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-faint); }
.nota { font-size:.84rem; color:var(--ink-soft); margin:0 0 2.2rem; }
.dia { margin:0 0 2rem; }
.dia-cab { display:flex; align-items:baseline; gap:.65rem; margin:0 0 .8rem;
  padding-bottom:.4rem; border-bottom:1px solid var(--panel-edge); }
.dia-cab h2 { font-family:var(--mono); font-size:.9rem; font-weight:600; margin:0;
  font-variant-numeric:tabular-nums; }
.quente { font-size:.62rem; letter-spacing:.12em; text-transform:uppercase; color:var(--brass);
  border:1px solid var(--brass-soft); border-radius:2px; padding:.1rem .42rem; }
.portao { background:var(--panel); border:1px solid var(--panel-edge); border-radius:4px;
  padding:.95rem 1.05rem; margin:0 0 .65rem; box-shadow:var(--shadow);
  border-left:3px solid var(--c); }
.portao[data-done="1"] { opacity:.6; }
.p--calm { --c:var(--calm); } .p--warn { --c:var(--warn); } .p--nada { --c:var(--ink-faint); }
.portao > header { display:flex; gap:.7rem; align-items:baseline; justify-content:space-between;
  flex-wrap:wrap; margin-bottom:.4rem; }
.frente { font-weight:600; font-size:.93rem; }
.lat { font-family:var(--mono); font-size:.71rem; white-space:nowrap; color:var(--c); }
.portao p { margin:0 0 .75rem; font-size:.89rem; color:var(--ink-soft); }
.veredito { display:flex; gap:.38rem; flex-wrap:wrap; }
.veredito button { font:inherit; font-size:.77rem; padding:.32rem .78rem; cursor:pointer;
  background:transparent; color:var(--ink-faint); border:1px solid var(--panel-edge);
  border-radius:2px; transition:all .13s; }
.veredito button:hover { border-color:var(--brass-soft); color:var(--ink); }
.veredito button:focus-visible { outline:2px solid var(--brass); outline-offset:2px; }
.veredito button[aria-pressed="true"] { background:var(--brass); border-color:var(--brass);
  color:var(--panel); font-weight:600; }
.barra { position:sticky; bottom:0; margin-top:2.2rem; padding:.85rem 1.05rem;
  background:var(--panel); border:1px solid var(--panel-edge); border-radius:4px;
  display:flex; gap:1rem; align-items:center; justify-content:space-between; flex-wrap:wrap;
  box-shadow:0 -2px 14px rgba(35,33,28,.08); }
.progresso { font-size:.82rem; color:var(--ink-soft); }
.progresso b { color:var(--ink); font-family:var(--mono); font-variant-numeric:tabular-nums; }
.acoes { display:flex; gap:.45rem; }
.acoes button { font:inherit; font-size:.79rem; padding:.42rem .95rem; cursor:pointer;
  border-radius:2px; border:1px solid var(--brass); background:var(--brass);
  color:var(--panel); font-weight:600; transition:all .13s; }
.acoes button.ghost { background:transparent; color:var(--ink-faint);
  border-color:var(--panel-edge); font-weight:400; }
.acoes button:hover { background:var(--ink); border-color:var(--ink); }
.acoes button.ghost:hover { background:transparent; border-color:var(--brass); color:var(--ink); }
.acoes button:focus-visible { outline:2px solid var(--brass); outline-offset:2px; }
#saida { width:100%; margin-top:.7rem; font:12px/1.5 var(--mono); padding:.7rem;
  border:1px solid var(--panel-edge); border-radius:3px; background:var(--paper);
  color:var(--ink); display:none; }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
"""

JS = r"""
const CHAVE="duplicatas";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".grupo").forEach(g=>{
    const v=salvo[g.dataset.uid];
    g.dataset.done=v?"1":"";
    g.querySelectorAll("button[data-v]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.v===v)));
    if(v)n++;
  });
  document.getElementById("feitos").textContent=n;
}
document.querySelectorAll("button[data-v]").forEach(b=>{
  b.addEventListener("click",()=>{
    const g=b.closest(".grupo"), uid=g.dataset.uid;
    if(salvo[uid]===b.dataset.v) delete salvo[uid]; else salvo[uid]=b.dataset.v;
    localStorage.setItem(CHAVE,JSON.stringify(salvo)); pintar();
  });
});
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("saida").style.display="none"; pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=[]; let c=0;
  document.querySelectorAll(".grupo").forEach(g=>{
    const v=salvo[g.dataset.uid]; if(!v) return; c++;
    L.push(v==="nao" ? ("NAO "+g.dataset.uid) : ("MERGE "+g.dataset.uid+" -> manter #"+v));
  });
  const txt=L.join("\n"), btn=document.getElementById("copiar"), out=document.getElementById("saida");
  out.value=txt; out.style.display="block";
  try{ await navigator.clipboard.writeText(txt); btn.textContent="Copiado ("+c+")"; }
  catch(e){ btn.textContent="Copie abaixo"; out.select(); }
  setTimeout(()=>btn.textContent="Copiar resultado",1800);
});
pintar();
"""

EXTRA_CSS = """
.motivos { font-family:var(--mono); font-size:.73rem; color:var(--brass); margin:0 0 .6rem; }
table { width:100%; border-collapse:collapse; font-size:.83rem; margin-bottom:.7rem; }
th { text-align:left; font-size:.63rem; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink-faint); font-weight:600; padding:.2rem .4rem; border-bottom:1px solid var(--panel-edge); }
td { padding:.28rem .4rem; border-bottom:1px solid var(--panel-edge); }
td.id { font-family:var(--mono); font-size:.76rem; color:var(--ink-faint); }
td.n { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
.sub { display:block; font-size:.72rem; color:var(--ink-faint); }
.g--forte { --c:var(--crit); }
.g--fraco { --c:var(--ink-faint); }
.grupo { background:var(--panel); border:1px solid var(--panel-edge); border-radius:4px;
  padding:.95rem 1.05rem; margin:0 0 .65rem; box-shadow:var(--shadow); border-left:3px solid var(--c); }
.grupo[data-done="1"] { opacity:.6; }
.grupo > header { display:flex; gap:.7rem; align-items:baseline; justify-content:space-between;
  flex-wrap:wrap; margin-bottom:.35rem; }
.veredito button { font:inherit; font-size:.77rem; padding:.32rem .7rem; cursor:pointer;
  background:transparent; color:var(--ink-faint); border:1px solid var(--panel-edge);
  border-radius:2px; margin-right:.35rem; }
.veredito button:hover { border-color:var(--brass-soft); color:var(--ink); }
.veredito button[aria-pressed="true"] { background:var(--brass); border-color:var(--brass);
  color:var(--panel); font-weight:600; }
"""


def _esc(t):
    return (str(t) if t is not None else "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def render(grupos: list) -> str:
    cards = []
    for g in grupos:
        fichas = sorted(g["fichas"].values(), key=lambda f: (-f["msgs"], -f["fatos"], f["id"]))
        uid = "-".join(str(f["id"]) for f in fichas)
        forte = g["forca"] >= 3
        sel = "".join(
            '<button type="button" data-v="%d">manter #%d</button>' % (f["id"], f["id"])
            for f in fichas)
        linhas = "".join(
            '<tr><td class="id">#%d</td><td>%s%s</td><td class="n">%d</td>'
            '<td class="n">%d</td><td class="n">%d</td><td class="n">%s</td></tr>' % (
                f["id"], _esc(f["nome"])[:38],
                ('<span class="sub">%s</span>' % _esc(f["empresa"])[:26]) if f["empresa"] else "",
                f["msgs"], f["fatos"], f["tasks"], _esc(f["circulo"] or "-"))
            for f in fichas)
        cards.append(
            '<article class="grupo %s" data-uid="%s">'
            '<header><span class="frente">%s</span><span class="lat">%s</span></header>'
            '<p class="motivos">%s</p>'
            '<table><thead><tr><th></th><th>ficha</th><th>msgs</th><th>fatos</th>'
            '<th>tasks</th><th>circ</th></tr></thead><tbody>%s</tbody></table>'
            '<div class="veredito">%s<button type="button" data-v="nao">nao sao a mesma</button></div>'
            '</article>' % (
                "g--forte" if forte else "g--fraco", uid, _esc(fichas[0]["nome"])[:44],
                "prova forte" if forte else "so o nome",
                _esc(" · ".join(sorted(set(g["motivos"]))))[:150], linhas, sel))

    n = len(grupos)
    fortes = sum(1 for g in grupos if g["forca"] >= 3)
    corpo = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fichas duplicadas</title>
<style>@@CSSFULL@@</style></head><body>
<div class="wrap">
  <p class="eyebrow">Higiene de contatos</p>
  <h1>Fichas duplicadas</h1>
  <div class="filete"></div>
  <p class="intro">@@N@@ grupos suspeitos &middot; <b>@@F@@ com prova forte</b> (mesmo telefone
  ou e-mail). Os outros casam so pelo nome &mdash; homonimo existe, ainda mais em familia.</p>
  <p class="intro">Duplicata <b>espalha o historico</b>: metade das mensagens numa ficha, metade
  na outra. Health score, "ultima interacao" e o portao da camada enxergam menos do que existe.
  Escolhe a ficha que fica &mdash; normalmente a de mais mensagens.</p>
  @@CARDS@@
  <div class="barra">
    <span class="progresso"><b id="feitos">0</b> de <b>@@N@@</b> decididos</span>
    <div class="acoes">
      <button type="button" class="ghost" id="limpar">Limpar</button>
      <button type="button" id="copiar">Copiar resultado</button>
    </div>
  </div>
  <textarea id="saida" rows="6" readonly></textarea>
</div>
<script>@@JS@@</script></body></html>"""
    return (corpo.replace("@@CSSFULL@@", CSS + EXTRA_CSS)
                 .replace("@@CARDS@@", "".join(cards))
                 .replace("@@N@@", str(n)).replace("@@F@@", str(fortes))
                 .replace("@@JS@@", JS))
