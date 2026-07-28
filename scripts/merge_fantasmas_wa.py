#!/usr/bin/env python3
"""
Merge das fichas fantasma do WhatsApp nas fichas reais.

CONTEXTO: até o commit `2862ece`, o match telefone->ficha comparava os dígitos
crus do WhatsApp contra o texto do JSON como está gravado — e o hífen do
formato do Google ("+55 (11) 99252-6344") caía no meio dos 8 dígitos
comparados. Resultado: quem tinha o número formatado era invisível, e o
webhook criava ficha "Desconhecido +55..." pra quem JÁ tinha ficha.

O vazamento está fechado. Isto aqui é o PASSIVO: as fichas fantasma que
nasceram enquanto ele estava aberto, cada uma com a conversa presa dentro.

CRITÉRIO (conservador): só entra no merge quem casa pelo número INTEIRO
normalizado. Match por sufixo de 8 dígitos é o que a busca usa, mas pra
FUNDIR ficha — operação que apaga uma linha — sufixo não basta: um celular de
outro DDD pode terminar igual.

A ficha REAL é sempre a primária (é a que tem nome, e-mail, cargo, histórico
do Google); a fantasma é absorvida. `merge_duplicate_contacts` repointa as FKs
de 35 tabelas ANTES do DELETE — sem isso o CASCADE levaria a conversa junto.

Quando o mesmo número tem VÁRIAS fichas reais (Dorita tem 3, Raimundo 3), o
merge vai pra ficha de MENOR id — a mais antiga, que é onde está o histórico
mais longo. Isso não resolve a duplicata entre as reais; essa é o passivo
separado que o Renato parqueou em 27/07.

Uso:
    python3 scripts/merge_fantasmas_wa.py                    # dry-run (padrão)
    python3 scripts/merge_fantasmas_wa.py --executar         # escreve

    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/merge_fantasmas_wa.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from database import get_db  # noqa: E402
from services.contact_dedup import merge_duplicate_contacts  # noqa: E402

# Pares (fantasma, real) com número INTEIRO idêntico. A query que os produz
# está no corpo de `levantar()`.
CAMPOS = ("id, nome, empresa, cargo, emails, telefones, foto_url, linkedin, "
          "contexto, google_contact_id")


def levantar(cursor):
    """Pares fantasma->real, só match de número inteiro. Read-only."""
    cursor.execute("""
        WITH nums AS (
          SELECT c.id, c.origem,
                 regexp_replace(
                   jsonb_array_elements(
                     CASE WHEN jsonb_typeof(c.telefones) = 'array'
                          THEN c.telefones ELSE '[]'::jsonb END)->>'number',
                   '[^0-9]', '', 'g') AS d
            FROM contacts c
           WHERE c.telefones IS NOT NULL
        ),
        fant AS (SELECT * FROM nums WHERE origem = 'wa_unknown' AND length(d) >= 10),
        reais AS (SELECT * FROM nums WHERE origem IS DISTINCT FROM 'wa_unknown'
                                       AND length(d) >= 10)
        SELECT f.id AS fantasma_id, min(r.id) AS real_id, f.d AS numero,
               count(DISTINCT r.id) AS candidatos
          FROM fant f
          JOIN reais r ON r.d = f.d          -- número INTEIRO, não sufixo
         GROUP BY f.id, f.d
         ORDER BY f.id
    """)
    return [dict(r) for r in cursor.fetchall()]


def contar_mensagens(cursor, contact_id):
    cursor.execute("""
        SELECT count(*) AS n FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
         WHERE c.contact_id = %s
    """, (contact_id,))
    return cursor.fetchone()["n"]


def main():
    executar = "--executar" in sys.argv

    with get_db() as conn:
        cursor = conn.cursor()
        pares = levantar(cursor)

        print(f"{'EXECUTANDO' if executar else 'DRY-RUN'} — "
              f"{len(pares)} fantasma(s) com ficha real por número inteiro\n")

        total_msgs = 0
        for p in pares:
            cursor.execute(f"SELECT {CAMPOS} FROM contacts WHERE id = %s",
                           (p["real_id"],))
            real = cursor.fetchone()
            cursor.execute(f"SELECT {CAMPOS} FROM contacts WHERE id = %s",
                           (p["fantasma_id"],))
            fantasma = cursor.fetchone()
            if not real or not fantasma:
                print(f"  #{p['fantasma_id']}: ficha sumiu entre o levantamento "
                      f"e agora — pulando")
                continue

            msgs = contar_mensagens(cursor, p["fantasma_id"])
            total_msgs += msgs
            extra = (f"  ({p['candidatos']} fichas reais com este número — "
                     f"vai pro menor id)") if p["candidatos"] > 1 else ""
            print(f"  #{p['fantasma_id']:6} {fantasma['nome'][:34]:36} -> "
                  f"#{p['real_id']:6} {real['nome'][:32]:34} "
                  f"{msgs:3} msg{extra}")

            if executar:
                # Real primeiro: `merge_contacts` usa contacts[0] como base.
                r = merge_duplicate_contacts(
                    [dict(real), dict(fantasma)], conn)
                print(f"         -> {r['status']}, apagadas: {r['deleted_ids']}")

        print(f"\n{total_msgs} mensagens sairiam da ficha fantasma "
              f"pra ficha real." if not executar
              else f"\n{total_msgs} mensagens migradas.")
        if not executar:
            print("Nada foi escrito. Para aplicar: --executar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
