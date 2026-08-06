#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Backfill do histórico de grupos WhatsApp direto da Evolution.

POR QUE EXISTE (05-06/08/2026). O sync regular pedia 50 mensagens por grupo, uma
vez por dia, e reportava sucesso. Quem passasse de 50 naquele dia perdia o
excedente PARA SEMPRE — não há catch-up. Medido nos 8 grupos rastreados: o INTEL
tinha **20.646 de 56.037 (37%)**.

O sync foi corrigido (`2da93e3`, pagina até alcançar), mas paginar daqui pra
frente não traz o que ficou pra trás. Este script traz: varre a Evolution até o
fim do backlog do Baileys e grava o que falta.

Rodou em 06/08 e recuperou **26.712 mensagens** → cobertura 71% (o resto é
sticker/reaction/mídia sem texto, que não entra nem ao vivo).

Uso:
    ./backfill_grupo.py                      # dry-run, TODOS os grupos rastreados
    ./backfill_grupo.py --apply              # grava
    ./backfill_grupo.py <jid>@g.us --apply   # um grupo só

Grava direto em `group_messages`, idempotente por `message_id` — NÃO passa pelo
handler de webhook. Isso é deliberado: o handler dispara efeito de saída, e
reprocessar 9 mil mensagens de meses atrás por ele seria agir no mundo sobre
conversa velha (a mesma armadilha do replay que motivou a guarda `_catchup`).
Aqui só o dado entra.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg2

ROOT = "/Users/rap/prospect-system"
sys.path.insert(0, f"{ROOT}/app")

ALVO = next((a for a in sys.argv[1:] if a.endswith("@g.us")), None)
APPLY = "--apply" in sys.argv
JID = None                       # setado por grupo no laço de main()


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    sys.exit(k)


def pagina(evo, key, page, offset=100, tentativas=3):
    """Uma página, com retry. A Evolution dá timeout esporádico em página funda.

    A 1a versão varria 220 páginas acumulando em memória e só gravava no fim —
    um timeout na página 180 jogou fora ~11 mil mensagens já lidas. Trabalho
    caro que não sobrevive a uma falha de rede é trabalho que se vai refazer.
    """
    import time
    for t in range(tentativas):
        try:
            req = urllib.request.Request(
                f"{evo}/chat/findMessages/rap-whatsapp",
                data=json.dumps({"where": {"key": {"remoteJid": JID}},
                                 "page": page, "offset": offset}).encode(),
                method="POST", headers={"apikey": key, "Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            return d.get("messages", {}).get("records", [])
        except Exception as e:
            if t == tentativas - 1:
                print(f"  ⚠ página {page} falhou após {tentativas} tentativas: {str(e)[:60]}")
                return None                      # None = falha; [] = fim real
            time.sleep(2 * (t + 1))
    return None


def um_grupo(evo, key, conn, jid, nome):
    """Backfill de UM grupo. Devolve (inseridas, falhas)."""
    global JID
    JID = jid
    from integrations.evolution_api import _extract_group_message_content

    cur = conn.cursor()
    cur.execute("SELECT message_id FROM group_messages WHERE group_jid = %s", (JID,))
    ja = {r[0] for r in cur.fetchall()}
    print(f"    já no INTEL: {len(ja)}")

    def grava(lote):
        if not lote or not APPLY:
            return 0
        for n in lote:
            cur.execute("""
                INSERT INTO group_messages
                  (group_jid, message_id, sender_phone, sender_name, content,
                   message_type, timestamp, from_me, metadata, criado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (message_id) DO NOTHING
            """, (JID, n["message_id"], n["sender_phone"], n["sender_name"],
                  n["content"], n["message_type"], n["timestamp"], n["from_me"],
                  json.dumps({"fonte": "backfill_evolution_05_08_2026"})))
        conn.commit()                # commit POR LOTE: o que entrou, fica
        return len(lote)

    novos, vistos, vazios, gravados, falhas = [], 0, 0, 0, 0
    page = 1
    while page <= 220:
        recs = pagina(evo, key, page)
        if recs is None:             # falha de rede: pula a página, segue
            falhas += 1
            page += 1
            continue
        if not recs:
            break
        vistos += len(recs)
        for r in recs:
            mid = (r.get("key") or {}).get("id")
            if not mid or mid in ja:
                continue
            ts = int(r.get("messageTimestamp") or 0)
            if not ts:
                continue
            content, tipo = _extract_group_message_content(r.get("message") or {})
            if not content:
                vazios += 1          # reaction/sticker/mídia: não entra nem ao vivo
                continue
            part = (r.get("key") or {}).get("participantAlt") or \
                   (r.get("key") or {}).get("participant") or ""
            novos.append({
                "message_id": mid,
                "sender_phone": re.sub(r"@.*", "", part),
                "sender_name": "Renato" if (r.get("key") or {}).get("fromMe")
                               else (r.get("pushName") or ""),
                "content": content, "message_type": tipo,
                "timestamp": datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None),
                "from_me": bool((r.get("key") or {}).get("fromMe")),
            })
            ja.add(mid)
        if len(novos) >= 500:        # descarrega a cada 500 pra não perder tudo
            gravados += grava(novos)
            novos = []
        if page % 40 == 0:
            print(f"  ...página {page} · {gravados + len(novos)} novos até aqui")
        page += 1
    gravados += grava(novos)
    if APPLY:
        novos = []

    print(f"    varridas {vistos} · {vazios} sem texto · {falhas} páginas falharam · "
          + (f"{gravados} INSERIDAS" if APPLY else f"seriam {len(novos)}"))
    return gravados, falhas


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    evo, key = env("EVOLUTION_API_URL"), env("EVOLUTION_API_KEY")
    if APPLY and not (os.getenv("DB_TARGET") == "prod"
                      and os.getenv("ALLOW_PROD_FROM_LOCAL") == "1"):
        sys.exit("alvo não declarado: exige DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1")
    conn = psycopg2.connect(env("DATABASE_URL"))
    c = conn.cursor()
    if ALVO:
        c.execute("SELECT group_jid, group_name FROM social_groups_cache WHERE group_jid=%s", (ALVO,))
    else:
        # todos os rastreados, menor primeiro: se algo der errado, erra barato
        c.execute("""SELECT s.group_jid, s.group_name FROM social_groups_cache s
                     WHERE s.sync_enabled ORDER BY
                       (SELECT count(*) FROM group_messages g WHERE g.group_jid=s.group_jid) ASC""")
    grupos = c.fetchall()
    print(f"{len(grupos)} grupos rastreados\n")
    tot = falhas_tot = 0
    for jid, nome in grupos:
        print(f"  ▸ {(nome or jid)[:40]}")
        try:
            n, f = um_grupo(evo, key, conn, jid, nome)
            tot += n; falhas_tot += f
        except Exception as e:
            print(f"    ERRO: {str(e)[:90]}")
    print(f"\nTOTAL: {tot} inseridas · {falhas_tot} páginas falharam")
    return 0


if __name__ == "__main__":
    sys.exit(main())
