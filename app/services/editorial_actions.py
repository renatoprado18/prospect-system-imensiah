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

# Tolerancia ±30% (aprox) por janela: (min_h, max_h).
# - min_h: a partir desse delta a janela "abre" (vale coletar).
# - max_h: depois disso a janela esta perdida se nao foi coletada.
# Cron horario nem sempre roda na hora exata, entao o range absorve drift.
# Ex: janela 72h cobre coletas entre 48h e 100h (target +/- ~30%).
WINDOW_RANGES = {
    "1h":   (0.5,   1.5),
    "6h":   (4.0,   9.0),
    "24h":  (16.0,  32.0),
    "72h":  (48.0,  100.0),
    "168h": (116.0, 220.0),
}


def classify_post_windows(horas_publicado: float, snapshots: list) -> dict:
    """Classifica cada janela (1h/6h/24h/72h/168h) pro post como
    'coletada' | 'aberta' | 'perdida' | 'futura' usando tolerancia.

    snapshots: lista de dicts vindo de editorial_metrics_history. Cada item
    pode ter:
      - janela: '1h'/'6h'/.../'168h' (preferencial — match exato)
      - coletado_em: datetime/iso (fallback — calcula horas e bate range)
      - dias_apos_publicacao: int legado

    Regras por janela j com range (lo, hi):
      coletada se ha snapshot com janela == j (match exato)
                 OR snapshot.coletado_em - data_pub esta em [lo, hi]
                 OR mapping legado dias_apos_publicacao -> janela bate
      aberta   se nao coletada E lo <= horas_publicado <= hi
      perdida  se nao coletada E horas_publicado > hi
      futura   se horas_publicado < lo

    Retorna:
      {
        "horas_publicado": 192.3,
        "janelas": {
          "1h":   {"status": "perdida", "coletado_em": None, "fonte": None},
          "72h":  {"status": "coletada", "coletado_em": "...", "fonte": "..."},
          "168h": {"status": "aberta",   "horas_restantes": 28},
          ...
        },
        "proxima_acao": "168h" | None,
        "urgente": False  # True se proxima_acao ja passou do hi (perdida)
      }
    """
    from datetime import datetime as _dt

    # Indice rapido por janela explicita
    by_janela: dict = {}
    # Snapshots que tem coletado_em mas nao tem janela atribuida
    untagged: list = []
    for s in snapshots or []:
        j = s.get("janela")
        if j and j in WINDOW_RANGES:
            # Mantem o mais recente (em caso de duplicata, ultimo vence)
            existing = by_janela.get(j)
            if not existing:
                by_janela[j] = s
            else:
                # Compara coletado_em
                cur = s.get("coletado_em")
                old = existing.get("coletado_em")
                if cur and (not old or cur > old):
                    by_janela[j] = s
        else:
            untagged.append(s)

    # Mapeamento legado dias_apos_publicacao -> janela (best effort)
    legacy_map = {0: "24h", 1: "24h", 3: "72h", 7: "168h"}
    for s in untagged:
        d = s.get("dias_apos_publicacao")
        if d is None:
            continue
        # >=7 -> 168h
        if isinstance(d, int) and d >= 7:
            j = "168h"
        else:
            j = legacy_map.get(d)
        if j and j not in by_janela:
            by_janela[j] = {**s, "janela": j}

    # Tenta tambem casar untagged via coletado_em em range absoluto
    for s in untagged:
        coletado = s.get("coletado_em")
        if not coletado:
            continue
        try:
            if isinstance(coletado, str):
                coletado_dt = _dt.fromisoformat(coletado.replace("Z", "+00:00"))
            else:
                coletado_dt = coletado
        except Exception:
            continue
        # Calcula horas relativas: precisa do data_publicado.
        # Como o caller passa horas_publicado e data_atual, derivamos:
        # delta_coleta_h = horas_publicado - (NOW() - coletado_em)/3600.
        # Pra simplificar: se coletado_em ja esta no untagged sem janela explicita,
        # confiamos no mapping legacy acima. Skip aqui.
        # (Fica como gancho futuro caso seja necessario.)
        pass

    janelas_status: dict = {}
    proxima_acao = None
    urgente = False

    for j in JANELAS_ORDER:
        lo, hi = WINDOW_RANGES[j]
        target_h = JANELA_HORAS[j]
        snap = by_janela.get(j)
        if snap:
            janelas_status[j] = {
                "status": "coletada",
                "coletado_em": snap.get("coletado_em").isoformat()
                    if hasattr(snap.get("coletado_em"), "isoformat")
                    else snap.get("coletado_em"),
                "fonte": snap.get("fonte"),
            }
        elif horas_publicado < lo:
            janelas_status[j] = {"status": "futura"}
        elif lo <= horas_publicado <= hi:
            # Aberta: dentro do range tolerado. 'urgente' se passou do target.
            janelas_status[j] = {
                "status": "aberta",
                "horas_restantes": round(hi - horas_publicado, 1),
                "atrasada": horas_publicado > target_h,
            }
        else:
            # horas_publicado > hi e nao coletada — perdida pra sempre
            janelas_status[j] = {"status": "perdida"}

    # Proxima acao: SO janela 'aberta' conta. Se nao tem aberta, sem acao.
    # Posts com tudo coletada/perdida/futura saem da fila (caller filtra).
    aberta = next((j for j in JANELAS_ORDER if janelas_status[j]["status"] == "aberta"), None)
    if aberta:
        proxima_acao = aberta
        # Vermelho fixo: cron deveria ter coletado (passou do target dentro do range).
        urgente = bool(janelas_status[aberta].get("atrasada"))

    return {
        "horas_publicado": round(horas_publicado, 1),
        "janelas": janelas_status,
        "proxima_acao": proxima_acao,
        "urgente": urgente,
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
