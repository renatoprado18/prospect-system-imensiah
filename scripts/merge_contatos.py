#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
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


def consolidar_google_ids(cur, fica: int, absorvidas: list, aplicar: bool) -> int:
    """Junta na ficha que fica os vinculos Google das que serao apagadas.

    POR QUE (07/08/26). Ate aqui o merge cuidava das 32 FKs e ignorava
    `google_contact_id`, que nao e FK — e uma coluna escalar POR CONTA. O Renato
    tem duas agendas, e o passivo do import de 02/05 e exatamente esse: a mesma
    pessoa com uma ficha por agenda, cada uma com seu gid. Apagar a segunda sem
    guardar o gid dela deixa aquele vinculo orfao, e o sync daquela conta volta a
    tratar a pessoa como desconhecida — o merge viraria enxugar gelo, com a ficha
    renascendo no ciclo seguinte ([[feedback_livelock_reprocessamento]]).

    O mapa multi-conta (`empresa_dados._google_contact_ids`) e onde a cascata de
    identidade procura primeiro; grava-se no formato novo `{conta: [gids]}`, que
    a leitura ja aceita junto com o legado `{conta: "gid"}`.

    220 fichas tem gid escalar sem entrada no mapa — dessas nao da pra saber a
    conta, entao o gid vai pra chave `_orfaos`. Nao e chute de conta: e registro
    de que o vinculo existiu, disponivel pra quem for reconciliar depois.
    """
    cur.execute("""SELECT id, google_contact_id, COALESCE(empresa_dados,'{}'::jsonb) AS ed
                     FROM contacts WHERE id = ANY(%s)""", ([fica] + absorvidas,))
    fichas = {r["id"]: r for r in cur.fetchall()}
    if fica not in fichas:
        return 0, None

    mapa, orfaos = {}, []
    for cid, r in fichas.items():
        blob = r["ed"] if isinstance(r["ed"], dict) else json.loads(r["ed"] or "{}")
        atual = blob.get("_google_contact_ids") or {}
        vistos = set()
        for conta, gids in (atual.items() if isinstance(atual, dict) else []):
            valores = [gids] if isinstance(gids, str) else (gids if isinstance(gids, list) else [])
            for g in valores:
                if g:
                    mapa.setdefault(str(conta), [])
                    if str(g) not in mapa[str(conta)]:
                        mapa[str(conta)].append(str(g))
                    vistos.add(str(g))
        g_escalar = r["google_contact_id"]
        if g_escalar and str(g_escalar) not in vistos:
            orfaos.append(str(g_escalar))

    if orfaos:
        mapa.setdefault("_orfaos", [])
        for g in orfaos:
            if g not in mapa["_orfaos"]:
                mapa["_orfaos"].append(g)
    # 10/08/26 — os dois early returns devolviam `0` escalar enquanto o final
    # devolve `(n, candidato)`: o chamador desempacota em dois nomes, então
    # `TypeError: cannot unpack non-iterable int` derrubava o merge inteiro.
    # E este caminho não é raro — `mapa` fica vazio sempre que NENHUMA das
    # fichas do grupo tem vínculo Google, que é o caso comum de ficha criada
    # pelo WhatsApp. Entrou em 24dd021 (08/08), junto com a tupla; os testes
    # estavam vermelhos desde então, fora da lista de vermelhos conhecidos.
    if not mapa:
        return 0, None

    # O gid escalar da absorvida NAO pode ser adotado aqui: as duas fichas ainda
    # coexistem neste ponto (o DELETE vem depois) e `google_contact_id` tem
    # indice UNICO — atribuir agora derruba o grupo inteiro com "duplicate key".
    # Foi o que aconteceu no lote de 150 em 08/08: 118 de 150 grupos abortaram,
    # todos aqueles em que a ficha mantida tinha gid NULL. Devolvo o candidato
    # pro chamador aplicar DEPOIS do DELETE.
    candidato = next((str(fichas[i]["google_contact_id"]) for i in absorvidas
                      if fichas.get(i) and fichas[i]["google_contact_id"]), None)
    if aplicar:
        cur.execute(
            """UPDATE contacts
                  SET empresa_dados = COALESCE(empresa_dados,'{}'::jsonb)
                                      || jsonb_build_object('_google_contact_ids', %s::jsonb),
                      atualizado_em = now()
                WHERE id = %s""",
            (json.dumps(mapa, ensure_ascii=False), fica))
    return sum(len(v) for v in mapa.values()), candidato


def merge(cur, fica: int, absorvidas: list, aplicar: bool) -> dict:
    movidos = defaultdict(int)
    # ANTES de qualquer DELETE: o vinculo Google nao e FK e some junto com a linha.
    n_gids, gid_candidato = consolidar_google_ids(cur, fica, absorvidas, aplicar)
    if n_gids:
        movidos["google_ids(consolidados)"] = n_gids
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

    # ANTES do DELETE: depois dele estas linhas não existem mais e o
    # `google_contact_id` da ficha absorvida some do banco para sempre — é
    # exatamente por isso que o passivo acumulado é irrecuperável por consulta.
    cur.execute("""SELECT id, google_contact_id FROM contacts
                    WHERE id = ANY(%s) AND google_contact_id IS NOT NULL""",
                (absorvidas,))
    gids_orfaos = [r["google_contact_id"] for r in cur.fetchall()]

    if aplicar:
        cur.execute("DELETE FROM contacts WHERE id = ANY(%s)", (absorvidas,))
        movidos["contacts(apagadas)"] = cur.rowcount
        # AGORA o gid escalar pode ser adotado: a linha que o detinha nao existe
        # mais, entao o indice unico esta livre. Ficha do import antigo (sem
        # vinculo) herda o da agenda — sem isto ela fica sem gid escalar e so o
        # mapa multi-conta a liga ao Google.
        if gid_candidato:
            cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                            WHERE id = %s AND google_contact_id IS NULL""",
                        (gid_candidato, fica))
            if cur.rowcount:
                movidos["gid_escalar(adotado)"] = cur.rowcount

        # ⚠️ ESTE SCRIPT NÃO APAGA A FICHA NO GOOGLE — e isso tem consequência.
        #
        # Ele consolida os `google_contact_id` no mapa multi-conta (acima), mas
        # não chama a People API. A ficha absorvida continua viva na agenda; e
        # como o sync completo traz de volta o que existe no Google, ela
        # REAPARECE como duplicata nova no INTEL na rodada seguinte — foi o que
        # aconteceu com a Manuela e a Wanelise em 26/07. O trabalho volta como
        # novidade e ninguém liga uma coisa à outra.
        #
        # A rota e o `merge_duplicates.py` passaram a usar `duplicados.merge_par`
        # em 16/08, que propaga por dentro (diretriz do Renato: "consistência
        # INTEL⇄Google é por CAMINHO, não por desenho"). Este script tem
        # implementação própria — SQL direto, com a consolidação de gids que os
        # outros dois não têm — e ligá-lo ao serviço é frente própria, aberta no
        # backlog. Até lá, o aviso é gritado em vez de omitido: silêncio aqui é
        # que produziu o passivo que ninguém consegue medir (não há tabela de
        # auditoria de merge, então os órfãos já criados são irrecuperáveis por
        # consulta).
        if gids_orfaos:
            movidos["google(NAO_APAGADO)"] = len(gids_orfaos)
            print(f"\n  ⚠️  {len(gids_orfaos)} ficha(s) seguem VIVAS no Google: "
                  f"{', '.join(str(g) for g in gids_orfaos[:5])}"
                  f"{' …' if len(gids_orfaos) > 5 else ''}", file=sys.stderr)
            print("      O próximo sync completo pode recriá-las como duplicatas. "
                  "Apague-as na agenda ou refunda pela tela (/contatos), que propaga.",
                  file=sys.stderr)
    return dict(movidos)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("uso:  ./merge_contatos.py <arquivo-de-decisoes> [--apply]\n"
              "      cada linha: 'MERGE 1-2-3 -> manter #1'  ou  'NAO 4-5'")
        return 1
    arq = args[0]
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
