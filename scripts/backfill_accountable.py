#!/usr/bin/env python3
"""Preenche `tasks.accountable_id` a partir do dono escrito no titulo.

DRY-RUN POR PADRAO. Sem `--aplicar` nao escreve nada.

POR QUE (31/08/26). No projeto #26 (Alba) o "A" do RACI nao existia como dado:
51 de 59 tasks com `accountable_id` NULL, inclusive as 6 `delegated` cujo dono
esta em CAIXA ALTA no proprio titulo. O responsavel vivia como TEXTO. Qualquer
RACI gerado a partir do campo saia vazio; gerado do titulo, dependia de alguem
reler linha a linha.

REGRAS DE SEGURANCA (esta e uma escrita em massa no executivo):
  - so toca linha com `accountable_id` IS NULL — nunca sobrescreve;
  - resolve nome -> contato SEM hardcode de id ([[feedback_no_hardcoded_contact_ids]]).
    O Renato sai do e-mail dele em `contacts.emails`; os demais, por nome;
  - AMBIGUIDADE ABSTEM. Dois contatos com o mesmo primeiro nome => nao preenche
    e reporta. Chutar aqui poria a tarefa no nome da pessoa errada, e ninguem
    reveria depois;
  - `--projeto` obrigatorio: nao existe modo "banco inteiro" por acidente.

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/backfill_accountable.py --projeto 26
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/backfill_accountable.py --projeto 26 --aplicar
"""
import argparse
import os
import re
import sys
import unicodedata

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

EMAIL_DONO = "renato@almeida-prado.com"


def _norm(s):
    """Sem acento, minusculo — 'ANDRÉ' e 'André' tem que casar com 'Andre'."""
    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _ficha_do_dono(cur):
    """A ficha do Renato pelo e-mail, nunca por id fixo."""
    # `emails` guarda objetos ({type,email,primary}) OU strings, conforme a
    # origem do registro. Mesmo CASE que o importador do Fathom usa — tratar so
    # um dos formatos daria "nao achei" numa ficha que existe.
    cur.execute(
        """SELECT DISTINCT c.id, c.nome FROM contacts c,
                jsonb_array_elements(COALESCE(c.emails,'[]'::jsonb)) AS e
           WHERE LOWER(CASE
                   WHEN jsonb_typeof(e) = 'object' THEN e->>'email'
                   WHEN jsonb_typeof(e) = 'string' THEN e#>>'{}'
                 END) = %s
           LIMIT 1""",
        (EMAIL_DONO,))
    r = cur.fetchone()
    return (r["id"], r["nome"]) if r else (None, None)


def _pessoas_do_projeto(cur, projeto):
    """Quem ja esta LIGADO a este projeto, por `contact_id` OU `accountable_id`.

    Este e o universo de candidatos, e nao o `contacts` inteiro. Ver a nota em
    `_resolve` — foi aqui que o backfill quase pos tarefa na pessoa errada.

    `accountable_id` entra junto de proposito: o vinculo que ESTE script cria
    hoje passa a valer como evidencia na proxima rodada. Sem isso, cada run
    recomecaria do mesmo ponto cego.
    """
    cur.execute(
        """SELECT DISTINCT c.id, c.nome, c.apelido FROM tasks t
           JOIN contacts c ON c.id IN (t.contact_id, t.accountable_id)
           WHERE t.project_id = %s""", (projeto,))
    return [dict(r) for r in cur.fetchall()]


def _resolve(nome, pessoas, cache):
    """nome -> (contact_id, motivo). (None, motivo) quando abstem.

    ⚠️ CASAR POR NOME NO `contacts` INTEIRO E PERIGOSO, e o dry-run de 31/08
    provou: `SANDRA:` nas 3 tasks delegadas da Alba deu match EXATO com o
    contato #16597, cujo nome completo e literalmente "Sandra" — mas ele e da
    `chpgraf.com.br`. A Sandra da Alba e a **#18951 Sandra Bakker**, dona de 25
    tasks do mesmo projeto. Aplicar aquilo teria posto tres tarefas da Alba no
    nome de uma pessoa de outra empresa, e o "match exato" faria parecer certo.

    Entao o universo de candidatos e quem JA ESTA no projeto — vinculo medido,
    nao semelhanca de string. Primeiro nome que bate com exatamente UMA pessoa
    do projeto resolve; zero ou mais de uma, abstem.
    """
    chave = _norm(nome)
    if chave in cache:
        return cache[chave]
    primeiro = chave.split()[0] if chave.split() else chave

    def _primeiros(p):
        """Primeiro nome do `nome` E do `apelido`. O recap diz `GUILHERME:` e a
        ficha se chama `Gui Zorze` — sem o apelido, o vinculo que o Renato
        confirmou em 31/08 se perderia na proxima rodada."""
        out = set()
        for campo in (p.get("nome"), p.get("apelido")):
            partes = _norm(campo).split()
            if partes:
                out.add(partes[0])
        return out

    bate = [p for p in pessoas if primeiro in _primeiros(p)]
    if len(bate) == 1:
        p = bate[0]
        res = (p["id"], f"no projeto: #{p['id']} {p['nome']}")
    elif not bate:
        res = (None, "ninguem com esse nome esta ligado ao projeto")
    else:
        # Nome completo desempata entre pessoas do proprio projeto.
        exatos = [p for p in bate if _norm(p["nome"]) == chave]
        if len(exatos) == 1:
            res = (exatos[0]["id"],
                   f"no projeto (exato): #{exatos[0]['id']} {exatos[0]['nome']}")
        else:
            nomes = ", ".join(f"#{p['id']} {p['nome']}" for p in bate[:5])
            res = (None, f"AMBIGUO no projeto ({len(bate)}): {nomes}")
    cache[chave] = res
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projeto", type=int, required=True)
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()

    from database import get_db
    from services.raci_parser import parse_raci

    print(f"╔═ BACKFILL accountable_id — projeto #{args.projeto} ═╗")
    print(f"  modo: {'APLICAR' if args.aplicar else 'DRY-RUN (nao escreve)'}\n")

    with get_db() as conn:
        cur = conn.cursor()
        dono_id, dono_nome = _ficha_do_dono(cur)
        print(f"  ficha do Renato (por e-mail): #{dono_id} {dono_nome}\n")
        if not dono_id:
            print("  ⚠️  nao achei a ficha do dono — abortando (sem ela, todo "
                  "'Renato:' viraria abstencao silenciosa)")
            return

        cur.execute(
            """SELECT id, titulo, descricao, status FROM tasks
               WHERE project_id = %s AND accountable_id IS NULL
               ORDER BY id""", (args.projeto,))
        tasks = [dict(r) for r in cur.fetchall()]

        pessoas = _pessoas_do_projeto(cur, args.projeto)
        print(f"  universo de candidatos = {len(pessoas)} pessoa(s) ligada(s) "
              f"ao projeto (nao o contacts inteiro)\n")

        cache, resolvidas, abstidas, sem_dono = {}, [], [], 0
        for t in tasks:
            raci = parse_raci(t["titulo"] or "", t["descricao"] or "")
            if raci.source == "none" or not raci.responsible:
                sem_dono += 1
                continue
            if raci.is_renato:
                resolvidas.append((t, raci.responsible, dono_id,
                                   f"dono: #{dono_id}"))
                continue
            cid, motivo = _resolve(raci.responsible, pessoas, cache)
            (resolvidas if cid else abstidas).append(
                (t, raci.responsible, cid, motivo))

    print(f"  {len(tasks)} sem accountable · {sem_dono} sem dono no texto · "
          f"{len(resolvidas)} resolvidas · {len(abstidas)} abstencoes\n")

    print("  ── RESOLVIDAS")
    for t, nome, cid, motivo in resolvidas:
        print(f"   #{t['id']:>7} [{t['status']:<9}] {nome:<22} -> {motivo}")
        print(f"            {t['titulo'][:88]}")
    if abstidas:
        print("\n  ── ABSTENCOES (nao preenche; chutar poria no nome errado)")
        for t, nome, _cid, motivo in abstidas:
            print(f"   #{t['id']:>7} {nome:<22} {motivo}")

    if not args.aplicar:
        print(f"\n  (dry-run — rode com --aplicar para gravar {len(resolvidas)})")
        return

    with get_db() as conn:
        cur = conn.cursor()
        n = 0
        for t, _nome, cid, _m in resolvidas:
            # Recheca o NULL na escrita: o dry-run e a aplicacao sao dois
            # momentos, e outra sessao pode ter preenchido no meio.
            cur.execute(
                "UPDATE tasks SET accountable_id=%s, atualizado_em=NOW() "
                "WHERE id=%s AND accountable_id IS NULL", (cid, t["id"]))
            n += cur.rowcount
        conn.commit()
    print(f"\n  ✅ {n} task(s) atualizada(s)")


if __name__ == "__main__":
    main()
