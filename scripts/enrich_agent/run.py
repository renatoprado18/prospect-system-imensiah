#!/usr/bin/env python3
"""Enriquecimento de contatos como AGENTE local — no Max, custo de API zero.

POR QUE MIGROU (10/08/2026). Auditoria de custo: `enrichment.contact` era
US$ 9,30/mês, o batch mais gordo do sistema, rodando 1×/dia no cron
`run-auto-enrich` contra a API. Roda desassistido, mas "desassistido" deixou de
ser sinônimo de "API" quando o agente de frentes provou (31/07) que dá pra rodar
trabalho autônomo no Max via launchd.

O GANHO NÃO É SÓ CUSTO. A versão da API montava um pacote fixo de contexto
(N mensagens, M fatos) e pedia ao modelo que concluísse sobre o que coubesse
ali. Aqui o agente DECIDE o que buscar: se o cargo do cadastro contradiz a
assinatura do e-mail, ele investiga; se a pessoa só aparece em grupo, ele
procura no grupo. É a mesma diferença que fez o agente de frentes achar a data
da FAAP que estava 40 caracteres além do corte da janela.

DESENHO, herdado do cos_agent:
  - o AGENTE (`claude -p`) recebe só `COS_RO_URL` — não escreve nada, negado
    pelo Postgres e não por instrução;
  - o RUNNER (este arquivo) grava com `COS_OWNER_URL`.

Por que OWNER e não `cos_agent_rw`: o rw não tem — e não deve ter — UPDATE em
`contacts`. Aquela lista fechada existe para a camada de frentes mexer em
cadastro de FRENTE. Ampliá-la para caber o enriquecimento misturaria dois
regimes de risco diferentes numa credencial só.

Uso:
    source ~/.cos-agent/env
    python3 scripts/enrich_agent/run.py [--limit N] [--contato ID] [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

MAX_QUERIES = 12
MAX_TURNS = 24
AGENT_TIMEOUT_S = 420
# Teto por rodada. O limite real não é dinheiro (no Max não é cobrança) e sim a
# capacidade que o Renato usa pra trabalhar — mesmo raciocínio do cos_agent.
MAX_POR_RODADA = 5
JOB_ID = "enrich-agent-local"


def _env(nome: str) -> str:
    v = (os.getenv(nome) or "").strip()
    if not v:
        print(f"[enrich-agent] {nome} ausente (ver ~/.cos-agent/env)", file=sys.stderr)
        sys.exit(2)
    return v


def _conn(url: str):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


# --------------------------------------------------------------------------
# 1. Triagem — quem precisa de retrato novo
# --------------------------------------------------------------------------
def triar(ro_url: str, limite: int, contato: int = 0) -> list[dict]:
    """Mesma régua do cron que isto substitui: círculo 1-2, sem resumo ou com
    resumo de mais de 30 dias.

    ⚠️ ORDEM IMPORTA POR CAUSA DE LIVELOCK. Em 03/08 mediu-se 4 de 5 contatos
    falhando todo dia, sempre os mesmos: quem falha não atualiza
    `ultimo_enriquecimento`, volta ao topo da fila no dia seguinte e ocupa a
    vaga de novo — os que estavam atrás nunca chegavam a ser tentados. Por isso
    o desempate é por `ultimo_enriquecimento NULLS FIRST` **e** o runner marca
    tentativa mesmo quando falha (ver `marcar_tentativa`).
    """
    with _conn(ro_url) as c, c.cursor() as cur:
        if contato:
            cur.execute("SELECT id, nome, empresa, circulo FROM contacts WHERE id = %s",
                        (contato,))
            return list(cur.fetchall())
        cur.execute("""
            SELECT id, nome, empresa, circulo
              FROM contacts
             WHERE circulo <= 2
               AND (resumo_ai IS NULL OR resumo_ai = ''
                    OR ultimo_enriquecimento < NOW() - INTERVAL '30 days')
             ORDER BY circulo ASC, ultimo_enriquecimento ASC NULLS FIRST,
                      ultimo_contato DESC NULLS LAST
             LIMIT %s
        """, (limite,))
        return list(cur.fetchall())


# --------------------------------------------------------------------------
# 2. O agente — um subprocesso por pessoa, sem credencial de escrita
# --------------------------------------------------------------------------
def retratar(pessoa: dict, ro_url: str) -> dict:
    prompt = (BASE / "prompt_contato.md").read_text(encoding="utf-8")
    prompt = (prompt
              .replace("{CONTACT_ID}", str(pessoa["id"]))
              .replace("{CONTACT_NAME}", pessoa["nome"] or "")
              .replace("{HOJE}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
              .replace("{MAX_QUERIES}", str(MAX_QUERIES)))

    env = {k: v for k, v in os.environ.items() if k not in ("COS_OWNER_URL", "COS_RW_URL")}
    env["COS_RO_URL"] = ro_url

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(MAX_TURNS),
        "--allowedTools", "Bash", "Read", "Grep", "Glob",
        "--disallowedTools", "Write", "Edit", "MultiEdit", "NotebookEdit",
    ]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=AGENT_TIMEOUT_S, env=env, cwd=str(BASE))
    except subprocess.TimeoutExpired:
        return {"contact_id": pessoa["id"], "error": f"timeout > {AGENT_TIMEOUT_S}s"}
    dur = int(time.monotonic() - t0)

    if r.returncode != 0:
        return {"contact_id": pessoa["id"],
                "error": f"exit {r.returncode}: {(r.stderr or '')[:300]}"}

    try:
        env_json = json.loads(r.stdout)
        texto = env_json.get("result") or ""
        custo = env_json.get("total_cost_usd")
    except json.JSONDecodeError:
        texto, custo = r.stdout, None

    ini, fim = texto.find("{"), texto.rfind("}") + 1
    if ini < 0 or fim <= ini:
        return {"contact_id": pessoa["id"], "error": "resposta sem JSON"}
    try:
        d = json.loads(texto[ini:fim])
    except json.JSONDecodeError as e:
        return {"contact_id": pessoa["id"], "error": f"JSON inválido: {e}"}

    return {
        "contact_id": pessoa["id"],
        "nome": pessoa["nome"],
        "resumo": (d.get("resumo") or "").strip(),
        "insights": d.get("insights") or {},
        "oportunidades": d.get("oportunidades") or [],
        "sugestoes": d.get("sugestoes") or [],
        "fatos_novos": d.get("fatos_novos") or [],
        "trajetoria": d.get("trajetoria") or [],
        "nao_consegui_saber": d.get("nao_consegui_saber") or [],
        "_meta": {"duracao_s": dur, "custo_usd": custo, "motor": "agente_local"},
    }


# --------------------------------------------------------------------------
# 3. Escrita — com a credencial do dono, nunca com a do agente
# --------------------------------------------------------------------------
def marcar_tentativa(owner_url: str, contact_id: int, erro: str) -> None:
    """Carimba a tentativa mesmo tendo falhado. SEM ISTO HÁ LIVELOCK: quem falha
    volta ao topo da fila amanhã e ocupa a vaga de novo, e quem está atrás nunca
    é tentado. Foi o que aconteceu de 21/07 a 03/08 — 4 de 5 falhando todo dia,
    sempre os mesmos.

    `enriquecimento_status='error'` distingue de quem nunca foi tentado, para a
    fila não confundir "falhou" com "novo".
    """
    try:
        with _conn(owner_url) as c, c.cursor() as cur:
            cur.execute("""UPDATE contacts
                              SET ultimo_enriquecimento = CURRENT_TIMESTAMP,
                                  enriquecimento_status = 'error'
                            WHERE id = %s""", (contact_id,))
            c.commit()
    except Exception as e:                                    # noqa: BLE001
        print(f"[enrich-agent] falha ao marcar tentativa #{contact_id}: {e}",
              file=sys.stderr)


def gravar(owner_url: str, r: dict) -> dict:
    """Grava resumo + insights + fatos. Devolve o placar desta pessoa."""
    placar = {"resumo": False, "fatos": 0}

    # Resumo vazio é resultado legítimo (regra 7 do prompt): a pessoa não tinha
    # material suficiente. Gravar vazio a carimbaria como "enriquecida" e ela
    # sumiria da fila sem nunca ter sido retratada.
    if not r.get("resumo"):
        marcar_tentativa(owner_url, r["contact_id"], "sem material")
        return placar

    insights = dict(r.get("insights") or {})
    insights["oportunidades"] = r.get("oportunidades") or []
    insights["sugestoes"] = r.get("sugestoes") or []

    CATEGORIAS = {"relationship", "professional", "personal", "preference", "opportunity"}

    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())[:120]

    with _conn(owner_url) as c, c.cursor() as cur:
        cur.execute("""UPDATE contacts
                          SET resumo_ai = %s, insights_ai = %s,
                              ultimo_enriquecimento = CURRENT_TIMESTAMP,
                              enriquecimento_status = 'complete'
                        WHERE id = %s""",
                    (r["resumo"], json.dumps(insights, ensure_ascii=False), r["contact_id"]))
        placar["resumo"] = True

        cur.execute("SELECT fato FROM contact_facts WHERE contact_id = %s", (r["contact_id"],))
        existentes = {_norm(x["fato"]) for x in cur.fetchall()}

        for f in (r.get("fatos_novos") or [])[:5]:
            texto = (f.get("fato") or "").strip()
            origem = (f.get("origem") or "").strip()
            cat = (f.get("categoria") or "").strip().lower()
            # Mesmas recusas do cos_agent: sem origem não se audita; repetido
            # vira ruído; categoria fora do enum já deixou lixo em dois idiomas.
            if len(texto) < 15 or not origem or _norm(texto) in existentes:
                continue
            if cat not in CATEGORIAS:
                cat = "professional"
            cur.execute(
                """INSERT INTO contact_facts (contact_id, categoria, fato, confianca, fonte)
                   VALUES (%s, %s, %s, %s, %s)""",
                (r["contact_id"], cat, texto, float(f.get("confianca") or 0.8),
                 f"enrich_agent: {origem}"[:200]),
            )
            existentes.add(_norm(texto))
            placar["fatos"] += 1
        c.commit()
    return placar


def bater_ponto(owner_url: str, n: int) -> None:
    """Batimento para o fallback do cron saber que o Mac rodou hoje."""
    try:
        with _conn(owner_url) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO cron_heartbeats (job_id, fired_at) VALUES (%s, now())",
                (JOB_ID,))
            c.commit()
    except Exception as e:                                    # noqa: BLE001
        print(f"[enrich-agent] batimento falhou (não fatal): {e}", file=sys.stderr)


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=MAX_POR_RODADA)
    ap.add_argument("--contato", type=int, default=0, help="só esta pessoa")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ro_url = _env("COS_RO_URL")
    owner_url = _env("COS_OWNER_URL")

    fila = triar(ro_url, a.limit, a.contato)
    print(f"[enrich-agent] {len(fila)} pessoa(s) na fila")
    for p in fila:
        print(f"   #{p['id']:<7} círculo {p['circulo']}  {(p['nome'] or '')[:44]}")
    if a.dry_run:
        print("[enrich-agent] dry-run — nada executado, nada gravado")
        return 0
    if not fila:
        bater_ponto(owner_url, 0)
        return 0

    t0 = time.monotonic()
    ok = falhas = fatos = 0
    for p in fila:
        r = retratar(p, ro_url)
        if r.get("error"):
            falhas += 1
            print(f"[enrich-agent] #{p['id']} falhou: {r['error']}", file=sys.stderr)
            marcar_tentativa(owner_url, p["id"], r["error"])
            continue
        pl = gravar(owner_url, r)
        if pl["resumo"]:
            ok += 1
        fatos += pl["fatos"]
        print(f"[enrich-agent] #{p['id']} {(p['nome'] or '')[:34]:34s} "
              f"resumo={'sim' if pl['resumo'] else 'SEM MATERIAL'} fatos={pl['fatos']}")

    bater_ponto(owner_url, ok)
    print(f"[enrich-agent] {ok} retrato(s), {fatos} fato(s), {falhas} falha(s) "
          f"em {int(time.monotonic() - t0)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
