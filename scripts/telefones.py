#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/telefones.html — fichas que dividem o mesmo número: quem é quem?

POR QUE ESTA TELA EXISTE, e o erro que a originou (07/08/2026). Na tela de nomes
eu agrupei fichas pelo contato do Google e propaguei a decisão dele para as
"irmãs" — assumindo que casar com o mesmo contato significava ser a mesma
pessoa. Não significa: elas casam porque DIVIDEM UM TELEFONE (portaria, empresa,
celular corporativo). O resultado foram renomeações absurdas — "Sucuriú" virou
"César Cestari", "Oyster Construtora" virou "Floriano Itapira" — todas
revertidas.

A pergunta certa para esses casos não é "qual nome fica", é **"são a mesma pessoa
ou pessoas diferentes com o mesmo número?"**. Só ele sabe, e a resposta muda
tudo: mesma pessoa → merge; pessoas diferentes → as fichas ficam, e o telefone
simplesmente não serve pra identificar ninguém ali.

ESCOPO: dos 2.159 grupos com telefone repetido, **46 têm alguma mensagem** (159
fichas). Os outros 2.113 são cadastro morto — decidir sobre eles é gastar
atenção onde não há consequência. O contador de mensagens fica visível pra ele
parar quando quiser.

Uso:  ./telefones.py             # gera e abre
      ./telefones.py --gravar -  # aplica o que ele colou
"""
import os
import re
import subprocess
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/telefones.html")


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


SQL = """
WITH tel AS (
  SELECT c.id, right(regexp_replace(e.j->>'number','\\D','','g'), 8) AS k
    FROM contacts c
    CROSS JOIN LATERAL jsonb_array_elements(c.telefones) e(j)
   WHERE c.telefones IS NOT NULL AND jsonb_typeof(c.telefones) = 'array'
     AND length(regexp_replace(e.j->>'number','\\D','','g')) >= 10
),
grupos AS (
  SELECT k FROM tel WHERE length(k) = 8 GROUP BY k HAVING count(DISTINCT id) > 1
)
SELECT t.k, c.id, c.nome, c.empresa, c.cargo, c.origem, c.criado_em, c.contexto,
       (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs,
       (SELECT max(m.criado_em) FROM messages m WHERE m.contact_id = c.id) AS ultima,
       (SELECT e2.j->>'number' FROM jsonb_array_elements(c.telefones) e2(j) LIMIT 1) AS tel
  FROM tel t JOIN grupos g ON g.k = t.k JOIN contacts c ON c.id = t.id
 ORDER BY t.k, msgs DESC
"""

CSS = """
:root{--paper:#f4f0e7;--panel:#fbf9f3;--edge:#e4ddcc;--ink:#23211c;--soft:#57534a;
--faint:#8a8477;--brass:#9c7a33;--brass2:#b79a55;--calm:#4f6b4a;--calmbg:#e3e9dc;
--warn:#9a6a13;--warnbg:#f0e6cf;--shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
--serif:Georgia,serif;--sans:system-ui,-apple-system,sans-serif;--mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
.wrap{max-width:54rem;margin:0 auto;padding:2.2rem 1.2rem 8rem}
.eyebrow{font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--brass);font-weight:600;margin:0 0 .4rem}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(1.6rem,4vw,2.1rem);margin:0}
.filete{width:3rem;height:2px;background:var(--brass2);margin:.9rem 0 1rem}
.lead{font-size:.9rem;color:var(--soft);max-width:66ch;margin:0 0 1.8rem}
.grupo{background:var(--panel);border:1px solid var(--edge);border-radius:8px;padding:.85rem 1rem;
margin:0 0 .7rem;box-shadow:var(--shadow)}
.grupo.decidido{border-left:3px solid var(--calm)}
.tel{font-family:var(--mono);font-size:.78rem;color:var(--brass);margin-bottom:.5rem}
.tel b{color:var(--ink)}
.fichas{display:flex;flex-direction:column;gap:.3rem;margin-bottom:.55rem}
.ficha{display:flex;gap:.6rem;align-items:baseline;font-size:.87rem;padding:.35rem .5rem;
border:1px solid transparent;border-radius:4px;cursor:pointer;background:var(--paper)}
.ficha:hover{border-color:var(--brass2)}
.ficha[aria-pressed="true"]{background:var(--calmbg);border-color:var(--calm);font-weight:600;color:var(--calm)}
.ficha .n{flex:1}
.ficha .m{font-family:var(--mono);font-size:.7rem;color:var(--faint);white-space:nowrap}
.acoes{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;font-size:.78rem;color:var(--faint)}
.acoes button{font:inherit;font-size:.76rem;padding:.3rem .7rem;cursor:pointer;border-radius:4px;
border:1px solid var(--edge);background:var(--paper);color:var(--soft)}
.acoes button:hover{border-color:var(--brass2)}
.acoes button[aria-pressed="true"]{background:var(--warnbg);border-color:var(--warn);color:var(--warn);font-weight:600}
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
const CHAVE="telefones-decisao";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".grupo").forEach(g=>{
    const e=salvo[g.dataset.k];
    g.querySelectorAll(".ficha").forEach(f=>f.setAttribute("aria-pressed",String(e&&e.tipo==="merge"&&e.id===f.dataset.id)));
    g.querySelector(".diferentes").setAttribute("aria-pressed",String(!!e&&e.tipo==="diferentes"));
    g.classList.toggle("decidido",!!e);
    if(e)n++;
  });
  document.getElementById("n").textContent=n;
}
document.querySelectorAll(".ficha").forEach(f=>f.addEventListener("click",()=>{
  const g=f.closest(".grupo"),k=g.dataset.k,e=salvo[k];
  if(e&&e.tipo==="merge"&&e.id===f.dataset.id)delete salvo[k];
  else salvo[k]={tipo:"merge",id:f.dataset.id};
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
}));
document.querySelectorAll(".diferentes").forEach(b=>b.addEventListener("click",()=>{
  const g=b.closest(".grupo"),k=g.dataset.k,e=salvo[k];
  if(e&&e.tipo==="diferentes")delete salvo[k]; else salvo[k]={tipo:"diferentes"};
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
}));
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);document.getElementById("saida").style.display="none";pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=["TELEFONES — decisao (cole no Claude)"];let c=0;
  document.querySelectorAll(".grupo").forEach(g=>{
    const e=salvo[g.dataset.k];if(!e)return;c++;
    if(e.tipo==="diferentes")L.push("TEL "+g.dataset.k+" DIFERENTES :: "+g.dataset.ids);
    else L.push("TEL "+g.dataset.k+" MERGE #"+e.id+" :: "+g.dataset.ids);
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


def render(grupos):
    ago = datetime.now()
    blocos = []
    for k, fichas in grupos:
        ids = ",".join(str(f["id"]) for f in fichas)
        linhas = "".join(
            f'<div class="ficha" data-id="{f["id"]}" role="button" aria-pressed="false">'
            f'<span class="n">{esc(f["nome"])}'
            f'{" · " + esc(f["empresa"]) if f["empresa"] else ""}</span>'
            f'<span class="m">#{f["id"]} · {f["msgs"]} msg'
            f'{" · " + f["ultima"].strftime("%d/%m/%y") if f["ultima"] else ""}</span></div>'
            for f in fichas)
        blocos.append(f"""<div class="grupo" data-k="{k}" data-ids="{ids}">
  <div class="tel">mesmo número <b>{esc(fichas[0]['tel'])}</b> · {len(fichas)} fichas</div>
  <div class="fichas">{linhas}</div>
  <div class="acoes">clique na ficha que deve FICAR (as outras são absorvidas) &nbsp;ou&nbsp;
    <button class="diferentes" type="button">são pessoas diferentes</button></div>
</div>""")
    total_f = sum(len(f) for _, f in grupos)
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telefones dividido — quem é quem</title><style>{CSS}</style></head><body><div class="wrap">
<p class="eyebrow">INTEL · contatos</p>
<h1>Mesmo número, quantas pessoas?</h1>
<div class="filete"></div>
<p class="lead">{len(grupos)} números aparecem em mais de uma ficha <b>e têm conversa</b> — são os que
importam ({total_f} fichas). Para cada um: se for <b>a mesma pessoa</b>, clique na ficha que deve ficar
e as outras são absorvidas; se forem <b>pessoas diferentes</b> dividindo um número (portaria, empresa,
celular corporativo), diga isso — aí o telefone simplesmente não identifica ninguém ali, e o sistema
para de tentar.<br>
<b>Por que esta tela existe:</b> hoje eu presumi que fichas com o mesmo número eram a mesma pessoa e
renomeei sete delas errado — "Sucuriú" virou "César Cestari". Revertido. A pergunta é sua, não minha.</p>
{''.join(blocos)}
<p class="nota">Gerado {ago:%d/%m/%Y %H:%M}. Nada muda por esta página: ela só registra sua escolha.
Fora daqui há mais {2159 - len(grupos)} números repetidos sem conversa nenhuma — cadastro morto, não vale
sua atenção.</p>
<div class="barra">
  <span><b id="n">0</b> decidido(s) de {len(grupos)}</span>
  <div><button class="ghost" id="limpar">Limpar</button>
  <button id="copiar">Copiar para o Claude</button></div>
</div>
<textarea id="saida" rows="6" readonly></textarea>
</div>{JS}</body></html>"""


def gravar(texto):
    """Traduz as escolhas em merges (arquivo pro merge_contatos.py) e marcações."""
    merges, diferentes = [], []
    for l in texto.splitlines():
        m = re.match(r"^TEL (\d+) MERGE #(\d+) :: (.+)$", l.strip())
        if m:
            fica, ids = int(m.group(2)), [int(x) for x in m.group(3).split(",")]
            merges.append((fica, [i for i in ids if i != fica]))
            continue
        d = re.match(r"^TEL (\d+) DIFERENTES :: (.+)$", l.strip())
        if d:
            diferentes.append((d.group(1), [int(x) for x in d.group(2).split(",")]))
    if merges:
        caminho = "/tmp/merge_telefones.txt"
        with open(caminho, "w") as fh:
            for fica, saem in merges:
                fh.write(f"MERGE {fica}-{'-'.join(str(s) for s in saem)} -> manter #{fica}\n")
        print(f"{len(merges)} merges → {caminho}")
        print(f"  revise e rode:  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 "
              f"./scripts/merge_contatos.py {caminho} --apply")
    if diferentes:
        # Marca no contexto pra ninguém (nem eu) tentar mergear de novo. Sem este
        # registro, a mesma pergunta volta na próxima varredura — e a decisão
        # dele evapora, que foi o defeito do check-G até hoje de manhã.
        conn = psycopg2.connect(env("DATABASE_URL"))
        cur = conn.cursor()
        n = 0
        for tel, ids in diferentes:
            cur.execute("""UPDATE contacts SET contexto = COALESCE(contexto||' | ','') ||
                             'telefone compartilhado (confirmado pelo Renato 07/08) — nao mergear por numero'
                            WHERE id = ANY(%s)
                              AND COALESCE(contexto,'') NOT LIKE '%%telefone compartilhado%%'""", (ids,))
            n += cur.rowcount
        conn.commit()
        print(f"{len(diferentes)} grupos marcados como telefone compartilhado ({n} fichas)")
    return 0


def main():
    if "--gravar" in sys.argv:
        i = sys.argv.index("--gravar")
        alvo = sys.argv[i + 1] if len(sys.argv) > i + 1 else "-"
        return gravar(sys.stdin.read() if alvo == "-" else open(alvo).read())

    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(SQL)
    por_k = {}
    for r in cur.fetchall():
        # dedup por id: a mesma ficha pode ter o número repetido no array de
        # telefones (ou dois números com os mesmos 8 dígitos finais), e apareceria
        # duas vezes no próprio grupo — sugerindo duplicata onde há uma ficha só.
        alvo = por_k.setdefault(r["k"], [])
        if not any(x["id"] == r["id"] for x in alvo):
            alvo.append(dict(r))
    # só os que têm conversa: decidir sobre cadastro morto é gastar atenção onde
    # não há consequência
    # O QUE ELE JÁ DECIDIU NÃO VOLTA. Marcar "telefone compartilhado" no contexto
    # não servia de nada se a tela seguisse perguntando — foi o que aconteceu com
    # Orestes/Douglas e Carla/Vânia, que ele teve de responder duas vezes. Mesma
    # lição do check-G hoje de manhã: decisão registrada que não filtra a próxima
    # varredura é decisão que evapora.
    grupos = [(k, v) for k, v in por_k.items()
              if sum(f["msgs"] for f in v) > 0
              and not all("telefone compartilhado" in (f.get("contexto") or "") for f in v)]
    grupos.sort(key=lambda kv: -sum(f["msgs"] for f in kv[1]))
    open(SAIDA, "w").write(render(grupos))
    print(f"→ {SAIDA} ({len(grupos)} grupos, {sum(len(v) for _, v in grupos)} fichas)")
    subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
