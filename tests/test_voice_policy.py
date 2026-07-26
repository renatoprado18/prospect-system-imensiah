"""Testes da política de voz (fase 1 — WhatsApp).

Cobre as partes determinísticas: limpeza do corpus, classificação de perfil e
medição. A síntese por LLM não é testada aqui (não-determinística) — o que se
testa é que ela recebe corpus LIMPO, que é onde estava o risco real.
"""
from services.voice_policy import (
    BOT_INSTANCE,
    DEFAULT_EXCLUDE_FROM_MEMORY,
    SELF_CONTACT_IDS,
    classify_recipient,
    clean_outgoing,
    measure_style,
    render_policy_md,
    sample_for_profile,
)


class TestCleanOutgoing:
    """O corpus vinha contaminado de três formas — cada uma atribuiria a voz de
    outra pessoa (ou de nenhuma) ao Renato."""

    def test_bloco_de_export_e_truncado_nao_descartado(self):
        """Import .txt do iOS cola a conversa inteira dentro de uma mensagem,
        incluindo falas de TERCEIROS. Medido: 67 casos, um deles com linhas da
        Wanelise. O texto ANTES do bloco é dele e é bom corpus."""
        bruto = ("Está bem. Só queria me certificar que estamos atentos.\nGrato!\n"
                 "[05/03/26, 16:57:53] Wanelise B Carvalho: image omitted")
        limpo = clean_outgoing(bruto)
        assert limpo == "Está bem. Só queria me certificar que estamos atentos.\nGrato!"
        assert "Wanelise" not in limpo

    def test_marca_invisivel_do_ios_sai(self):
        assert clean_outgoing("‎Grato ‏") == "Grato"

    def test_mensagem_normal_passa_intacta(self):
        t = "Você pode verificar com a contabilidade se o ICMS entra como crédito?"
        assert clean_outgoing(t) == t

    def test_vazio_e_none(self):
        assert clean_outgoing(None) == ""
        assert clean_outgoing("   ") == ""


class TestClassifyRecipient:
    def test_companheira_vence_familia(self):
        c = {"tags": ["companheira", "familia", "C0"], "cargo": "Massagista", "circulo": 1}
        assert classify_recipient(c) == "companheira"

    def test_familia_por_tag(self):
        assert classify_recipient({"tags": ["filha", "familia"], "circulo": 1}) == "familia"
        assert classify_recipient({"tags": ["pai", "familia"], "circulo": 1}) == "familia"

    def test_assistente_por_cargo_nao_por_nome(self):
        """Papel, não pessoa — nome muda, cargo não
        (ver feedback_no_hardcoded_contact_ids)."""
        assert classify_recipient({"tags": [], "cargo": "Assistente Virtual", "circulo": 2}) == "assistente"
        assert classify_recipient({"tags": [], "cargo": "Secretária executiva", "circulo": 2}) == "assistente"

    def test_par_profissional_por_cargo_ou_tag(self):
        assert classify_recipient({"tags": ["conselheiro"], "circulo": 2}) == "par_profissional"
        assert classify_recipient({"tags": [], "cargo": "CEO/Chairman", "circulo": 2}) == "par_profissional"

    def test_cliente_por_tag(self):
        assert classify_recipient({"tags": ["cliente", "mentor"], "circulo": 3}) == "cliente"

    def test_prestador_por_cargo(self):
        assert classify_recipient({"tags": [], "cargo": "Dermatologista", "circulo": 1}) == "prestador"

    def test_fallback_por_circulo(self):
        assert classify_recipient({"tags": [], "cargo": "", "circulo": 2}) == "circulo_proximo"
        assert classify_recipient({"tags": [], "cargo": "", "circulo": 5}) == "outros"

    def test_tags_como_string_json(self):
        """`contacts.tags` chega como str em alguns caminhos."""
        assert classify_recipient({"tags": '["filho", "familia"]', "circulo": 1}) == "familia"


class TestMeasureStyle:
    def test_conta_grato_e_obrigado_separado(self):
        """A hipótese herdada das regras manuais que o corpus tinha de julgar."""
        m = measure_style(["Segue o material. Grato", "Valeu, obrigado!", "Grato pela ajuda"])
        assert m["fechamentos"]["grato"] == 2
        assert m["fechamentos"]["obrigado"] == 1

    def test_saudacao_so_conta_no_inicio_pra_oi(self):
        """'Oi' no meio da frase não é saudação ('...falei oi pra ele')."""
        m = measure_style(["Oi, tudo bem? Preciso de um retorno hoje."])
        assert m["com_saudacao"] == 1
        m2 = measure_style(["Passei e falei oi pra ele na portaria hoje"])
        assert m2["com_saudacao"] == 0

    def test_percentuais_e_media(self):
        m = measure_style(["Mensagem com pergunta?", "Sem nada aqui agora"])
        assert m["n"] == 2
        assert m["pct_pergunta"] == 50.0
        assert m["len_medio"] > 0

    def test_corpus_vazio_nao_estoura(self):
        assert measure_style([]) == {"n": 0}


class TestSampleDeterminismo:
    def test_mesma_entrada_mesma_amostra(self):
        """Se a amostra fosse aleatória, a política mudaria de texto a cada
        regeneração sem o corpus ter mudado — e o Renato não saberia se a
        mudança veio dele ou do sorteio."""
        msgs = [f"mensagem numero {i} com tamanho variado {'x' * (i % 40)}" for i in range(200)]
        assert sample_for_profile(msgs, k=10) == sample_for_profile(msgs, k=10)

    def test_cobre_curtas_e_longas(self):
        msgs = ["curta"] * 5 + ["media " * 20] * 5 + ["longa " * 90] * 5
        a = sample_for_profile(msgs, k=6)
        assert len(min(a, key=len)) < len(max(a, key=len))

    def test_corpus_menor_que_k_devolve_tudo(self):
        assert len(sample_for_profile(["a", "b"], k=25)) == 2


class TestRenderDoc:
    def _destilado(self):
        return {
            "assistente": {
                "metricas": measure_style(["Segue o pedido. Grato", "Pode confirmar o horário?"]),
                "regras": {"abertura": "direto", "fechamento": "Grato", "tamanho": "curto",
                           "tom": "executivo", "faça": ["ir ao ponto"], "evite": ["saudação"],
                           "exemplo_canonico": "Pode confirmar o horário?"},
            },
            "companheira": {
                "metricas": measure_style(["te amo", "chego às sete"]),
                "regras": {"abertura": "sem saudação", "fechamento": "beijo",
                           "tamanho": "curtíssimo", "tom": "íntimo", "faça": [], "evite": [],
                           "exemplo_canonico": "chego às sete"},
            },
        }

    def test_perfil_intimo_omitido_por_padrao(self):
        """A política mora em system_memories, que o briefing e o bot leem."""
        assert "companheira" in DEFAULT_EXCLUDE_FROM_MEMORY
        md = render_policy_md(self._destilado())
        assert "Omitido deste documento por privacidade" in md
        assert "beijo" not in md
        assert "Grato" in md  # os outros perfis seguem completos

    def test_pode_incluir_tudo_explicitamente(self):
        md = render_policy_md(self._destilado(), exclude=())
        assert "beijo" in md

    def test_veredito_da_hipotese_aparece(self):
        md = render_policy_md(self._destilado())
        assert "grato" in md.lower()
        assert "CONFIRMADA" in md or "REFUTADA" in md


class TestGuardasDoCorpus:
    def test_bot_e_self_chat_declarados(self):
        """Os dois filtros que impedem a política de aprender a voz errada."""
        assert BOT_INSTANCE == "intel-bot-v2"
        assert 25613 in SELF_CONTACT_IDS
