#!/usr/bin/env python3
"""Aplica a 074 (entidade `locais`) e povoa a partir do que já existe.

POR QUE APLICAR E POVOAR JUNTO. Tabela nova que nasce vazia é o defeito que este
repositório já catalogou duas vezes: a fila do projeto "Sistema / Dev" nasceu com
duas tarefas dentro e nenhum leitor, no mesmo dia ([[feedback_consumidor_morto_wiring]]).
`locais` sem lugares dentro seria a mesma coisa com outro nome — estrutura que
parece pronta e não responde a nenhuma pergunta. O backfill traz os lugares que
já estão no banco em forma de texto livre, e a ligação `calendar_events.local_id`
dá a eles o primeiro consumidor.

O QUE O BACKFILL RECUSA, e é a parte que importa:
- URL (`https://…`) — Zoom e Meet não são lugares. Eram 32 dos 100 eventos com
  `location` em 13/08.
- Nome de plataforma sem endereço ("TEAMS", "Reuniões do Microsoft Teams") — 12
  ocorrências. Não casa por URL e passaria batido, enchendo a entidade de linhas
  que ninguém consegue visitar.
- `location` vazio ou só espaço.
Tudo que é recusado é CONTADO E MOSTRADO. Backfill que descarta em silêncio
produz um total que parece cobertura e não é ([[feedback_regua_cobertura_parcial]]).

O QUE ELE NÃO TENTA RESOLVER: duplicata por redação. "R. Dr. Amâncio de Carvalho,
182 - Cj 202/203" e "Alba e Bernhoeft Recursos Humanos (Rua Dr. Amâncio de
Carvalho 182…)" são o mesmo lugar em 10 eventos, e entram como DOIS. Fundir
automaticamente é decidir no ambíguo — aqui eles saem listados como candidatos,
para decisão humana ([[feedback_guarda_abstencao_vira_fabrica]] vale para o
oposto: a dúvida não é engolida, é mostrada).

Uso:
  set -a && source .env && set +a && \
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/aplica_074.py [--dry-run]
"""
import os
import re
import sys
import pathlib
import unicodedata

import psycopg2
from psycopg2.extras import RealDictCursor

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SQL = (RAIZ / "scripts" / "migrations" / "074_locais.sql").read_text(encoding="utf-8")

# Não-lugares que não são URL. Casados no texto inteiro normalizado, não por
# substring: "Teams" dentro de "Sala Teams, Av. Paulista 1000" seria um lugar.
NAO_LUGAR = {
    "teams", "reunioes do microsoft teams", "reuniao do microsoft teams",
    "microsoft teams", "google meet", "meet", "zoom", "online", "remoto",
    "videochamada", "video chamada", "a definir", "-",
}

# A lista fechada acima não bastou, e o furo foi real: "Via Zoom - Link na
# descrição" passou no primeiro backfill e virou o local #21 em produção. Casar
# texto INTEIRO não pega a plataforma quando ela vem com sufixo, e o defeito é
# calado — nasce uma linha que parece lugar e ninguém consegue visitar
# ([[feedback_filtro_vocabulario_errado_falha_calado]]). Estes padrões pegam a
# forma "meio de acesso", não o endereço: âncora no INÍCIO do texto, para não
# recusar "Sala Zoom, Av. Paulista 1000".
NAO_LUGAR_RE = [
    re.compile(r"^(via|por|pelo|pela)\s+(zoom|meet|teams|google\s*meet|microsoft\s*teams|hangout)", re.I),
    re.compile(r"^(link|links?)\b", re.I),
    re.compile(r"link na descri", re.I),
    re.compile(r"^(sala\s+virtual|conferencia\s+virtual|chamada\s+de\s+video)$", re.I),
]


def norm(texto: str) -> str:
    """Minúscula, sem acento, espaços colapsados. Só para dedup exata."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def e_lugar(location: str) -> tuple[bool, str]:
    """Devolve (é_lugar, motivo_da_recusa)."""
    if not location or not location.strip():
        return False, "vazio"
    if re.match(r"^https?://", location.strip(), re.I):
        return False, "URL"
    if norm(location) in NAO_LUGAR:
        return False, "plataforma, não lugar"
    n = norm(location)
    # O padrão de plataforma só derruba quando NÃO há indício de endereço junto.
    # Sem esta segunda condição o regex é ganancioso: um escritório chamado "Via
    # Zoom Office, Av. Paulista 1000" seria recusado por causa do nome. Endereço
    # presente ganha da heurística — errar aceitando um lugar é barato (fica
    # visível na lista), errar recusando some sem deixar rastro.
    tem_endereco = bool(re.search(r"\b(av|avenida|r|rua|al|alameda|praca|rod|rodovia|estrada|"
                                  r"largo|travessa|km)\b\.?\s", n) or re.search(r"\d{3}", n))
    if not tem_endereco:
        for rx in NAO_LUGAR_RE:
            if rx.search(n):
                return False, "meio de acesso, não lugar"
    return True, ""


def partes(location: str) -> dict:
    """Quebra grosseira do endereço brasileiro. Erra? Erra — e por isso o texto
    original fica em `endereco_bruto`. O objetivo aqui é dar cidade/estado
    pesquisáveis, não fazer geocoding."""
    d = {"nome": location.split(",")[0].split(" - ")[0].strip()[:120],
         "cidade": None, "estado": None, "cep": None}
    m = re.search(r"\b(\d{5}-?\d{3})\b", location)
    if m:
        d["cep"] = m.group(1)
    m = re.search(r"([A-Za-zÀ-ÿ\s]+)\s*-\s*([A-Z]{2})\b", location)
    if m:
        d["cidade"] = m.group(1).strip()[:80]
        d["estado"] = m.group(2)
    return d


def main() -> int:
    dry = "--dry-run" in sys.argv
    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo not in ("prod", "local"):
        print("❌ DB_TARGET não declarado (prod|local). Alvo deduzido é alvo errado.")
        return 2
    if alvo == "prod" and os.getenv("ALLOW_PROD_FROM_LOCAL") != "1":
        print("❌ prod exige ALLOW_PROD_FROM_LOCAL=1.")
        return 2

    url = (os.getenv("DATABASE_URL_PROD") if alvo == "prod" else None) or os.environ["DATABASE_URL"]
    m_host = re.search(r"@([^/?]+)", url)
    print(f"[074] alvo={alvo} · host={m_host.group(1) if m_host else '?'}")

    conn = psycopg2.connect(url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ---- 1. DDL ----------------------------------------------------------
    # A MESMA RÉGUA DO `verifica_modelo.py`: todos os schemas menos os de
    # sistema. Contar só `public` daria 151→153 e não bateria com o baseline de
    # 162 do contrato — dois números certos medindo coisas diferentes é como se
    # perde a confiança no verificador ([[feedback_medir_o_consumidor_certo]]).
    CONTA = ("SELECT count(*) n FROM information_schema.tables "
             "WHERE table_schema NOT IN ('pg_catalog','information_schema')")
    cur.execute(CONTA)
    antes = cur.fetchone()["n"]
    cur.execute(SQL)
    cur.execute(CONTA)
    depois = cur.fetchone()["n"]
    print(f"[074] tabelas (régua do verifica_modelo): {antes} → {depois}")

    # ---- 2. Backfill dos lugares vindos da agenda ------------------------
    cur.execute("""SELECT id, location FROM calendar_events
                    WHERE location IS NOT NULL AND btrim(location) <> ''""")
    eventos = cur.fetchall()
    recusas, por_chave = {}, {}
    for e in eventos:
        ok, motivo = e_lugar(e["location"])
        if not ok:
            recusas.setdefault(motivo, []).append(e["location"][:60])
            continue
        por_chave.setdefault(norm(e["location"]), []).append(e)

    print(f"[074] {len(eventos)} eventos com location · "
          f"{len(por_chave)} lugares distintos · {sum(len(v) for v in recusas.values())} recusados")
    for motivo, itens in sorted(recusas.items(), key=lambda kv: -len(kv[1])):
        print(f"        recusado ({motivo}): {len(itens)}× — ex.: {itens[0]!r}")

    criados = ligados = 0
    for chave, evs in sorted(por_chave.items(), key=lambda kv: -len(kv[1])):
        bruto = evs[0]["location"].strip()
        p = partes(bruto)
        cur.execute("""INSERT INTO locais (nome, logradouro, cidade, estado, cep,
                                           endereco_bruto, chave_norm, origem)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'backfill_074:calendar_events')
                       ON CONFLICT (chave_norm) DO UPDATE SET atualizado_em = now()
                       RETURNING id, (xmax = 0) AS novo""",
                    (p["nome"], bruto[:300], p["cidade"], p["estado"], p["cep"], bruto, chave))
        r = cur.fetchone()
        criados += 1 if r["novo"] else 0
        cur.execute("UPDATE calendar_events SET local_id=%s WHERE id = ANY(%s) AND local_id IS NULL",
                    (r["id"], [e["id"] for e in evs]))
        ligados += cur.rowcount

    print(f"[074] locais criados: {criados} · eventos ligados: {ligados}")

    # ---- 3. Candidatos a duplicata (NÃO fundidos) ------------------------
    # Heurística de exibição apenas: mesmo CEP, ou nome de um contido no outro.
    cur.execute("""SELECT a.id ida, a.nome na, b.id idb, b.nome nb, a.cep
                     FROM locais a JOIN locais b ON a.id < b.id
                    WHERE (a.cep IS NOT NULL AND a.cep = b.cep)
                       OR (length(a.nome) > 8 AND b.endereco_bruto ILIKE '%%'||a.nome||'%%')
                       OR (length(b.nome) > 8 AND a.endereco_bruto ILIKE '%%'||b.nome||'%%')""")
    dups = cur.fetchall()
    if dups:
        print(f"[074] ⚠️ {len(dups)} par(es) que PARECEM o mesmo lugar — não fundidos, decisão humana:")
        for d in dups:
            print(f"        #{d['ida']} {d['na'][:36]!r}  ×  #{d['idb']} {d['nb'][:36]!r}")

    # ---- 4. Controle: a estrutura aguenta o caso que originou a frente? ---
    # Insere o vínculo com papel, confere, e desfaz. Sem isto, "aplicado" só diz
    # que o DDL não deu erro — não que a entidade serve pra qualquer coisa.
    cur.execute("SAVEPOINT prova")
    cur.execute("""INSERT INTO locais (nome, cidade, estado, chave_norm, origem)
                   VALUES ('__prova__','Guaxupé','MG','__prova_074__','teste') RETURNING id""")
    lid = cur.fetchone()["id"]
    cur.execute("SELECT id FROM contacts ORDER BY id LIMIT 1")
    cid = cur.fetchone()["id"]
    cur.execute("INSERT INTO locais_contatos (local_id, contact_id, papel) VALUES (%s,%s,'proprietario')", (lid, cid))
    cur.execute("INSERT INTO locais_contatos (local_id, contact_id, papel) VALUES (%s,%s,'gerente')", (lid, cid))
    cur.execute("SELECT count(*) n FROM locais_contatos WHERE local_id=%s", (lid,))
    n_papeis = cur.fetchone()["n"]
    dupe_barrada = False
    try:
        cur.execute("SAVEPOINT dup")
        cur.execute("INSERT INTO locais_contatos (local_id, contact_id, papel) VALUES (%s,%s,'gerente')", (lid, cid))
        cur.execute("RELEASE SAVEPOINT dup")
    except psycopg2.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT dup")
        dupe_barrada = True
    cur.execute("ROLLBACK TO SAVEPOINT prova")
    print(f"[074] prova: 2 papéis da mesma pessoa no mesmo lugar = {n_papeis} linhas "
          f"{'✅' if n_papeis == 2 else '❌'} · papel repetido barrado pelo UNIQUE "
          f"{'✅' if dupe_barrada else '❌'}")

    if dry:
        conn.rollback()
        print("[074] --dry-run: revertido, nada persistiu.")
    else:
        conn.commit()
        print("[074] ✅ aplicado e commitado.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
