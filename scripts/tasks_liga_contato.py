#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Liga tasks órfãs ao contato citado no próprio título.

POR QUE EXISTE (06/08/2026). Toda reconciliação do INTEL cruza por
`tasks.contact_id`: o check F ("parece resolvida no WA"), o check G ("já virou
task") e as views novas `acao_do_renato` / `atos_que_resolvem`. Task sem
contact_id é invisível para todos eles — e **74 de 119 tasks abertas (62%)
estão assim**.

Foi o que produziu o caso #999562: a Andressa escreveu "Está sim. Paguei hoje."
no WhatsApp, a task do G100 continuou aberta, e o Renato perguntou duas vezes no
cockpit por quê. O cruzamento nunca teve como acontecer. Construir mais um
cruzamento em cima de um campo vazio seria decorar o cano furado.

O QUE ELE FAZ: procura, no título, menção a um contato conhecido — executores
de `tonha_role_contacts` primeiro (Andressa, Priscila, Piccino), depois nomes de
contatos com histórico. Propõe o vínculo com a evidência do casamento.

O QUE ELE NÃO FAZ: não adivinha por projeto, não usa similaridade fuzzy, não
liga o que ficou ambíguo. Vínculo errado é pior que vínculo ausente — ele faz o
check F fechar a task errada, e aí o sistema mente com confiança. Na dúvida,
fica de fora e aparece na lista de "não deu".

Uso:
    ./tasks_liga_contato.py                # dry-run: mostra o que faria
    ./tasks_liga_contato.py --apply        # grava
"""
import os
import re
import sys
import unicodedata

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
APPLY = "--apply" in sys.argv

# Menos de 4 letras casa dentro de qualquer palavra ("Ana" em "Analisar",
# "Gui" em "Seguir"). O piso vem de um falso positivo real durante o teste.
MIN_NOME = 4


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    sys.exit(k)


def normaliza(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def candidatos(cur):
    cur.execute("""
        SELECT r.role, r.contact_id, c.nome
          FROM tonha_role_contacts r JOIN contacts c ON c.id = r.contact_id
    """)
    executores = [{"id": r["contact_id"], "nome": r["nome"], "papel": r["role"]}
                  for r in cur.fetchall()]

    # Contatos com histórico real: nome que nunca apareceu numa conversa
    # dificilmente é o dono de uma tarefa, e alarga a chance de colisão.
    cur.execute("""
        SELECT c.id, c.nome FROM contacts c
         WHERE c.nome IS NOT NULL AND length(c.nome) >= %s
           AND EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id)
    """, (MIN_NOME,))
    outros = [{"id": r["id"], "nome": r["nome"], "papel": None} for r in cur.fetchall()]
    return executores, outros


def _citado_como_nome_proprio(titulo, primeiro_nome):
    """O token aparece no título COM inicial maiúscula?

    Sem esta checagem o script ligou "Cobertura de recepção/secretária" ao
    contato *Recepção Buddha Spa* e "contatar torrefação indicada em Portugal"
    à *Torrefação Muzambinho* — que fica em Minas. Dois falsos positivos em 15
    (13%), e cada um faria o check F fechar a tarefa errada, com o sistema
    mentindo com confiança.

    Nome próprio no título vem capitalizado; substantivo comum, não. É um sinal
    barato e do lado certo do erro: título todo em minúscula deixa de casar, e
    não ligar é sempre reversível.
    """
    for m in re.finditer(rf"\b\w+\b", titulo):
        if normaliza(m.group(0)) == primeiro_nome and m.group(0)[:1].isupper():
            return True
    return False


def acha(titulo, executores, outros):
    """(contato, evidência) ou (None, motivo). Ambiguidade nunca vira palpite."""
    t = normaliza(titulo)

    for e in executores:                      # executor tem precedência
        primeiro = normaliza(e["nome"]).split()[0]
        if (len(primeiro) >= MIN_NOME and re.search(rf"\b{re.escape(primeiro)}\b", t)
                and _citado_como_nome_proprio(titulo, primeiro)):
            return e, f"executor '{primeiro}' citado no título"

    achados = []
    for c in outros:
        partes = [p for p in normaliza(c["nome"]).split() if len(p) >= MIN_NOME]
        if (partes and re.search(rf"\b{re.escape(partes[0])}\b", t)
                and _citado_como_nome_proprio(titulo, partes[0])):
            achados.append(c)
    if len(achados) == 1:
        return achados[0], f"nome '{achados[0]['nome'].split()[0]}' citado no título"
    if len(achados) > 1:
        nomes = ", ".join(a["nome"].split()[0] for a in achados[:4])
        return None, f"AMBÍGUO ({len(achados)}: {nomes}) — não liga"
    return None, "nenhum contato citado no título"


def main():
    conn = psycopg2.connect(env("DATABASE_URL"), connect_timeout=30)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    executores, outros = candidatos(cur)

    cur.execute("""
        SELECT id, titulo FROM tasks
         WHERE status IN ('pending', 'in_progress', 'on_hold') AND contact_id IS NULL
         ORDER BY data_criacao DESC
    """)
    orfas = cur.fetchall()

    liga, fora = [], []
    for t in orfas:
        c, motivo = acha(t["titulo"], executores, outros)
        (liga if c else fora).append((t, c, motivo))

    print(f"tasks abertas sem contact_id: {len(orfas)}")
    print(f"  ligáveis pelo título ....... {len(liga)}")
    print(f"  sem casamento seguro ....... {len(fora)}\n")

    for t, c, motivo in liga:
        print(f"  #{t['id']} → {c['nome'][:26]:26} (#{c['id']})  {motivo}")
        print(f"        {t['titulo'][:88]}")

    ambiguas = [x for x in fora if "AMBÍGUO" in x[2]]
    if ambiguas:
        print(f"\n  deixadas de fora por ambiguidade ({len(ambiguas)}):")
        for t, _, motivo in ambiguas:
            print(f"  #{t['id']} {t['titulo'][:56]:56} {motivo}")

    if not APPLY:
        print("\ndry-run — nada gravado. Use --apply.")
        return

    for t, c, _ in liga:
        cur.execute("UPDATE tasks SET contact_id = %s WHERE id = %s AND contact_id IS NULL",
                    (c["id"], t["id"]))
    conn.commit()
    print(f"\n{len(liga)} task(s) ligadas. Restam {len(fora)} órfãs — "
          f"nenhum check cruza com elas até que ganhem contato.")


if __name__ == "__main__":
    main()
