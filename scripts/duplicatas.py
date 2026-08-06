#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Acha fichas duplicadas e monta a página de revisão.

POR QUE. Duplicata apareceu SOZINHA em três frentes diferentes de 04/08: o
resolvedor de fatos confirmou uma hipótese com a ficha gêmea da mesma pessoa
(Rosimeri, #5365 e #26134, mesmo telefone); o casamento da árvore devolveu 5
ambíguos, TODOS duplicata; e o Ricardo Lindenbojm tem 5 fichas. Não foram
procuradas — tropeçamos nelas.

Duplicata não é só sujeira: ela ESPALHA o histórico. Metade das mensagens numa
ficha, metade na outra, e qualquer leitura por contato (health score, "última
interação", o portão da camada) enxerga menos do que existe.

Critérios independentes, e a força importa: telefone canônico igual é prova
forte (ver reference_telefone_br_normalizacao — comparar só DÍGITOS, nunca LIKE);
e-mail igual idem; nome igual sozinho é indício — homônimo existe, ainda mais
em família.

NÃO faz merge. Merge migra FKs e apaga ficha: é destrutivo e passa pelo Renato.
"""
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SAIDA = os.path.expanduser("~/cockpit/duplicatas.html")


def norm_nome(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(de|da|do|das|dos|e|sr|sra|dr|dra|prof)\b", " ", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", " ", s)).strip()


def digitos(tel):
    """Núcleo canônico do telefone: últimos 8 dígitos, SÓ se o número for plausível.

    Medido em 04/08: sem o teste de comprimento, "55119912" aparecia em 15
    fichas e "551212" em 13 — números truncados no cadastro cujos últimos 8
    dígitos colidem. Isso inflou o resultado pra 3.410 grupos "duplicados",
    2.134 deles com "prova forte" que não era prova de nada. Régua que acusa
    tudo não acusa nada.

    BR completo tem 10-13 dígitos com DDI/DDD. Fora dessa faixa é cadastro
    quebrado, e cadastro quebrado não serve de evidência de identidade.
    """
    d = re.sub(r"\D", "", tel or "")
    return d[-8:] if 10 <= len(d) <= 13 else ""


def main():
    url = [l.split("=", 1)[1].strip().strip('"')
           for l in open("/Users/rap/prospect-system/.env")
           if l.startswith("DATABASE_URL=")][0]
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.nome, c.empresa, c.cargo, c.telefones::text AS tel,
               c.emails::text AS mail, c.circulo, c.criado_em,
               (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs,
               (SELECT count(*) FROM contact_facts f WHERE f.contact_id = c.id) AS fatos,
               (SELECT count(*) FROM tasks t WHERE t.contact_id = c.id) AS tasks
        FROM contacts c
        WHERE c.nome IS NOT NULL AND btrim(c.nome) <> ''
    """)
    fichas = cur.fetchall()

    por_nome, por_tel, por_mail = defaultdict(list), defaultdict(list), defaultdict(list)
    for f in fichas:
        n = norm_nome(f["nome"])
        if n and len(n) > 4:
            por_nome[n].append(f)
        for t in re.findall(r'"number":\s*"([^"]+)"', f["tel"] or ""):
            d = digitos(t)
            if d:
                por_tel[d].append(f)
        for e in re.findall(r'"(?:email|address)":\s*"([^"]+)"', f["mail"] or ""):
            e = e.strip().lower()
            if "@" in e:
                por_mail[e].append(f)

    grupos = {}

    def juntar(chave, lista, motivo, forca):
        ids = tuple(sorted({x["id"] for x in lista}))
        if len(ids) < 2:
            return
        g = grupos.setdefault(ids, {"fichas": {x["id"]: x for x in lista},
                                    "motivos": [], "forca": 0})
        g["motivos"].append(f"{motivo}: {chave}")
        g["forca"] = max(g["forca"], forca)

    # Identificador compartilhado por MUITA gente não identifica ninguém: é
    # telefone de empresa, e-mail de contato geral, ou lixo de cadastro. Acima
    # de 3 fichas o dado deixa de ser pessoal e vira ruído.
    LIMITE = 3
    descartados = 0
    for k, v in por_tel.items():
        if len({x["id"] for x in v}) > LIMITE:
            descartados += 1
            continue
        juntar(k, v, "mesmo telefone", 3)
    for k, v in por_mail.items():
        if len({x["id"] for x in v}) > LIMITE:
            descartados += 1
            continue
        juntar(k, v, "mesmo e-mail", 3)
    for k, v in por_nome.items():
        if len({x["id"] for x in v}) > LIMITE:
            continue
        juntar(k, v, "mesmo nome", 1)
    if descartados:
        print(f"  ({descartados} identificadores descartados por aparecer em >{LIMITE} fichas)")

    todos = sorted(grupos.values(),
                   key=lambda g: (-g["forca"],
                                  -sum(f["msgs"] for f in g["fichas"].values())))
    fortes = [g for g in todos if g["forca"] >= 3]

    # SÓ VALE REVISAR O QUE TEM HISTÓRICO. Medido em 04/08: 2.133 grupos por
    # telefone, e em apenas 246 alguma ficha tem mensagem, fato ou task. Os
    # outros ~1.900 são fichas vazias trigeminadas por importações repetidas
    # (as faixas de id denunciam: uma antiga, uma ~#21300, uma ~#25000) — não
    # espalham histórico nenhum porque não têm histórico.
    #
    # Mandar 3.193 cards pra revisão humana seria transformar um achado real
    # em trabalho impossível. Duplicata que não divide dado é higiene de fundo,
    # não decisão do Renato.
    com_uso = [g for g in todos
               if sum(f["msgs"] + f["fatos"] + f["tasks"] for f in g["fichas"].values()) > 0]

    print(f"{len(todos)} grupos suspeitos · {len(fortes)} com prova forte")
    print(f"  → {len(com_uso)} REVISÁVEIS (alguma ficha tem histórico); "
          f"{len(todos) - len(com_uso)} são fichas vazias — sem decisão a tomar")
    ordenados = com_uso

    from duplicatas_html import render
    open(SAIDA, "w").write(render(ordenados))
    print(f"→ {SAIDA}")
    os.system(f'open "{SAIDA}"')


if __name__ == "__main__":
    main()
