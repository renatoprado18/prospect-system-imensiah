"""Template da fila de fatos incertos. Mesmos tokens do ~/cockpit do Renato."""

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
const CHAVE="fatos-incertos";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".portao").forEach(p=>{
    const e=salvo[p.dataset.uid], v=e&&e.v;
    p.dataset.done=v?"1":"";
    p.dataset.v=v||"";
    const inp=p.querySelector(".fix");
    if(inp&&e&&e.txt!==undefined&&inp.value!==e.txt) inp.value=e.txt;
    p.querySelectorAll(".veredito button").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.v===v)));
    if(v)n++;
  });
  document.getElementById("feitos").textContent=n;
}
document.querySelectorAll(".veredito button").forEach(b=>{
  b.addEventListener("click",()=>{
    const card=b.closest(".portao"), uid=card.dataset.uid;
    const atual=salvo[uid];
    if(atual&&atual.v===b.dataset.v) delete salvo[uid];
    else salvo[uid]={v:b.dataset.v, txt:(atual&&atual.txt)||""};
    localStorage.setItem(CHAVE,JSON.stringify(salvo)); pintar();
    if(salvo[uid]&&salvo[uid].v==="corrige") card.querySelector(".fix").focus();
  });
});
document.querySelectorAll(".fix").forEach(inp=>{
  inp.addEventListener("input",()=>{
    const uid=inp.closest(".portao").dataset.uid;
    if(!salvo[uid]) salvo[uid]={v:"corrige",txt:""};
    salvo[uid].txt=inp.value;
    localStorage.setItem(CHAVE,JSON.stringify(salvo));
  });
});
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("saida").style.display="none"; pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=[]; let c=0;
  document.querySelectorAll(".portao").forEach(p=>{
    const e=salvo[p.dataset.uid]; if(!e||!e.v) return;
    if(e.v==="corrige"&&!(e.txt||"").trim()) return;   // correção sem texto não vale
    c++;
    L.push("#"+p.dataset.uid+" — "+e.v.toUpperCase()+(e.v==="corrige"?": "+e.txt.trim():""));
  });
  const txt=L.join("\n"), btn=document.getElementById("copiar"), out=document.getElementById("saida");
  out.value=txt; out.style.display="block";
  try{ await navigator.clipboard.writeText(txt); btn.textContent="Copiado ("+c+")"; }
  catch(e){ btn.textContent="Copie abaixo"; out.select(); }
  setTimeout(()=>btn.textContent="Copiar resultado",1800);
});
pintar();
"""


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(itens: list) -> str:
    cards = []
    for f in itens:
        ev = ""
        if f.get("evidencia"):
            linhas = "".join(
                f'<li><b>{_esc(e["termo"])}</b> — {_esc(e["texto"])[:150]}</li>'
                for e in f["evidencia"][:4])
            ev = f'<div class="ctx"><span class="ctx-t">o que encontrei no INTEL</span><ul>{linhas}</ul></div>'
        else:
            ev = '<div class="ctx"><span class="ctx-t">o que encontrei no INTEL</span>' \
                 '<p class="nada">nada — nenhuma ficha nem menção que confirme ou refute</p></div>'
        cards.append(
            f'<article class="portao p--nada" data-uid="{f["id"]}">'
            f'<header><span class="frente">{_esc(f["contato"])}</span>'
            f'<span class="lat">{_esc(f["categoria"])} · conf {f["confianca"]:.2f}</span></header>'
            f'<p>{_esc(f["fato"])}</p>{ev}'
            f'<div class="veredito">'
            f'<button type="button" data-v="confirma">Confirma</button>'
            f'<button type="button" data-v="corrige">Corrige</button>'
            f'<button type="button" data-v="descarta">Descarta</button>'
            f'</div>'
            f'<input class="fix" type="text" placeholder="como é de verdade — vira o fato novo">'
            f'</article>')

    n = len(itens)
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fatos incertos — precisam de você</title>
<style>{CSS}
.ctx {{ margin:.15rem 0 .8rem; padding:.55rem .7rem; background:var(--paper);
  border:1px solid var(--panel-edge); border-radius:3px; }}
.ctx-t {{ font-size:.62rem; letter-spacing:.12em; text-transform:uppercase; color:var(--brass); }}
.ctx ul {{ margin:.35rem 0 0; padding-left:1.1rem; font-size:.82rem; color:var(--ink-soft); }}
.ctx li {{ margin-bottom:.2rem; }}
.ctx .nada {{ margin:.3rem 0 0; font-size:.82rem; color:var(--ink-faint); font-style:italic; }}
.fix {{ display:none; width:100%; margin-top:.5rem; padding:.42rem .6rem; font:inherit;
  font-size:.84rem; border:1px solid var(--brass-soft); border-radius:2px;
  background:var(--paper); color:var(--ink); }}
.fix:focus-visible {{ outline:2px solid var(--brass); outline-offset:1px; }}
.portao[data-v="corrige"] .fix {{ display:block; }}
</style></head><body>
<div class="wrap">
  <p class="eyebrow">Enriquecimento · hipóteses não resolvidas</p>
  <h1>Fatos que precisam de você</h1>
  <div class="filete"></div>
  <p class="intro">{n} hipóteses que o banco <b>não conseguiu</b> confirmar sozinho.
  As que ele resolveu já foram aplicadas e não aparecem aqui. Cada uma vem com o
  que a busca encontrou — se não encontrou nada, está dito.</p>
  {"".join(cards)}
  <div class="barra">
    <span class="progresso"><b id="feitos">0</b> de <b>{n}</b> julgados</span>
    <div class="acoes">
      <button type="button" class="ghost" id="limpar">Limpar</button>
      <button type="button" id="copiar">Copiar resultado</button>
    </div>
  </div>
  <textarea id="saida" rows="6" readonly></textarea>
</div>
<script>{{JS}}</script></body></html>""".replace("{JS}", JS)
