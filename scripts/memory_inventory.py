#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Inventário da memória — o mapa da sessão de saneamento do MEMORY.md (#999768).

READ-ONLY: não altera nenhum arquivo nem o banco. Só mede e classifica.

POR QUE ESTA VERSÃO EXISTE (08/08/26). A primeira casava arquivo↔banco pelo
TÍTULO, e o título de uma memória é o `name:` do frontmatter — prosa, reescrita
a esmo. Resultado: **falso negativo em 4 dos 9 "arquivos sem registro"** (o
`catapora_idiom` estava lá desde sempre, id 180; o script não via porque o
`name:` começa com aspa e o `strip('"')` comia só uma ponta). Um saneamento que
corta linha do índice apoiado nesse mapa corta pelo mapa errado.

A chave estável sempre existiu e ninguém usava: o endpoint de sync grava
`tags = ["<arquivo>.md"]` em 100% dos registros. É por ela que se casa aqui, e
foi ela que virou identidade no banco (migration 069).

O QUE ESTE SCRIPT NÃO FAZ MAIS: dar veredito. A versão anterior tinha uma coluna
"sugestão" que marcava `SAI→recall` por regex de palavra ("RESOLVIDO", "ENTREGUE")
— e marcou assim o `board_hunt`, que é frente ATIVA. Veredito de máquina sobre
prosa vira corte errado com cara de método. Aqui vão só os SINAIS (idade, marca
de resolvido, onde vive, tem embedding); o balde fica/sai/merge/histórico é
julgamento humano, lendo o memo.

Uso:  ./memory_inventory.py            # relatório + HTML
      ./memory_inventory.py --quiet    # só o drift acionável (abertura de sessão)
"""
import argparse
import collections
import datetime
import html
import json
import os
import re
import sys

import psycopg2

MEM_DIR = "/Users/rap/.claude/projects/-Users-rap-prospect-system/memory"
INDEX = os.path.join(MEM_DIR, "MEMORY.md")
OUT_HTML = os.path.expanduser("~/cockpit/memory_inventory.html")
FONTE = "claude_code_migration"


def db_url():
    with open("/Users/rap/prospect-system/.env", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DATABASE_URL_UNPOOLED="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("sem DATABASE_URL_UNPOOLED")


def coletar():
    with open(INDEX, encoding="utf-8") as f:
        index_text = f.read()
    indexed = set(re.findall(r'\]\(([a-zA-Z0-9_]+)\.md\)', index_text))

    conn = psycopg2.connect(db_url())
    cur = conn.cursor()
    # Casa pelo ARQUIVO (tags->>0), não pelo título — ver docstring.
    cur.execute(
        "SELECT tags->>0, id, titulo, (embedding IS NULL), atualizado_em "
        "FROM system_memories WHERE fonte = %s AND tags->>0 IS NOT NULL",
        (FONTE,),
    )
    por_arquivo = collections.defaultdict(list)
    for fn, mid, tit, e_null, upd in cur.fetchall():
        por_arquivo[fn].append(dict(id=mid, titulo=tit, sem_embedding=e_null, upd=upd))
    # Rebaixados pela 069 — ficam no banco, fora da busca. Contados só pra
    # mostrar que o conserto pegou; não são drift.
    cur.execute(
        "SELECT count(*) FROM system_memories WHERE fonte = %s",
        (FONTE + "_superseded",),
    )
    rebaixados = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM system_memories")
    db_total = cur.fetchone()[0]
    conn.close()

    SINAL = re.compile(r'RESOLVID|ENTREGUE|DESATIVAD|DESLIGAD|obsolet|DEPRECAT|SUPERAD', re.I)
    hoje = datetime.datetime.now()
    linhas = []
    for fn in sorted(os.listdir(MEM_DIR)):
        if not fn.endswith(".md") or fn == "MEMORY.md":
            continue
        slug = fn[:-3]
        caminho = os.path.join(MEM_DIR, fn)
        with open(caminho, encoding="utf-8") as f:
            body = f.read()
        m = re.search(r'^name:\s*(.+)$', body, re.M)
        nome = m.group(1).strip().strip('"') if m else slug
        d = re.search(r'^description:\s*(.+)$', body, re.M)
        desc = (d.group(1).strip().strip('"') if d else "")[:90]
        t = re.search(r'^\s*(?:type|node_type):\s*(\w+)', body, re.M)
        tipo = t.group(1) if t else (slug.split("_")[0] if "_" in slug else "?")
        # `type: user` é pulado pelo endpoint DE PROPÓSITO (redundante com o
        # CLAUDE.md, que já entra em toda sessão). Ausência aqui não é buraco.
        tipo_user = bool(re.search(r'^type:\s*user\s*$', body, re.M))

        regs = por_arquivo.get(fn, [])
        linhas.append(dict(
            slug=slug, arquivo=fn, tipo=tipo, nome=nome, desc=desc,
            kb=round(len(body.encode("utf-8")) / 1024, 1),
            dias=(hoje - datetime.datetime.fromtimestamp(os.path.getmtime(caminho))).days,
            no_indice=slug in indexed,
            regs=regs,
            no_banco=bool(regs),
            sem_embedding=any(r["sem_embedding"] for r in regs),
            duplicado=len(regs) > 1,
            marca_resolvido=bool(SINAL.search(body)),
            historico=slug.endswith("_historico"),
            tipo_user=tipo_user,
        ))
    return linhas, indexed, db_total, rebaixados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só o drift acionável")
    a = ap.parse_args()

    linhas, indexed, db_total, rebaixados = coletar()
    agora = datetime.datetime.now()

    # --- os 3 drifts que importam, cada um com consequência própria ---
    # 1. arquivo sem registro: invisível ao recall. Tirar do índice = PERDER.
    sem_banco = [l for l in linhas if not l["no_banco"] and not l["tipo_user"]]
    # 2. registro duplicado: o velho responde a busca como se fosse atual.
    duplicados = [l for l in linhas if l["duplicado"]]
    # 3. sem embedding: só aparece por palavra-chave, não por sentido.
    sem_emb = [l for l in linhas if l["no_banco"] and l["sem_embedding"]]
    # contexto (não é drift): quem já vive só de recall.
    fora_indice = [l for l in linhas if not l["no_indice"]]
    idx_kb = round(os.path.getsize(INDEX) / 1024, 1)

    print("╔═ INVENTÁRIO DA MEMÓRIA ═╗")
    print(f"  {agora:%d/%m/%Y %H:%M} · read-only · casamento por ARQUIVO (tags->>0)")
    print(f"  índice: {idx_kb} KB · {len(indexed)} entradas · {len(linhas)} arquivos .md "
          f"· {db_total} registros no banco" + (f" · {rebaixados} rebaixados" if rebaixados else ""))
    print("\n  ─ Drift ─")
    print(f"  · {len(sem_banco)} arquivos SEM registro  → invisíveis ao recall: "
          f"tirar do índice é PERDER")
    print(f"  · {len(duplicados)} arquivos com registro DUPLICADO → o velho responde a busca")
    print(f"  · {len(sem_emb)} sem embedding → só aparecem por palavra-chave, não por sentido")
    print(f"  · {len(fora_indice)} fora do índice (contexto: já vivem de recall)")

    for titulo, grupo, detalhe in (
        ("SEM registro no banco", sem_banco, lambda l: ""),
        ("DUPLICADOS (o velho continua buscável)", duplicados,
         lambda l: "  ids: " + ", ".join(str(r["id"]) for r in sorted(l["regs"], key=lambda r: r["id"]))),
        ("SEM embedding", sem_emb, lambda l: ""),
    ):
        if grupo:
            print(f"\n  {titulo}:")
            for l in grupo:
                marca = " [no índice]" if l["no_indice"] else ""
                print(f"    - {l['slug']}{marca}{detalhe(l)}")

    if a.quiet:
        # Falha alto pelo mesmo motivo do verifica_boards: o sync é best-effort
        # e sempre sai 0. Sem alguém comparando com a FONTE (os arquivos), a
        # falha do hook é silenciosa — foi assim que 5 memos ficaram de fora.
        return 1 if (sem_banco or duplicados) else 0

    print(f"\n{'slug':<50} {'tipo':<9} {'KB':>4} {'dias':>4} idx db emb dup resolv")
    print("-" * 96)
    for l in sorted(linhas, key=lambda l: (l["no_banco"], not l["duplicado"], -l["dias"])):
        print(f"{l['slug'][:49]:<50} {l['tipo'][:8]:<9} {l['kb']:>4} {l['dias']:>4} "
              f"{'✓' if l['no_indice'] else '·':^3} {'✓' if l['no_banco'] else '·':^2} "
              f"{'·' if l['sem_embedding'] else '✓':^3} {'⚠' if l['duplicado'] else ' ':^3} "
              f"{'sim' if l['marca_resolvido'] else ''}")

    escrever_html(linhas, agora, idx_kb, indexed, db_total,
                  sem_banco, duplicados, sem_emb, fora_indice)
    print(f"\n→ HTML: {OUT_HTML}   (abrir: open {OUT_HTML})")
    return 0


def escrever_html(linhas, agora, idx_kb, indexed, db_total,
                  sem_banco, duplicados, sem_emb, fora_indice):
    def td(x):
        return f"<td>{html.escape(str(x))}</td>"

    trs = []
    for l in sorted(linhas, key=lambda l: (l["no_banco"], not l["duplicado"], -l["dias"])):
        cls = ("semdb" if not l["no_banco"] and not l["tipo_user"]
               else "dup" if l["duplicado"]
               else "sememb" if l["sem_embedding"]
               else "recall" if not l["no_indice"] else "")
        ids = ", ".join(str(r["id"]) for r in sorted(l["regs"], key=lambda r: r["id"])) or "—"
        trs.append(
            f'<tr class="{cls}">{td(l["slug"])}{td(l["tipo"])}{td(l["kb"])}{td(l["dias"])}'
            f'<td class=c>{"✓" if l["no_indice"] else "·"}</td>'
            f'<td class=c>{ids}</td>'
            f'<td class=c>{"·" if l["sem_embedding"] else "✓"}</td>'
            f'<td class=c>{"⚠" if l["duplicado"] else ""}</td>'
            f'<td class=c>{"sim" if l["marca_resolvido"] else ""}</td>{td(l["desc"])}</tr>'
        )
    doc = f"""<!doctype html><meta charset=utf-8><title>Inventário da memória</title>
<style>
:root{{--ink:#2b2b2b;--soft:#6b6b6b;--gold:#b8935a;--line:#e6e0d4;--bg:#faf7f1}}
body{{font:14px/1.5 -apple-system,'Avenir Next',Segoe UI,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:28px 32px}}
h1{{font:600 22px/1.2 'Playfair Display',Georgia,serif;margin:0 0 4px}}
.sub{{color:var(--soft);margin:0 0 18px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 8px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:118px}}
.card b{{font-size:26px;display:block;color:var(--gold)}}
.card span{{color:var(--soft);font-size:12px}}
.nota{{color:var(--soft);font-size:12.5px;max-width:70ch;margin:6px 0 18px}}
.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}}
th{{background:#f0ebe0;cursor:pointer;position:sticky;top:0;font-weight:600}}
td.c{{text-align:center}}
tr.semdb{{background:#fdeaea}} tr.dup{{background:#fdf4ef}} tr.sememb{{background:#f6f2e8}} tr.recall{{opacity:.62}}
.leg span{{display:inline-block;margin-right:14px;font-size:12px;color:var(--soft)}}
.sw{{display:inline-block;width:11px;height:11px;border-radius:2px;vertical-align:-1px;margin-right:4px}}
</style>
<h1>Inventário da memória</h1>
<p class=sub>{agora:%d/%m/%Y %H:%M} · read-only · casado por ARQUIVO, não por título</p>
<div class=cards>
<div class=card><b>{idx_kb}</b><span>KB no índice</span></div>
<div class=card><b>{len(indexed)}</b><span>entradas linkadas</span></div>
<div class=card><b>{len(linhas)}</b><span>arquivos .md</span></div>
<div class=card><b>{db_total}</b><span>no banco</span></div>
<div class=card><b>{len(sem_banco)}</b><span>sem registro</span></div>
<div class=card><b>{len(duplicados)}</b><span>duplicados</span></div>
<div class=card><b>{len(sem_emb)}</b><span>sem embedding</span></div>
</div>
<p class=nota>Não há coluna de veredito, e é de propósito: a versão anterior deste
inventário marcava &ldquo;sai&rdquo; por regex de palavra e marcou assim o board_hunt, que é
frente ativa. Aqui vão só os sinais — quem fica, sai, funde ou vira histórico se
decide lendo o memo.</p>
<p class=leg>
<span><i class=sw style=background:#fdeaea></i>sem registro (invisível ao recall — tirar do índice é perder)</span>
<span><i class=sw style=background:#fdf4ef></i>duplicado (o velho responde a busca)</span>
<span><i class=sw style=background:#f6f2e8></i>sem embedding (só por palavra-chave)</span>
<span><i class=sw style="background:#eee;opacity:.62"></i>já vive de recall</span>
</p>
<div class=wrap><table id=t>
<thead><tr><th>slug</th><th>tipo</th><th>KB</th><th>dias</th><th>índice</th><th>id(s) no banco</th><th>emb</th><th>dup</th><th>marca</th><th>descrição</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>
<script>
document.querySelectorAll('#t th').forEach((th,i)=>th.onclick=()=>{{
  const tb=th.closest('table').tBodies[0],rows=[...tb.rows];
  const num=[2,3].includes(i);
  const asc=th.dataset.asc=th.dataset.asc==='1'?'':'1';
  rows.sort((a,b)=>{{let x=a.cells[i].innerText,y=b.cells[i].innerText;
    if(num){{x=+x;y=+y}} return (x>y?1:x<y?-1:0)*(asc?1:-1)}});
  rows.forEach(r=>tb.appendChild(r));
}});
</script>"""
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    sys.exit(main())
