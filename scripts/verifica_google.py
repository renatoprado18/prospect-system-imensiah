#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Verificador de divergência INTEL ⇄ Google — torna o passivo MENSURÁVEL.

POR QUE EXISTE. O board pedia este script em dois lugares, pela mesma razão:
*"é o que tornaria o passivo mensurável"* e *"sem auditoria de merge, não há como
saber quais fichas ficaram órfãs"*. Enquanto ele não existiu, o tamanho do
problema vinha de números escritos à mão em memo — e envelheceram todos: "327
pessoas" no docstring do `nomes.py` virou "260 nomes divergentes" no board, que
na re-medição de 18/08/26 eram **45**; "199 fichas que nunca foram ao Google"
eram **119**, nenhuma no círculo ≤3.

Número de banco em memória vira diagnóstico errado. Quem conta é este script,
na hora ([[feedback_numero_herdado_medir_a_regua]]).

O QUE ELE SEPARA — e por que a separação é o ponto. A régua antiga chamava tudo
de "divergência" e empilhava 460 decisões pro Renato quando 11 tinham histórico:

  🟠 divergência real ....... nomes diferentes, uma ficha de cada lado. Decisão
                             dele: A ou B. É o único balde que vai pra tela.
  🔵 duplicata no Google .... a MESMA pessoa cadastrada 2× na MESMA conta, com
                             grafias diferentes. Rescaldo do bug do `etag`
                             (`712c9c9`), que criava ficha nova a cada tentativa
                             de corrigir. Limpeza lá, não escolha aqui.
  🟣 telefone compartilhado . `Copersucar` no INTEL e `Sidnei Rosa` no Google, o
                             mesmo número: a empresa e quem atende. Não é
                             defeito, e some da fila.
  🔴 órfã de merge .......... ficha viva no Google cujo id não é apontado por
                             NENHUM contato do INTEL, num telefone cujo contato
                             aponta pra outro id. É a assinatura de um merge que
                             apagou no INTEL e não propagou — o passivo que o
                             `scripts/merge_contatos.py` ainda gera ao rodar por
                             script (a rota e o `merge_duplicates.py` já
                             propagam desde o `5b2a7c7`).
  ⚪ ausente no Google ...... contato com telefone e sem contraparte lá.

Órfã é detectada PROSPECTIVAMENTE: o passivo antigo é irrecuperável (a ficha
some do INTEL no merge e leva o vínculo junto), mas daqui pra frente cada merge
não-propagado aparece aqui na rodada seguinte.

Uso:  ./verifica_google.py                 # varre as duas contas e reporta
      ./verifica_google.py --quiet         # só fala se houver o que decidir
      ./verifica_google.py --cache 6       # reusa varredura de até 6h atrás
      ./verifica_google.py --json out.json # despeja os baldes pra outro script
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

CACHE = os.path.expanduser("~/.cos-agent/google_contatos_cache.json")
CONTAS = ("renato@almeida-prado.com", "renato.almeida.prado@gmail.com")

# Partículas e títulos não distinguem pessoa: "Dr. Gregorio" e "Gregorio" são o
# mesmo sujeito, e "de/da/dos" aparece em metade da base.
TITULOS = {"dr", "dra", "prof", "sr", "sra", "eng", "phd", "jr", "filho", "neto",
           "de", "da", "do", "dos", "das", "e", "the", "of", "ltda", "com", "br"}


def chave(v):
    """Últimos 8 dígitos: imune a +55, DDI, DDD e ao 9 móvel.

    Ver [[reference_telefone_br_normalizacao]] — comparar telefone formatado é
    o erro que fez o hífen do Google esconder 40% da base em 28/07.
    """
    d = re.sub(r"\D", "", v or "")
    return d[-8:] if len(d) >= 8 else ""


def norm(n):
    return (n or "").strip().lower()


def tokens(n):
    s = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z]+", s) if len(t) >= 3 and t not in TITULOS}


async def varrer_google(max_idade_h=0):
    """Fichas das duas contas. O cache existe porque são ~13 mil fichas e a
    varredura completa leva minutos — mas ele NUNCA é o default: um verificador
    que lê cache velho por conta própria certifica um estado que já mudou."""
    if max_idade_h and os.path.exists(CACHE):
        idade_h = (time.time() - os.path.getmtime(CACHE)) / 3600
        if idade_h <= max_idade_h:
            print(f"  (cache de {idade_h:.1f}h atrás)")
            return json.load(open(CACHE))

    import integrations.google_contacts as gc
    fichas = []
    for conta in CONTAS:
        t = await gc.get_valid_token(conta)
        if not t:
            # Sem uma das contas o diagnóstico sai pela metade e "0 divergências"
            # seria mentira. Abortar é a única resposta honesta.
            raise RuntimeError(f"sem token para {conta} — reconecte antes de medir")
        pessoas = await gc.fetch_all_contacts(t)
        print(f"  {conta}: {len(pessoas)} fichas")
        for p in pessoas:
            fichas.append({
                "nome": ((p.get("names") or [{}])[0].get("displayName")) or "",
                "rid": (p.get("resourceName") or "").replace("people/", ""),
                "conta": conta,
                "tels": [pn.get("value") for pn in (p.get("phoneNumbers") or [])],
            })
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(fichas, open(CACHE, "w"))
    return fichas


def classificar(fichas_google, contatos, nao_fundir=frozenset(), mantidas=frozenset()):
    por_tel = defaultdict(list)
    for p in fichas_google:
        for tel in p["tels"]:
            k = chave(tel)
            if k:
                por_tel[k].append(p)

    rid_do_intel = {c["google_contact_id"] for c in contatos if c["google_contact_id"]}
    conta_do_rid = {p["rid"]: p["conta"] for p in fichas_google}
    por_rid_nome = {p["rid"]: p["nome"] for p in fichas_google}

    # Duplicata DENTRO do INTEL: dois contatos distintos no mesmo telefone. Saiu
    # da primeira rodada deste script — "Leo" #16072 e "Leo" #21985 apareciam
    # como duas divergências contra o mesmo "Léo" do Google, e nenhuma das duas
    # era decisão de nome: era ficha repetida aqui.
    # Indexado por id dentro de cada chave: um contato com "+5511988887777" e
    # "11988887777" cadastrados tem DOIS telefones que normalizam pro mesmo
    # sufixo, e apareceria duplicado consigo mesmo (#23360+#23360).
    intel_por_tel = defaultdict(dict)
    for c in contatos:
        for t in (c["telefones"] or []):
            if isinstance(t, dict) and chave(t.get("number")):
                intel_por_tel[chave(t.get("number"))][c["id"]] = c

    baldes = {"divergencia": [], "duplicata": [], "compartilhado": [],
              "orfa": [], "ausente": [], "dupe_intel": [],
              "tel_compartilhado_intel": []}
    vistos_dupe = set()
    for k, por_id in intel_por_tel.items():
        if len(por_id) < 2:
            continue
        cs = list(por_id.values())
        ids = tuple(sorted(por_id))
        if ids in vistos_dupe:
            continue
        vistos_dupe.add(ids)
        # Par já decidido não volta a ser perguntado. Douglas Bassi e Orestes
        # (#1360/#4376) colidem pelo fixo da Virtus, e o Renato já tinha dito
        # isso ANTES — só não havia onde escrever, então a pergunta voltava a
        # cada rodada. Ver migration 077.
        if len(ids) == 2 and tuple(sorted(ids)) in nao_fundir:
            continue
        # Duas naturezas no mesmo balde, de novo: fichas REPETIDAS da mesma
        # pessoa (Bettina Berman 3×) e telefone de empresa que várias pessoas
        # atendem (Carla / Vania Leister). Só o primeiro grupo é fundível; o
        # segundo pede o olho de quem conhece a relação — e misturá-los faria a
        # lista pedir julgamento onde bastava limpeza, que é como os "460 nomes"
        # viraram fila do Renato.
        ts = [tokens(c["nome"]) for c in cs]
        mesma_pessoa = any(ts[i] & ts[j]
                           for i in range(len(ts)) for j in range(i + 1, len(ts)))
        baldes["dupe_intel" if mesma_pessoa else "tel_compartilhado_intel"].append({
            "ids": list(ids), "nomes": [c["nome"] for c in cs],
            "msgs": max(c["msgs"] for c in cs)})
    casados = 0

    for f in contatos:
        tels = [t.get("number") for t in (f["telefones"] or []) if isinstance(t, dict)]
        ks = [chave(t) for t in tels if chave(t)]
        entradas = next((por_tel[k] for k in ks if k in por_tel), None)
        if not entradas:
            baldes["ausente"].append({"id": f["id"], "intel": f["nome"], "msgs": f["msgs"]})
            continue
        casados += 1

        # Órfã: ficha viva no Google que NINGUÉM no INTEL aponta, no telefone de
        # um contato que aponta pra outra — SÓ VALE DENTRO DA MESMA CONTA.
        #
        # ⚠️ A primeira versão disto acusou 3.949 órfãs, e eram falsas. A mesma
        # pessoa tem resourceName DIFERENTE em cada conta Google e
        # `contacts.google_contact_id` é escalar: guarda um dos dois. A ficha da
        # outra conta é, por construção, "não apontada por ninguém" — legítima e
        # inevitável. Comparar entre contas transformava a base inteira em
        # passivo. Restrito à conta do id vinculado, sobra o que é de fato sobra
        # de merge.
        conta_vinculada = conta_do_rid.get(f["google_contact_id"]) if f["google_contact_id"] else None
        if conta_vinculada:
            principal = por_rid_nome.get(f["google_contact_id"], "")
            for e in entradas:
                if not (e["conta"] == conta_vinculada
                        and e["rid"] != f["google_contact_id"]
                        and e["rid"] not in rid_do_intel):
                    continue
                # ⚠️ NÃO BASTA COMPARTILHAR O TELEFONE (medido 22/08/26, antes de
                # apagar). Sem exigir parentesco de nome, este balde acusava 231
                # "órfãs" — e a maioria era COLEGA DE TRABALHO no telefone da
                # empresa: a ficha #697 ("Vanessa Ikeno") arrastava Magda
                # Glasser, Lucca Najar, Camila Lisbôa e mais quatro, todos com
                # e-mail @carambola.com.vc; a #2856 arrastava meia Amcham.
                # Apagar teria destruído dezenas de contatos reais da agenda.
                #
                # É a MESMA premissa falsa que já custou dois baldes hoje
                # ("mesmo telefone = mesma pessoa"), e o caso Douglas × Orestes
                # deu o nome dela. Órfã de merge é ficha REPETIDA: o nome tem que
                # ter parentesco com o da principal.
                if not (tokens(e["nome"]) & tokens(principal)) and (e["nome"] or "").strip():
                    continue
                # decisão registrada não volta a ser perguntada (078)
                if (f["id"], e["rid"]) in mantidas:
                    continue
                baldes["orfa"].append({
                    "id": f["id"], "intel": f["nome"], "msgs": f["msgs"],
                    "orfa_nome": e["nome"], "orfa_rid": e["rid"], "conta": e["conta"],
                    "vinculado_a": f["google_contact_id"]})

        nomes = {norm(p["nome"]) for p in entradas}
        por_conta = defaultdict(set)
        for p in entradas:
            por_conta[p["conta"]].add(norm(p["nome"]))
        colisao = next((v for v in por_conta.values() if len(v) > 1), None)

        if norm(f["nome"]) in nomes and not colisao:
            continue

        item = {"id": f["id"], "intel": f["nome"], "msgs": f["msgs"],
                "google": sorted({p["nome"] for p in entradas}),
                "intel_bate": norm(f["nome"]) in nomes}
        if colisao:
            ts = [tokens(n) for n in colisao]
            mesma = any(ts[i] & ts[j] for i in range(len(ts)) for j in range(i + 1, len(ts)))
            baldes["duplicata" if mesma else "compartilhado"].append(item)
        else:
            baldes["divergencia"].append(item)

    for l in baldes.values():
        l.sort(key=lambda x: -x["msgs"])
    return baldes, casados




def _fichas_mantidas(cur) -> set:
    """Fichas do Google que o Renato mandou MANTER (migration 078).

    Par (contact_id, google_rid). Sem isto, as três que ele decidiu em 22/08
    voltavam a aparecer como órfã em toda rodada — a decisão existia e o script
    não sabia lê-la. Tabela ausente devolve vazio: alvo sem a 078 mostra tudo, que
    é o comportamento antigo, e não silencia nada por engano.
    """
    try:
        cur.execute("SELECT contact_id, google_rid FROM google_nao_fundir")
        return {(r["contact_id"], r["google_rid"]) for r in cur.fetchall()}
    except Exception:
        return set()


def _pares_decididos(cur) -> set:
    """Pares que o Renato já decidiu que NÃO são a mesma pessoa (migration 077).

    Sem isto o verificador reapresenta a mesma dupla a cada rodada, e cada
    reapresentação gasta a atenção dele de novo — foi o que aconteceu com Douglas
    Bassi × Orestes, decidido uma vez e perguntado outra. Tabela ausente devolve
    conjunto vazio: alvo sem a 077 mostra tudo, o que é o comportamento antigo, e
    não silencia nada por engano.
    """
    try:
        cur.execute("SELECT contact_a, contact_b FROM contact_nao_fundir")
        return {(r["contact_a"], r["contact_b"]) for r in cur.fetchall()}
    except Exception:
        return set()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só fala se houver decisão pendente")
    ap.add_argument("--cache", type=float, default=0, metavar="H",
                    help="reusa a varredura se tiver menos de H horas")
    ap.add_argument("--json", metavar="ARQ", help="despeja os baldes")
    a = ap.parse_args()

    from database import get_connection

    fichas = await varrer_google(a.cache)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""SELECT c.id, c.nome, c.telefones, c.google_contact_id,
                          (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs
                     FROM contacts c
                    WHERE c.telefones IS NOT NULL AND c.telefones::text <> '[]'""")
    contatos = [dict(r) for r in cur.fetchall()]

    nao_fundir = _pares_decididos(cur)
    b, casados = classificar(fichas, contatos, nao_fundir, _fichas_mantidas(cur))
    decisao = len(b["divergencia"])
    com_hist = sum(1 for d in b["divergencia"] if d["msgs"] > 0)

    if a.quiet and not decisao and not b["orfa"]:
        return 0

    print(f"\n╔═ INTEL ⇄ GOOGLE ═╗")
    print(f"  {len(fichas)} fichas no Google · {len(contatos)} contatos com telefone · {casados} casados")
    print(f"  {'─'*64}")
    print(f"  🟠 divergência real (decisão do Renato) ...... {decisao:5}  ({com_hist} com histórico)")
    print(f"  🔵 duplicata no Google (limpeza lá) .......... {len(b['duplicata']):5}"
          f"  ({sum(1 for d in b['duplicata'] if d['intel_bate'])} com o INTEL já certo)")
    print(f"  🟣 telefone compartilhado (não é defeito) .... {len(b['compartilhado']):5}")
    print(f"  🔴 órfã: ficha REPETIDA não apontada .......... {len(b['orfa']):5}")
    print(f"  🟤 ficha REPETIDA no INTEL (mesma pessoa) ... {len(b['dupe_intel']):5}")
    print(f"  ⚫ telefone de empresa, pessoas diferentes ... {len(b['tel_compartilhado_intel']):5}  (não é defeito)")
    print(f"  ⚪ ausente no Google ......................... {len(b['ausente']):5}")
    print(f"  {'─'*64}")
    print(f"  ⚠️  🔴 e 🔵 se SOBREPÕEM — não somar. São a mesma doença (ficha")
    print(f"     repetida no Google): 🔵 conta as que têm grafias diferentes,")
    print(f"     🔴 as que ninguém aponta, inclusive as de nome idêntico.")

    if b["orfa"]:
        print("\n  🔴 ÓRFÃS — ficha no Google que ninguém no INTEL aponta:")
        for o in b["orfa"][:15]:
            print(f"     #{o['id']:6} INTEL \"{o['intel'][:28]}\" → órfã \"{o['orfa_nome'][:28]}\" ({o['orfa_rid'][:12]}…)")
        print("     Some com merge pela TELA ou `merge_duplicates.py`; o "
              "`merge_contatos.py --apply` é quem ainda deixa rastro.")

    if b["dupe_intel"]:
        print("\n  🟤 FICHA REPETIDA no INTEL — mesma pessoa, mais de um cadastro:")
        for d in sorted(b["dupe_intel"], key=lambda x: -x["msgs"])[:10]:
            print(f"     {'+'.join('#'+str(i) for i in d['ids'])}  {d['msgs']:4} msgs  "
                  f"\"{' / '.join(n or '(sem nome)' for n in d['nomes'])[:52]}\"")
        print("     Fundir pela TELA (propaga ao Google) — ver [[reference_contact_merge_fix]].")

    if decisao:
        print(f"\n  🟠 A DECIDIR (top 10 por histórico) — a tela é `scripts/nomes.py`:")
        for d in b["divergencia"][:10]:
            print(f"     #{d['id']:6} {d['msgs']:4} msgs  INTEL \"{d['intel'][:30]}\" × Google \"{' / '.join(d['google'])[:30]}\"")

    if a.json:
        json.dump(b, open(a.json, "w"), ensure_ascii=False, indent=1)
        print(f"\n  → {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
