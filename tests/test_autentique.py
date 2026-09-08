"""Autentique — assinatura de documento deixa de ser canal cego (08/09/26).

O INTEL so soube que o Baeta assinou o acordo de nao-circunvencao porque o
Renato contou. O documento existia na conta (`a0b9ec99…`, 08/09 16h49, 2/2) e
nenhum caminho do sistema o alcancava.

O risco que estes testes cobrem e o webhook falhar CALADO — o modo que fez o
audio transcrito e o `news_pendente` custarem semanas. Sao DOIS formatos que
nao se parecem: JSON com HMAC (novo) e x-www-form-urlencoded sem assinatura
nenhuma (antigo). Tratar so um deles quebraria em silencio.

Rodar: PYTHONPATH=app python -m pytest tests/test_autentique.py -v
"""
import hashlib
import hmac
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.autentique import (  # noqa: E402
    _resumo_signatarios,
    parse_webhook,
    verificar_assinatura,
)

SEGREDO = "segredo-de-teste"

# Formato NOVO: envelope com event.type e event.data.object.
PAYLOAD_JSON = {
    "id": "wh_1",
    "object": "webhook",
    "event": {
        "id": "evt_abc123",
        "type": "signature.accepted",
        "data": {
            "object": {
                "email": "marcos@baetaippolito.com.br",
                "document": {"id": "a0b9ec99", "name": "Acordo de não-circunvenção"},
            }
        },
        "created_at": "2026-09-08T16:49:25.000000Z",
    },
}

# Formato ANTIGO: chaves achatadas, entregue como x-www-form-urlencoded.
CORPO_FORM = (
    "documento[id]=a0b9ec99&documento[nome]=Acordo+de+n%C3%A3o-circunven%C3%A7%C3%A3o"
    "&partes[0][email]=renato%40almeida-prado.com"
    "&partes[0][assinado][data]=2026-09-08+16%3A49%3A25"
    "&partes[1][email]=marcos%40baetaippolito.com.br"
    "&partes[1][assinado][data]=2026-09-08+16%3A52%3A10"
)


def _assinar(corpo: bytes, segredo: str = SEGREDO) -> str:
    return hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


class TestParseJSON:
    def test_extrai_o_essencial_do_formato_novo(self):
        raw = json.dumps(PAYLOAD_JSON).encode()
        e = parse_webhook(raw)
        assert e["formato"] == "json"
        assert e["evento_chave"] == "evt_abc123"
        assert e["tipo"] == "signature.accepted"
        assert e["documento_id"] == "a0b9ec99"
        assert e["signatario"] == "marcos@baetaippolito.com.br"

    def test_usa_o_id_do_evento_como_chave(self):
        """Idempotencia: reentrega por timeout tem o MESMO event.id e nao pode
        virar segundo aviso do mesmo fato."""
        raw = json.dumps(PAYLOAD_JSON).encode()
        outro = json.loads(raw)
        outro["event"]["created_at"] = "2026-09-09T00:00:00Z"  # reentrega depois
        assert parse_webhook(raw)["evento_chave"] == parse_webhook(json.dumps(outro).encode())["evento_chave"]

    def test_documento_no_topo_quando_o_evento_e_do_documento(self):
        p = {"event": {"id": "evt_2", "type": "document.finished",
                       "data": {"object": {"id": "doc-9", "name": "Contrato X"}}}}
        e = parse_webhook(json.dumps(p).encode())
        assert e["documento_id"] == "doc-9"
        assert e["documento_nome"] == "Contrato X"


class TestParseForm:
    def test_le_o_formato_antigo(self):
        """`request.json()` estouraria neste corpo — o formato antigo entrega
        x-www-form-urlencoded, nao JSON."""
        e = parse_webhook(CORPO_FORM.encode())
        assert e["formato"] == "form"
        assert e["documento_id"] == "a0b9ec99"
        assert "circunven" in (e["documento_nome"] or "")

    def test_pega_quem_assinou_por_ultimo(self):
        e = parse_webhook(CORPO_FORM.encode())
        assert e["signatario"] == "marcos@baetaippolito.com.br"
        assert e["ocorrido_em"].startswith("2026-09-08")

    def test_sem_id_de_evento_a_chave_vem_do_corpo(self):
        """O formato antigo nao tem id. Corpo igual -> mesma chave (barra a
        reentrega); corpo diferente -> chave diferente (nao engole evento novo)."""
        a = parse_webhook(CORPO_FORM.encode())["evento_chave"]
        b = parse_webhook(CORPO_FORM.encode())["evento_chave"]
        c = parse_webhook((CORPO_FORM + "&x=1").encode())["evento_chave"]
        assert a == b and a != c

    def test_decide_pelo_conteudo_nao_pelo_content_type(self):
        """Content-Type errado e comum em webhook. Cair no parser errado por
        causa dele seria falhar calado."""
        e = parse_webhook(CORPO_FORM.encode(), content_type="application/json")
        assert e["formato"] == "form"
        j = parse_webhook(json.dumps(PAYLOAD_JSON).encode(),
                          content_type="application/x-www-form-urlencoded")
        assert j["formato"] == "json"


class TestParseDegradado:
    def test_corpo_vazio_ou_lixo_devolve_none(self):
        assert parse_webhook(b"") is None
        assert parse_webhook(b"   ") is None

    def test_json_quebrado_nao_estoura(self):
        assert parse_webhook(b'{"event": {') is None


class TestHMAC:
    def test_assinatura_valida(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        corpo = json.dumps(PAYLOAD_JSON).encode()
        assert verificar_assinatura(corpo, {"X-Autentique-Signature": _assinar(corpo)})

    def test_assinatura_do_corpo_ERRADO_e_recusada(self, monkeypatch):
        """O digest tem de ser do corpo CRU. Se alguem re-serializar o JSON
        antes de conferir, espacos e ordem de chaves mudam e a guarda passa a
        recusar tudo — ou, pior, a aceitar o que nao devia."""
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        corpo = json.dumps(PAYLOAD_JSON).encode()
        reserializado = json.dumps(PAYLOAD_JSON, indent=2).encode()
        assert not verificar_assinatura(corpo, {"X-Autentique-Signature": _assinar(reserializado)})

    def test_aceita_prefixo_sha256(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        corpo = b"qualquer"
        assert verificar_assinatura(corpo, {"X-Autentique-Signature": "sha256=" + _assinar(corpo)})

    def test_sem_segredo_configurado_NAO_confia(self, monkeypatch):
        """Falso, nao 'verdadeiro por otimismo'. Evento nao verificado serve
        pra avisar; nunca pra deixar um estranho mexer no sistema."""
        monkeypatch.delenv("AUTENTIQUE_WEBHOOK_SECRET", raising=False)
        corpo = b"x"
        assert not verificar_assinatura(corpo, {"X-Autentique-Signature": _assinar(corpo)})

    def test_sem_header_nao_confia(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        assert not verificar_assinatura(b"x", {})

    def test_header_case_insensitive(self, monkeypatch):
        """HTTP nao garante caixa de header; procurar so pela forma exata
        recusaria evento legitimo."""
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        corpo = b"x"
        assert verificar_assinatura(corpo, {"x-autentique-signature": _assinar(corpo)})


class TestResumoSignatarios:
    def test_conta_assinados_e_acha_o_ultimo(self):
        doc = {"signatures": [
            {"email": "a@x.com", "signed": {"created_at": "2026-09-08T16:49:25Z"}},
            {"email": "b@x.com", "signed": {"created_at": "2026-09-08T16:52:10Z"}},
        ]}
        sigs, assinados, ultimo = _resumo_signatarios(doc)
        assert len(sigs) == 2 and assinados == 2
        assert ultimo.startswith("2026-09-08T16:52")

    def test_quem_nao_assinou_nao_conta(self):
        """Caso real: o contrato Mayer tem 3 partes e uma NUNCA abriu."""
        doc = {"signatures": [
            {"email": "a@x.com", "signed": {"created_at": "2025-03-05T11:00:00Z"}},
            {"email": "b@x.com", "signed": {"created_at": "2025-03-05T12:00:00Z"}},
            {"email": "c@x.com"},
        ]}
        sigs, assinados, _ = _resumo_signatarios(doc)
        assert len(sigs) == 3 and assinados == 2
        assert sigs[2]["assinado_em"] is None and sigs[2]["visto_em"] is None

    def test_recusa_e_registrada(self):
        """No formato antigo a RECUSA nao chega por webhook — so aparece aqui,
        na leitura de estado. Por isso silencio nunca prova que ninguem
        recusou: prova que ninguem assinou."""
        doc = {"signatures": [{"email": "a@x.com", "rejected": {"created_at": "2026-01-02T10:00:00Z"}}]}
        sigs, assinados, ultimo = _resumo_signatarios(doc)
        assert assinados == 0 and ultimo is None
        assert sigs[0]["recusado_em"].startswith("2026-01-02")


class TestRotaRegistrada:
    def test_webhook_nao_leva_require_api_auth(self):
        """Guard do incidente de 03/08: uma varredura de 'rotas sem consumidor'
        pos `require_api_auth` nos webhooks e a Fathom levou 401 por um dia.
        Webhook tem o consumidor FORA do repo — o porteiro dele e o HMAC."""
        import re
        src = (open(os.path.join(_ROOT, "app", "main.py"), encoding="utf-8").read())
        m = re.search(r'@app\.post\("/api/webhooks/autentique"([^)]*)\)', src)
        assert m, "a rota do webhook sumiu"
        assert "require_api_auth" not in m.group(1)
        assert "processar_webhook" in src


class TestPorteiro:
    """A rota GRAVA evento e AVISA o Renato. Sem porteiro, um estranho
    inventaria 'fulano assinou' no sistema."""

    def _autorizar(self, *a, **kw):
        from services.autentique import autorizar_webhook
        return autorizar_webhook(*a, **kw)

    def test_hmac_valido_autoriza_E_marca_conferido(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        corpo = json.dumps(PAYLOAD_JSON).encode()
        ok, conferido = self._autorizar(corpo, {"X-Autentique-Signature": _assinar(corpo)})
        assert ok and conferido

    def test_token_na_url_autoriza_mas_NAO_marca_conferido(self, monkeypatch):
        """O formato antigo nao assina nada — o segredo na URL prova que quem
        chamou conhece o segredo, nao que o Autentique emitiu o evento. A
        distincao fica gravada: evento nao conferido nunca deve MEXER em nada."""
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        ok, conferido = self._autorizar(CORPO_FORM.encode(), {}, token_query=SEGREDO)
        assert ok and not conferido

    def test_token_errado_nao_passa(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        assert self._autorizar(b"x", {}, token_query="chute") == (False, False)

    def test_anonimo_nao_passa(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", SEGREDO)
        assert self._autorizar(CORPO_FORM.encode(), {}) == (False, False)

    def test_sem_segredo_configurado_NADA_passa(self, monkeypatch):
        """FAIL-CLOSED. Enquanto o segredo nao existe a porta fica fechada, em
        vez de virar uma porta que ninguem lembra que ficou aberta."""
        monkeypatch.delenv("AUTENTIQUE_WEBHOOK_SECRET", raising=False)
        corpo = json.dumps(PAYLOAD_JSON).encode()
        assert self._autorizar(corpo, {"X-Autentique-Signature": _assinar(corpo)}) == (False, False)
        assert self._autorizar(corpo, {}, token_query="qualquer") == (False, False)

    def test_a_rota_rejeita_com_401(self):
        """Guard do call-site: se alguem tirar o `raise`, a rota volta a gravar
        de anonimo — e o teto de rotas mutantes deixaria passar, porque o
        porteiro mora no servico e nao no corpo da rota."""
        import re
        src = open(os.path.join(_ROOT, "app", "main.py"), encoding="utf-8").read()
        i = src.index('@app.post("/api/webhooks/autentique")')
        corpo = src[i:i + 1800]
        assert "autorizar_webhook" in corpo
        assert "status_code=401" in corpo


class TestSegredosSeparados:
    """08/09: o painel entregou um *Endpoint Secret* na criação do endpoint —
    e ele é uma coisa DIFERENTE do `?token=` da URL.

    O token prova que quem chamou conhece um segredo nosso; a assinatura prova
    que o corpo veio do Autentique. Como a URL registrada no painel carrega o
    token, trocar `AUTENTIQUE_WEBHOOK_SECRET` pelo signing secret quebraria a
    ENTREGA sem quebrar teste nenhum — daí duas vars.
    """

    def test_hmac_usa_o_signing_secret_quando_existe(self, monkeypatch):
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", "token-da-url")
        monkeypatch.setenv("AUTENTIQUE_SIGNING_SECRET", "secret-do-painel")
        corpo = b"corpo qualquer"
        assinado_certo = hmac.new(b"secret-do-painel", corpo, hashlib.sha256).hexdigest()
        assinado_errado = hmac.new(b"token-da-url", corpo, hashlib.sha256).hexdigest()
        assert verificar_assinatura(corpo, {"X-Autentique-Signature": assinado_certo})
        assert not verificar_assinatura(corpo, {"X-Autentique-Signature": assinado_errado})

    def test_sem_signing_secret_cai_no_compartilhado(self, monkeypatch):
        """Não deixar a verificação morta enquanto a var não existe."""
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", "so-esse")
        monkeypatch.delenv("AUTENTIQUE_SIGNING_SECRET", raising=False)
        corpo = b"x"
        assert verificar_assinatura(
            corpo, {"X-Autentique-Signature": hmac.new(b"so-esse", corpo, hashlib.sha256).hexdigest()})

    def test_token_da_url_NAO_aceita_o_signing_secret(self, monkeypatch):
        """O porteiro do `?token=` compara com o segredo compartilhado. Se
        aceitasse o signing secret também, um vazamento de um valeria pelos
        dois caminhos."""
        from services.autentique import autorizar_webhook
        monkeypatch.setenv("AUTENTIQUE_WEBHOOK_SECRET", "token-da-url")
        monkeypatch.setenv("AUTENTIQUE_SIGNING_SECRET", "secret-do-painel")
        assert autorizar_webhook(b"x", {}, token_query="secret-do-painel") == (False, False)
        assert autorizar_webhook(b"x", {}, token_query="token-da-url") == (True, False)
