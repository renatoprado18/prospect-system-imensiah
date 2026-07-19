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

Modo topico (flag CRUZAMENTO_TOPICAL, default OFF)
--------------------------------------------------
Por padrao o cruzamento e ESTRITO: so cruza manchetes onde a entidade aparece
NOMEADA no titulo (word-start). Isso mata FP topico (ex: watcher 'Vallen Clinic'
ingeriu 'del Valle' de futebol -> estrito suprime).

Com CRUZAMENTO_TOPICAL=on (ou 1/true), alem das manchetes nomeadas o detector
tambem inclui as manchetes TOPICAS: as demais do watcher daquele projeto que NAO
nomeiam a empresa mas o watcher puxou por TEMA (ex: noticia de mercado de cafe
especial -> projeto Cafe Jabo). Guardrails contra o ruido:
  1. So watchers TEMATICOS geram topicos (>=3 tokens significativos na query;
     watcher de nome curto como 'Vallen Clinic' NAO -> 'del Valle' segue morto).
  2. Cada manchete e marcada com match_type 'named'|'topical' pra a Tonia pesar.
  3. O gate de contato-casado (via tokens da entidade) permanece: topico so
     aflora se o Renato TEM contato naquela entidade.
OFF = comportamento v0 byte-a-byte (nenhum campo extra, nenhuma manchete topica).
"""
from __future__ import annotations

import os
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

# Minimo de tokens SIGNIFICATIVOS (via _entity_tokens: >=4 chars, nao-STOP) pra
# um watcher ser TEMATICO (puxa por assunto, nao por nome de entidade). Watcher
# com <=2 tokens significativos = nome de empresa curto: suas manchetes que NAO
# nomeiam a entidade sao ruido (ex: 'Vallen Clinic' -> ['vallen'], 1 token ->
# 'del Valle' de futebol NAO vira topico; 'Wadhwani Foundation Brasil' ->
# ['wadhwani'], foundation/brasil sao STOP, 1 token -> tambem nome de entidade).
# 'cafe especial Sul de Minas microlote' -> ['especial','minas','microlote'], 3
# tokens -> TEMATICO. Ver _is_topical_watcher e o flag CRUZAMENTO_TOPICAL.
TOPICAL_MIN_TOKENS = 3

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


def _topical_enabled() -> bool:
    """Flag CRUZAMENTO_TOPICAL. Default OFF (estrito). Lido a cada run (strip:
    Vercel/Railway as vezes colam \\n na env var — ver [[feedback_env_var_whitespace]])."""
    return (os.getenv("CRUZAMENTO_TOPICAL") or "off").strip().lower() in ("on", "1", "true")


def _is_topical_watcher(query: str) -> bool:
    """Query parece TEMA (assunto) e nao nome de entidade curto?

    Reusa a MESMA maquina de stopword de _entity_tokens: conta tokens
    significativos (>=4 chars, nao-STOP). >=TOPICAL_MIN_TOKENS => tematico. Isso
    classifica certo os watchers reais: 'cafe especial Sul de Minas microlote' ->
    3 tokens => tematico; 'Vallen Clinic' e 'Wadhwani Foundation Brasil' -> 1
    token cada (sufixos/descritores caem no STOP) => nome de entidade, NAO
    tematico. Escolha conservadora: um tema real de 2 palavras significativas
    (ex 'mercado imobiliario') fica de fora — preferimos perder topico raro a
    reabrir o FP de futebol. So chamado com o flag ON."""
    return len(_entity_tokens(query)) >= TOPICAL_MIN_TOKENS


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    topical_on = _topical_enabled()
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
                    "hits": [],
                })
                # watcher_query POR HIT (nao so o do grupo) — a elegibilidade
                # topica e avaliada no watcher que ORIGINOU a manchete, pois um
                # projeto pode ter watcher-entidade + watcher-tema misturados.
                grp["hits"].append({
                    "title": r["title"],
                    "url": r["url"],
                    "watcher_query": r["watcher_query"] or "",
                })

            # 2) Pra cada projeto, resolve entidade -> tokens -> contatos no CRM.
            for pid, grp in by_project.items():
                # Entidade = empresa_relacionada OU watcher_query. NUNCA o nome do
                # projeto: ele carrega nome de pessoa/descritor ("... — Marcos
                # Tanaka (VP)") que vaza e casa contatos errados (falso-positivo).
                entidade = grp["empresa_relacionada"] or grp["watcher_query"]
                tokens = _entity_tokens(entidade)
                if not tokens:
                    continue

                # So cruza manchetes onde a ENTIDADE aparece NOMEADA no titulo
                # (word-start). O watcher agrega por projeto e puxa ruido topico
                # (ex: watcher 'Vallen Clinic' pega 'del Valle' de futebol) — o
                # cross so vale se a noticia menciona a empresa. Sem manchete
                # qualificada => sem signal.
                token_res = [re.compile(r"\b" + t, re.IGNORECASE) for t in tokens]
                named = [
                    h for h in grp["hits"]
                    if any(rx.search(_norm(h["title"])) for rx in token_res)
                ]
                if topical_on:
                    # Manchetes nomeadas primeiro (marcadas), depois preenche o
                    # teto com topicas: hits NAO-nomeados cujo watcher e tematico.
                    named_titles = {h["title"] for h in named}
                    headlines = [
                        {"title": h["title"], "url": h["url"], "match_type": "named"}
                        for h in named[:MAX_HEADLINES]
                    ]
                    if len(headlines) < MAX_HEADLINES:
                        topical = [
                            h for h in grp["hits"]
                            if h["title"] not in named_titles
                            and _is_topical_watcher(h["watcher_query"])
                        ]
                        headlines += [
                            {"title": h["title"], "url": h["url"], "match_type": "topical"}
                            for h in topical[: MAX_HEADLINES - len(headlines)]
                        ]
                else:
                    # OFF: comportamento v0 byte-a-byte (so nomeadas, sem match_type).
                    headlines = [
                        {"title": h["title"], "url": h["url"]}
                        for h in named
                    ][:MAX_HEADLINES]
                if not headlines:
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
                    "n_noticias": len(headlines),
                    "headlines": headlines,
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
                # Metadata topica so no modo ON (OFF fica byte-a-byte com v0). Se
                # o cruzamento e SO topico (nenhuma manchete nomeia a entidade),
                # reescreve o angulo pra nao overclaimar "saiu noticia sobre X".
                if topical_on:
                    n_topical = sum(1 for h in headlines if h.get("match_type") == "topical")
                    n_named = len(headlines) - n_topical
                    ctx["topical_mode"] = True
                    ctx["n_topical"] = n_topical
                    ctx["n_named"] = n_named
                    if n_topical and not n_named:
                        ctx["angulo"] = (
                            f"Noticia de TEMA ligado a {entidade} (mercado/setor, sem "
                            f"citar a empresa). Voce tem {len(contatos)} contato(s) la"
                            + (" (inclui circulo 1)" if best_circ == 1 else "")
                            + " — contexto de fundo, pese antes de acionar."
                        )
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
