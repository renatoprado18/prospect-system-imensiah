"""Merge de fichas duplicadas: migra tudo, depois apaga.

⚠️ DESTRUTIVO. Das 32 FKs que apontam pra `contacts`, a MAIORIA é CASCADE —
apagar a ficha absorvida sem migrar antes levaria junto mensagens, fatos,
conversas e conversas inteiras. É exatamente a armadilha registrada em
[[feedback_cascade_chain_warning]] e [[reference_contact_merge_fix]].

Ordem obrigatória: (1) migrar TODA FK, (2) resolver os conflitos de UNIQUE,
(3) só então DELETE.

SEIS TABELAS têm UNIQUE envolvendo contact_id e explodiriam no UPDATE quando as
duas fichas já tiverem a mesma linha (as duas com conversa de WhatsApp, as duas
no mesmo projeto). Nessas, a linha da ficha absorvida é DESCARTADA em vez de
migrada — a da ficha que fica já cobre o caso.

--apply grava; sem ele, dry-run com a contagem do que seria movido.
"""
import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"

# tabela -> coluna. Extraído do catálogo (32 FKs para contacts).
FKS = [
    ("action_proposals", "contact_id"), ("ai_suggestions", "contact_id"),
    ("calendar_events", "contact_id"), ("campaign_enrollments", "contact_id"),
    ("conselhoos_board_members", "contact_id"), ("conselhoos_links", "contact_id"),
    ("contact_briefings", "contact_id"), ("contact_facts", "contact_id"),
    ("contact_interactions", "contact_id"), ("contact_memories", "contact_id"),
    ("contact_prospect_link", "contact_id"), ("contact_rodas", "contact_id"),
    ("contact_snoozes", "contact_id"), ("contact_today_manual", "contact_id"),
    ("conversations", "contact_id"), ("cos_draft_responses", "contact_id"),
    ("delegations", "contact_id"), ("email_triage", "contact_id"),
    ("group_messages", "contact_id"), ("health_predictions", "contact_id"),
    ("linkedin_engagement_signals", "contact_id"),
    ("linkedin_enrichment_history", "contact_id"), ("messages", "contact_id"),
    ("project_members", "contact_id"), ("projects", "owner_contact_id"),
    ("proposal_block_rules", "contact_id"), ("reminders", "contact_id"),
    ("tasks", "contact_id"), ("timeline_summaries", "contact_id"),
    ("users", "contact_id"), ("wa_triage", "contact_id"),
    ("whatsapp_messages", "contact_id"),
]

# UNIQUE que envolve contact_id -> a chave que acompanha. Migrar cegamente aqui
# viola a constraint quando as duas fichas já têm a mesma linha.
CONFLITO = {
    "campaign_enrollments": "campaign_id",
    "contact_prospect_link": "prospect_id",
    "contact_today_manual": "data",
    "project_members": "project_id",
    "timeline_summaries": "cache_hash",
}


def parse_decisoes(texto: str):
    merges, naos = [], []
    for l in texto.splitlines():
        l = l.strip()
        m = re.match(r"MERGE\s+([\d\-]+)\s*->\s*manter\s*#(\d+)", l)
        if m:
            ids = [int(x) for x in m.group(1).split("-")]
            fica = int(m.group(2))
            if fica not in ids:
                print(f"  ? '{l[:50]}': #{fica} não está no grupo — pulado")
                continue
            merges.append((fica, [i for i in ids if i != fica]))
        elif l.startswith("NAO"):
            naos.append(l)
    return merges, naos


def merge(cur, fica: int, absorvidas: list, aplicar: bool) -> dict:
    movidos = defaultdict(int)
    for tabela, col in FKS:
        try:
            if tabela in CONFLITO:
                chave = CONFLITO[tabela]
                # descarta a linha redundante ANTES de migrar o resto
                cur.execute(
                    f"DELETE FROM {tabela} WHERE {col} = ANY(%s) AND {chave} IN "
                    f"(SELECT {chave} FROM {tabela} WHERE {col} = %s)",
                    (absorvidas, fica))
                movidos[f"{tabela}(descartado)"] += cur.rowcount
            cur.execute(f"UPDATE {tabela} SET {col} = %s WHERE {col} = ANY(%s)",
                        (fica, absorvidas))
            if cur.rowcount:
                movidos[tabela] += cur.rowcount
        except psycopg2.Error as e:
            raise RuntimeError(f"{tabela}.{col}: {e}") from e

    if aplicar:
        cur.execute("DELETE FROM contacts WHERE id = ANY(%s)", (absorvidas,))
        movidos["contacts(apagadas)"] = cur.rowcount
    return dict(movidos)


def main() -> int:
    arq = sys.argv[1]
    aplicar = "--apply" in sys.argv
    if aplicar and not (os.getenv("DB_TARGET") == "prod"
                        and os.getenv("ALLOW_PROD_FROM_LOCAL") == "1"):
        sys.exit("alvo não declarado: exige DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1")

    url = [l.split("=", 1)[1].strip().strip('"')
           for l in open(f"{ROOT}/.env") if l.startswith("DATABASE_URL=")][0]
    merges, naos = parse_decisoes(open(arq).read())
    print(f"{len(merges)} merges · {len(naos)} marcados como 'não são a mesma'\n")

    def conectar():
        c = psycopg2.connect(
            url,
            # sem isto o Neon derruba a conexao no meio de um lote longo e o
            # processo fica pendurado no socket: vivo, sem query, sem progresso.
            keepalives=1, keepalives_idle=30, keepalives_interval=10,
            keepalives_count=5, connect_timeout=20,
        )
        c.autocommit = False
        return c

    conn = conectar()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # BACKUP antes de qualquer escrita: sem isto, um merge errado é irreversível.
    todos = sorted({i for f, abs_ in merges for i in [f] + abs_})
    cur.execute("SELECT * FROM contacts WHERE id = ANY(%s)", (todos,))
    bkp = f"{os.path.dirname(os.path.abspath(arq))}/backup_fichas_merge.json"
    with open(bkp, "w") as fh:
        json.dump([{k: str(v) for k, v in dict(r).items()} for r in cur.fetchall()],
                  fh, ensure_ascii=False, indent=1)
    print(f"backup de {len(todos)} fichas → {bkp}\n")

    total = defaultdict(int)
    falhas = []
    feitos = 0
    for fica, absorvidas in merges:
        try:
            cur.execute("SELECT id FROM contacts WHERE id = ANY(%s)", ([fica] + absorvidas,))
            existem = {r["id"] for r in cur.fetchall()}
            if fica not in existem:
                falhas.append((fica, absorvidas, "ficha a manter não existe"))
                continue
            vivos = [i for i in absorvidas if i in existem]
            if not vivos:
                continue                      # já mergeado numa rodada anterior
            m = merge(cur, fica, vivos, aplicar)
            if aplicar:
                conn.commit()          # por grupo: o que entrou, fica
            for k, v in m.items():
                total[k] += v
            feitos += 1
            if feitos % 25 == 0:
                print(f"  ...{feitos}/{len(merges)} grupos", flush=True)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            # conexao morreu: reconecta e segue. O que ja commitou, ficou.
            print(f"  ⚠ conexao caiu em #{fica}, reconectando: {str(e)[:60]}")
            try:
                conn.close()
            except Exception:
                pass
            conn = conectar()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            falhas.append((fica, absorvidas, "conexao caiu — refazer"))
            continue
        except Exception as e:
            conn.rollback()
            falhas.append((fica, absorvidas, str(e)[:110]))
            continue

    if aplicar:
        conn.commit()
        print(f"COMMIT · {feitos} grupos processados\n")
    else:
        conn.rollback()
        print("DRY-RUN (rollback)\n")

    print("registros movidos:")
    for k in sorted(total, key=lambda x: -total[x]):
        if total[k]:
            print(f"  {total[k]:6d}  {k}")
    if falhas:
        print(f"\n{len(falhas)} FALHAS:")
        for f, a, e in falhas:
            print(f"  manter #{f} ← {a}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
