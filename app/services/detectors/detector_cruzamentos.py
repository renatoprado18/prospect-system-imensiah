"""detector_cruzamentos — cruzamentos multi-fonte (F-D v0).

Ao contrario dos outros detectores (single-source), este OLHA DUAS FONTES e
emite a conexao que o humano ainda nao fez, como signal estruturado. Ate aqui
o cruzamento so existia no prompt do briefing (a Tonia conectava na hora de
escrever); agora vira signal com dedup/expire proprios.

Sinais:
- cruzamento_noticia_contato — Noticia recente sobre uma entidade vigiada
  (watcher/projeto) onde o Renato TEM contatos no CRM. Conecta evento externo
  a rede. Agregado por projeto (1 signal por entidade, nao por manchete).
  Urgencia 5-6 -> BRIEFING (nunca urgent; anti-ruido gen-1). So emite com >=1
  contato casado.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Set

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_cruzamentos"

# Janela de noticias consideradas "recentes" pro cruzamento.
NEWS_WINDOW_DAYS = 14
# Teto de manchetes/contatos embutidos no contexto (mantem signal enxuto).
MAX_HEADLINES = 5
MAX_CONTATOS = 8

# Tokens genericos que NAO identificam uma entidade — sufixos societarios,
# palavras de nome-de-projeto, ruido. Evita casar "cafe"/"clinic"/"brasil"
# com contatos aleatorios (falso-positivo).
_STOP: Set[str] = {
    # sufixos/descritores societarios
    "grupo", "clinic", "clinica", "consultoria", "associacao", "brasil",
    "group", "holding", "company", "empresa", "conselho", "conselhos",
    "empresarial", "empresariais", "foundation", "ltda", "sociedade",
    "participacoes", "servicos", "solucoes", "tecnologia", "digital",
    # palavras de nome-de-projeto (INTEL) que nao sao a entidade
    "recuperacao", "investimentos", "investimento", "exportacao", "direta",
    "projeto", "onboarding", "prospeccao", "originacao", "cafe", "membership",
}


def _norm(s: str) -> str:
    """lower + sem acento + espaco colapsado — base de comparacao estavel."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())


def _entity_tokens(*names: str) -> List[str]:
    """Tokens significativos (>=4 chars, nao-genericos) das strings de entidade."""
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
        for t in re.findall(r"[a-z0-9]+", _norm(name)):
            if len(t) >= 4 and t not in _STOP and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    try:
        with savepoint(conn, "noticia_contato"):
            # 1) Noticias recentes agrupadas por projeto vigiado.
            cur.execute(
                """
                SELECT w.project_id,
                       p.nome AS project_name,
                       p.empresa_relacionada,
                       w.query AS watcher_query,
                       h.title,
                       h.url,
                       h.hit_at
                FROM project_news_hits h
                JOIN project_news_watchers w ON w.id = h.watcher_id
                LEFT JOIN projects p ON p.id = w.project_id
                WHERE h.hit_at > NOW() - make_interval(days => %s)
                  AND h.archived_at IS NULL
                  AND w.project_id IS NOT NULL
                ORDER BY h.hit_at DESC
                """,
                (NEWS_WINDOW_DAYS,),
            )
            by_project: Dict[int, Dict] = {}
            for r in cur.fetchall():
                pid = r["project_id"]
                grp = by_project.setdefault(pid, {
                    "project_name": r["project_name"] or "",
                    "empresa_relacionada": r["empresa_relacionada"] or "",
                    "watcher_query": r["watcher_query"] or "",
                    "headlines": [],
                })
                if len(grp["headlines"]) < MAX_HEADLINES:
                    grp["headlines"].append({"title": r["title"], "url": r["url"]})
                grp["n_noticias"] = grp.get("n_noticias", 0) + 1

            # 2) Pra cada projeto, resolve entidade -> tokens -> contatos no CRM.
            for pid, grp in by_project.items():
                # Entidade = empresa_relacionada OU watcher_query. NUNCA o nome do
                # projeto: ele carrega nome de pessoa/descritor ("... — Marcos
                # Tanaka (VP)") que vaza e casa contatos errados (falso-positivo).
                entidade = grp["empresa_relacionada"] or grp["watcher_query"]
                tokens = _entity_tokens(entidade)
                if not tokens:
                    continue

                # Casa contatos cujo empresa (texto) COMECA com o token (word-start
                # `\y` sem fronteira final). Pega "Jabô"->"Jaboticabeiras" sem
                # exigir palavra identica; min 4 chars segura a precisao.
                patterns = [r"\y" + t for t in tokens]
                cur.execute(
                    """
                    SELECT id, nome, empresa, circulo, cargo
                    FROM contacts
                    WHERE empresa IS NOT NULL
                      AND unaccent(lower(empresa)) ~* ANY(%s)
                    ORDER BY circulo NULLS LAST, nome
                    LIMIT 50
                    """,
                    (patterns,),
                )
                contatos = cur.fetchall()
                if not contatos:
                    continue

                best_circ = min(
                    (c["circulo"] for c in contatos if c["circulo"] is not None),
                    default=9,
                )
                # circulo 1 -> urg 6; 2 -> 6; 3 -> 5; resto -> 5. Nunca >6 (briefing).
                urg = 6 if best_circ <= 2 else 5

                sh = make_signal_hash("cruzamento_noticia_contato", pid)
                current_hashes.append(sh)
                ctx = {
                    "project_id": pid,
                    "project_name": grp["project_name"],
                    "entidade": entidade,
                    "n_noticias": grp.get("n_noticias", len(grp["headlines"])),
                    "headlines": grp["headlines"],
                    "contatos": [
                        {
                            "nome": c["nome"],
                            "circulo": c["circulo"],
                            "cargo": (c["cargo"] or "")[:80],
                        }
                        for c in contatos[:MAX_CONTATOS]
                    ],
                    "n_contatos": len(contatos),
                    "angulo": (
                        f"Saiu noticia sobre {entidade}. Voce tem "
                        f"{len(contatos)} contato(s) la"
                        + (" (inclui circulo 1)" if best_circ == 1 else "")
                        + " — angulo de reaproximacao / contexto pro relacionamento."
                    ),
                }
                _bump(res, emit_signal(
                    conn,
                    tipo="cruzamento_noticia_contato",
                    signal_hash=sh,
                    urgencia=urg,
                    contexto=ctx,
                    detector=DETECTOR_NAME,
                ))
    except Exception as e:
        res.errors.append(f"noticia_contato: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
