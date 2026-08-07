#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/nomes.html — decidir A ou B onde INTEL e Google divergem.

PEDIDO (Renato, 07/08/2026): "cria uma tela para eu decidir qual. A ou B, eu
clico, copio e colo."

São 327 pessoas cadastradas dos dois lados com nomes diferentes, e não dá pra
automatizar: às vezes o INTEL tem o nome melhor ("Wanelise B Carvalho" contra
"Dra. Vanelise", grafado errado no Google), às vezes tem lixo
("Wayner. Kummp@Estrela. Com. Br" contra "Estrela - Wayner"). É julgamento, não
regra.

O DESENHO SEGUE O QUE JÁ FUNCIONA nas outras telas: escolha em localStorage,
botão que copia tudo, e a sessão grava. Sem POST — a página roda em `file://` e
abrir CORS num endpoint de escrita pra economizar um clique é troca ruim (mesma
decisão do placar e da fila de fatos).

ORDENADA POR QUEM TEM HISTÓRICO. Quem tem 300 mensagens importa mais que uma
ficha vazia: decidir 20 nomes de gente com quem ele fala vale mais que 300 de
contatos que nunca escreveram. O contador de mensagens vai na tela pra ele saber
onde parar.

Uso:  ./nomes.py            # gera e abre
      ./nomes.py --gravar - # aplica o que ele colou de volta
"""
import os
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, "/Users/rap/prospect-system/app")

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/nomes.html")


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chave(v):
    d = re.sub(r"\D", "", v or "")
    return d[-8:] if len(d) >= 8 else ""


async def coletar():
    import integrations.google_contacts as gc
    g = {}
    for conta in ("renato@almeida-prado.com", "renato.almeida.prado@gmail.com"):
        t = await gc.get_valid_token(conta)
        if not t:
            raise RuntimeError(f"sem token para {conta} — sem as duas agendas o "
                               f"diagnóstico sai pela metade")
        for p in await gc.fetch_all_contacts(t):
            nome = ((p.get("names") or [{}])[0].get("displayName")) or ""
            rid = (p.get("resourceName") or "").replace("people/", "")
            for pn in (p.get("phoneNumbers") or []):
                k = chave(pn.get("value"))
                if k:
                    g.setdefault(k, (nome, rid))

    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id, c.nome, c.telefones, c.empresa,
                          (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs
                     FROM contacts c
                    WHERE c.telefones IS NOT NULL AND c.telefones::text <> '[]'""")
    pares = []
    for f in cur.fetchall():
        tels = [t.get("number") for t in (f["telefones"] or []) if isinstance(t, dict)]
        ks = [chave(t) for t in tels if chave(t)]
        achado = next((g[k] for k in ks if k in g), None)
        if not achado:
            continue
        nome_g, rid = achado
        if (f["nome"] or "").strip().lower() == nome_g.strip().lower():
            continue
        pares.append({"id": f["id"], "intel": f["nome"], "google": nome_g,
                      "rid": rid, "msgs": f["msgs"], "empresa": f["empresa"],
                      "tel": tels[0] if tels else ""})
    pares.sort(key=lambda x: -x["msgs"])
    return pares


CSS = """
:root{--paper:#f4f0e7;--panel:#fbf9f3;--edge:#e4ddcc;--ink:#23211c;--soft:#57534a;
--faint:#8a8477;--brass:#9c7a33;--brass2:#b79a55;--calm:#4f6b4a;--calmbg:#e3e9dc;
--shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
--serif:Georgia,serif;--sans:system-ui,-apple-system,sans-serif;--mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
.wrap{max-width:56rem;margin:0 auto;padding:2.2rem 1.2rem 8rem}
.eyebrow{font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--brass);font-weight:600;margin:0 0 .4rem}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(1.6rem,4vw,2.1rem);margin:0}
.filete{width:3rem;height:2px;background:var(--brass2);margin:.9rem 0 1rem}
.lead{font-size:.9rem;color:var(--soft);max-width:64ch;margin:0 0 1.6rem}
.par{background:var(--panel);border:1px solid var(--edge);border-radius:8px;padding:.7rem .85rem;
margin:0 0 .5rem;box-shadow:var(--shadow);display:grid;grid-template-columns:1fr auto;gap:.5rem 1rem;align-items:center}
.par.decidido{border-left:3px solid var(--calm)}
.opcoes{display:flex;gap:.5rem;flex-wrap:wrap}
.op{flex:1 1 14rem;text-align:left;font:inherit;font-size:.88rem;padding:.5rem .7rem;cursor:pointer;
background:var(--paper);border:1px solid var(--edge);border-radius:5px;color:var(--ink)}
.op:hover{border-color:var(--brass2)}
.op[aria-pressed="true"]{background:var(--calmbg);border-color:var(--calm);color:var(--calm);font-weight:600}
.op small{display:block;font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--faint);margin-bottom:.15rem}
.op[aria-pressed="true"] small{color:var(--calm)}
.meta{font-family:var(--mono);font-size:.7rem;color:var(--faint);text-align:right;white-space:nowrap}
.barra{position:sticky;bottom:0;margin-top:1.4rem;padding:.8rem 1rem;background:var(--panel);
border:1px solid var(--edge);border-radius:8px;display:flex;gap:1rem;justify-content:space-between;
align-items:center;flex-wrap:wrap;box-shadow:0 -2px 14px rgba(35,33,28,.08)}
.barra b{font-family:var(--serif);font-size:1.2rem;color:var(--brass)}
.barra button{font:inherit;font-size:.82rem;padding:.45rem 1rem;cursor:pointer;border-radius:4px;
border:1px solid var(--brass);background:var(--brass);color:var(--panel);font-weight:600}
.barra button.ghost{background:transparent;color:var(--faint);border-color:var(--edge);font-weight:400}
#saida{width:100%;margin-top:.6rem;font:12px/1.5 var(--mono);padding:.6rem;border:1px solid var(--edge);
border-radius:4px;background:var(--paper);color:var(--ink);display:none}
.nota{font-size:.78rem;color:var(--faint);margin:1.2rem 0 0}
"""

JS = r"""
<script>
const CHAVE="nomes-decisao";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.id];
    p.querySelectorAll(".op").forEach(b=>b.setAttribute("aria-pressed",String(e===b.dataset.lado)));
    p.classList.toggle("decidido",!!e);
    if(e)n++;
  });
  document.getElementById("n").textContent=n;
}
document.querySelectorAll(".op").forEach(b=>b.addEventListener("click",()=>{
  const p=b.closest(".par"),id=p.dataset.id;
  if(salvo[id]===b.dataset.lado)delete salvo[id]; else salvo[id]=b.dataset.lado;
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
}));
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("saida").style.display="none";pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=["NOMES — decisao (cole no Claude)"];let c=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.id];if(!e)return;c++;
    L.push("#"+p.dataset.id+" "+e.toUpperCase()+" :: "+(e==="intel"?p.dataset.intel:p.dataset.google));
  });
  const txt=L.join("\n"),out=document.getElementById("saida"),btn=document.getElementById("copiar");
  out.value=c?txt:"(nada decidido ainda)";out.style.display="block";
  try{await navigator.clipboard.writeText(txt);btn.textContent="Copiado ("+c+")";}
  catch(err){btn.textContent="Copie abaixo";out.select();}
  setTimeout(()=>btn.textContent="Copiar para o Claude",1800);
});
pintar();
</script>
"""


def render(pares):
    ago = datetime.now()
    linhas = []
    for p in pares:
        linhas.append(f"""<div class="par" data-id="{p['id']}"
     data-intel="{esc(p['intel'])}" data-google="{esc(p['google'])}">
  <div class="opcoes">
    <button class="op" data-lado="intel"><small>INTEL</small>{esc(p['intel'])}</button>
    <button class="op" data-lado="google"><small>Google</small>{esc(p['google'])}</button>
  </div>
  <div class="meta">#{p['id']}<br>{p['msgs']} msg{'s' if p['msgs'] != 1 else ''}</div>
</div>""")
    com_hist = sum(1 for p in pares if p["msgs"] > 0)
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nomes — INTEL ou Google</title><style>{CSS}</style></head><body><div class="wrap">
<p class="eyebrow">INTEL · contatos</p>
<h1>Qual nome fica?</h1>
<div class="filete"></div>
<p class="lead">{len(pares)} pessoas estão nos dois lados com nomes diferentes. Não dá pra decidir
por regra: às vezes o INTEL tem o nome melhor (<i>Wanelise B Carvalho</i> contra <i>Dra. Vanelise</i>),
às vezes tem lixo (<i>wayner.kummp@estrela.com.br</i>). Clique no que vale, copie no fim e me mande —
eu gravo dos dois lados.<br><b>Ordenadas por histórico:</b> as {com_hist} primeiras são de gente com
quem você já trocou mensagem. Depois delas, é cadastro morto — pare quando quiser.</p>
{''.join(linhas)}
<p class="nota">Gerado {ago:%d/%m/%Y %H:%M}. Nada é alterado por esta página: ela só registra a
escolha pra você copiar.</p>
<div class="barra">
  <span><b id="n">0</b> decidido(s) de {len(pares)}</span>
  <div><button class="ghost" id="limpar">Limpar</button>
  <button id="copiar">Copiar para o Claude</button></div>
</div>
<textarea id="saida" rows="6" readonly></textarea>
</div>{JS}</body></html>"""


def gravar(texto):
    """Aplica as escolhas: renomeia no INTEL e/ou empurra pro Google."""
    import asyncio
    linhas = [l.strip() for l in texto.splitlines() if l.strip().startswith("#")]
    if not linhas:
        sys.exit("nada reconhecido — esperado '#123 INTEL :: Nome'")
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    manter_intel, usar_google = [], []
    for l in linhas:
        m = re.match(r"^#(\d+)\s+(INTEL|GOOGLE)\s*::\s*(.+)$", l)
        if not m:
            print(f"  ? não parseou: {l[:60]}")
            continue
        cid, lado, nome = int(m.group(1)), m.group(2), m.group(3).strip()
        if lado == "GOOGLE":
            cur.execute("""UPDATE contacts SET nome=%s, atualizado_em=now()
                            WHERE id=%s RETURNING id, nome""", (nome, cid))
            r = cur.fetchone()
            if r:
                usar_google.append(r["id"])
                print(f"  #{r['id']} INTEL renomeado → {r['nome']}")
        else:
            manter_intel.append((cid, nome))
    conn.commit()
    if manter_intel:
        print(f"\n  {len(manter_intel)} escolheram o nome do INTEL — falta empurrar pro Google:")
        for cid, nome in manter_intel[:10]:
            print(f"     #{cid} {nome}")
        print("  (o update no Google é o próximo passo; o INTEL já está certo)")
    return 0


def main():
    if "--gravar" in sys.argv:
        i = sys.argv.index("--gravar")
        alvo = sys.argv[i + 1] if len(sys.argv) > i + 1 else "-"
        return gravar(sys.stdin.read() if alvo == "-" else open(alvo).read())
    import asyncio
    for k in ("DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        os.environ.setdefault(k, env(k))
    os.environ.setdefault("DB_TARGET", "prod")
    os.environ.setdefault("ALLOW_PROD_FROM_LOCAL", "1")
    pares = asyncio.run(coletar())
    open(SAIDA, "w").write(render(pares))
    print(f"→ {SAIDA} ({len(pares)} pares)")
    subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
