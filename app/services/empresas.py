"""
F2 — Empresas como entidade.

Tabela `empresas` agrega contatos/sinais por empresa. ConselhoOS continua em
app externo (CONSELHOOS_DATABASE_URL via services/conselhoos_sync.py); F2 liga
via campo `conselhoos_empresa_id` UUID quando ha match.

Empresas nascem on-demand: nenhuma migracao retroativa de contacts.empresa
TEXT. `contacts.empresa_id` FK convive com `contacts.empresa` TEXT (legado).
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)


def _normalize_name(nome: str) -> str:
    """Normaliza pra match case/acento-insensitive: lower, sem acentos,
    pontuacao trivial removida, whitespace colapsado."""
    s = (nome or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[.,;]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def get_by_id(empresa_id: int) -> Optional[Dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM empresas WHERE id = %s", (empresa_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def find_by_name(nome: str) -> Optional[Dict]:
    """Lookup case-insensitive em nome_canonico OU dentro de aliases.
    Retorna None se nada bate. NAO cria."""
    nome_norm = _normalize_name(nome)
    if not nome_norm:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM empresas
            WHERE LOWER(nome_canonico) = %s
               OR aliases ? %s
            ORDER BY id
            LIMIT 1
            """,
            (nome_norm, nome),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_paginated(
    limit: int = 50,
    offset: int = 0,
    setor: Optional[str] = None,
    with_aggregates: bool = True,
) -> List[Dict]:
    """Lista empresas. Se with_aggregates=True, inclui count_contatos +
    health_avg + last_interaction (LEFT JOIN contacts)."""
    params: list = []
    where = ""
    if setor:
        where = "WHERE e.setor = %s"
        params.append(setor)

    if with_aggregates:
        sql = f"""
            SELECT e.*,
                   COUNT(c.id) AS count_contatos,
                   MAX(c.ultimo_contato) AS last_interaction,
                   AVG(NULLIF(c.health_score, 0))::int AS health_avg
            FROM empresas e
            LEFT JOIN contacts c ON c.empresa_id = e.id
            {where}
            GROUP BY e.id
            ORDER BY count_contatos DESC NULLS LAST, LOWER(e.nome_canonico)
            LIMIT %s OFFSET %s
        """
    else:
        sql = f"""
            SELECT e.* FROM empresas e
            {where}
            ORDER BY LOWER(e.nome_canonico)
            LIMIT %s OFFSET %s
        """
    params.extend([limit, offset])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def get_with_contacts(empresa_id: int) -> Optional[Dict]:
    """Detalhe + contatos linkados (top primeiro por health)."""
    emp = get_by_id(empresa_id)
    if not emp:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, nome, apelido, cargo, foto_url, health_score, circulo,
                   ultimo_contato, linkedin
            FROM contacts
            WHERE empresa_id = %s
            ORDER BY health_score DESC NULLS LAST,
                     ultimo_contato DESC NULLS LAST
            """,
            (empresa_id,),
        )
        contatos = [dict(r) for r in cur.fetchall()]
    emp["contatos"] = contatos
    emp["count_contatos"] = len(contatos)
    if contatos:
        emp["health_avg"] = (
            sum(c.get("health_score") or 0 for c in contatos) // max(1, len(contatos))
        )
        emp["last_interaction"] = max(
            (c.get("ultimo_contato") for c in contatos if c.get("ultimo_contato")),
            default=None,
        )
    else:
        emp["health_avg"] = None
        emp["last_interaction"] = None
    return emp


def _match_conselhoos_uuid(nome: str) -> Tuple[Optional[str], str]:
    """Tenta achar UUID correspondente no ConselhoOS externo.

    Retorna (uuid_or_none, reason). reasons:
    - 'env_missing': CONSELHOOS_DATABASE_URL nao setado
    - 'conn_error: <details>': falha conexao/timeout Neon externo
    - 'no_empresas': endpoint OK mas lista vazia (user_id errado?)
    - 'no_match': listou N empresas, nenhuma bate nome
    - 'matched': achou, uuid valido retornado
    """
    if not (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip():
        logger.info(f"_match_conselhoos_uuid({nome!r}): env_missing")
        return None, "env_missing"

    try:
        from services.conselhoos_sync import get_conselhoos_sync_service

        svc = get_conselhoos_sync_service()
        empresas = svc.get_empresas()
    except Exception as e:
        logger.warning(f"_match_conselhoos_uuid({nome!r}): conn_error: {e}")
        return None, f"conn_error: {e}"

    if not empresas:
        logger.info(f"_match_conselhoos_uuid({nome!r}): no_empresas")
        return None, "no_empresas"

    nome_norm = _normalize_name(nome)
    for emp in empresas:
        if _normalize_name(emp.get("nome", "")) == nome_norm:
            eid = emp.get("id")
            uuid = str(eid) if eid else None
            logger.info(
                f"_match_conselhoos_uuid({nome!r}): matched uuid={uuid}"
            )
            return uuid, "matched"

    logger.info(
        f"_match_conselhoos_uuid({nome!r}): no_match (viu {len(empresas)} empresas)"
    )
    return None, "no_match"


def refresh_conselhoos_match(empresa_id: int) -> Dict:
    """Tenta (re)matchar empresa local com ConselhoOS externo. Diagnostica
    motivo se falha. UPDATE conselhoos_empresa_id soh se achou match.
    Retorna {empresa_id, nome, matched, uuid, reason}."""
    emp = get_by_id(empresa_id)
    if not emp:
        return {"empresa_id": empresa_id, "error": "empresa nao encontrada"}

    nome = emp.get("nome_canonico", "")
    uuid, reason = _match_conselhoos_uuid(nome)
    updated = False
    if uuid and uuid != emp.get("conselhoos_empresa_id"):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE empresas SET conselhoos_empresa_id = %s, "
                "atualizado_em = NOW() WHERE id = %s",
                (uuid, empresa_id),
            )
            conn.commit()
            updated = cur.rowcount > 0

    return {
        "empresa_id": empresa_id,
        "nome": nome,
        "matched": uuid is not None,
        "uuid": uuid,
        "reason": reason,
        "updated": updated,
        "previous_uuid": emp.get("conselhoos_empresa_id"),
    }


def create(
    nome: str,
    *,
    cnpj: Optional[str] = None,
    website: Optional[str] = None,
    setor: Optional[str] = None,
    notas: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    auto_match_conselhoos: bool = True,
) -> Optional[Dict]:
    """Cria empresa. Idempotente: se ja existir por nome (case/acento-insens
    ou em aliases), retorna a existente sem mexer. Se auto_match_conselhoos,
    tenta popular conselhoos_empresa_id via lookup no app externo."""
    nome = (nome or "").strip()
    if not nome:
        return None

    existing = find_by_name(nome)
    if existing:
        return existing

    if auto_match_conselhoos:
        conselhoos_uuid, _reason = _match_conselhoos_uuid(nome)
    else:
        conselhoos_uuid = None
    aliases_json = json.dumps(aliases or [])

    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO empresas
                    (nome_canonico, aliases, cnpj, website, setor, notas,
                     conselhoos_empresa_id)
                VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (nome, aliases_json, cnpj, website, setor, notas, conselhoos_uuid),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"create empresa {nome!r} falhou: {e}")
            return None


def link_contact(empresa_id: int, contact_id: int) -> bool:
    """Associa contact_id -> empresa_id (UPDATE direto). Idempotente."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE contacts SET empresa_id = %s WHERE id = %s",
            (empresa_id, contact_id),
        )
        conn.commit()
        return cur.rowcount > 0


def suggest_contacts(empresa_id: int, limit: int = 100) -> List[Dict]:
    """Sugere contatos pra linkar via fuzzy match em contacts.empresa TEXT.

    3 niveis de confidence:
    - 'high'   : unaccent(lower(empresa)) bate exato com nome ou alias
    - 'medium' : unaccent(lower(empresa)) bate substring estreita (len ate 1.5x)
    - 'low'    : bate primeira palavra forte (>3 chars) do nome

    Filtra contacts.empresa_id IS NULL (so candidatos sem vinculo).
    Requer extension unaccent no Postgres.
    """
    emp = get_by_id(empresa_id)
    if not emp:
        return []

    # Pool de nomes pra match: canonical + aliases
    aliases = emp.get("aliases") or []
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except Exception:
            aliases = []
    nomes = [emp["nome_canonico"]] + [a for a in aliases if isinstance(a, str)]

    # Normalizacao Python (lower, sem acento, pontuacao trivial)
    nomes_norm = [_normalize_name(n) for n in nomes if n]
    nomes_norm = [n for n in nomes_norm if n]
    if not nomes_norm:
        return []

    # Primeira palavra forte pra fallback low — pega de cada nome
    palavras_low = []
    for n in nomes_norm:
        for p in n.split():
            if len(p) > 3 and p not in palavras_low:
                palavras_low.append(p)
                break

    results: Dict[int, Dict] = {}

    with get_db() as conn:
        cur = conn.cursor()

        # HIGH: match exato apos normalize
        for nome_norm in nomes_norm:
            cur.execute(
                """
                SELECT id, nome, apelido, cargo, empresa, foto_url,
                       health_score, ultimo_contato, circulo
                FROM contacts
                WHERE empresa_id IS NULL
                  AND empresa IS NOT NULL
                  AND unaccent(LOWER(TRIM(empresa))) = %s
                ORDER BY health_score DESC NULLS LAST
                LIMIT %s
                """,
                (nome_norm, limit),
            )
            for r in cur.fetchall():
                if r["id"] not in results:
                    results[r["id"]] = {**dict(r), "confidence": "high",
                                        "match_term": nome_norm}

        # MEDIUM: substring bidirecional
        # (a) empresa CONTEM nome_canonico  -> "Despertar" bate "Associacao Despertar"
        # (b) nome_canonico CONTEM empresa  -> "Associacao Despertar" bate empresa="Despertar"
        # Filtro min length 3 evita match espurio com empresa "SA"/"BR"/etc.
        for nome_norm in nomes_norm:
            cur.execute(
                """
                SELECT id, nome, apelido, cargo, empresa, foto_url,
                       health_score, ultimo_contato, circulo
                FROM contacts
                WHERE empresa_id IS NULL
                  AND empresa IS NOT NULL
                  AND LENGTH(unaccent(LOWER(TRIM(empresa)))) >= 3
                  AND (
                    unaccent(LOWER(empresa)) LIKE %s
                    OR %s LIKE '%%' || unaccent(LOWER(TRIM(empresa))) || '%%'
                  )
                ORDER BY health_score DESC NULLS LAST
                LIMIT %s
                """,
                (f"%{nome_norm}%", nome_norm, limit),
            )
            for r in cur.fetchall():
                if r["id"] not in results:
                    results[r["id"]] = {**dict(r), "confidence": "medium",
                                        "match_term": nome_norm}

        # LOW: palavra forte
        for palavra in palavras_low:
            cur.execute(
                """
                SELECT id, nome, apelido, cargo, empresa, foto_url,
                       health_score, ultimo_contato, circulo
                FROM contacts
                WHERE empresa_id IS NULL
                  AND empresa IS NOT NULL
                  AND unaccent(LOWER(empresa)) ~ %s
                ORDER BY health_score DESC NULLS LAST
                LIMIT %s
                """,
                (rf"\m{palavra}\M", limit),  # \m \M = word boundaries no PG regex
            )
            for r in cur.fetchall():
                if r["id"] not in results:
                    results[r["id"]] = {**dict(r), "confidence": "low",
                                        "match_term": palavra}

    # Ordena: high > medium > low, depois health_score desc
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        results.values(),
        key=lambda x: (order[x["confidence"]], -(x.get("health_score") or 0)),
    )[:limit]


def link_contacts_bulk(empresa_id: int, contact_ids: List[int]) -> int:
    """Linka multiplos contatos a uma empresa. Retorna count atualizado.
    Sobrescreve empresa_id existente caso ja tenha (caller decide se filtra)."""
    if not contact_ids:
        return 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE contacts SET empresa_id = %s WHERE id = ANY(%s)",
            (empresa_id, contact_ids),
        )
        conn.commit()
        return cur.rowcount


def update_fields(empresa_id: int, **fields) -> Optional[Dict]:
    """Update parcial. Campos suportados: nome_canonico, cnpj, website,
    setor, notas, aliases (lista, vira jsonb)."""
    allowed = {"nome_canonico", "cnpj", "website", "setor", "notas"}
    sets: list = []
    values: list = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            values.append(v)
        elif k == "aliases":
            sets.append("aliases = %s::jsonb")
            values.append(json.dumps(v or []))
    if not sets:
        return get_by_id(empresa_id)

    sets.append("atualizado_em = NOW()")
    values.append(empresa_id)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE empresas SET {', '.join(sets)} WHERE id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
