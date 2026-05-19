"""
Editorial Rules — interpretacao de hipoteses ativas como regras enforceaveis.

Antes deste servico, a tabela `editorial_hypotheses` era decorativa: o painel
em /editorial permitia criar/encerrar hipoteses, mas nada lia o conteudo pra
filtrar selecao de posts. Resultado: usuario criou H4 "Bloquear categoria
Complexidade e Adaptacao" e o auto_publisher continuou promovendo posts
dessa categoria pq a regra estava hardcoded em outro lugar (auto_publisher.py:154).

Agora hipoteses podem ter coluna `regras JSONB` com lista de dicts no formato:
    [{"action": "block", "target_field": "ai_categoria", "values": ["X", "Y"]}]

get_active_blocklist() agrega todos os values onde action='block' e
target_field='ai_categoria' de hipoteses status='ativa', com fallback pro
comportamento legado (Complexidade) se nao houver nenhuma hipotese com regras.

Por que NAO usar Claude pra parsear titulo/descricao: hipoteses em texto livre
sao ambiguas, custam tokens, e adicionar latencia no caminho critico do cron.
Coluna estruturada eh deterministica e auditavel.
"""
from __future__ import annotations

import logging
import json
from typing import List, Dict, Any, Optional

from database import get_db

logger = logging.getLogger(__name__)

# Fallback: categoria bloqueada legado (PDCA Mes 1, ver auto_publisher.py:154).
# Mantido como default se nao houver hipotese ativa com regras estruturadas —
# preserva comportamento atual e evita regressao quando o pool de hipoteses
# esta vazio ou todas sao informational-only.
LEGACY_BLOCKED_CATEGORIES: List[str] = [
    "Complexidade e Adaptação",
    "Complexidade e Adaptacao",
]


def _parse_regras(raw: Any) -> List[Dict[str, Any]]:
    """Normaliza regras: aceita None, list, dict ou JSON string."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
            if isinstance(parsed, dict):
                return [parsed]
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def get_active_blocklist(target_field: str = "ai_categoria") -> List[str]:
    """Devolve lista de valores bloqueados pra target_field (default ai_categoria).

    Le `editorial_hypotheses WHERE status='ativa'` e agrega `regras` onde
    action='block' e target_field bate. Sem hipoteses ativas com regras
    estruturadas, devolve LEGACY_BLOCKED_CATEGORIES (Complexidade) — assim
    o comportamento atual eh preservado mesmo apos deploy desta feature.

    Returns:
        Lista deduplicada de strings. Inclui ambas formas (com e sem acento)
        pq SQL faz comparacao exata.
    """
    blocked: List[str] = []
    found_structured = False

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT regras FROM editorial_hypotheses
                WHERE status = 'ativa' AND regras IS NOT NULL
            """)
            for row in cursor.fetchall():
                regras = _parse_regras(row.get("regras"))
                if regras:
                    found_structured = True
                for r in regras:
                    if r.get("action") != "block":
                        continue
                    if r.get("target_field") != target_field:
                        continue
                    values = r.get("values") or []
                    if isinstance(values, str):
                        values = [values]
                    for v in values:
                        if v and isinstance(v, str):
                            blocked.append(v)
    except Exception:
        # Fail-safe: erro de DB nao pode quebrar o cron de auto-publish.
        # Fallback pro legado preserva comportamento anterior ao bug fix.
        logger.exception("get_active_blocklist: erro lendo hipoteses, usando legado")
        return list(LEGACY_BLOCKED_CATEGORIES)

    if not found_structured:
        # Nenhuma hipotese ativa com regras estruturadas — preserva legado.
        # Comportamento documentado: ate o user explicitar regras, mantemos
        # Complexidade bloqueada (eng% 0.44% no Mes 1, motivo da regra original).
        return list(LEGACY_BLOCKED_CATEGORIES)

    # Dedup preservando ordem
    seen = set()
    result: List[str] = []
    for v in blocked:
        if v not in seen:
            seen.add(v)
            result.append(v)

    # Garante variante sem acento pra cada item com acento — defesa em
    # profundidade pq registros antigos podem ter sido salvos sem acento.
    accent_map = {
        "Complexidade e Adaptação": "Complexidade e Adaptacao",
        "Governança Corporativa": "Governanca Corporativa",
        "Liderança Executiva": "Lideranca Executiva",
        "Gestão de Riscos": "Gestao de Riscos",
        "Estratégia Empresarial": "Estrategia Empresarial",
        "Transformação Digital": "Transformacao Digital",
        "M&A e Fusões": "M&A e Fusoes",
        "ESG e Sustentabilidade": "ESG e Sustentabilidade",
    }
    extra: List[str] = []
    for v in result:
        alt = accent_map.get(v)
        if alt and alt not in seen:
            seen.add(alt)
            extra.append(alt)
    return result + extra


def get_active_schedule_constraints() -> Dict[str, Any]:
    """Devolve constraints de agendamento derivadas das hipoteses ativas.

    Antes: H#2 (16h BRT) e H#3 (Ter/Qui/Sex) tinham regras=NULL, entao o painel
    de auto-publish enforce-ava mas o modal manual "Agendar Hot Take" hardcodava
    seg-sex e horarios 9h/12h/17h. User reportava "opcoes fora das hipoteses".

    Le hipoteses com regras estruturadas:
      {"action": "restrict_weekdays", "values": [1,3,4]}   # Ter=1, Qui=3, Sex=4
      {"action": "restrict_hours", "values": [16, 21]}     # horas locais BRT

    Returns:
        {
            "allowed_weekdays": [1,3,4] | None  # None = sem restricao
            "allowed_hours": [16, 21] | None
            "source_hypotheses": [N, ...]       # numeros das hipoteses contribuintes
        }
    """
    allowed_weekdays: Optional[List[int]] = None
    allowed_hours: Optional[List[int]] = None
    sources: List[int] = []

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, regras FROM editorial_hypotheses
                WHERE status = 'ativa' AND regras IS NOT NULL
            """)
            for row in cursor.fetchall():
                regras = _parse_regras(row.get("regras"))
                contributed = False
                for r in regras:
                    action = r.get("action")
                    values = r.get("values") or []
                    if isinstance(values, (int, str)):
                        values = [values]
                    if action == "restrict_weekdays":
                        weekdays = [int(v) for v in values if str(v).lstrip('-').isdigit()]
                        if weekdays:
                            allowed_weekdays = sorted(set(weekdays))
                            contributed = True
                    elif action == "restrict_hours":
                        hours = [int(v) for v in values if str(v).lstrip('-').isdigit() and 0 <= int(v) <= 23]
                        if hours:
                            allowed_hours = sorted(set(hours))
                            contributed = True
                if contributed:
                    sources.append(row.get("id"))
    except Exception:
        logger.exception("get_active_schedule_constraints: erro lendo hipoteses")

    return {
        "allowed_weekdays": allowed_weekdays,
        "allowed_hours": allowed_hours,
        "source_hypotheses": sources,
    }


def build_block_clause(
    column: str = "ai_categoria",
    target_field: str = "ai_categoria",
) -> tuple[str, tuple]:
    """Devolve (sql_fragment, params) pra usar em WHERE clause.

    Uso:
        clause, params = build_block_clause("ep.ai_categoria")
        cursor.execute(f"SELECT ... WHERE status='draft' {clause}", base_params + params)

    sql_fragment vira `AND COALESCE(column, '') NOT IN %s` (sempre comeca com AND).
    Se blocklist vazio (caso degenerado, nao deveria ocorrer): devolve clause vazia.
    """
    blocked = get_active_blocklist(target_field)
    if not blocked:
        return "", tuple()
    fragment = f" AND COALESCE({column}, '') NOT IN %s"
    return fragment, (tuple(blocked),)
