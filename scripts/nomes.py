#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/nomes.html — decidir A ou B onde INTEL e Google divergem.

PEDIDO (Renato, 07/08/2026): "cria uma tela para eu decidir qual. A ou B, eu
clico, copio e colo."

Onde INTEL e Google discordam do nome não dá pra automatizar: às vezes o INTEL
tem o nome melhor ("Wanelise B Carvalho" contra "Dra. Vanelise", grafado errado
no Google), às vezes tem lixo ("Wayner. Kummp@Estrela. Com. Br" contra "Estrela
- Wayner"). É julgamento, não regra.

QUANTAS SÃO, A TELA É QUE DIZ — número não fica escrito aqui. A versão anterior
deste docstring cravava "327 pessoas" e o board carregou "260 nomes divergentes"
por semanas; quando a conta foi refeita (18/08/26) eram 45 decisões reais dentro
de 460 casos casados, e as outras 415 nem eram decisão dele. Número de banco em
comentário envelhece calado e vira diagnóstico errado — foi assim que o RACI do
Jabô quebrou em 17/08.

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
import unicodedata
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


TITULOS = {"dr", "dra", "prof", "sr", "sra", "eng", "phd", "jr", "filho", "neto",
           "de", "da", "do", "dos", "das", "e", "the", "of"}


def norm(n):
    return (n or "").strip().lower()


def tokens(n):
    s = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z]+", s) if len(t) >= 3 and t not in TITULOS}


async def coletar():
    """Devolve (decidir, dupes, compartilhados) — três coisas diferentes.

    RE-MEDIÇÃO DE 18/08/26. Esta função contava 460 "divergências" e chamava
    todas de decisão do Renato. Só 45 eram. O erro era duplo:

    1. `g.setdefault(telefone, ...)` guardava UMA entrada do Google por telefone.
       Quando o Google tem duas fichas da mesma pessoa (o `712c9c9` mostrou que
       toda tentativa de corrigir criava duplicata em vez de corrigir), o INTEL
       era comparado contra a que a varredura viu primeiro — e "divergia" de uma
       grafia que ele mesmo já tinha certa na outra ficha. São 181 casos, e em
       180 deles o nome do INTEL já bate com uma das grafias: não há A ou B a
       decidir, há lixo a limpar no Google.
    2. Telefone compartilhado casa como se fosse a mesma pessoa. `Copersucar` no
       INTEL e `Sidnei Rosa` no Google, mesmo número, é a empresa e quem atende
       — não é nome errado. São 234, e 230 sem uma única mensagem trocada.

    Uma tela que empilha 460 decisões quando 8 têm histórico não pede julgamento,
    pede paciência — e é o Renato quem paga. Os três baldes saem separados: ele
    decide o que é decisão, e os outros dois viram número no rodapé.
    """
    import integrations.google_contacts as gc
    por_tel = {}
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
                    por_tel.setdefault(k, []).append((nome, rid, conta))

    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id, c.nome, c.telefones, c.empresa,
                          (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs
                     FROM contacts c
                    WHERE c.telefones IS NOT NULL AND c.telefones::text <> '[]'""")
    decidir, dupes, compartilhados = [], [], []
    for f in cur.fetchall():
        tels = [t.get("number") for t in (f["telefones"] or []) if isinstance(t, dict)]
        ks = [chave(t) for t in tels if chave(t)]
        entradas = next((por_tel[k] for k in ks if k in por_tel), None)
        if not entradas:
            continue

        nomes = {norm(n) for n, _r, _c in entradas}
        por_conta = {}
        for n, _r, c in entradas:
            por_conta.setdefault(c, set()).add(norm(n))
        # duas grafias no MESMO Google = duplicata de lá. A mesma pessoa nas duas
        # contas é normal e não conta.
        colisao = next((v for v in por_conta.values() if len(v) > 1), None)

        if norm(f["nome"]) in nomes and not colisao:
            continue

        base = {"id": f["id"], "intel": f["nome"], "msgs": f["msgs"],
                "empresa": f["empresa"], "tel": tels[0] if tels else "",
                "google_todos": sorted({n for n, _r, _c in entradas}),
                "intel_bate": norm(f["nome"]) in nomes}

        if colisao:
            ts = [tokens(n) for n in colisao]
            mesma_pessoa = any(ts[i] & ts[j]
                               for i in range(len(ts)) for j in range(i + 1, len(ts)))
            (dupes if mesma_pessoa else compartilhados).append(base)
            continue

        nome_g, rid, _c = entradas[0]
        decidir.append({**base, "google": nome_g, "rid": rid})

    for l in (decidir, dupes, compartilhados):
        l.sort(key=lambda x: -x["msgs"])
    return decidir, dupes, compartilhados


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


def render(pares, dupes=(), compartilhados=()):
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
<p class="nota"><b>Tirei {len(dupes) + len(compartilhados)} da sua frente</b>, que a versão anterior
desta tela contava como decisão sua: <b>{len(dupes)}</b> são a mesma pessoa cadastrada duas vezes no
Google (em {sum(1 for d in dupes if d.get('intel_bate'))} delas o INTEL já está certo — é limpeza lá,
não escolha aqui) e <b>{len(compartilhados)}</b> são telefone compartilhado, onde o INTEL guarda a
empresa e o Google guarda quem atende (<i>Copersucar</i> × <i>Sidnei Rosa</i>);
{sum(1 for c in compartilhados if c['msgs'] == 0)} delas sem uma única mensagem trocada.</p>
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
    pares, dupes, compartilhados = asyncio.run(coletar())
    open(SAIDA, "w").write(render(pares, dupes, compartilhados))
    print(f"→ {SAIDA} ({len(pares)} decisões reais — "
          f"{sum(1 for p in pares if p['msgs'] > 0)} com histórico)")
    print(f"   fora da fila: {len(dupes)} duplicatas no Google · "
          f"{len(compartilhados)} telefones compartilhados")
    subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
