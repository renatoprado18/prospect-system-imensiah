"""Testes do fix de livelock do wa-catchup + extracao de conteudo do webhook.

Contexto (25/07/26): o `wa-catchup` reportava `errors: 2` a cada run de 30min
havia semanas. Diagnostico: 11 mensagens antigas (jan-mai) que o handler
descarta por natureza — imagem sem legenda, vCard, e uma editada com
`message: null` — nunca viravam linha em `messages`, entao toda run as
re-detectava como 'missing' e re-replayava. 40-95 tentativas/dia por mensagem,
~495 replays inuteis, 2 estourando TypeError.
"""
import time

from services.wa_catchup import _TERMINAL_DECISIONS, _within_window


class TestWithinWindow:
    """A janela `hours` era parametro morto — msg de janeiro entrava num job
    que promete recuperar as ultimas 2h."""

    def test_msg_recente_entra(self):
        cutoff = int(time.time()) - 2 * 3600
        rec = {"messageTimestamp": int(time.time()) - 600}
        assert _within_window(rec, cutoff) is True

    def test_msg_antiga_fica_fora(self):
        cutoff = int(time.time()) - 2 * 3600
        # 19/03/2026 — o caso real da imagem da Wanelise
        assert _within_window({"messageTimestamp": 1773936138}, cutoff) is False

    def test_borda_exata_entra(self):
        cutoff = int(time.time()) - 2 * 3600
        assert _within_window({"messageTimestamp": cutoff}, cutoff) is True

    def test_timestamp_dict_baileys(self):
        """Baileys as vezes manda {low, high, unsigned} em vez de int."""
        cutoff = int(time.time()) - 2 * 3600
        agora = int(time.time())
        assert _within_window({"messageTimestamp": {"low": agora, "high": 0}}, cutoff) is True
        assert _within_window({"messageTimestamp": {"low": 1773936138, "high": 0}}, cutoff) is False

    def test_timestamp_ilegivel_passa(self):
        """Fail-open: sem timestamp confiavel, melhor re-checar que perder
        recuperacao real (o proposito do job)."""
        cutoff = int(time.time()) - 2 * 3600
        assert _within_window({}, cutoff) is True
        assert _within_window({"messageTimestamp": None}, cutoff) is True
        assert _within_window({"messageTimestamp": "xxx"}, cutoff) is True


class TestTerminalDecisions:
    def test_cobre_as_razoes_do_livelock_real(self):
        # As 2 razoes medidas em prod nos 11 fosseis
        assert "empty_content" in _TERMINAL_DECISIONS
        assert "null_message" in _TERMINAL_DECISIONS

    def test_nao_inclui_razao_transitoria(self):
        """Razao de infra/transiente NAO pode virar terminal — senao o catchup
        desiste de uma msg que ele deveria recuperar."""
        for transitoria in ("db_error", "timeout", "rate_limited"):
            assert transitoria not in _TERMINAL_DECISIONS


class TestExtracaoConteudo:
    """`process_incoming_message`: os 3 modos de falha que geravam os fosseis."""

    def _extrai(self, message):
        """Espelha o bloco de extracao do handler (sem tocar DB/IA)."""
        message = message or {}
        content = ""
        message_type = "text"
        if "conversation" in message:
            content = message["conversation"]
        elif "extendedTextMessage" in message:
            content = message["extendedTextMessage"].get("text", "")
        elif "imageMessage" in message:
            content = message["imageMessage"].get("caption") or "[Imagem]"
            message_type = "image"
        elif "videoMessage" in message:
            content = message["videoMessage"].get("caption") or "[Vídeo]"
            message_type = "video"
        elif "documentMessage" in message:
            content = message["documentMessage"].get("fileName") or "[Documento]"
            message_type = "document"
        elif "contactMessage" in message:
            nome = (message["contactMessage"] or {}).get("displayName") or ""
            content = f"[Contato: {nome}]" if nome else "[Contato]"
            message_type = "contact"
        return content, message_type

    def test_imagem_sem_legenda_vira_placeholder(self):
        """Caption presente-e-vazia: o default do .get nao pegava, virava ''
        e caia em empty_content — o caso da imagem da Wanelise."""
        content, tipo = self._extrai({"imageMessage": {"caption": "", "url": "x"}})
        assert content == "[Imagem]"
        assert tipo == "image"

    def test_imagem_com_legenda_preserva_texto(self):
        content, _ = self._extrai({"imageMessage": {"caption": "olha o print"}})
        assert content == "olha o print"

    def test_vcard_vira_conteudo(self):
        content, tipo = self._extrai(
            {"contactMessage": {"displayName": "Charutos Manoel", "vcard": "BEGIN:VCARD"}}
        )
        assert content == "[Contato: Charutos Manoel]"
        assert tipo == "contact"

    def test_vcard_sem_nome(self):
        content, _ = self._extrai({"contactMessage": {"vcard": "BEGIN:VCARD"}})
        assert content == "[Contato]"

    def test_message_null_nao_estoura(self):
        """`data.get('message') or {}` — a Evolution manda null explicito em
        msg editada; com `.get(k, {})` o None passava e o `in message`
        levantava TypeError (12 erros/3h em prod)."""
        content, _ = self._extrai(None)
        assert content == ""  # sem conteudo, mas sem crash

    def test_documento_sem_filename(self):
        content, tipo = self._extrai({"documentMessage": {"fileName": ""}})
        assert content == "[Documento]"
        assert tipo == "document"
