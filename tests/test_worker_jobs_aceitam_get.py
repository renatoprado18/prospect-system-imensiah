"""O JOB QUE NUNCA RODOU — todo cron do worker tem de aceitar GET (05/10/2026).

O `prune-telemetry` foi declarado em 31/07/26 com `CronTrigger(hour=4, minute=40)`
e **nenhuma poda aconteceu** em mais de dois meses. O scheduler do worker chama
todos os jobs por GET (`_call_vercel_cron` -> `client.get(url)`); o endpoint nascera
`@app.post`-only. Resultado: 405 todo dia as 4h40.

Precisao que custou uma conferida: `cron_runs` NAO esta vazia pra esse path — tem
uma linha, de 01/08/26, `success`. Lendo o `result_json` dela: `"dry_run": true`,
1.031 linhas contadas e nenhuma apagada. Era a validacao manual do autor, no dia
seguinte ao de escrever a politica. Entao "0 execucoes em 14 dias" (o que o
inventario mediu) e "a poda nunca rodou" (o que importa) sao ambos verdade, e
"nunca houve linha" seria falso — a unica linha existente prova o oposto do que
parece: alguem testou, viu funcionar por POST, e o agendamento nunca pegou.

O que torna essa classe pior do que uma excecao e a ausencia de rastro: o 405 vem
do roteador do FastAPI, ANTES do handler — portanto antes do `@track_cron_run`.
Nao ha linha de `error` em `cron_runs`, ha **ausencia de linha**. E ausencia nao
aparece em nenhuma regua que agregue por path: o `monitor-cron-health` olha o que
rodou, e um job que nunca roda nao tem o que olhar. Levou dois meses e um
inventario que contava declaracoes (nao execucoes) pra alguem notar.

Custo medido no dia do conserto: 187.331 linhas e 70.534 payloads esperando poda,
banco em 719 MB (eram 397 MB quando a politica de retencao foi escrita), 53% dele
nas tres tabelas de telemetria que este job existe pra podar.

A guarda e barata porque o contrato e simples: o worker so sabe falar GET. Quem
declarar job novo com endpoint POST-only quebra aqui, na hora, em vez de descobrir
em dois meses.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_worker_jobs_aceitam_get.py -q
"""
import os
import re
from pathlib import Path

import pytest

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WORKER = _ROOT / "workers" / "audio-transcriber" / "main.py"
_MAIN = _ROOT / "app" / "main.py"

_JOB_RE = re.compile(r'\(\s*"([^"]+)"\s*,\s*"(/api/cron/[^"?]+)')


def _jobs_ativos():
    """Os (nome, path) de `_SCHEDULER_JOBS` que NAO estao comentados.

    Linha comentada e a forma do kill-switch neste arquivo (ha dezenas), entao
    filtrar por `#` nao e heuristica frouxa — e ler o estado declarado.
    """
    jobs = []
    dentro = False
    for linha in _WORKER.read_text().splitlines():
        if "_SCHEDULER_JOBS = [" in linha:
            dentro = True
            continue
        if dentro and linha.strip() == "]":
            break
        if not dentro:
            continue
        s = linha.strip()
        if s.startswith("#"):
            continue
        m = _JOB_RE.search(s)
        if m:
            jobs.append((m.group(1), m.group(2)))
    return jobs


def _metodos_declarados(fonte: str, path: str):
    """Verbos que o `app/main.py` declara pra esse path exato."""
    metodos = set()
    for kind, lista in re.findall(
        r'@app\.(get|post|api_route)\("' + re.escape(path) + r'"(?:,\s*methods=\[([^\]]*)\])?\)',
        fonte,
    ):
        if kind in ("get", "post"):
            metodos.add(kind.upper())
        else:
            metodos |= {x.strip().strip("\"'").upper() for x in lista.split(",") if x.strip()}
    return metodos


def test_worker_so_sabe_falar_get():
    """Documenta a premissa da guarda. Se o scheduler aprender POST, ela muda."""
    fonte = _WORKER.read_text()
    assert "resp = await client.get(url, headers=headers)" in fonte, (
        "`_call_vercel_cron` nao chama mais `client.get` — a premissa desta guarda "
        "mudou. Reveja o teste ANTES de relaxa-lo: ele existe porque um verbo "
        "divergente nao deixa rastro em cron_runs."
    )


def test_ha_jobs_ativos_pra_verificar():
    """Controle positivo: uma varredura que nao varre nada passa verde.

    Sem esta linha, um regex que deixasse de casar transformaria a guarda num
    certificado de conformidade que nunca checou nada
    ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).
    """
    jobs = _jobs_ativos()
    assert len(jobs) >= 30, (
        f"so {len(jobs)} jobs ativos encontrados — o parser de _SCHEDULER_JOBS "
        "provavelmente quebrou (eram 46 em 05/10/26, ja com os 8 cortes do dia)"
    )


@pytest.mark.parametrize("nome,path", _jobs_ativos(), ids=lambda v: v if isinstance(v, str) else "")
def test_todo_job_ativo_aceita_get(nome, path):
    fonte = _MAIN.read_text()
    metodos = _metodos_declarados(fonte, path)
    assert metodos, (
        f"job `{nome}` aponta pra {path} e nao ha endpoint com esse path em "
        "app/main.py — o worker vai receber 404 todo disparo, calado"
    )
    assert "GET" in metodos, (
        f"job `{nome}` ({path}) so aceita {sorted(metodos)}, mas o scheduler do "
        "worker chama por GET: 405 em cada disparo, e o 405 nasce no roteador — "
        "ANTES do @track_cron_run — entao NAO aparece em cron_runs. Foi assim que "
        "o prune-telemetry passou dois meses sem rodar. Adicione @app.get."
    )


def test_a_guarda_pegaria_o_prune_telemetry_de_antes():
    """Controle positivo da regra, nao do parser.

    Reproduz a fonte como era em 31/07 (POST-only) e exige que `_metodos_declarados`
    recuse. Sem isso, so sabemos que a guarda passa hoje — nao que ela PEGA o
    defeito que a motivou ([[feedback_controle_positivo_pega_o_furo_real]]).
    """
    antes = '@app.post("/api/cron/prune-telemetry")\n@track_cron_run\nasync def x(): ...'
    assert _metodos_declarados(antes, "/api/cron/prune-telemetry") == {"POST"}

    depois = (
        '@app.get("/api/cron/prune-telemetry")\n'
        '@app.post("/api/cron/prune-telemetry")\n'
        "@track_cron_run\nasync def x(): ..."
    )
    assert _metodos_declarados(depois, "/api/cron/prune-telemetry") == {"GET", "POST"}


def test_api_route_com_methods_tambem_conta():
    """Varios crons do repo usam `@app.api_route(..., methods=[...])`.

    Se o parser nao entendesse essa forma, ela viraria falso positivo em massa — e
    a reacao natural seria afrouxar a guarda inteira.
    """
    fonte = (
        '@app.api_route("/api/cron/agent-intents-tick", methods=["GET", "POST"])\n'
        "@track_cron_run\nasync def x(): ..."
    )
    assert _metodos_declarados(fonte, "/api/cron/agent-intents-tick") == {"GET", "POST"}
