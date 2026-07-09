#!/usr/bin/env python3
"""
Backfill one-time: tonia_messages → messages (conversa WhatsApp do Renato).

Contexto (08/07/2026): desde 04/07 a Tonia parou de repassar os upserts da
instância intel-bot-v2 pro INTEL (bug de resposta dupla) — o histórico
Renato↔Tonia desse período só existe em tonia_messages (mesma DB Neon,
tabelas tonia_* coabitam). Este script copia as mensagens role user/assistant
das conversas channel='wa' desde 2026-07-04 pra tabela messages, na conversa
WhatsApp do contato Renato (id 25613).

Mapeamento:
  role='user'      → direcao='incoming'  (Renato mandou)
  role='assistant' → direcao='outgoing'  (Tonia respondeu)

Dedup (idempotente — rodar 2x não duplica): metadata da mensagem inserida é
  {"source": "tonia_backfill", "tonia_message_id": <id>, "instance": "intel-bot-v2"}
e o script pula qualquer tonia_message_id já presente na conversa.

DRY-RUN por default. Use --apply pra executar de verdade.

Uso:
  python scripts/backfill_tonia_messages.py                # dry-run
  python scripts/backfill_tonia_messages.py --apply        # insere
  python scripts/backfill_tonia_messages.py --since 2026-07-01 --contact-id 25613
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from dotenv import load_dotenv
load_dotenv()

from database import get_db  # noqa: E402

DEFAULT_CONTACT_ID = 25613  # Renato de Faria e Almeida Prado
DEFAULT_SINCE = "2026-07-04"
BACKFILL_SOURCE = "tonia_backfill"
INSTANCE = "intel-bot-v2"

ROLE_TO_DIRECAO = {
    "user": "incoming",
    "assistant": "outgoing",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="Executa os INSERTs (default: dry-run)")
    ap.add_argument("--contact-id", type=int, default=DEFAULT_CONTACT_ID,
                    help=f"Contato destino (default: {DEFAULT_CONTACT_ID})")
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"Data mínima de tm.created_at (default: {DEFAULT_SINCE})")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] backfill tonia_messages → messages "
          f"(contato {args.contact_id}, desde {args.since})\n")

    with get_db() as conn:
        cursor = conn.cursor()

        # 1) Conversa WhatsApp do contato (mesma regra de whatsapp_sync).
        cursor.execute("""
            SELECT id FROM conversations
            WHERE contact_id = %s AND canal = 'whatsapp'
            LIMIT 1
        """, (args.contact_id,))
        row = cursor.fetchone()
        if not row:
            print(f"ERRO: contato {args.contact_id} não tem conversa canal='whatsapp'. "
                  "Crie/verifique antes de rodar o backfill.")
            sys.exit(1)
        conversation_id = row["id"]
        print(f"Conversa destino: {conversation_id}")

        # 2) Fonte: tonia_messages de conversas channel='wa' desde a data.
        cursor.execute("""
            SELECT tm.id, tm.role, tm.content, tm.created_at
            FROM tonia_messages tm
            JOIN tonia_conversations tc ON tc.id = tm.conversation_id
            WHERE tc.channel = 'wa'
              AND tm.role IN ('user', 'assistant')
              AND tm.created_at >= %s
              AND tm.content IS NOT NULL
              AND btrim(tm.content) <> ''
            ORDER BY tm.created_at ASC, tm.id ASC
        """, (args.since,))
        source_rows = cursor.fetchall()
        print(f"Fonte: {len(source_rows)} mensagens tonia (channel='wa', "
              f"role user/assistant, desde {args.since})")

        # 3) Dedup: tonia_message_ids já backfilled nesta conversa.
        cursor.execute("""
            SELECT metadata->>'tonia_message_id' AS tid
            FROM messages
            WHERE conversation_id = %s
              AND metadata->>'source' = %s
              AND metadata->>'tonia_message_id' IS NOT NULL
        """, (conversation_id, BACKFILL_SOURCE))
        existing = {r["tid"] for r in cursor.fetchall()}
        print(f"Já backfilled na conversa: {len(existing)}")

        to_insert = [r for r in source_rows if str(r["id"]) not in existing]
        skipped = len(source_rows) - len(to_insert)

        by_role = {}
        for r in to_insert:
            by_role[r["role"]] = by_role.get(r["role"], 0) + 1

        print(f"\nA inserir: {len(to_insert)} "
              f"(user→incoming: {by_role.get('user', 0)}, "
              f"assistant→outgoing: {by_role.get('assistant', 0)})")
        print(f"Puladas (dedup): {skipped}")

        if to_insert:
            first, last = to_insert[0], to_insert[-1]
            print(f"Range: {first['created_at']} → {last['created_at']}")

        if not args.apply:
            print("\nDRY-RUN — nada inserido. Use --apply pra executar.")
            return

        # 4) INSERT.
        inserted = 0
        for r in to_insert:
            metadata = json.dumps({
                "source": BACKFILL_SOURCE,
                "tonia_message_id": r["id"],
                "instance": INSTANCE,
            })
            cursor.execute("""
                INSERT INTO messages
                    (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                conversation_id,
                args.contact_id,
                ROLE_TO_DIRECAO[r["role"]],
                r["content"],
                r["created_at"],
                metadata,
            ))
            inserted += 1

        if inserted:
            cursor.execute("""
                UPDATE conversations
                SET total_mensagens = total_mensagens + %s,
                    ultimo_mensagem = GREATEST(ultimo_mensagem, %s)
                WHERE id = %s
            """, (inserted, to_insert[-1]["created_at"], conversation_id))

        conn.commit()
        print(f"\nAPPLY concluído: {inserted} mensagens inseridas "
              f"na conversa {conversation_id}.")


if __name__ == "__main__":
    main()
