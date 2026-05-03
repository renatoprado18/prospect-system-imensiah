"""Acoes recomendadas e insights por janela do ciclo de vida de um post LinkedIn.

Janelas baseadas no algoritmo do feed (recomendacao de especialista):
  1h   -> golden hour: algoritmo decide alcance, eng aqui = boost massivo
  6h   -> pico: se vai viralizar, descobre aqui
  24h  -> ~70% do alcance final capturado
  72h  -> long tail consolidando
  168h -> snapshot final, ~95% capturado

Este modulo nao bate em LinkdAPI nem no banco diretamente — apenas mapeia
janela -> {label, descricao, acoes[]}. A camada chamadora (endpoint
/timeline) eh quem busca snapshots e calcula insights comparativos.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Ordem cronologica importante — usada pra "proxima janela esperada"
JANELAS_ORDER = ["1h", "6h", "24h", "72h", "168h"]

# Mapping pra horas (usado em CTEs/queries)
JANELA_HORAS = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "72h": 72,
    "168h": 168,
}

WINDOW_ACTIONS: Dict[str, Dict] = {
    "1h": {
        "label": "Golden hour",
        "descricao": "Algoritmo decidindo alcance. Engaja agora pra dobrar.",
        "acoes": [
            {
                "tipo": "respond_comments",
                "label": "Responder top 5 comentarios",
                "urgent": True,
            },
            {
                "tipo": "pin_if_hot",
                "label": "Pinear se ja tem >50 reactions",
                "condicao": "reacoes > 50",
            },
        ],
    },
    "6h": {
        "label": "Validacao do pico",
        "descricao": "Pico do alcance. Validar se tese funcionou.",
        "acoes": [
            {
                "tipo": "compare_benchmark",
                "label": "Comparar com seu top quartile",
            },
            {
                "tipo": "reshare_reflection",
                "label": "Repostar como reflexao",
                "condicao": "engajamento > media",
            },
        ],
    },
    "24h": {
        "label": "Score consolidado",
        "descricao": "70% do alcance final ja aconteceu.",
        "acoes": [
            {
                "tipo": "compare_benchmark",
                "label": "Score: top/medio/bottom?",
            },
        ],
    },
    "72h": {
        "label": "Long tail",
        "descricao": "Tracking. Nada acionavel.",
        "acoes": [],
    },
    "168h": {
        "label": "Final",
        "descricao": "Arquiva pra benchmark futuro.",
        "acoes": [
            {
                "tipo": "archive",
                "label": "Marcar como arquivado",
            },
        ],
    },
}


def get_window_meta(janela: str) -> Dict:
    """Retorna o dict {label, descricao, acoes} pra uma janela.
    Se janela invalida retorna fallback vazio."""
    return WINDOW_ACTIONS.get(janela, {
        "label": janela,
        "descricao": "",
        "acoes": [],
    })


def evaluate_actions(janela: str, snapshot: Optional[Dict]) -> List[Dict]:
    """Resolve condicionais das acoes contra um snapshot real.

    snapshot: dict com keys impressoes, reacoes, comentarios, etc. Pode ser None
    se a janela ainda nao foi coletada — nesse caso retornamos as acoes sem
    avaliar condicionais (deixa client decidir).

    Retorna lista de acoes com campo 'aplicavel' (bool) preenchido pra
    aquelas que tem 'condicao'.
    """
    meta = get_window_meta(janela)
    acoes = meta.get("acoes") or []
    out: List[Dict] = []
    for acao in acoes:
        item = dict(acao)
        condicao = item.get("condicao")
        if condicao and snapshot:
            try:
                # Avaliacao limitada: so leemos campos do snapshot.
                ctx = {
                    "reacoes": int(snapshot.get("reacoes") or 0),
                    "comentarios": int(snapshot.get("comentarios") or 0),
                    "impressoes": int(snapshot.get("impressoes") or 0),
                    "compartilhamentos": int(snapshot.get("compartilhamentos") or 0),
                }
                ctx["engajamento"] = ctx["reacoes"] + ctx["comentarios"] + ctx["compartilhamentos"]
                # Suporta comparacao com 'media' como sentinela — sem dados historicos
                # vira False; client recebe e nao mostra a acao.
                ctx["media"] = float("inf")
                # eval restrito — safe pq dict de strings hardcoded em WINDOW_ACTIONS
                item["aplicavel"] = bool(eval(condicao, {"__builtins__": {}}, ctx))
            except Exception:
                item["aplicavel"] = False
        else:
            # Sem condicao = sempre aplicavel
            item["aplicavel"] = True
        out.append(item)
    return out


def janelas_esperadas_para(horas_desde_pub: float) -> List[str]:
    """Lista de janelas que ja deveriam ter sido coletadas dado um delta em horas."""
    return [j for j in JANELAS_ORDER if JANELA_HORAS[j] <= horas_desde_pub]


def proxima_janela(horas_desde_pub: float) -> Optional[str]:
    """Proxima janela ainda nao alcancada (ou None se ja passou de 168h)."""
    for j in JANELAS_ORDER:
        if JANELA_HORAS[j] > horas_desde_pub:
            return j
    return None


def computar_insight(snapshot: Optional[Dict], stats_user: Optional[Dict]) -> str:
    """Gera string curta comparando engajamento do snapshot com benchmark do user.

    stats_user: {avg_eng, p20_eng, p80_eng} — agregados historicos do user.
    Retorna string com emoji + label. Vazio se nao da pra calcular.
    """
    if not snapshot:
        return ""
    reacoes = int(snapshot.get("reacoes") or 0)
    comentarios = int(snapshot.get("comentarios") or 0)
    compartilhamentos = int(snapshot.get("compartilhamentos") or 0)
    eng = reacoes + comentarios + compartilhamentos
    if not stats_user or not stats_user.get("avg_eng"):
        return ""
    p80 = stats_user.get("p80_eng") or 0
    p20 = stats_user.get("p20_eng") or 0
    avg = stats_user.get("avg_eng") or 0
    if p80 and eng >= p80:
        return "Top 20% dos seus posts"
    if p20 and eng <= p20:
        return "Abaixo da media (bottom 20%)"
    if avg and eng >= avg:
        return "Acima da media"
    return "Performance regular"
