#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/empresas.html — a empresa do contato mudou e o INTEL não soube.

POR QUE EXISTE (22/08/2026). O board carregava "89 de 231 contatos com `empresa`
≠ LinkedIn atual" como decisão pendente. Re-medido: são 87 de 286 — o primeiro
número herdado da semana que estava certo. Mas o balde único escondia três
naturezas, e só uma é decisão:

  🔤  2  grafia ("Virtus BR Partners" × "VirtusBR Partners") — não é decisão.
  🕐 27  o INTEL guarda o emprego ANTERIOR, e o próprio `linkedin_experience`
         traz essa empresa COM data de saída. A pessoa mudou e o cadastro não
         acompanhou.
  🟠 58  divergência sem confirmação de saída — pode ser cargo paralelo, empresa
         do grupo, ou o LinkedIn é que está velho.

ESTA TELA MOSTRA SÓ OS 27, e a razão de não aplicar sozinho é concreta: parte é
VÍNCULO PARALELO, não troca de emprego. O #34 é advogado da LBZ **e** dirigente
da Federação ASSESPRO-SP; o INTEL guarda o vínculo pelo qual o Renato se
relaciona com a pessoa. Trocar daria um dado tecnicamente correto e um contexto
errado — e contexto de relacionamento é o que este sistema existe pra guardar.

O período de saída vai na tela junto. "Saiu da Raízen em 2024" é o que deixa a
decisão ser de um segundo; sem isso ele teria que ir conferir no LinkedIn, e a
ferramenta viraria tarefa dele.

Uso:  ./empresas.py            # gera e abre
      ./empresas.py --gravar - --apply
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SAIDA = os.path.expanduser("~/cockpit/empresas.html")
SUFIXOS = {"ltda", "sa", "s", "me", "eireli", "inc", "llc", "ltd", "corp", "group",
           "grupo", "holding", "participacoes", "consultoria", "co", "company",
           "the", "de", "da", "do", "e"}

import nomes as tela  # noqa: E402  (CSS/esc — uma identidade visual só)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(t for t in re.split(r"[^a-z0-9]+", s) if t and t not in SUFIXOS)


def _exp(valor):
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except Exception:
            return []
    return valor if isinstance(valor, list) else []


def _atual(exp):
    for x in exp:
        if not ((x or {}).get("end_date") or {}).get("year"):
            return x
    return exp[0] if exp else None


def levantar():
    from database import get_connection
    cur = get_connection().cursor()
    cur.execute("""SELECT id, nome, empresa, cargo, linkedin_experience,
                          (SELECT count(*) FROM messages m WHERE m.contact_id = contacts.id) AS msgs
                     FROM contacts
                    WHERE linkedin_experience IS NOT NULL
                      AND linkedin_experience::text NOT IN ('', '[]', 'null')""")
    casos = []
    for r in cur.fetchall():
        exp = _exp(r["linkedin_experience"])
        atual = _atual(exp)
        li = (atual or {}).get("company", "").strip() if atual else ""
        if not li or not (r["empresa"] or "").strip():
            continue
        a, b = norm(r["empresa"]), norm(li)
        if a == b or (a and b and (a in b or b in a)):
            continue
        # a empresa do INTEL aparece no LinkedIn COM data de saída?
        saida = None
        for x in exp:
            ed = (x or {}).get("end_date") or {}
            p = norm(x.get("company"))
            if ed.get("year") and p and (p == a or p in a or a in p):
                saida = ed
                break
        if not saida:
            continue
        ini = (atual or {}).get("start_date") or {}
        casos.append({
            "id": r["id"], "nome": r["nome"], "msgs": r["msgs"],
            "de": r["empresa"], "para": li,
            "cargo_intel": r["cargo"] or "", "cargo_novo": (atual or {}).get("title") or "",
            "saiu_em": f"{saida.get('month') or ''}/{saida.get('year')}".strip("/"),
            "entrou_em": f"{ini.get('month') or ''}/{ini.get('year')}".strip("/") if ini.get("year") else "",
        })
    casos.sort(key=lambda x: -x["msgs"])
    return casos


JS = r"""
<script>
const CHAVE="empresas-decisao";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.id];
    p.querySelectorAll(".op").forEach(b=>b.setAttribute("aria-pressed",String(e===b.dataset.acao)));
    p.classList.toggle("decidido",!!e); if(e)n++;
  });
  document.getElementById("n").textContent=n;
}
document.querySelectorAll(".op").forEach(b=>b.addEventListener("click",()=>{
  const p=b.closest(".par"),k=p.dataset.id;
  if(salvo[k]===b.dataset.acao)delete salvo[k]; else salvo[k]=b.dataset.acao;
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
}));
document.getElementById("todos").addEventListener("click",()=>{
  document.querySelectorAll(".par").forEach(p=>salvo[p.dataset.id]="atualizar");
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
});
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("saida").style.display="none";pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=["EMPRESAS — decisao (cole no Claude)"];let c=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.id];if(!e)return;c++;
    L.push("#"+p.dataset.id+" "+e.toUpperCase()+" :: "+p.dataset.para);
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

EXTRA = """
.par{grid-template-columns:1fr auto}
.mudanca{font-size:.95rem}
.mudanca .de{color:var(--faint);text-decoration:line-through}
.mudanca .para{font-weight:600}
.quando{font-family:var(--mono);font-size:.68rem;color:var(--brass);display:block;margin-top:.25rem}
.acoes{display:flex;gap:.5rem;flex-wrap:wrap;grid-column:1/-1;margin-top:.5rem}
.op{flex:0 1 auto;min-width:11rem}
"""


def render(casos):
    from datetime import datetime
    ago = datetime.now()
    linhas = []
    for c in casos:
        quando = []
        if c["saiu_em"]:
            quando.append(f"saiu de {tela.esc(c['de'])} em {c['saiu_em']}")
        if c["entrou_em"]:
            quando.append(f"entrou em {c['entrou_em']}")
        cargo = f" — {tela.esc(c['cargo_novo'])}" if c["cargo_novo"] else ""
        linhas.append(f"""<div class="par" data-id="{c['id']}" data-para="{tela.esc(c['para'])}">
  <div class="mudanca"><b>{tela.esc(c['nome'])}</b><br>
    <span class="de">{tela.esc(c['de'])}</span> → <span class="para">{tela.esc(c['para'])}</span>{cargo}
    <span class="quando">{tela.esc(' · '.join(quando))}</span>
  </div>
  <div class="meta">#{c['id']}<br>{c['msgs']} msg{'s' if c['msgs'] != 1 else ''}</div>
  <div class="acoes">
    <button class="op" data-acao="atualizar"><small>trocar</small>a empresa mudou mesmo</button>
    <button class="op" data-acao="manter"><small>manter</small>é vínculo paralelo</button>
  </div>
</div>""")
    com_hist = sum(1 for c in casos if c["msgs"] > 0)
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Empresas desatualizadas</title><style>{tela.CSS}{EXTRA}</style></head>
<body><div class="wrap">
<p class="eyebrow">INTEL · cadastro</p>
<h1>A empresa mudou e o INTEL não soube</h1>
<div class="filete"></div>
<p class="lead">Nestes {len(casos)}, o próprio LinkedIn da pessoa registra a <b>saída</b> da empresa
que está no seu cadastro — com data. Não é palpite de nome parecido.
<br><b>Mas nem toda saída é troca:</b> tem gente que acumula vínculo. O #34 é advogado da LBZ
<i>e</i> dirigente da Federação ASSESPRO-SP, e o INTEL guarda aquele pelo qual você se relaciona
com a pessoa. Por isso a pergunta, em vez de eu trocar tudo.
<br>{com_hist} têm histórico de conversa com você.</p>
{''.join(linhas)}
<p class="nota">Gerado {ago:%d/%m/%Y %H:%M}. Nada é alterado por esta página.</p>
<div class="barra">
  <span><b id="n">0</b> decidido(s) de {len(casos)}</span>
  <div><button class="ghost" id="limpar">Limpar</button>
  <button class="ghost" id="todos">Trocar todas</button>
  <button id="copiar">Copiar para o Claude</button></div>
</div>
<textarea id="saida" rows="6" readonly></textarea>
</div>{JS}</body></html>"""


def aplicar(texto, dry=True):
    from database import get_connection
    conn = get_connection()
    cur = conn.cursor()
    trocados, mantidos = 0, 0
    for l in texto.splitlines():
        m = re.match(r"^#(\d+)\s+(ATUALIZAR|MANTER)\s*::\s*(.+)$", l.strip())
        if not m:
            continue
        cid, acao, nova = int(m.group(1)), m.group(2), m.group(3).strip()
        if acao == "MANTER":
            mantidos += 1
            continue
        cur.execute("SELECT nome, empresa FROM contacts WHERE id = %s", (cid,))
        r = cur.fetchone()
        if not r:
            print(f"  ⚠️  #{cid} não existe")
            continue
        print(f"  [{'dry' if dry else 'APPLY'}] #{cid} {r['nome'][:28]}: "
              f"\"{r['empresa']}\" → \"{nova}\"")
        if not dry:
            cur.execute("UPDATE contacts SET empresa = %s, atualizado_em = now() WHERE id = %s",
                        (nova, cid))
            trocados += 1
    if not dry:
        conn.commit()
    print(f"\n  {'planejado' if dry else 'aplicado'}: {trocados} trocados · {mantidos} mantidos")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gravar", metavar="ARQ")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.gravar:
        texto = sys.stdin.read() if a.gravar == "-" else open(a.gravar).read()
        return aplicar(texto, dry=not a.apply)
    casos = levantar()
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    open(SAIDA, "w").write(render(casos))
    print(f"→ {SAIDA} ({len(casos)} com saída confirmada no LinkedIn)")
    subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
