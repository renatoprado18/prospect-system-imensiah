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
import re
import unicodedata
from typing import Dict, List, Optional

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


def _match_conselhoos_uuid(nome: str) -> Optional[str]:
    """Tenta achar UUID correspondente no ConselhoOS externo. Best-effort:
    falha silenciosa se CONSELHOOS_DATABASE_URL nao configurado ou conexao
    falhar."""
    try:
        from services.conselhoos_sync import ConselhoOSSyncService

        svc = ConselhoOSSyncService()
        nome_norm = _normalize_name(nome)
        for emp in svc.get_empresas():
            if _normalize_name(emp.get("nome", "")) == nome_norm:
                eid = emp.get("id")
                return str(eid) if eid else None
    except Exception as e:
        logger.warning(f"_match_conselhoos_uuid falhou: {e}")
    return None


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

    conselhoos_uuid = _match_conselhoos_uuid(nome) if auto_match_conselhoos else None
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
