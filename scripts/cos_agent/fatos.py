#!/usr/bin/env python3
"""Fatos incertos: resolve o que der sozinho, pergunta o resto COM contexto.

POR QUE. O backfill de 04/08 gerou 167 fatos e 13% eram palpite vestido de
certeza. A guarda em `contact_enrichment` já rebaixa a confiança pra 0.45, mas
rebaixar só esconde: o fato fica lá, meio morto, sem ninguém decidir.

O PEDIDO DO RENATO (04/08): "poderia pedir minha confirmação em alguns casos" e
"fazer uma pesquisa mais ampla para confirmar hipóteses antes de confirmar
comigo, sempre trazendo o contexto".

ENTÃO A ORDEM É: resolver → só depois perguntar. Interromper o Renato com algo
que o próprio banco responde é o desperdício mais caro que este sistema comete.
Prova de conceito medida no dia: o fato "relacionamento com Nizan (possivelmente
Nizan Guanaes ou executivo homônimo)" foi gravado como hipótese — e a ficha
**#4339 'Nizan Guanaes / Grupo ABC'** já existia no banco. Uma query resolvia.

    ./fatos.py --revisar    # resolve, aplica o que confirmou, abre o resto
    ./fatos.py --gravar -   # lê o veredito do botão "Copiar resultado"

O QUE ELE NÃO FAZ: web search. Fica pro próximo passo — a busca interna é grátis
e resolve o caso comum (entidade que já é contato). Sair pra internet antes de
olhar o que se tem em casa seria começar pelo caro.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import _conn  # noqa: E402

DESTINO = os.path.expanduser("~/cockpit/fatos.html")
CONF_HEDGE = 0.45
CONF_CONFIRMADO = 0.85

# Nome próprio candidato: 1-3 palavras capitalizadas seguidas. Filtra o que é
# capitalizado por posição (início de frase) e não por ser nome.
_ENTIDADE = re.compile(r"\b([A-ZÀ-Ú][a-zà-ú]{2,}(?:\s+[A-ZÀ-Ú][a-zà-ú]{2,}){0,2})\b")
_RUIDO = {"Renato", "Grupo", "Conselho", "Associação", "Empresa", "Diretor",
          "Possivelmente", "Provavelmente", "Aparentemente", "Trabalha", "Atua",
          "Tem", "Está", "Cuida", "Frequenta", "Representa", "Passou", "Chegou"}


def _rw_url() -> str:
    for l in open("/Users/rap/prospect-system/.env"):
        if l.startswith("DATABASE_URL="):
            return l.split("=", 1)[1].strip().strip('"')
    sys.exit("DATABASE_URL ausente")


# Marcador de dúvida seguido de NOME PRÓPRIO — a dúvida é de IDENTIDADE.
# É o único formato que uma busca de ficha consegue resolver.
_DUVIDA_DE_IDENTIDADE = re.compile(
    r"(?:possivelmente|provavelmente|talvez|aparentemente|presumivelmente)\s+"
    r"([A-ZÀ-Ú][a-zà-ú]{2,}(?:\s+[A-ZÀ-Ú][a-zà-ú]{2,}){0,2})",
)


def candidatos(texto: str) -> list[str]:
    """Nomes que a busca pode tentar resolver.

    ⚠️ SÓ dúvida de IDENTIDADE, e essa restrição custou três rodadas de falso
    positivo em 04/08 pra ficar clara. Achar uma ficha parecida NÃO confirma uma
    AFIRMAÇÃO:
      · "Luis Cláudio é PROVAVELMENTE advogado tributarista" — a ficha dele
        existe e não diz nada sobre a profissão;
      · "hábito de ir ao 'charuto' (PROVAVELMENTE espaço de charutaria)" — foi
        dado por confirmado por "#1631 Emma Russo · RESEARCHER", que não tem
        relação nenhuma com a frase.

    O caso do Nizan funcionava porque ali a dúvida ERA de identidade:
    "(possivelmente Nizan Guanaes ou executivo homônimo)" — existe uma ficha
    "Nizan Guanaes" e ela responde exatamente o que estava em aberto.

    Então o padrão é estreito: marcador de dúvida IMEDIATAMENTE seguido de nome
    próprio. "provavelmente advogado" não casa (minúscula); "possivelmente Nizan
    Guanaes" casa.
    """
    vistos, saida = set(), []
    for m in _DUVIDA_DE_IDENTIDADE.finditer(texto):
        nome = m.group(1).strip()
        if nome in _RUIDO or nome.split()[0] in _RUIDO or nome.lower() in vistos:
            continue
        vistos.add(nome.lower())
        saida.append(nome)
    return saida


def resolver(cur, fato: dict) -> dict:
    """Tenta fechar a hipótese com o que o INTEL já sabe.

    Devolve `status` = confirmado | inconclusivo, mais a EVIDÊNCIA — que é o que
    o Renato pediu ("sempre trazendo o contexto"). Sem evidência, confirmar
    automaticamente seria trocar um palpite do modelo por um palpite meu.

    ⚠️ A REGRA DE CONFIRMAÇÃO É ESTREITA DE PROPÓSITO, e a primeira versão dela
    estava ERRADA. Ela confirmava quando achava UMA ficha — e a ficha que achava
    era, quase sempre, a do PRÓPRIO contato do fato. Resultado medido em 04/08,
    antes de reverter: "Luis Cláudio é PROVAVELMENTE advogado tributarista" foi
    dado por confirmado pela existência da ficha "#19166 Luis Cláudio" — que não
    diz nada sobre a profissão. E "Relacionamento ativo e cordial…" casou com um
    contato literalmente chamado "Relacionamento" (#4914).

    O que confirma uma hipótese é achar OUTRA entidade que explique a dúvida —
    o caso do Nizan: o fato é sobre Monforte e a ficha achada é a de Nizan
    Guanaes. Ficha do próprio sujeito do fato é tautologia, não evidência.
    """
    ev, resolveu = [], []
    for nome in candidatos(fato["fato"]):
        # (a) a entidade já é contato? Match por nome completo, não prefixo:
        # "Nizan" casando com "Nizanete" confirmaria a coisa errada.
        # `id <> contact_id` NÃO basta: ficha DUPLICADA da mesma pessoa passa
        # pelo filtro de id e vira "evidência" de si mesma. Medido em 04/08: o
        # fato "Rosimeri é a caseira/cuidadora" (ficha #5365) foi dado por
        # confirmado pela ficha #26134 — mesmo nome, MESMO TELEFONE, duplicata.
        # Por isso o `nome <> nome`: duplicata é achado de higiene, não prova.
        cur.execute("""
            SELECT id, nome, empresa, cargo FROM contacts
            WHERE (nome ILIKE %s OR nome ILIKE %s)
              AND id <> %s
              AND lower(btrim(nome)) <> lower(btrim(%s))
            ORDER BY length(nome) LIMIT 3
        """, (nome, f"{nome} %", fato["contact_id"], fato["contato"]))
        for c in cur.fetchall():
            # O nome tem que ter sobrenome OU a ficha trazer empresa/cargo. Uma
            # ficha de uma palavra só ("Relacionamento", "Joeseph") não é
            # identificação — é colisão de vocabulário.
            forte = (" " in c["nome"].strip()) or c.get("empresa") or c.get("cargo")
            item = {"tipo": "ficha", "termo": nome, "forte": bool(forte),
                    "texto": f"#{c['id']} {c['nome']}"
                             + (f" · {c['empresa']}" if c.get("empresa") else "")
                             + (f" · {c['cargo']}" if c.get("cargo") else "")}
            ev.append(item)
            if forte:
                resolveu.append(item)

        # (b) a conversa fala dele com mais detalhe em outro lugar?
        cur.execute("""
            SELECT left(conteudo, 150) AS trecho FROM messages
            WHERE contact_id = %s AND conteudo ILIKE %s LIMIT 2
        """, (fato["contact_id"], f"%{nome}%"))
        for m in cur.fetchall():
            ev.append({"tipo": "mensagem", "termo": nome, "forte": False,
                       "texto": " ".join(m["trecho"].split())})

    # Confirma sozinho SÓ com ficha FORTE de OUTRA entidade — o caso do Nizan.
    # Menção em mensagem vira contexto, nunca prova: o texto citar o nome não
    # diz que a relação inferida existe.
    status = "confirmado" if len(resolveu) == 1 else "inconclusivo"
    return {"status": status, "evidencia": ev}


def carregar(cur, limite: int) -> list[dict]:
    cur.execute("""
        SELECT f.id, f.contact_id, f.categoria, f.fato, f.confianca, f.fonte,
               c.nome AS contato
        FROM contact_facts f JOIN contacts c ON c.id = f.contact_id
        WHERE f.confianca <= %s AND NOT f.verificado
        ORDER BY f.criado_em DESC LIMIT %s
    """, (CONF_HEDGE, limite))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revisar", action="store_true")
    ap.add_argument("--gravar", metavar="ARQ")
    ap.add_argument("--limite", type=int, default=40)
    ap.add_argument("--aplicar-confirmados", action="store_true", default=True)
    a = ap.parse_args()

    url = _rw_url()
    if a.gravar:
        texto = sys.stdin.read() if a.gravar == "-" else open(a.gravar).read()
        return gravar(url, texto)
    if not a.revisar:
        ap.print_help()
        return 1

    with _conn(url) as conn, conn.cursor() as cur:
        pend = carregar(cur, a.limite)
        if not pend:
            print("Nenhum fato incerto pendente.")
            return 0

        confirmados, restantes = [], []
        for f in pend:
            r = resolver(cur, f)
            f["evidencia"] = r["evidencia"]
            (confirmados if r["status"] == "confirmado" else restantes).append(f)

        print(f"{len(pend)} fatos incertos · {len(confirmados)} resolvidos pelo banco "
              f"· {len(restantes)} precisam de você")

        if confirmados and a.aplicar_confirmados:
            w = conn.cursor()
            for f in confirmados:
                w.execute("""UPDATE contact_facts
                             SET confianca=%s, verificado=TRUE, atualizado_em=NOW()
                             WHERE id=%s""", (CONF_CONFIRMADO, f["id"]))
                ev = f["evidencia"][0]["texto"]
                print(f"  ✓ {f['fato'][:58]}…\n      confirmado por: {ev}")
            conn.commit()

    if not restantes:
        print("\nNada a perguntar — o banco respondeu tudo.")
        return 0

    from fatos_html import render
    open(DESTINO, "w").write(render(restantes))
    print(f"\n{len(restantes)} → {DESTINO}")
    subprocess.run(["open", DESTINO], check=False)
    return 0


def gravar(url: str, texto: str) -> int:
    """Aplica o veredito. Formato: '#id — CONFIRMA|CORRIGE|DESCARTA'."""
    linhas = [l.strip() for l in texto.splitlines() if l.strip().startswith("#")]
    if not linhas:
        sys.exit("nada reconhecido — esperado '#id — VEREDITO'")
    ok = 0
    with _conn(url) as conn, conn.cursor() as cur:
        for l in linhas:
            m = re.match(r"#(\d+)\s*—\s*([A-ZÀ-Ú]+)", l)
            if not m:
                print(f"  ? {l[:50]}"); continue
            fid, v = int(m.group(1)), m.group(2)
            if v == "CONFIRMA":
                cur.execute("""UPDATE contact_facts SET confianca=%s, verificado=TRUE,
                               atualizado_em=NOW() WHERE id=%s""", (CONF_CONFIRMADO, fid))
            elif v == "DESCARTA":
                # `valido_ate` em vez de DELETE: o fato errado é evidência de que
                # o extrator erra, e apagar some com essa evidência.
                cur.execute("""UPDATE contact_facts SET valido_ate=NOW(), verificado=TRUE,
                               atualizado_em=NOW() WHERE id=%s""", (fid,))
            else:
                print(f"  ? veredito '{v}' desconhecido"); continue
            ok += 1
        conn.commit()
    print(f"{ok} aplicados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
