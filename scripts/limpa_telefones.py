#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Tira do campo `telefones` o que não é telefone. ⚠️ DESTRUTIVO (com backup).

POR QUE (07/08/2026). A tela de telefones compartilhados sugeriu juntar 15
pessoas sem relação nenhuma — Securato, Raphael Mielle, Gabriel Sidi. A causa
não era a tela: era o dado. Duas sujeiras, e a primeira é irreversível:

  1. NOTAÇÃO CIENTÍFICA. Telefones importados de planilha viraram `5.51132E+11`.
     Normalizados pra dígitos, todos os `5.5113XE+11` colidem no mesmo prefixo —
     e qualquer casamento por telefone passa a juntar quem não tem relação.
     **O número original NÃO é recuperável**: sobraram 6 dígitos significativos,
     o resto se perdeu no Excel muito antes de chegar aqui.
  2. NÃO-TELEFONE: sem dígito nenhum, ou dígitos repetidos.

⚠️ O QUE ELE **NÃO** REMOVE, e a primeira versão removia: número curto. Eu tratei
"<10 dígitos" como lixo e ia apagar o telefone de **182 fichas** — só que
"7678-5627" e "99606-8427" são telefones REAIS sem DDD, cadastrados antes de ele
virar obrigatório. Dado incompleto não é dado falso. O que ele não pode é servir
pra CASAR fichas — e isso se conserta no critério de casamento (exigir 10+
dígitos), não apagando o contato de quem só tem esse número.

Medido: 175 entradas científicas em 116 fichas — e **114 delas têm outro
telefone válido no mesmo array**, então a limpeza não perde contato nenhum. As 2
restantes ficariam sem telefone e são listadas em vez de silenciosamente
esvaziadas.

O QUE ELE NÃO FAZ: não inventa número. Onde não há como recuperar, remove — o
dado ruim é pior que a ausência, porque a ausência não produz merge falso.

Uso:  ./limpa_telefones.py           # dry-run: mostra o que sairia
      ./limpa_telefones.py --apply   # aplica, com backup em JSON antes
"""
import json
import os
import re
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
BACKUP = os.path.expanduser(f"~/cockpit/backup_telefones_{datetime.now():%Y%m%d_%H%M}.json")


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def e_lixo(numero: str) -> str:
    """Motivo pelo qual isto não é um telefone — ou "" se for."""
    if not numero:
        return "vazio"
    if re.search(r"e\+\d", numero, re.I):
        return "notação científica (irrecuperável)"
    d = re.sub(r"\D", "", numero)
    if not d:
        return "sem dígito nenhum"
    if len(set(d)) <= 2:
        return "dígitos repetidos"
    # ⚠️ NÃO remover número curto. A primeira versão tratava "<10 dígitos" como
    # lixo e ia apagar 182 contatos — mas "7678-5627" e "99606-8427" são
    # telefones REAIS sem DDD, cadastrados antes de o DDD virar obrigatório.
    # Dado incompleto não é dado falso: o que ele não pode é ser usado pra
    # CASAR fichas, e isso se resolve no critério de casamento (exigir 10+
    # dígitos), não apagando o telefone de quem tem só ele.
    return ""


def main():
    aplicar = "--apply" in sys.argv
    conn = psycopg2.connect(env("DATABASE_URL"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT id, nome, telefones FROM contacts
                    WHERE telefones IS NOT NULL AND jsonb_typeof(telefones) = 'array'
                      AND jsonb_array_length(telefones) > 0""")
    fichas = cur.fetchall()

    mudancas, ficariam_sem, por_motivo = [], [], {}
    for f in fichas:
        limpos, tirados = [], []
        for t in (f["telefones"] or []):
            num = t.get("number") if isinstance(t, dict) else str(t)
            motivo = e_lixo(num)
            if motivo:
                tirados.append((num, motivo))
                por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
            else:
                limpos.append(t)
        if not tirados:
            continue
        mudancas.append((f["id"], f["nome"], f["telefones"], limpos, tirados))
        if not limpos:
            ficariam_sem.append((f["id"], f["nome"], [x[0] for x in tirados]))

    print(f"{len(fichas)} fichas com telefone · {len(mudancas)} têm entrada inválida")
    for m, n in sorted(por_motivo.items(), key=lambda x: -x[1]):
        print(f"   {n:4d}  {m}")
    print(f"\n{len(ficariam_sem)} ficariam SEM telefone nenhum:")
    for cid, nome, nums in ficariam_sem:
        print(f"   #{cid:<7} {str(nome)[:34]:36s} {nums}")

    if not aplicar:
        print("\nDRY-RUN — nada foi alterado. Rode com --apply pra valer.")
        print("Exemplos do que sairia:")
        for cid, nome, _, _, tirados in mudancas[:6]:
            print(f"   #{cid:<7} {str(nome)[:28]:30s} ← {tirados[0][0]}")
        return 0

    # Backup ANTES da primeira escrita: o número científico não volta de lugar
    # nenhum, então o JSON é a única chance de auditar depois.
    with open(BACKUP, "w") as fh:
        json.dump([{"id": c, "nome": n, "telefones_antes": a}
                   for c, n, a, _, _ in mudancas], fh, ensure_ascii=False, indent=1, default=str)
    print(f"\nbackup de {len(mudancas)} fichas → {BACKUP}")

    for cid, _, _, limpos, _ in mudancas:
        cur.execute("UPDATE contacts SET telefones = %s, atualizado_em = now() WHERE id = %s",
                    (json.dumps(limpos), cid))
    conn.commit()
    print(f"{len(mudancas)} fichas limpas · {sum(por_motivo.values())} entradas removidas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
