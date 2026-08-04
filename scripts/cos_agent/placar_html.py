"""Template do placar. Separado de placar.py: HTML e apresentacao, nao logica.

O CSS sai do `~/cockpit/index.html` do Renato — mesmos tokens, tema CLARO
(ele preferiu explicitamente ao dark). Ver reference_cockpit_local_page.
"""

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

JS = """
const CHAVE="placar-cos-"+document.body.dataset.chave;
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".portao").forEach(p=>{
    const v=salvo[p.dataset.uid];
    p.dataset.done=v?"1":"";
    p.querySelectorAll(".veredito button").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.v===v)));
    if(v)n++;
  });
  document.getElementById("feitos").textContent=n;
}
document.querySelectorAll(".veredito button").forEach(b=>{
  b.addEventListener("click",()=>{
    const uid=b.closest(".portao").dataset.uid;
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
  const L=["PLACAR CAMADA CoS — 30/07 a 04/08"], c={certa:0,errada:0,passou:0};
  document.querySelectorAll(".dia").forEach(sec=>{
    const dia=sec.querySelector("h2").textContent;
    sec.querySelectorAll(".portao").forEach(p=>{
      const v=salvo[p.dataset.uid]; if(!v) return; c[v]++;
      const rot={certa:"CERTA",errada:"COBROU A TOA",passou:"DEIXOU PASSAR"}[v];
      L.push(dia+" · "+p.querySelector(".frente").textContent+" — "+rot);
    });
  });
  L.push("", "certas "+c.certa+" · cobrou a toa "+c.errada+" · deixou passar "+c.passou);
  const txt=L.join("\n"), btn=document.getElementById("copiar"), out=document.getElementById("saida");
  out.value=txt; out.style.display="block";
  try{ await navigator.clipboard.writeText(txt); btn.textContent="Copiado"; }
  catch(e){ btn.textContent="Copie abaixo"; out.select(); }
  setTimeout(()=>btn.textContent="Copiar resultado",1800);
});
pintar();
"""


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rotulo(l):
    if l is None: return ("sem ação", "nada")
    if l == 0:    return ("agiu no dia", "calm")
    if l <= 2:    return (f"agiu em {l}d", "calm")
    return (f"agiu em {l}d", "warn")


def render(pend: list[dict]) -> str:
    por_dia = {}
    for d in pend:
        por_dia.setdefault(d["dia"], []).append(d)

    hoje = max(por_dia) if por_dia else ""
    secoes = []
    for dia in sorted(por_dia):
        quente = dia >= hoje
        cab = f'<h2>{dia[8:10]}/{dia[5:7]}</h2>'
        if quente:
            cab += '<span class="quente">ainda quente</span>'
        itens = []
        for i, d in enumerate(por_dia[dia]):
            txt, cls = _rotulo(d["latencia"])
            itens.append(
                f'<article class="portao p--{cls}" data-uid="{dia}-{i}">'
                f'<header><span class="frente">{_esc(d["frente"])}</span>'
                f'<span class="lat">{txt}</span></header>'
                f'<p>{_esc(d["portao"])}</p>'
                f'<div class="veredito">'
                f'<button type="button" data-v="certa">Certa</button>'
                f'<button type="button" data-v="errada">Cobrou à toa</button>'
                f'<button type="button" data-v="passou">Deixou passar</button>'
                f'</div></article>')
        secoes.append(f'<section class="dia"><div class="dia-cab">{cab}</div>'
                      + "".join(itens) + "</section>")

    chave = f'{min(por_dia)}-{max(por_dia)}' if por_dia else 'vazio'
    n = len(pend)
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Placar da camada CoS</title>
<style>{CSS}</style></head><body data-chave="{chave}">
<div class="wrap">
  <p class="eyebrow">Camada CoS · gate do caminho A</p>
  <h1>Placar de qualidade</h1>
  <div class="filete"></div>
  <p class="intro">{n} portões ainda sem veredito. A latência ao lado é medida
  (task concluída ou WhatsApp enviado na frente) e serve de <b>contexto</b> —
  não prevê a resposta certa: medido em 04/08, portão certo aparece tanto com
  ação quanto sem.</p>
  {"".join(secoes)}
  <div class="barra">
    <span class="progresso"><b id="feitos">0</b> de <b>{n}</b> julgados</span>
    <div class="acoes">
      <button type="button" class="ghost" id="limpar">Limpar</button>
      <button type="button" id="copiar">Copiar resultado</button>
    </div>
  </div>
  <textarea id="saida" rows="8" readonly></textarea>
</div>
<script>{JS}</script></body></html>"""
