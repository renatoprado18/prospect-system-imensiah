"""
Guarda de COBERTURA do notification_router: quem manda WhatsApp passa pelo router?

Contexto (medicao de 28/07/2026 contra o Neon de producao, self-chat conv 590,
janela de 14 dias): 117 mensagens saidas, e so 33 tinham linha correspondente em
`channel_decisions`. **84 (71%) passavam por fora do router** — logo por fora do
teto diario de interrupcao ligado em 27/07 e por fora do dedup cross-produtor.

O board registrava apenas dois desses bypasses (`group-digest` e
`raci-weekly-report`, estimados em ~4 msgs/dia). A medicao achou cinco no INTEL,
e o MAIOR produtor de WhatsApp da casa inteira estava entre os nao-registrados:

    19x  raci_smart_updates:657   "Update RACI ... precisa aprovacao"
    15x  main.py:29035            "[INTEL] Cron health alert"
    14x  group_digest.py:65       "Digest dos Grupos"
     7x  raci_group_shadow.py:389 "RACI — N propostas do grupo (shadow)"
     6x  raci_weekly_report:349   "PREVIEW RACI"

Por que um teste e nao so o fix: o bypass nao quebra nada. A mensagem chega, o
cron fica verde, ninguem reclama — ele so torna a REGUA mentirosa (o teto mede a
minoria e reporta sucesso). E exatamente a classe de defeito que so aparece
quando alguem vai medir. Um call-site novo escrito por habito
(`send_intel_notification` esta em toda parte) reabre o buraco em silencio, e a
proxima medicao mente de novo.

Cobre:
  - guarda estrutural: nenhum dos 5 modulos volta a chamar send_intel_notification
  - wiring vivo: cada produtor chama mesmo o router (nao basta importar)
  - o dedup de cada um colapsa repeticao e deixa passar conteudo diferente

Nao cobre (deliberado): o canary de saldo Anthropic ("SEM SALDO"/"recuperado"),
que manda WA direto POR DESENHO — ele precisa funcionar quando o router e a
tonIAH estao caidos, que e justamente o cenario em que ele dispara. E os dois
produtores do repo tonIAH (briefing "Bom dia" 15x + urgent-tick 4x), que sao
outro deploy.

Rodar:
  PYTHONPATH=app .venv/bin/python -m pytest tests/test_notification_router_coverage.py -q
"""
import asyncio
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


# Os 5 produtores INTEL que a medicao pegou fora do router. O caminho e o
# arquivo; o segundo item e so o rotulo que aparece quando o teste falha.
_MIGRATED = [
    ("app/services/group_digest.py", "Digest dos Grupos"),
    ("app/services/raci_smart_updates.py", "Update RACI precisa aprovacao"),
    ("app/services/raci_group_shadow.py", "RACI propostas do grupo (shadow)"),
    ("app/services/raci_weekly_report.py", "PREVIEW RACI"),
]


def _referencias_reais(src: str, nome: str):
    """Linhas onde `nome` aparece como CODIGO, via AST.

    Grep textual nao serve: os cinco modulos migrados carregam um comentario ou
    docstring explicando de onde vieram, e todos citam a funcao antiga pelo
    nome. O AST so enxerga identificador de verdade — chamada, import ou
    atributo — entao a prosa nunca reprova e uma chamada nova nunca escapa.
    """
    import ast

    achados = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name) and node.id == nome:
            achados.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == nome:
            achados.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == nome:
                    achados.append(node.lineno)
    return sorted(set(achados))


@pytest.mark.parametrize("path,rotulo", _MIGRATED)
def test_produtor_nao_volta_a_chamar_send_intel_notification(path, rotulo):
    """Guarda de regressao: o bypass e uma linha de codigo de distancia."""
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    hits = _referencias_reais(src, "send_intel_notification")
    assert not hits, (
        f"{rotulo} ({path}) voltou a mandar WhatsApp por fora do router "
        f"(linhas {hits}). Use services.notification_router.notify() — senao a "
        "mensagem sai, mas nao entra em channel_decisions e o teto diario nao a "
        "governa."
    )


def test_cron_health_no_main_tambem_migrou():
    """O 5o produtor mora no main.py, que tem outros usos legitimos da funcao
    antiga (chat do bot, respostas de audio). Ancora no bloco do alerta."""
    src = open(os.path.join(_ROOT, "app/main.py"), encoding="utf-8").read()
    linha_ancora = src[: src.index('lines = ["[INTEL] Cron health alert:"]')].count("\n") + 1
    # Janela generosa em volta do bloco do alerta; fora dela o main.py TEM usos
    # legitimos da funcao antiga (chat do bot, resposta de audio).
    janela = range(linha_ancora, linha_ancora + 60)

    antigas = [n for n in _referencias_reais(src, "send_intel_notification") if n in janela]
    assert not antigas, (
        f"cron-health voltou a notificar por fora do router (main.py:{antigas})."
    )
    novas = [n for n in _referencias_reais(src, "notify") if n in janela]
    assert novas, "cron-health nao chama notify() — o alerta ficou sem router."


# ---------------------------------------------------------------------------
# Wiring vivo — importar o router nao prova nada; o teste chama a funcao real
# e captura o que chegou em route_to_renato.
# ---------------------------------------------------------------------------
class _Capture:
    """Substitui route_to_renato e guarda as chamadas."""

    def __init__(self, action="sent"):
        self.calls = []
        self.action = action

    async def __call__(self, **kw):
        self.calls.append(kw)
        return {"action": self.action, "pending_id": None}


@pytest.fixture
def cap(monkeypatch):
    from services import notification_router as nr

    c = _Capture()
    monkeypatch.setattr(nr, "route_to_renato", c)
    return c


def test_raci_group_shadow_passa_pelo_router(cap):
    from services import raci_group_shadow as m

    props = [
        {"id": 74, "empresa": "Vallen Clinic", "item_acao": "Comunicado",
         "new_status": "concluido", "confianca": "media", "evidencia": "ja enviado"},
        {"id": 75, "empresa": "Alba", "item_acao": "Revisar", "action": "nota",
         "confianca": "baixa", "evidencia": ""},
    ]
    asyncio.run(m._notify_renato(props))

    assert len(cap.calls) == 1, "shadow nao chamou o router"
    kw = cap.calls[0]
    assert kw["source"] == "raci_group_shadow"
    assert kw["urgency_score"] == 8
    # dedup ordenado pelos ids -> a MESMA lista em outra ordem e a mesma chave
    assert kw["dedup_key"] == "raci_shadow:74-75"
    assert "RACI-74" in kw["message_text"], "o texto do WA se perdeu no caminho"


def test_raci_group_shadow_dedup_muda_quando_a_lista_muda(cap):
    from services import raci_group_shadow as m

    base = {"empresa": "Vallen", "item_acao": "x", "confianca": "media", "evidencia": ""}
    asyncio.run(m._notify_renato([{**base, "id": 74}]))
    asyncio.run(m._notify_renato([{**base, "id": 74}, {**base, "id": 80}]))

    chaves = [c["dedup_key"] for c in cap.calls]
    assert chaves == ["raci_shadow:74", "raci_shadow:74-80"], (
        "proposta nova entrando na lista tem que gerar chave nova, senao a "
        "novidade seria engolida pelo dedup"
    )


def test_raci_smart_update_dedup_por_conteudo_nao_por_empresa():
    """O caso real de 27/07: DUAS notificacoes da mesma Vallen Clinic (13:31 e
    15:14) com evidencias diferentes. Uma chave por empresa+dia engoliria a
    segunda — que era noticia nova, nao repeticao."""
    from services import raci_smart_updates as m

    def chave(evidencia):
        pending = [{"evidencia": evidencia, "new_status": "concluido", "confianca": "media"}]
        return m.pending_review_dedup_key("vallen", m.format_pending_review_wa(pending, "Vallen Clinic"))

    a = chave("Jessica fica fora de 08-26/08")
    b = chave("buscamos uma administrativa ou colocamos a Jessica")
    assert a != b, "evidencias diferentes tem que gerar chaves diferentes"
    assert a == chave("Jessica fica fora de 08-26/08"), "a chave tem que ser estavel"
    # A empresa continua na chave: mesmo texto em empresas diferentes nao colide.
    mesmo_corpo = m.format_pending_review_wa([{"evidencia": "ok", "confianca": "media"}], "X")
    assert m.pending_review_dedup_key("a", mesmo_corpo) != m.pending_review_dedup_key("b", mesmo_corpo)


def test_raci_smart_update_texto_preservado_na_migracao():
    """O corpo que sai no WA tem que ser byte-a-byte o de antes da migracao —
    a mudanca era de CANAL, nao de conteudo."""
    from services import raci_smart_updates as m

    pending = [
        {"evidencia": "documento ja elaborado", "new_status": "concluido", "confianca": "media"},
        {"evidencia": "", "confianca": "media"},
    ]
    esperado = (
        "⚠️ Update RACI Vallen Clinic precisa aprovacao:\n"
        "- _documento ja elaborado_\n"
        "  → propor concluido (conf media)\n"
        "- __\n"
        "  → propor note (conf media)\n"
        "\nResponda no app /editorial ou edite manual no ConselhoOS."
    )
    assert m.format_pending_review_wa(pending, "Vallen Clinic") == esperado


async def _fake_digest(group_jid, group_name, days=1):
    # Formato que _format_digests_for_whatsapp consome: group/messages/summary.
    return {"group": group_name, "messages": 4, "summary": "resumo do grupo"}


class _FakeConn:
    """Conexao minima: a funcao so le a lista de grupos com sync ligado."""

    def cursor(self):
        return self

    def execute(self, *_a, **_kw):
        return None

    def fetchall(self):
        return [{"group_jid": "j@g.us", "group_name": "Grupo Teste"}]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_group_digest_dedup_por_dia_brt(cap, monkeypatch):
    """O digest e 1x/dia — duas runs no mesmo dia BRT sao a mesma noticia."""
    from services import group_digest as m
    from services.tz import now_utc, to_brt

    esperado = "group_digest:" + to_brt(now_utc()).strftime("%Y%m%d")

    monkeypatch.setattr(m, "get_db", lambda: _FakeConn())
    monkeypatch.setattr(m, "_generate_group_digest", _fake_digest)
    res = asyncio.run(m.generate_daily_group_digests())

    assert len(cap.calls) == 1, f"digest nao chamou o router (res={res})"
    kw = cap.calls[0]
    assert kw["source"] == "group_digest"
    assert kw["urgency_score"] == 8
    assert kw["dedup_key"] == esperado
    assert res["digests_sent"] == 1


def test_preview_semanal_conta_entrega_nao_canal(cap):
    """Regressao sutil: o caller contava `previews_sent` pelo retorno do envio.
    Com o router no meio, um preview REBAIXADO pra push devolveria False no
    helper notify() e seria contado como ERRO — quando na verdade foi entregue.
    So 'skipped' e falha de verdade."""
    from services import raci_weekly_report as m

    for action, esperado in [
        ("sent", True),
        ("queued", True),       # foi pra fila, nao se perdeu
        ("duplicate", True),    # dedup funcionando != erro
        ("skipped", False),     # nao entregou por canal nenhum
    ]:
        cap.action = action
        ok = asyncio.run(m._send_preview("t", "corpo", "dedup:1"))
        assert ok is esperado, f"action={action} deveria contar entrega={esperado}"


def test_preview_semanal_dedup_por_semana_iso():
    from services import raci_weekly_report as m

    w = m._iso_week()
    assert re.match(r"^\d{4}-W\d{2}$", w), f"formato inesperado: {w}"
    assert m._iso_week() == w, "a chave da semana tem que ser estavel dentro da semana"
