"""Veredito do portão inferido do COMPORTAMENTO — para o Renato parar de marcar.

POR QUE EXISTE (07/08/2026, e a frase que originou é dele): *"Deste jeito parece
que eu trabalho para você e não o inverso."* Estava certo. O placar de precisão é
instrumentação para EU calibrar a camada, e virou uma fila de 26 itens esperando
que ele classificasse o desempenho do sistema, com campo de justificativa.

A inversão: o sistema lê o que ele FEZ depois do portão e conclui sozinho; ele só
corrige o que discordar.

ISTO NÃO É O SISTEMA SE AUTO-AVALIANDO, e a distinção é o que torna a mudança
legítima. O placar manual existia porque "a camada julgando a própria qualidade"
é circular. Aqui o juiz é o COMPORTAMENTO OBSERVADO — WhatsApp que ele mandou,
task que fechou, evento que aceitou. É evidência externa, do mesmo tipo que ele
usaria para responder, só que colhida sem custo para ele.

OS DOIS LADOS PRECISAM SER MEDÍVEIS. Um inferidor que só reconhece acerto infla
a precisão e deixa o gate verde para sempre — seria pior que nenhum. Por isso:

    certa   ← houve ato dele (ou de executor) sobre o assunto do pedido
    errada  ← 7 dias sem ato NENHUM e o próprio agente parou de repetir o pedido
              (insistiu, ninguém agiu, ele mesmo desistiu — o pedido evaporou)
    None    ← ambíguo. NÃO grava e NÃO pergunta. Placar com buraco honesto vale
              mais que placar completo por invenção.

E ANTES DE VALER, ELE É MEDIDO CONTRA O HUMANO. Os 32 vereditos que o Renato deu
viram gabarito: `medir_concordancia()` roda o inferidor sobre os mesmos portões e
compara. Baixa concordância significa que a medição precisava dele mesmo — e
então eu digo isso com número, em vez de manter a fila aberta na esperança.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from database import get_db
from services.atos_task import _termos

logger = logging.getLogger(__name__)

DIAS_PARA_AGIR = 7        # janela em que um pedido legítimo costuma virar ação
MIN_TERMOS = 1            # sobreposição mínima entre o pedido e a evidência


def _portoes(cur, dias: int) -> List[Dict]:
    """Portões abertos na janela, um por (dia, frente) — a mesma chave do placar."""
    cur.execute(
        """SELECT run_date, run_at, payload FROM cos_daily_review
            WHERE run_date > CURRENT_DATE - %s ORDER BY run_at""",
        (dias,),
    )
    vistos, out = set(), []
    for r in cur.fetchall():
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                continue
        for f in (p.get("frentes") or []):
            pv = f.get("precisa_de_voce") or {}
            pid = f.get("project_id")
            if not pv.get("sim") or pid is None:
                continue
            chave = (r["run_date"], pid)
            pedido = pv.get("o_que") or pv.get("pergunta") or ""
            if chave in vistos:
                # Mesma frente reaberta no mesmo dia: guarda o ÚLTIMO pedido —
                # é o que ficou de pé no fim do dia.
                out[-1]["pedido"] = pedido if out[-1]["chave"] == chave else out[-1]["pedido"]
                continue
            vistos.add(chave)
            out.append({"chave": chave, "run_date": r["run_date"], "run_at": r["run_at"],
                        "project_id": pid, "frente": f.get("frente"), "pedido": pedido})
    return out


def _pediu_de_novo_depois(cur, project_id: int, run_date, termos: set) -> bool:
    """O agente voltou a pedir a MESMA coisa depois? Então ele não desistiu — e
    'cobrou à toa' fica prematuro: o assunto ainda está de pé."""
    cur.execute(
        """SELECT payload FROM cos_daily_review
            WHERE run_date > %s AND run_date <= %s + %s ORDER BY run_at""",
        (run_date, run_date, DIAS_PARA_AGIR),
    )
    for r in cur.fetchall():
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                continue
        for f in (p.get("frentes") or []):
            if f.get("project_id") != project_id:
                continue
            pv = f.get("precisa_de_voce") or {}
            if pv.get("sim") and len(termos & _termos(pv.get("o_que") or "")) >= 2:
                return True
    return False


def avaliar(cur, portao: Dict) -> Dict:
    """Veredito de UM portão, com a evidência que o sustenta.

    Devolve sempre o raciocínio junto — `motivo` e `evidencia` — porque um
    veredito automático sem o porquê é exatamente o tipo de número que ninguém
    consegue contestar depois.
    """
    termos = _termos(portao["pedido"])
    if not termos:
        return {**portao, "veredito": None, "motivo": "pedido sem termos indexáveis"}

    # Pessoas ligadas à frente. A ficha do próprio Renato fica fora: mensagem da
    # camada pra ele não é ato dele ([[feedback_maquina_ouve_o_proprio_eco]]).
    cur.execute(
        """SELECT DISTINCT pm.contact_id AS p FROM project_members pm
            WHERE pm.project_id = %s AND pm.contact_id IS NOT NULL
              AND pm.contact_id <> COALESCE((SELECT contact_id FROM users WHERE id=1), -1)""",
        (portao["project_id"],),
    )
    pessoas = [r["p"] for r in cur.fetchall()]

    ato = None
    if pessoas:
        cur.execute(
            """SELECT a.quem, a.canal, a.quando, a.evidencia
                 FROM atos_que_resolvem a
                WHERE a.pessoa_id = ANY(%(p)s) AND a.quando > %(de)s
                  AND a.quando < %(de)s::timestamp + make_interval(days => %(d)s)
                ORDER BY a.quando LIMIT 200""",
            {"p": pessoas, "de": portao["run_at"], "d": DIAS_PARA_AGIR},
        )
        for a in cur.fetchall():
            if len(termos & _termos(a.get("evidencia") or "")) >= MIN_TERMOS:
                ato = dict(a)
                break

    if ato:
        return {**portao, "veredito": "certa",
                "motivo": f"ele agiu sobre o assunto em {ato['quando']:%d/%m} ({ato['canal']})",
                "evidencia": (ato.get("evidencia") or "")[:200]}

    # Task do projeto concluída na janela também é ação — nem tudo passa por
    # mensagem (pagar, assinar, entregar).
    cur.execute(
        """SELECT titulo, data_conclusao FROM tasks
            WHERE project_id = %(proj)s AND status = 'completed'
              AND data_conclusao > %(de)s
              AND data_conclusao < %(de)s::timestamp + make_interval(days => %(d)s)""",
        {"proj": portao["project_id"], "de": portao["run_at"], "d": DIAS_PARA_AGIR},
    )
    for t in cur.fetchall():
        if len(termos & _termos(t["titulo"])) >= MIN_TERMOS:
            return {**portao, "veredito": "certa",
                    "motivo": f"task do assunto concluída em {t['data_conclusao']:%d/%m}",
                    "evidencia": t["titulo"][:200]}

    # Nada aconteceu. Só vira "cobrou à toa" se o próprio agente tiver desistido
    # de pedir: enquanto ele repete, o assunto continua de pé e o Renato pode
    # simplesmente não ter chegado lá ainda.
    if _pediu_de_novo_depois(cur, portao["project_id"], portao["run_date"], termos):
        return {**portao, "veredito": None,
                "motivo": "sem ação, mas o agente segue pedindo — ainda de pé"}

    cur.execute("SELECT max(run_date) AS ate FROM cos_daily_review")
    ate = cur.fetchone()["ate"]
    if ate and (ate - portao["run_date"]).days < DIAS_PARA_AGIR:
        return {**portao, "veredito": None,
                "motivo": f"janela de {DIAS_PARA_AGIR}d ainda não fechou"}

    return {**portao, "veredito": "errada",
            "motivo": f"{DIAS_PARA_AGIR} dias sem ato nenhum e o agente parou de pedir"}


def aplicar(dias: int = 14) -> Dict:
    """Grava os vereditos inferidos — como SENTINELA, nunca como gabarito.

    Medido em 07/08 contra os 32 vereditos do Renato: **80% de concordância,
    62% de cobertura, e as 4 divergências TODAS no mesmo sentido** (o inferidor
    disse "certa" onde ele disse "cobrou à toa"). O viés é sistemático e
    otimista — ele confunde "o Renato agiu naquilo dias depois" com "o portão
    estava certo em cobrar hoje".

    Por isso esta função grava com `origem='inferido'` e o resto do sistema
    NUNCA soma os dois: um inferidor otimista alimentando o placar deixaria o
    gate do caminho A verde por construção, que é o pior defeito possível num
    número que existe pra autorizar uma decisão.

    Para que serve, então: TENDÊNCIA. Rodando todo dia sobre a mesma régua, uma
    queda brusca da taxa inferida é sinal de que a camada piorou — e aí vale
    pedir uma amostra humana nova. É vigia, não juiz.
    """
    gravados, por_veredito = 0, {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT run_date, project_id FROM cos_portao_veredito
                WHERE COALESCE(origem, 'humano') = 'humano'""")
        humano = {(r["run_date"], r["project_id"]) for r in cur.fetchall()}

        for p in _portoes(cur, dias):
            if p["chave"] in humano:
                continue          # o julgamento dele manda; não se sobrescreve
            r = avaliar(cur, p)
            if not r["veredito"]:
                continue
            cur.execute(
                """INSERT INTO cos_portao_veredito
                       (run_date, project_id, frente, portao, veredito, nota,
                        motor, origem, evidencia)
                   VALUES (%s,%s,%s,%s,%s,%s,'agente_local','inferido',%s)
                   ON CONFLICT (run_date, frente, portao) DO UPDATE
                     SET veredito = EXCLUDED.veredito,
                         evidencia = EXCLUDED.evidencia
                   WHERE cos_portao_veredito.origem = 'inferido'""",
                (p["run_date"], p["project_id"], p["frente"],
                 f"julgamento da rodada de {p['run_date']}", r["veredito"],
                 r["motivo"], (r.get("evidencia") or "")[:400]),
            )
            gravados += cur.rowcount
            por_veredito[r["veredito"]] = por_veredito.get(r["veredito"], 0) + 1
        conn.commit()
    logger.info(f"veredito_inferido: {gravados} gravados — {por_veredito}")
    return {"gravados": gravados, "por_veredito": por_veredito}


def medir_concordancia(dias: int = 21) -> Dict:
    """Roda o inferidor sobre os portões que o RENATO já julgou e compara.

    É o gate deste módulo: sem concordância alta, ele não substitui ninguém.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT run_date, project_id, frente, veredito FROM cos_portao_veredito
                WHERE COALESCE(origem, 'humano') = 'humano'""")
        humano = {(r["run_date"], r["project_id"]): r["veredito"] for r in cur.fetchall()}
        portoes = [p for p in _portoes(cur, dias) if p["chave"] in humano]

        acertos, divergencias, sem_opiniao = 0, [], 0
        for p in portoes:
            inf = avaliar(cur, p)
            h = humano[p["chave"]]
            if inf["veredito"] is None:
                sem_opiniao += 1
            elif inf["veredito"] == h:
                acertos += 1
            else:
                divergencias.append({"frente": p["frente"], "dia": str(p["run_date"]),
                                     "humano": h, "inferido": inf["veredito"],
                                     "motivo": inf["motivo"]})
    opinou = acertos + len(divergencias)
    return {
        "gabarito": len(humano),
        "comparaveis": len(portoes),
        "opinou": opinou,
        "sem_opiniao": sem_opiniao,
        "acertos": acertos,
        "divergencias": divergencias,
        "concordancia_pct": round(100 * acertos / opinou, 1) if opinou else None,
        # Cobertura importa tanto quanto acerto: um inferidor que só opina em 10%
        # dos casos pode ter 100% de concordância e ainda não servir pra nada.
        "cobertura_pct": round(100 * opinou / len(portoes), 1) if portoes else None,
    }
