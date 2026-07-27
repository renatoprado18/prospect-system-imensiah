"""Testes da RECALIBRAÇÃO SILENCIOSA da camada esperta do inbox (27/07/2026).

Origem: medição em produção do bloco "Precisa de você" — 10 itens, 6 dos quais
não deviam ter interrompido (3 robôs, 1 duplicata literal, 2 repetições do
urgent-tick). Renato quer MINIMIZAR interações.

Cobre:
  - guard de REMETENTE AUTOMÁTICO: robô de marketing/alerta não escala; robô
    TRANSACIONAL (fatura, cobrança, segurança, judicial, gov) CONTINUA escalando
  - dedup do item repetido (mesmo e-mail nas 2 contas Google)
  - novo formato do resumo (sem "Precisa de você", sem promessa de "desfaz N")
  - teto de 1 resumo/dia BRT + kill-switches de tudo

Rodar: PYTHONPATH=app .venv/bin/python -m pytest tests/test_inbox_smart_quiet.py -q
"""
import os
import sys
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import email_triage as et  # noqa: E402


# ===========================================================================
# Kill-switches — defaults valem com a CAMADA LIGADA (INBOX_SMART_LAYER=0 não
# é "modo silencioso": ele ressuscita o ping individual por e-mail).
# ===========================================================================
class TestKillSwitchDefaults:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for k in ("INBOX_SMART_NEEDS_YOU_WA", "INBOX_SMART_SUMMARY_MAX_PER_DAY",
                  "INBOX_SMART_AUTO_SENDER_GUARD", "INBOX_SMART_UNDO_HINT",
                  "EMAIL_URGENT_WA_ENABLED"):
            monkeypatch.delenv(k, raising=False)

    def test_needs_you_wa_default_off(self):
        assert et.is_smart_needs_you_wa_enabled() is False

    def test_summary_cap_default_1_por_dia(self):
        assert et.smart_summary_max_per_day() == 1

    def test_auto_sender_guard_default_on(self):
        assert et.is_smart_auto_sender_guard_enabled() is True

    def test_undo_hint_default_off(self):
        assert et.is_smart_undo_hint_enabled() is False

    def test_urgent_wa_default_on_preserva_comportamento(self):
        # O urgent-tick é o caminho que FICA (one-shot por gmail_id); ganhou
        # kill-switch só porque não tinha nenhum.
        assert et.is_email_urgent_wa_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "on", "YES", " sim\n"])
    def test_reversivel_por_env_com_whitespace(self, monkeypatch, val):
        monkeypatch.setenv("INBOX_SMART_NEEDS_YOU_WA", val)
        assert et.is_smart_needs_you_wa_enabled() is True

    def test_cap_zero_remove_teto(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_SUMMARY_MAX_PER_DAY", "0")
        assert et.smart_summary_max_per_day() == 0

    def test_cap_lixo_cai_no_default(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_SUMMARY_MAX_PER_DAY", "abc")
        assert et.smart_summary_max_per_day() == 1

    def test_urgent_wa_desligavel(self, monkeypatch):
        monkeypatch.setenv("EMAIL_URGENT_WA_ENABLED", "0")
        assert et.is_email_urgent_wa_enabled() is False


# ===========================================================================
# GUARD — remetente automático não escala
# ===========================================================================
class TestAutomatedSenderDetection:
    @pytest.mark.parametrize("addr", [
        "nao-responda@jusbrasil.com.br",
        "noreply@email.quintoandar.com.br",
        "no-reply@example.com",
        "no_reply@example.com",
        "notificacao@banco.example",
        "notificacoes@banco.example",
        "notifications@github.com",
        "notif-trilha@ibgc.org.br",
        "newsletter@veja.abril.com.br",
        "donotreply@microsoft.com",
        "mkt@loja.example",
        "marketing@loja.example",
    ])
    def test_reconhece_robo(self, addr):
        assert et._is_automated_sender(addr) is True

    @pytest.mark.parametrize("addr", [
        "orestes@gmail.com",
        "renato@almeida-prado.com",
        "andressa@almeida-prado.com",
        "jessica.silva@vallen.com.br",
        "contato@escritorio.adv.br",
    ])
    def test_humano_nao_e_robo(self, addr):
        assert et._is_automated_sender(addr) is False


class TestMarketingNoReplyNaoEscala:
    """Os 3 robôs MEDIDOS em produção no bloco "Precisa de você"."""

    def test_jusbrasil_alerta_automatico(self):
        r = et.smart_automated_no_escalate(
            "nao-responda@jusbrasil.com.br",
            "Verifique a situação atual do processo",
            "Acompanhe seus processos monitorados no Jusbrasil.",
        )
        assert r is not None

    def test_ibgc_marketing_22h30(self):
        r = et.smart_automated_no_escalate(
            "notif@lifelonglearner.ibgc.org.br",
            "🟣 Trilha de Atualização | Workshop - Governança em Family Office",
            "Garanta sua vaga no workshop.",
        )
        assert r is not None

    def test_quintoandar_promocao(self):
        r = et.smart_automated_no_escalate(
            "noreply@email.quintoandar.com.br",
            "Entre agora e só volte a pagar em setembro 🏡",
            "Oferta por tempo limitado.",
        )
        assert r is not None

    def test_list_unsubscribe_reforca_o_motivo(self):
        r = et.smart_automated_no_escalate(
            "noreply@news.vendor.example", "Novidades da semana", "conteudo",
            has_unsubscribe=True,
        )
        assert r == "remetente automatico + list-unsubscribe"

    def test_assunto_de_campanha_reforca_o_motivo(self):
        r = et.smart_automated_no_escalate(
            "noreply@vendor.example", "Últimas vagas | Masterclass de governança", "",
        )
        assert r == "remetente automatico + assunto de campanha"


class TestNoReplyTransacionalContinuaEscalando:
    """FALSO-POSITIVO INVERSO — o caro. Alerta transacional legítimo também vem
    de no-reply@: fatura, cobrança, segurança, movimentação, intimação. Cada um
    tem que devolver None (= escala)."""

    @pytest.mark.parametrize("frm,subj,body", [
        # fatura / cobrança (evidência no assunto)
        ("noreply@itau.com.br", "Sua fatura do cartão vence em 05/08", ""),
        ("no-reply@comgas.com.br", "Conta de consumo — 2ª via", ""),
        ("nao-responda@enel.com.br", "Boleto disponível", ""),
        # evidência só no CORPO (mais conservador que o guard de telecom)
        ("noreply@banco.example", "Aviso", "Segue o código de barras para pagamento."),
        ("noreply@banco.example", "Comunicado", "Sua fatura está em atraso."),
        # caixa de cobrança vence o local-part automático
        ("noreply-suafatura@comgas.com.br", "Conta Comgas", ""),
        # segurança de conta
        ("noreply@nubank.com.br", "Alerta de segurança", ""),
        ("no-reply@accounts.google.com", "Atividade suspeita na sua conta", ""),
        ("noreply@service.example", "Redefinir sua senha", ""),
        # código de verificação / novo dispositivo (RC7, reusado invertido)
        ("noreply@google.com", "Código de verificação: 883012", ""),
        ("noreply@anthropic.com", "Security alert: new trusted device added", ""),
        # movimentação bancária
        ("noreply@bradesco.com.br", "Movimentação na conta", "Pagamento confirmado."),
        # judicial / fiscal (assunto)
        ("nao-responda@tjsp.jus.br", "Intimação eletrônica — prazo 15 dias", ""),
        ("noreply@vendor.example", "Mandado de citação", ""),
        ("noreply@vendor.example", "Certidão de Dívida Ativa", ""),
        ("noreply@vendor.example", "CDA's Federais - Empresas Inativas", ""),
        # tabelião / protesto
        ("noreply@cartorio.example", "Instrumento de protesto", ""),
        # contratual / extrato (assunto)
        ("noreply@cora.com.br", "Alteração dos Termos e Condições do Cartão", ""),
        ("noreply@corretora.example", "Informe de rendimentos 2026", ""),
        # órgão público — robô de gov nunca é marketing
        ("nao-responda@receita.fazenda.gov.br", "Novidades do e-CAC", ""),
        ("noreply@stf.jus.br", "Comunicado", "texto qualquer"),
    ])
    def test_continua_escalando(self, frm, subj, body):
        assert et.smart_automated_no_escalate(frm, subj, body) is None

    def test_humano_nunca_e_tocado_pelo_guard(self):
        # O e-mail do pai ("Re: Regularização contábil das minhas 7 empresas")
        # é humano — o guard nem opina.
        assert et.smart_automated_no_escalate(
            "orestes@gmail.com", "Re: Regularização contábil das minhas 7 empresas",
            "Filho, segue.",
        ) is None


# ===========================================================================
# DEDUP — mesmo e-mail nas 2 contas Google vira 1 linha
# ===========================================================================
class TestDedupNoResumo:
    def test_dedup_por_assunto_e_remetente(self):
        items = [
            {"subject": "CDA's Federais - Empresas Inativas", "from": "Contab <c@x.com>"},
            {"subject": "CDA's Federais - Empresas Inativas", "from": "Contab <c@x.com>"},
            {"subject": "Outro assunto", "from": "Contab <c@x.com>"},
        ]
        out = et._dedup_by_subject_sender(items)
        assert len(out) == 2
        assert out[0]["subject"].startswith("CDA")

    def test_dedup_ignora_acento_caixa_e_espaco(self):
        items = [
            {"subject": "Regularização  contábil", "from": "Pai <p@x.com>"},
            {"subject": "REGULARIZACAO contabil", "from": "pai <P@X.com>"},
        ]
        assert len(et._dedup_by_subject_sender(items)) == 1

    def test_mesmo_assunto_de_remetente_diferente_nao_some(self):
        items = [
            {"subject": "Reunião", "from": "a@x.com"},
            {"subject": "Reunião", "from": "b@x.com"},
        ]
        assert len(et._dedup_by_subject_sender(items)) == 2

    def test_dedup_resolvidos_por_task_id(self):
        items = [{"task_id": 101, "action_label": "fechei"},
                 {"task_id": 101, "action_label": "fechei"},
                 {"task_id": 202, "action_label": "remarquei"}]
        assert len(et._dedup_by_task_id(items)) == 2


# ===========================================================================
# NOVO FORMATO DO RESUMO + teto diário
# ===========================================================================
_RESOLVED = [{"task_id": 101, "action_label": "fechei", "reason": "confirmado"}]
_NEEDS = [
    {"subject": "CDA's Federais - Empresas Inativas", "from": "Contab <c@x.com>"},
    {"subject": "CDA's Federais - Empresas Inativas", "from": "Contab <c@x.com>"},
]


def _send(monkeypatch, *, forwarded=0, resolved=None, needs=None, sent=None,
          budget_used=0):
    """Executa _send_smart_summary em modo REAL (dry_run=False) com notify e
    ledger falsos. Devolve (stats, texto_enviado_ou_None)."""
    stats = {}
    captured = {}

    async def _fake_notify(source, title, body, score, **kw):
        captured["text"] = body
        captured["dedup"] = kw.get("dedup")
        captured["score"] = score
        return True

    import services.notification_router as nr
    monkeypatch.setattr(nr, "notify", _fake_notify)

    import services.agent_actions as aa
    logged = []
    monkeypatch.setattr(aa, "log_action", lambda **kw: logged.append(kw) or 1)
    monkeypatch.setattr(et, "_smart_summaries_sent_today", lambda: budget_used)

    asyncio.run(et._send_smart_summary(
        stats, forwarded, resolved or [], needs or [], dry_run=False))
    stats["_logged"] = logged
    return stats, captured.get("text")


class TestNovoFormatoDoResumo:
    def test_nao_lista_precisa_de_voce(self, monkeypatch):
        _, txt = _send(monkeypatch, forwarded=2, resolved=_RESOLVED, needs=_NEEDS)
        assert "Precisa de você" not in txt
        # a fila não some: o resumo declara o tamanho dela e aponta o dono
        assert "Deixei 1 em !!Renato" in txt          # 2 itens, dedup -> 1
        assert "briefing" in txt

    def test_reporta_o_que_fez(self, monkeypatch):
        _, txt = _send(monkeypatch, forwarded=3, resolved=_RESOLVED)
        assert "Encaminhei p/ Andressa: 3" in txt
        assert "fechei #101" in txt

    def test_nao_promete_desfaz_sem_receptor(self, monkeypatch):
        _, txt = _send(monkeypatch, resolved=_RESOLVED)
        assert 'desfaz 101' not in txt
        assert "/agent-actions" in txt

    def test_promessa_volta_com_kill_switch(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_UNDO_HINT", "1")
        _, txt = _send(monkeypatch, resolved=_RESOLVED)
        assert 'desfaz 101' in txt

    def test_item_repetido_sai_uma_vez_so(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_NEEDS_YOU_WA", "1")
        _, txt = _send(monkeypatch, needs=_NEEDS, resolved=_RESOLVED)
        assert "Precisa de você (1)" in txt
        assert txt.count("CDA's Federais") == 1

    def test_dry_run_nao_envia_nem_consome_teto(self, monkeypatch):
        stats = {}

        def _boom():
            raise AssertionError("dry-run não lê orçamento")
        monkeypatch.setattr(et, "_smart_summaries_sent_today", _boom)
        asyncio.run(et._send_smart_summary(stats, 1, _RESOLVED, _NEEDS, dry_run=True))
        assert "smart_summary_preview" in stats
        assert "smart_summary_sent" not in stats


class TestTetoDiario:
    def test_primeiro_do_dia_passa(self, monkeypatch):
        stats, txt = _send(monkeypatch, resolved=_RESOLVED, budget_used=0)
        assert stats.get("smart_summary_sent") is True
        assert txt is not None

    def test_segundo_do_dia_e_barrado(self, monkeypatch):
        stats, txt = _send(monkeypatch, resolved=_RESOLVED, budget_used=1)
        assert txt is None
        assert stats["smart_summary_skipped"] == "daily_cap:1/1"
        assert "smart_summary_sent" not in stats

    def test_teto_zero_deixa_passar_sempre(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_SUMMARY_MAX_PER_DAY", "0")
        stats, txt = _send(monkeypatch, resolved=_RESOLVED, budget_used=9)
        assert stats.get("smart_summary_sent") is True

    def test_envio_grava_ledger_do_orcamento(self, monkeypatch):
        stats, _ = _send(monkeypatch, forwarded=2, resolved=_RESOLVED, needs=_NEEDS)
        logged = stats["_logged"]
        assert len(logged) == 1
        assert logged[0]["action_type"] == "inbox_smart_summary_sent"
        # category='system' de propósito: o digest "Fiz por você" já lista as
        # ações reais (forward/task); este row é só o ledger do teto.
        assert logged[0]["category"] == "system"
        assert logged[0]["source"] == "inbox_smart_layer"

    def test_dedup_key_e_por_dia_brt(self, monkeypatch):
        from services.tz import now_utc, to_brt
        captured = {}

        async def _fake_notify(source, title, body, score, **kw):
            captured.update(kw)
            return True
        import services.notification_router as nr
        monkeypatch.setattr(nr, "notify", _fake_notify)
        import services.agent_actions as aa
        monkeypatch.setattr(aa, "log_action", lambda **kw: 1)
        monkeypatch.setattr(et, "_smart_summaries_sent_today", lambda: 0)

        asyncio.run(et._send_smart_summary({}, 0, _RESOLVED, [], dry_run=False))
        assert captured["dedup"] == f"inbox_smart:{to_brt(now_utc()):%Y%m%d}"
