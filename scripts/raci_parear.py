#!/usr/bin/env python3
"""Passo 3 do `docs/RACI_FONTE_UNICA_PLANO.md` — parear as linhas que já existem.

O plano previa este script no §4/passo 3 e ele não havia sido escrito: a tela
`/projetos/{id}/raci/parear` saiu primeiro, e a tela pede **43 confirmações do
Renato**. Destas, 19 são par de TEXTO IDÊNTICO e não precisam de olho humano —
pedir clique nelas é transformar ferramenta em tarefa dele.

O que este script faz, e o limite exato do que ele se permite:

- aplica SÓ o par `exato` (`sugestao_automatica=True`), aquele em que a chave
  normalizada dos dois lados é a MESMA FRASE (`_chave_dedup`: caixa, acento e
  pontuação — nunca nome próprio, nunca similaridade);
- ⛔ **não toca no resíduo.** Os pares de vocabulário divergente ("a Gestora" ×
  "Jéssica") ficam para a tela, porque exigem julgamento. Medido em 05/10 no
  próprio conjunto: "Regra de repasse da Dra. Daniela" × "Acordo de repasse da
  Dra. Sayonê" dá 0.58 por similaridade — duas médicas, dois contratos. Nenhum
  corte automático passa aqui;
- ⛔ **não marca "sem par".** Item sem candidato pode ser execução que nunca
  passou por conselho (o estado correto) ou linha de ata que o outro lado não
  devolveu porque caiu. Os dois casos têm a mesma aparência e consequências
  opostas, então quem decide é quem vê a tela.

`--dry-run` é o DEFAULT: escrever em RACI de cliente não acontece por descuido
de linha de comando. Toda aplicação grava um log de reversão e `--undo` o
desfaz pelos ids registrados.

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/raci_parear.py
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/raci_parear.py --apply
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/raci_parear.py --undo <log.json>
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.raci_matrix import propor_pareamento, definir_par, _chave_dedup  # noqa: E402
from services.tz import now_utc  # noqa: E402

# Projetos com vínculo ConselhoOS 1:1 (plano §4). Despertar (25) e AP Conselhos
# (36) entram pelo mesmo caminho se ganharem itens — por isso a lista é default
# de argumento, não constante escondida.
PROJETOS_VINCULADOS = [24, 26, 25, 36]

# Vocabulário equivalente, POR PROJETO, confirmado pelo Renato em 06/10/2026.
#
# As duas bases nomeiam a mesma pessoa de formas diferentes — o ConselhoOS
# escreve o PAPEL ("a Gestora"), o INTEL escreve o NOME ("Jéssica"). Com a
# equivalência declarada, esses pares deixam de ser *parecidos* e passam a ser
# *idênticos*, o que os tira da similaridade e os devolve à régua exata.
#
# ⛔ O MAPA É JULGAMENTO HUMANO, E SÓ ENTRA AQUI CONFIRMADO. O caso que prova a
# regra: foi proposto `Lara = Aptus` e o Renato DERRUBOU — a **Aptus é a
# empresa**, a Lara é uma das funcionárias dela e o **Amadeo é o chefe**. Casar
# "a Aptus treina X" com "a Lara treina X" fundiria responsabilidade de duas
# pessoas diferentes, que é exatamente o erro que o plano proíbe, só que com
# nome de empresa no lugar de nome de médica. Papel→pessoa só vale quando o
# papel é de UMA pessoa; razão social de fornecedor nunca é.
ALIASES = {
    24: {"gestora": "jessica"},  # Vallen Clinic
}


def _chave_alias(acao: str, project_id: int) -> str:
    """`_chave_dedup` + o vocabulário equivalente declarado em ALIASES.

    Substituição de PALAVRA INTEIRA sobre a chave já normalizada — determinística,
    sem similaridade: ou o termo está lá, ou não está.

    A repetição adjacente é colapsada por uma razão concreta: o INTEL escreve
    "contrato PJ da Jéssica (Gestora)", que depois de canonizar vira
    "jessica jessica", enquanto o lado da ata diz só "da Gestora" → "jessica".
    Sem o colapso, nomear pessoa E papel juntos impediria o casamento que a
    equivalência existe para permitir.
    """
    mapa = ALIASES.get(project_id) or {}
    tokens = [mapa.get(t, t) for t in _chave_dedup(acao).split()]
    out = []
    for t in tokens:
        if not out or out[-1] != t:
            out.append(t)
    return " ".join(out)


def coletar_por_alias(project_ids):
    """Pares que só são idênticos DEPOIS do vocabulário equivalente.

    Roda sobre quem sobrou do passe exato: lê os pendentes sem ponteiro e as
    linhas de ata ainda livres, e casa por `_chave_alias`. Nada de similaridade
    — o resíduo que não casar aqui continua sendo da tela, e deve continuar.
    """
    from services.raci_matrix import _fetch_conselhoos_status, get_db

    out = {"exatos": [], "erros": [], "ambiguos": [], "sem_alias": 0}

    for pid in project_ids:
        if not ALIASES.get(pid):
            out["sem_alias"] += 1
            continue

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT p.id, p.nome, e.conselhoos_empresa_id
                  FROM projects p LEFT JOIN empresas e ON e.id = p.empresa_id
                 WHERE p.id = %s
            """, (pid,))
            proj = cur.fetchone()
            if not proj:
                out["erros"].append({"project_id": pid, "error": "projeto não encontrado"})
                continue
            proj = dict(proj)
            cur.execute("""
                SELECT id, acao, conselhoos_raci_id FROM raci_itens
                 WHERE project_id = %s ORDER BY id
            """, (pid,))
            intel = [dict(r) for r in cur.fetchall()]

        if not proj.get("conselhoos_empresa_id"):
            out["erros"].append({"project_id": pid, "error": "projeto sem vínculo ConselhoOS"})
            continue

        cos_itens, erro = _fetch_conselhoos_status(str(proj["conselhoos_empresa_id"]))
        if erro:
            # Mesma razão do `propor_pareamento`: sem o outro lado não há "não há
            # par", há ignorância — e gravar sobre ignorância congela o erro.
            out["erros"].append({"project_id": pid, "error": f"ConselhoOS indisponível: {erro}"})
            continue

        apontados = {str(i["conselhoos_raci_id"]) for i in intel if i["conselhoos_raci_id"]}
        livres = [c for c in cos_itens if str(c["id"]) not in apontados]
        pendentes = [i for i in intel if not i["conselhoos_raci_id"]]

        ck_intel = Counter(_chave_alias(i["acao"], pid) for i in pendentes)
        ck_cos = Counter(_chave_alias(c["acao"], pid) for c in livres)

        for i in pendentes:
            chave = _chave_alias(i["acao"], pid)
            casados = [c for c in livres if _chave_alias(c["acao"], pid) == chave]
            if not casados:
                continue
            if ck_intel[chave] > 1 or ck_cos[chave] > 1:
                out["ambiguos"].append({"project_id": pid, "projeto": proj["nome"],
                                        "intel_id": i["id"], "acao": i["acao"],
                                        "motivo": "a mesma chave (pós-alias) aparece em mais de uma linha"})
                continue
            c = casados[0]
            out["exatos"].append({
                "project_id": pid, "projeto": proj["nome"],
                "intel_id": i["id"], "acao": i["acao"],
                "conselhoos_raci_id": str(c["id"]),
                "acao_cos": c["acao"], "r_cos": c.get("r"),
            })

    return out


def coletar(project_ids):
    """Pendentes exatos por projeto + o que este script deliberadamente NÃO faz.

    Devolve também `ambiguos`: chave que aparece em mais de um pendente do mesmo
    lado. Nesse caso o `propor_pareamento` escolhe o primeiro livre por ordem de
    id — arbitrário — então o script se recusa e manda para a tela. Medido em
    06/10 em prod: nenhum caso nos projetos 24 e 26, mas a checagem fica porque
    uma ata nova pode criar o empate a qualquer reunião.
    """
    out = {"exatos": [], "residuo": 0, "sem_candidato": 0, "erros": [], "ambiguos": []}

    for pid in project_ids:
        r = propor_pareamento(pid)
        if r.get("error"):
            out["erros"].append({"project_id": pid, "error": r["error"]})
            continue

        pendentes = r.get("pendentes") or []
        nome = (r.get("project") or {}).get("nome") or f"projeto {pid}"

        # Ambiguidade dos dois lados: mesma frase em >1 pendente INTEL, ou a
        # mesma linha de ata oferecida como exata a >1 pendente.
        chaves_intel = Counter(_chave_dedup(p["acao"]) for p in pendentes)
        cos_exatos = Counter(
            p["candidatos"][0]["id"]
            for p in pendentes
            if p.get("sugestao_automatica") and p.get("candidatos")
        )

        for p in pendentes:
            cands = p.get("candidatos") or []
            if not cands:
                out["sem_candidato"] += 1
                continue
            if not p.get("sugestao_automatica"):
                out["residuo"] += 1
                continue

            cos = cands[0]
            motivo_amb = None
            if chaves_intel[_chave_dedup(p["acao"])] > 1:
                motivo_amb = "dois itens do INTEL com a mesma frase"
            elif cos_exatos[cos["id"]] > 1:
                motivo_amb = "a mesma linha de ata casa com dois itens do INTEL"
            if motivo_amb:
                out["ambiguos"].append({"project_id": pid, "projeto": nome,
                                        "intel_id": p["intel_id"], "acao": p["acao"],
                                        "motivo": motivo_amb})
                continue

            out["exatos"].append({
                "project_id": pid, "projeto": nome,
                "intel_id": p["intel_id"], "acao": p["acao"],
                "conselhoos_raci_id": cos["id"],
                "acao_cos": cos["acao"], "r_cos": cos.get("r"),
            })

    return out


def main():
    ap = argparse.ArgumentParser(description="Pareia os pares de texto idêntico do RACI (passo 3).")
    ap.add_argument("--apply", action="store_true", help="grava de verdade (default é dry-run)")
    ap.add_argument("--undo", metavar="LOG", help="reverte os pares de um log de aplicação")
    ap.add_argument("--projetos", type=int, nargs="+", default=PROJETOS_VINCULADOS)
    ap.add_argument("--aliases", action="store_true",
                    help="usa o vocabulário equivalente declarado em ALIASES (papel↔pessoa)")
    args = ap.parse_args()

    if args.undo:
        with open(args.undo) as fh:
            log = json.load(fh)
        pares = log.get("aplicados") or []
        print(f"↩️  revertendo {len(pares)} pares de {args.undo}")
        for p in pares:
            r = definir_par(p["intel_id"], None)
            marca = "✅" if r.get("ok") else f"❌ {r.get('error')}"
            print(f"  {marca} INTEL#{p['intel_id']}  {p['acao'][:60]}")
        return 0

    if args.aliases:
        d = coletar_por_alias(args.projetos)
        d.setdefault("residuo", 0)
        d.setdefault("sem_candidato", 0)
        print("╔═ PASSO 3b — pares idênticos SOB O VOCABULÁRIO EQUIVALENTE ═╗")
        for pid, mapa in ALIASES.items():
            print(f"  projeto {pid}: " + ", ".join(f"{k} → {v}" for k, v in mapa.items()))
        print(f"  ⛔ fora do mapa de propósito: razão social de fornecedor (Aptus) — a Lara e o")
        print(f"     Amadeo são pessoas DELA, e casar empresa com funcionária funde responsabilidade.")
        print()
    else:
        d = coletar(args.projetos)

    print("╔═ PASSO 3 — pareamento dos pares de TEXTO IDÊNTICO ═╗" if not args.aliases else "")
    for e in d["erros"]:
        print(f"  ⚠️  projeto {e['project_id']}: {e['error']}")
    print(f"  pares exatos aplicáveis: {len(d['exatos'])}")
    print(f"  resíduo que FICA para a tela (vocabulário divergente): {d['residuo']}")
    print(f"  sem candidato — só o Renato decide se é 'nunca passou por conselho': {d['sem_candidato']}")
    if d["ambiguos"]:
        print(f"  ⛔ ambíguos, recusados de propósito: {len(d['ambiguos'])}")
        for a in d["ambiguos"]:
            print(f"      INTEL#{a['intel_id']} ({a['projeto']}): {a['motivo']}")
    print()

    por_projeto = {}
    for e in d["exatos"]:
        por_projeto.setdefault(e["projeto"], []).append(e)
    for nome, itens in por_projeto.items():
        print(f"  ── {nome} ({len(itens)})")
        for e in itens:
            print(f"     INTEL#{e['intel_id']} → {e['conselhoos_raci_id'][:8]}…  R={e['r_cos']}")
            print(f"        {e['acao'][:72]}")

    if not args.apply:
        print(f"\n  🔍 DRY-RUN — nada gravado. Para aplicar: --apply")
        print(f"  ⛔ não escrevo os {d['residuo']} de resíduo nem os {d['sem_candidato']} sem candidato: são da tela.")
        return 0

    if not d["exatos"]:
        print("\n  nada a aplicar.")
        return 0

    aplicados, falhas = [], []
    for e in d["exatos"]:
        r = definir_par(e["intel_id"], e["conselhoos_raci_id"])
        if r.get("ok"):
            aplicados.append(e)
        else:
            falhas.append({**e, "error": r.get("error")})

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"raci_parear_log_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(log_path, "w") as fh:
        json.dump({"quando_utc": now_utc().isoformat(), "db_target": os.environ.get("DB_TARGET", "?"),
                   "aplicados": aplicados, "falhas": falhas}, fh, ensure_ascii=False, indent=2)

    print(f"\n  ✅ aplicados: {len(aplicados)}")
    if falhas:
        print(f"  ❌ falhas: {len(falhas)}")
        for f in falhas:
            print(f"     INTEL#{f['intel_id']}: {f['error']}")
    print(f"  ↩️  reversão: --undo {log_path}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
