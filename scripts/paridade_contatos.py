#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Paridade INTEL ⇄ Google Contacts: o que existe de um lado e não do outro.

PERGUNTA QUE ORIGINOU (Renato, 07/08/2026): "como garantir que tudo que tem no
INTEL tem no Google e vice-versa?"

A resposta honesta é que não se garante por decreto — mede-se, e mede-se pelos
DOIS lados. O sync sempre foi de mão única (Google → INTEL), então "está tudo
sincronizado" nunca foi verificado: era suposição.

O QUE ELE MEDE, e cada linha é uma classe de defeito diferente:

  · SÓ NO INTEL   — ficha com telefone que não subiu. É o caso do Gui Nosé: o
                    número aparece sem nome no WhatsApp dele porque o celular lê
                    o Google, não o INTEL.
  · SÓ NO GOOGLE  — telefone na agenda sem ficha aqui. A camada é cega a essa
                    pessoa: mensagem dela cai como "Desconhecido" e não cruza
                    com nada.
  · ID QUEBRADO   — a ficha guarda um `google_contact_id` que não existe mais na
                    agenda. Descoberto em 07/08 na Daniela: o id apontava pro
                    vazio, então o sistema achava que estava vinculada e não
                    estava. É o pior dos três, porque parece resolvido.
  · NOME DIVERGE  — os dois lados têm a pessoa, com nomes diferentes.

Casa por TELEFONE CANÔNICO (últimos 8 dígitos — sobrevive a DDI, DDD e ao nono
dígito), nunca por nome: "Ana Maria Piccino" existe quatro vezes no CRM
([[reference_telefone_br_normalizacao]]).

READ-ONLY. Não escreve em lado nenhum: o conserto de cada classe é decisão
diferente (subir, baixar, religar, renomear), e o número vem primeiro.

Uso:  ./paridade_contatos.py [--conta renato@almeida-prado.com] [--limite 15]
"""
import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, "/Users/rap/prospect-system/app")

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def chave(v):
    """Últimos 8 dígitos — MAS só de número que tenha DDD.

    Exigir 10+ dígitos é o outro lado do conserto de 07/08: telefone sem DDD
    ("7678-5627") é real e fica na ficha, mas casar por ele junta gente de
    cidades diferentes que por acaso têm o mesmo número local. Dado incompleto
    serve pra ligar pra pessoa, não pra afirmar que duas fichas são a mesma.
    """
    d = re.sub(r"\D", "", v or "")
    return d[-8:] if len(d) >= 10 else ""


async def medir(contas: list, limite: int):
    """AS DUAS CONTAS, sempre.

    ⚠️ A primeira versão deste script comparava contra UMA conta e acusou **3.598
    ids quebrados** — 39% das fichas. Era falso: o Renato tem duas agendas
    Google (profissional 5.729 pessoas · pessoal 7.350), e o `google_contact_id`
    pode ser de qualquer uma. Medido contra as duas, o número real é **104**.
    Um verificador que olha metade da fonte não mede — inventa
    ([[feedback_mecanismo_que_nao_mede_o_denominador]]).
    """
    import integrations.google_contacts as gc

    pessoas = []
    for conta in contas:
        token = await gc.get_valid_token(conta)
        if not token:
            print(f"  ⚠️ sem token para {conta} — ABORTANDO: medir contra parte "
                  f"da fonte produz número inventado")
            return 1
        p = await gc.fetch_all_contacts(token)
        print(f"  {conta}: {len(p)} pessoas")
        pessoas.extend(p)

    g_por_tel, g_por_id = {}, {}
    for p in pessoas:
        rid = (p.get("resourceName") or "").replace("people/", "")
        nome = ((p.get("names") or [{}])[0].get("displayName")) or ""
        g_por_id[rid] = nome
        for pn in (p.get("phoneNumbers") or []):
            k = chave(pn.get("value"))
            if k:
                g_por_tel.setdefault(k, (rid, nome))

    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT id, nome, telefones, google_contact_id
                     FROM contacts
                    WHERE telefones IS NOT NULL AND telefones::text <> '[]'""")
    fichas = cur.fetchall()

    so_intel, id_quebrado, nome_diverge, casados = [], [], [], set()
    for f in fichas:
        tels = [t.get("number") for t in (f["telefones"] or []) if isinstance(t, dict)]
        ks = [chave(t) for t in tels if chave(t)]
        achado = next((g_por_tel[k] for k in ks if k in g_por_tel), None)
        if achado:
            casados.update(ks)
            rid, nome_g = achado
            if (f["nome"] or "").strip().lower() != nome_g.strip().lower():
                nome_diverge.append((f["id"], f["nome"], nome_g))
        else:
            so_intel.append((f["id"], f["nome"], tels[0] if tels else ""))
        gid = f["google_contact_id"]
        if gid and gid not in g_por_id:
            id_quebrado.append((f["id"], f["nome"], gid))

    so_google = [(rid, nome, k) for k, (rid, nome) in g_por_tel.items() if k not in casados]

    print("\n╔═ PARIDADE INTEL ⇄ GOOGLE CONTACTS ═╗")
    print(f"  INTEL: {len(fichas)} fichas com telefone · GOOGLE: {len(g_por_tel)} telefones "
          f"({len(pessoas)} pessoas nas {len(contas)} contas)\n")
    print(f"  🔴 só no INTEL (não subiram) ..... {len(so_intel)}")
    print(f"  🔴 só no GOOGLE (sem ficha) ...... {len(so_google)}")
    print(f"  🟡 id quebrado (aponta pro nada) . {len(id_quebrado)}")
    print(f"  🟡 nome diverge .................. {len(nome_diverge)}")

    def amostra(titulo, itens, fmt):
        if not itens:
            return
        print(f"\n  {titulo} (amostra de {min(limite, len(itens))} de {len(itens)}):")
        for x in itens[:limite]:
            print("     " + fmt(x))

    amostra("só no INTEL", so_intel, lambda x: f"#{x[0]:<7} {str(x[1])[:38]:40s} {x[2]}")
    amostra("só no GOOGLE", so_google, lambda x: f"{x[0][:20]:22s} {str(x[1])[:38]:40s} …{x[2]}")
    amostra("id quebrado", id_quebrado, lambda x: f"#{x[0]:<7} {str(x[1])[:38]:40s} {x[2]}")
    amostra("nome diverge", nome_diverge,
            lambda x: f"#{x[0]:<7} INTEL={str(x[1])[:30]:32s} GOOGLE={str(x[2])[:30]}")

    print("\n  Nada foi escrito. Cada classe tem conserto diferente:")
    print("   só no INTEL  → `services.google_push.push_varios(ids, conta, aplicar=True)`")
    print("   só no GOOGLE → decisão: nem todo contato do celular merece ficha no CRM")
    print("   id quebrado  → religar pelo telefone (foi o caso da Daniela em 07/08)")
    print("   nome diverge → decidir qual lado manda; o INTEL costuma ter o nome melhor")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conta", action="append",
                    help="repetível; default = as duas contas conectadas")
    ap.add_argument("--limite", type=int, default=10)
    a = ap.parse_args()
    for k in ("DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        os.environ.setdefault(k, env(k))
    os.environ.setdefault("DB_TARGET", "prod")
    os.environ.setdefault("ALLOW_PROD_FROM_LOCAL", "1")
    contas = a.conta or ["renato@almeida-prado.com", "renato.almeida.prado@gmail.com"]
    return asyncio.run(medir(contas, a.limite))


if __name__ == "__main__":
    sys.exit(main())
