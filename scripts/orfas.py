#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/orfas.html — decidir o que fazer com as fichas repetidas do Google.

POR QUE EXISTE (22/08/2026). O pedido foi "limpa as 231 órfãs no Google". Ao
baixar as duas pontas antes de apagar, a maioria não era duplicata: a ficha #697
("Vanessa Ikeno") arrastava sete pessoas com e-mail @carambola.com.vc — colegas
no telefone da empresa —, e a #2856, meia Amcham. Apagar teria destruído dezenas
de contatos reais. Com o corte de parentesco de nome sobraram 144, e a comparação
campo a campo separou o que ainda precisa de gente:

  🔁 MIGRAR E APAGAR — a órfã tem e-mail/telefone que só existe nela. Apagar
     direto perde dado; o certo é copiar pra principal antes. Mas isso ESCREVE na
     ficha boa, e escrita na ficha boa é decisão dele, não do script.
  🟡 REVER — parentesco fraco ("Totonho Munhoz" × "José Antonio Barros Munhoz").
     Pode ser a mesma pessoa, pode ser pai e filho. Só quem conhece decide.

O QUE ESTA TELA NÃO FAZ: pedir julgamento onde não há. As 37 fichas cuja
principal já tinha tudo foram apagadas sem passar por aqui — porque não havia
nada a decidir. Tela que empilha o mecânico junto do julgado é como os "460
nomes" viraram fila do Renato.

"PESSOAS DIFERENTES" VIRA REGISTRO, não só um não. A escolha vai pra
`contact_nao_fundir` (migration 077), então o par não volta a ser perguntado —
foi exatamente o que aconteceu com Douglas Bassi × Orestes, decidido uma vez e
reapresentado depois por não ter onde ser escrito.

Uso:  ./orfas.py                    # gera e abre
      ./orfas.py --cache 6          # reusa a varredura do Google
      ./orfas.py --gravar - --apply # aplica o que ele colou de volta
      ./orfas.py --gravar -         # (sem --apply: mostra o que faria)
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SAIDA = os.path.expanduser("~/cockpit/orfas.html")
CAMPOS = ("names,emailAddresses,phoneNumbers,organizations,addresses,biographies,"
          "birthdays,urls,relations,memberships,userDefined,events,nicknames")

import verifica_google as vg  # noqa: E402  (mesma pasta)
import nomes as tela  # noqa: E402  (CSS/esc reaproveitados — uma identidade visual só)


def esc(t):
    return tela.esc(t)


async def _get(cli, token, rid, pausa=0.35):
    """GET com backoff: 429 é cota da conta, não erro nosso — ver
    [[reference_voyage_rate_limits]] pro mesmo padrão em outra API."""
    for tentativa in range(5):
        r = await cli.get(f"https://people.googleapis.com/v1/people/{rid}",
                          headers={"Authorization": f"Bearer {token}"},
                          params={"personFields": CAMPOS})
        if r.status_code == 200:
            await asyncio.sleep(pausa)
            return r.json()
        if r.status_code == 429:
            await asyncio.sleep(pausa * (2 ** tentativa) + 1)
            continue
        return {"_erro": r.status_code}
    return {"_erro": "429_persistente"}


def _campos(p):
    return {
        "emails": {(e.get("value") or "").lower().strip()
                   for e in (p.get("emailAddresses") or []) if e.get("value")},
        "tels": {vg.chave(t.get("value")) for t in (p.get("phoneNumbers") or [])
                 if vg.chave(t.get("value"))},
        "orgs": {(o.get("name") or "").strip()
                 for o in (p.get("organizations") or []) if o.get("name")},
        "urls": {(u.get("value") or "").strip()
                 for u in (p.get("urls") or []) if u.get("value")},
    }


async def levantar(cache_h):
    import httpx
    import integrations.google_contacts as gc
    from database import get_connection

    fichas = await vg.varrer_google(cache_h)
    cur = get_connection().cursor()
    cur.execute("""SELECT id, nome, telefones, google_contact_id,
                          (SELECT count(*) FROM messages m WHERE m.contact_id = contacts.id) AS msgs
                     FROM contacts
                    WHERE telefones IS NOT NULL AND telefones::text <> '[]'""")
    contatos = [dict(r) for r in cur.fetchall()]
    b, _ = vg.classificar(fichas, contatos, vg._pares_decididos(cur),
                          vg._fichas_mantidas(cur))
    orfas = b["orfa"]
    print(f"  órfãs: {len(orfas)} — baixando os dois lados de cada par…")

    toks = {}
    for conta in {o["conta"] for o in orfas}:
        toks[conta] = await gc.get_valid_token(conta)
        if not toks[conta]:
            sys.exit(f"sem token para {conta} — reconecte antes de decidir")

    migrar, rever = [], []
    async with httpx.AsyncClient(timeout=30) as cli:
        for i, o in enumerate(orfas, 1):
            if i % 25 == 0:
                print(f"    ...{i}/{len(orfas)}")
            po = await _get(cli, toks[o["conta"]], o["orfa_rid"])
            pp = await _get(cli, toks[o["conta"]], o["vinculado_a"])
            if "_erro" in po or "_erro" in pp:
                continue
            co, cp = _campos(po), _campos(pp)
            exclusivo = {k: sorted(co[k] - cp[k]) for k in co if co[k] - cp[k]}
            no = ((po.get("names") or [{}])[0].get("displayName")) or ""
            np_ = ((pp.get("names") or [{}])[0].get("displayName")) or ""
            to, tp = vg.tokens(no), vg.tokens(np_)
            forte = (not no.strip()) or (to and tp and (to <= tp or tp <= to or len(to & tp) >= 2))
            item = {**o, "nome_orfa": no, "nome_principal": np_, "exclusivo": exclusivo}
            if not forte:
                rever.append(item)
            elif exclusivo:
                migrar.append(item)
            # sem exclusivo e com parentesco forte: já foi apagado, não vem pra tela
    for l in (migrar, rever):
        l.sort(key=lambda x: -x["msgs"])
    return migrar, rever


JS = r"""
<script>
const CHAVE="orfas-decisao";
const salvo=JSON.parse(localStorage.getItem(CHAVE)||"{}");
function pintar(){
  let n=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.rid];
    p.querySelectorAll(".op").forEach(b=>b.setAttribute("aria-pressed",String(e===b.dataset.acao)));
    p.classList.toggle("decidido",!!e);
    if(e)n++;
  });
  document.getElementById("n").textContent=n;
}
document.querySelectorAll(".op").forEach(b=>b.addEventListener("click",()=>{
  const p=b.closest(".par"),k=p.dataset.rid;
  if(salvo[k]===b.dataset.acao)delete salvo[k]; else salvo[k]=b.dataset.acao;
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
}));
document.getElementById("todos-migrar").addEventListener("click",()=>{
  document.querySelectorAll(".par[data-bloco='migrar']").forEach(p=>salvo[p.dataset.rid]="migrar");
  localStorage.setItem(CHAVE,JSON.stringify(salvo));pintar();
});
document.getElementById("limpar").addEventListener("click",()=>{
  Object.keys(salvo).forEach(k=>delete salvo[k]);
  localStorage.removeItem(CHAVE);
  document.getElementById("saida").style.display="none";pintar();
});
document.getElementById("copiar").addEventListener("click",async()=>{
  const L=["ORFAS — decisao (cole no Claude)"];let c=0;
  document.querySelectorAll(".par").forEach(p=>{
    const e=salvo[p.dataset.rid];if(!e)return;c++;
    L.push(p.dataset.rid+" "+e.toUpperCase()+" :: #"+p.dataset.cid+" "+p.dataset.nome);
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

EXTRA_CSS = """
.bloco{margin:2rem 0 .8rem}
.bloco h2{font-family:var(--serif);font-weight:400;font-size:1.15rem;margin:0 0 .2rem}
.bloco p{font-size:.84rem;color:var(--soft);margin:.2rem 0 1rem;max-width:64ch}
.ganho{font-family:var(--mono);font-size:.68rem;color:var(--brass);margin-top:.3rem;
display:block;white-space:normal;line-height:1.5}
.par{grid-template-columns:1fr auto}
.acoes{display:flex;gap:.5rem;flex-wrap:wrap}
.op{flex:0 1 auto;min-width:9rem}
"""


def _linha(d, bloco):
    ganho = " · ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                       for k, v in (d.get("exclusivo") or {}).items())
    if bloco == "migrar":
        acoes = ('<button class="op" data-acao="migrar"><small>fundir</small>migra e apaga a repetida</button>'
                 '<button class="op" data-acao="manter"><small>deixa</small>não mexe</button>')
    else:
        acoes = ('<button class="op" data-acao="migrar"><small>mesma pessoa</small>migra e apaga</button>'
                 '<button class="op" data-acao="diferentes"><small>outra pessoa</small>registra e nunca mais pergunta</button>')
    return f"""<div class="par" data-rid="{esc(d['orfa_rid'])}" data-bloco="{bloco}"
     data-cid="{d['id']}" data-nome="{esc(d['nome_orfa'] or '(sem nome)')}">
  <div>
    <b>{esc(d['nome_orfa'] or '(ficha sem nome)')}</b> → mantém <b>{esc(d['nome_principal'])}</b>
    {f'<span class="ganho">só na repetida → {esc(ganho)}</span>' if ganho else ''}
  </div>
  <div class="meta">#{d['id']}<br>{d['msgs']} msg{'s' if d['msgs'] != 1 else ''}</div>
  <div class="acoes" style="grid-column:1/-1">{acoes}</div>
</div>"""


def render(migrar, rever):
    from datetime import datetime
    ago = datetime.now()
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fichas repetidas no Google</title><style>{tela.CSS}{EXTRA_CSS}</style></head>
<body><div class="wrap">
<p class="eyebrow">INTEL · agenda do Google</p>
<h1>Fichas repetidas — o que fazer com cada uma</h1>
<div class="filete"></div>
<p class="lead">Já apaguei <b>37</b> em que a ficha boa tinha tudo — não havia o que decidir.
Estas <b>{len(migrar) + len(rever)}</b> sobraram porque alguma coisa se perde ou porque o nome não
fecha. <b>Nada aqui é mexido pela página</b>: ela só registra sua escolha pra você copiar no fim.</p>

<div class="bloco">
  <h2>🔁 {len(migrar)} têm dado que a ficha boa não tem</h2>
  <p>Apagar direto perderia o e-mail ou o telefone que só existe na repetida. "Fundir" copia
  esse dado pra ficha boa <i>e só então</i> apaga a outra.</p>
</div>
{''.join(_linha(d, 'migrar') for d in migrar)}

<div class="bloco">
  <h2>🟡 {len(rever)} podem não ser a mesma pessoa</h2>
  <p>O nome não fecha o suficiente pra eu decidir sozinho. Se forem pessoas diferentes,
  <b>a escolha fica registrada</b> e o par nunca mais aparece aqui.</p>
</div>
{''.join(_linha(d, 'rever') for d in rever)}

<p class="nota">Gerado {ago:%d/%m/%Y %H:%M}. O backup íntegro das duas fichas de cada par fica
guardado antes de qualquer exclusão.</p>
<div class="barra">
  <span><b id="n">0</b> decidido(s) de {len(migrar) + len(rever)}</span>
  <div><button class="ghost" id="limpar">Limpar</button>
  <button class="ghost" id="todos-migrar">Fundir todos os {len(migrar)} do 1º bloco</button>
  <button id="copiar">Copiar para o Claude</button></div>
</div>
<textarea id="saida" rows="6" readonly></textarea>
</div>{JS}</body></html>"""



async def aplicar(texto: str, dry: bool = True) -> int:
    """Executa o que o Renato decidiu na tela.

    Três destinos, e a ORDEM importa em um deles: `migrar` copia o campo
    exclusivo pra ficha boa **e só então** apaga a repetida. Apagar primeiro e
    copiar depois perderia o dado se a segunda chamada falhasse — o mesmo formato
    de dano do merge em CASCADE ([[feedback_cascade_chain_warning]]).

    `diferentes` não é um "não" solto: vira linha em `contact_nao_fundir`
    (migration 077), pra decisão tomada não voltar a ser perguntada.

    Guarda o estado íntegro das duas fichas ANTES de tocar em qualquer uma.
    """
    import httpx
    import integrations.google_contacts as gc
    from database import get_connection

    toks_backup = {}
    decisoes = {}
    for l in texto.splitlines():
        m = re.match(r"^(\S+)\s+(MIGRAR|MANTER|DIFERENTES)\s*::\s*#(\d+)\s*(.*)$", l.strip())
        if m:
            decisoes[m.group(1)] = {"acao": m.group(2).lower(),
                                    "contato_id": int(m.group(3)), "nome": m.group(4)}
    if not decisoes:
        sys.exit("nada reconhecido — esperado 'cXXXX MIGRAR :: #123 Nome'")

    migrar, rever = await levantar(cache_h=6)
    porrid = {d["orfa_rid"]: d for d in (migrar + rever)}

    # BACKUP ANTES DE TOCAR — e não depois. O DELETE do Google é definitivo; sem
    # o estado íntegro das duas pontas guardado ANTES, "reverter" vira reconstruir
    # de memória. O docstring desta função prometia isso desde o primeiro dia e o
    # código não fazia: promessa em comentário que o código não cumpre é a mesma
    # falha calada que o `_para_o_banco` e o "Force banco local" já custaram.
    if not dry:
        import httpx as _hx
        antes = []
        async with _hx.AsyncClient(timeout=30) as _c:
            for rid, dec in decisoes.items():
                d = porrid.get(rid)
                if not d or dec["acao"] == "manter":
                    continue
                tk = toks_backup.setdefault(d["conta"], await gc.get_valid_token(d["conta"]))
                antes.append({"orfa_rid": rid, "principal_rid": d["vinculado_a"],
                              "conta": d["conta"], "acao": dec["acao"],
                              "orfa": await _get(_c, tk, rid),
                              "principal": await _get(_c, tk, d["vinculado_a"])})
        destino_bk = os.path.expanduser("~/.cos-agent/orfas_backup_antes.json")
        os.makedirs(os.path.dirname(destino_bk), exist_ok=True)
        json.dump(antes, open(destino_bk, "w"), ensure_ascii=False, indent=1)
        print(f"  🔒 backup de {len(antes)} pares em {destino_bk}")

    conn = get_connection()
    cur = conn.cursor()
    toks, log = {}, []
    feitos = {"migrado": 0, "apagado": 0, "registrado": 0, "mantido": 0, "erro": 0}

    async with httpx.AsyncClient(timeout=30) as cli:
        for rid, dec in decisoes.items():
            d = porrid.get(rid)
            if not d:
                print(f"  ⚠️  {rid} não está mais entre as órfãs — pulado")
                continue
            if dec["acao"] == "manter":
                feitos["mantido"] += 1
                continue

            if dec["acao"] == "diferentes":
                a_, b_ = sorted((d["id"], dec["contato_id"])) if dec["contato_id"] != d["id"] else (None, None)
                # A tela decide sobre FICHAS DO GOOGLE; o par do INTEL só existe
                # quando os dois lados têm contato próprio. Sem isso, registrar
                # em contact_nao_fundir seria inventar um par.
                print(f"  [{'dry' if dry else 'APPLY'}] diferentes: \"{d['nome_orfa'][:30]}\" "
                      f"≠ \"{d['nome_principal'][:30]}\" — fica no Google, sem fundir")
                if not dry:
                    cur.execute(
                        """INSERT INTO contact_nao_fundir (contact_a, contact_b, motivo)
                           SELECT %s, %s, %s WHERE %s IS NOT NULL AND %s IS NOT NULL
                           ON CONFLICT DO NOTHING""",
                        (a_, b_, f"decidido na tela de fichas repetidas: "
                                 f"{d['nome_orfa']} nao e {d['nome_principal']}", a_, b_))
                    conn.commit()
                feitos["registrado"] += 1
                continue

            # MIGRAR: copia o exclusivo pra principal, depois apaga a repetida
            tok = toks.setdefault(d["conta"], await gc.get_valid_token(d["conta"]))
            exc = d.get("exclusivo") or {}
            nome_intel = (d.get("intel") or "").strip()
            vai_renomear = (nome_intel
                            and nome_intel.lower() != (d["nome_principal"] or "").strip().lower())
            print(f"  [{'dry' if dry else 'APPLY'}] migrar {json.dumps(exc, ensure_ascii=False)[:70]} "
                  f"→ \"{d['nome_principal'][:28]}\" e apagar \"{d['nome_orfa'][:28]}\""
                  + (f"  ↳ e renomeia pra \"{nome_intel[:34]}\"" if vai_renomear else ""))
            if dry:
                continue

            principal = await _get(cli, tok, d["vinculado_a"])
            if "_erro" in principal:
                feitos["erro"] += 1
                continue
            corpo = {"etag": principal.get("etag")}
            campos_upd = []
            if exc.get("emails"):
                corpo["emailAddresses"] = (principal.get("emailAddresses") or []) + \
                    [{"value": e} for e in exc["emails"]]
                campos_upd.append("emailAddresses")
            if exc.get("tels"):
                atuais = principal.get("phoneNumbers") or []
                ja_tem = {t.get("value") for t in atuais}
                # ⚠️ DOIS CUIDADOS, os dois aprendidos apagando de verdade em
                # 22/08. (1) checar o erro do GET: sem isso um 429 devolvia
                # {"_erro": ...} e `da_orfa` virava lista vazia. (2) mandar SÓ
                # value/type: o objeto que a API devolve traz
                # `metadata.source.id` da ficha de ORIGEM, e o Google ignora **em
                # silêncio** um campo cuja origem aponta pra outra ficha — PATCH
                # 200, log dizendo "migrado", telefone não migrado e a órfã
                # apagada logo depois. Foi esta a causa de 53 telefones que não
                # chegaram; o 429 foi a hipótese errada que testei primeiro.
                da_orfa_full = await _get(cli, tok, rid)
                if "_erro" in da_orfa_full:
                    print(f"        ❌ não li a órfã ({da_orfa_full['_erro']}) — NÃO apago")
                    feitos["erro"] += 1
                    continue
                da_orfa = da_orfa_full.get("phoneNumbers") or []
                faltando = [{"value": t.get("value"), "type": t.get("type", "other")}
                            for t in da_orfa
                            if vg.chave(t.get("value")) in set(exc["tels"])
                            and t.get("value") not in ja_tem]
                if not faltando:
                    print(f"        ⚠️  esperava migrar {exc['tels']} e não achei na órfã — NÃO apago")
                    feitos["erro"] += 1
                    continue
                corpo["phoneNumbers"] = atuais + faltando
                campos_upd.append("phoneNumbers")
            if exc.get("orgs"):
                corpo["organizations"] = (principal.get("organizations") or []) + \
                    [{"name": o} for o in exc["orgs"]]
                campos_upd.append("organizations")
            if exc.get("urls"):
                corpo["urls"] = (principal.get("urls") or []) + [{"value": u} for u in exc["urls"]]
                campos_upd.append("urls")

            # O NOME DA SOBREVIVENTE SEGUE O INTEL, e isso não é capricho: em
            # #4611 a ficha que fica chama "Perrotti e Barrueco" e a que sai
            # chama "Paulo Salvador Ribeiro Perrotti" — o nome que o Renato
            # escolheu na tela de 18/08. Fundir sem isto DESFARIA a decisão dele
            # da véspera, e ninguém ligaria uma coisa à outra depois.
            if vai_renomear:
                partes = nome_intel.split()
                corpo["names"] = [{"givenName": partes[0],
                                   "familyName": " ".join(partes[1:]) if len(partes) > 1 else ""}]
                campos_upd.append("names")

            if campos_upd:
                r = await cli.patch(
                    f"https://people.googleapis.com/v1/people/{d['vinculado_a']}:updateContact",
                    headers={"Authorization": f"Bearer {tok}"},
                    params={"updatePersonFields": ",".join(campos_upd)}, json=corpo)
                if r.status_code != 200:
                    print(f"        ❌ migração falhou ({r.status_code}) — NÃO apago a origem")
                    feitos["erro"] += 1
                    continue
                feitos["migrado"] += 1

            r = await cli.delete(f"https://people.googleapis.com/v1/people/{rid}:deleteContact",
                                 headers={"Authorization": f"Bearer {tok}"})
            if r.status_code in (200, 204):
                feitos["apagado"] += 1
            else:
                feitos["erro"] += 1
            log.append({"rid": rid, "nome": d["nome_orfa"], "migrado": campos_upd,
                        "http_delete": r.status_code})
            await asyncio.sleep(0.4)

    if not dry:
        destino = os.path.expanduser("~/.cos-agent/orfas_aplicadas.json")
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        json.dump(log, open(destino, "w"), ensure_ascii=False, indent=1)
        print(f"\n  → log em {destino}")
    # No dry-run o contador não pode falar no passado: "registrado: 1" sem ter
    # registrado nada é relatório que mente, ainda que pouco.
    print(f"\n  {'planejado' if dry else 'aplicado'}: {feitos}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=float, default=0, metavar="H")
    ap.add_argument("--gravar", metavar="ARQ")
    ap.add_argument("--apply", action="store_true",
                    help="sem isto, --gravar só mostra o que faria")
    a = ap.parse_args()

    if a.gravar:
        texto = sys.stdin.read() if a.gravar == "-" else open(a.gravar).read()
        return asyncio.run(aplicar(texto, dry=not a.apply))

    migrar, rever = asyncio.run(levantar(a.cache))
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    open(SAIDA, "w").write(render(migrar, rever))
    print(f"→ {SAIDA}")
    print(f"   🔁 {len(migrar)} migrar-e-apagar · 🟡 {len(rever)} rever")
    subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main())
