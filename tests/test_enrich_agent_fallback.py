"""O enriquecimento migrou pro Mac — e o cron virou rede de segurança.

Contexto (10/08/2026): `enrichment.contact` custava US$ 9,30/mês. Medindo a
distribuição horária, 174 das 201 chamadas caíam às 05h UTC — o cron
`run-auto-enrich`, US$ 7,96, 86% do total. O resto vem de outro caminho e NÃO é
capturado por esta migração.

O trabalho passou para `scripts/enrich_agent/` (launchd, Max, custo zero), que
além de não custar decide o que buscar em vez de receber janela fixa. Isso
importa: os 10 contatos travados em `error: API_ERROR` eram justamente os de
maior volume (27 mil interações) — contexto grande demais para um pacote fixo.

Estes testes guardam o que faz a migração ser segura em vez de só barata:
  1. o cron pula quando o Mac rodou — senão os dois rodam e não se economiza nada
  2. o cron RODA quando o Mac não rodou — senão a fila para em silêncio
  3. erro na checagem roda, não pula
  4. o batimento existe, senão o fallback não tem como saber

Rodar: PYTHONPATH=app .venv/bin/python -m pytest tests/test_enrich_agent_fallback.py -v
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

_MAIN = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
_RUNNER = (_ROOT / "scripts" / "enrich_agent" / "run.py").read_text(encoding="utf-8")
_PLIST = (_ROOT / "scripts" / "enrich_agent" /
          "com.almeidaprado.enrich-agent.plist").read_text(encoding="utf-8")


def _trecho_do_cron() -> str:
    i = _MAIN.index("run-auto-enrich: FALLBACK")
    return _MAIN[i - 1400:i + 400]


def test_cron_checa_o_batimento_do_agente():
    t = _trecho_do_cron()
    assert "enrich-agent-local" in t, "o cron não sabe procurar o batimento do Mac"
    assert "CURRENT_DATE" in t, "sem recorte de hoje, o batimento de ontem conta como hoje"


def test_cron_pula_quando_o_mac_rodou():
    t = _trecho_do_cron()
    assert '"status": "skipped"' in t and "custo_evitado" in t


def test_erro_na_checagem_roda_e_nao_pula():
    """Abster deixaria a fila parada justamente no dia em que a verificação
    falhou — a guarda que se abstém fabrica o defeito que devia impedir."""
    t = _trecho_do_cron()
    i = t.index("checagem do agente local falhou")
    assert "_mac_rodou = False" in t[i:i + 200], (
        "no except o padrão tem que ser RODAR, não pular"
    )


def test_runner_bate_ponto():
    """Sem batimento o fallback não tem como saber que o Mac trabalhou — e
    passaria a rodar todo dia, gastando igual."""
    assert 'JOB_ID = "enrich-agent-local"' in _RUNNER
    assert "INSERT INTO cron_heartbeats" in _RUNNER


def test_agente_nao_recebe_credencial_de_escrita():
    """A garantia central: o subprocesso lê, o runner escreve. Vazar
    COS_OWNER_URL ou COS_RW_URL pro ambiente do agente acabaria com ela."""
    i = _RUNNER.index("env = {k: v for k, v in os.environ.items()")
    linha = _RUNNER[i:_RUNNER.index("\n", i)]
    assert "COS_OWNER_URL" in linha and "COS_RW_URL" in linha, (
        f"o agente pode estar recebendo credencial de escrita: {linha}"
    )


def test_resumo_vazio_nao_carimba_como_enriquecido():
    """Resumo vazio é resultado legítimo (pessoa sem material). Gravar vazio a
    tiraria da fila sem nunca ter sido retratada."""
    i = _RUNNER.index('if not r.get("resumo"):')
    trecho = _RUNNER[i:i + 260]
    assert "marcar_tentativa" in trecho and "return placar" in trecho


def test_falha_marca_tentativa_contra_livelock():
    """Quem falha e não é carimbado volta ao topo da fila amanhã e ocupa a vaga
    de novo: foi assim que 4 de 5 falharam todo dia de 21/07 a 03/08, sempre os
    mesmos, e quem estava atrás nunca era tentado."""
    assert "def marcar_tentativa" in _RUNNER
    i = _RUNNER.index("if r.get(\"error\"):")
    assert "marcar_tentativa" in _RUNNER[i:i + 400]


def test_fila_ordena_contra_o_livelock():
    assert "ultimo_enriquecimento ASC NULLS FIRST" in _RUNNER, (
        "sem este desempate os que falham monopolizam as vagas"
    )


def test_quem_falhou_volta_para_a_fila():
    """Os 10 travados em `error: API_ERROR` tinham `ultimo_enriquecimento` de
    hoje (o cron carimba ao falhar), então a régua dos 30 dias os deixaria fora
    até setembro — e o agente que resolve exatamente esse caso nunca os veria.

    O desempate por `ultimo_enriquecimento ASC` impede que voltar à fila vire
    monopólio: quem falhou hoje vai para o fim.
    """
    assert "enriquecimento_status LIKE 'error%%'" in _RUNNER, (
        "quem falhou fica 30 dias fora da fila"
    )


def _horario_plist(texto: str) -> tuple:
    import re
    bloco = texto[texto.index("<key>StartCalendarInterval</key>"):]
    h = int(re.search(r"<key>Hour</key><integer>(\d+)</integer>", bloco).group(1))
    m = int(re.search(r"<key>Minute</key><integer>(\d+)</integer>", bloco).group(1))
    return h, m


def test_plist_roda_depois_do_pmset_e_do_cos_agent():
    """Três restrições reais, e o número sozinho não as expressa:

    · depois do `pmset wakeorpoweron 07:10`, que é o que acorda a máquina — na
      bateria o Mac dorme em 1 minuto e job antes disso simplesmente não roda;
    · depois do cos-agent das 07:12, que leva ~10 min e consome a MESMA cota do
      Max que o Renato usa pra trabalhar;
    · antes do cron das 09:00 BRT, senão a fila chega vazia ao Mac.
    """
    h, m = _horario_plist(_PLIST)
    minutos = h * 60 + m
    assert minutos >= 7 * 60 + 10, "roda antes do pmset acordar a máquina"
    assert minutos >= 7 * 60 + 22, "sobrepõe o cos-agent (07:12 + ~10min)"
    assert minutos < 9 * 60, "roda depois do cron — a fila chega vazia"


def test_cron_roda_depois_do_mac():
    """A ordem é o que faz o fallback significar algo. Com o cron às 02:25 ele
    esvaziava a fila antes de o Mac acordar, e o job do Mac batia ponto todo dia
    sobre trabalho nenhum — saudável na aparência, inútil no efeito."""
    worker = (_ROOT / "workers" / "audio-transcriber" / "main.py").read_text(encoding="utf-8")
    i = worker.index('("run-auto-enrich"')
    linha = worker[i:worker.index("\n", i)]
    assert "hour=12" in linha, f"cron cedo demais para o fallback significar algo: {linha}"


def test_fatos_exigem_origem():
    """Fato sem origem não se audita nem se invalida depois."""
    i = _RUNNER.index("for f in (r.get(\"fatos_novos\")")
    trecho = _RUNNER[i:i + 500]
    assert "not origem" in trecho and "continue" in trecho
