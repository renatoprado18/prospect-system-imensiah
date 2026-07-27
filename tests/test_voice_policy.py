"""Testes da política de voz (fase 1 — WhatsApp; fase 2 — e-mail).

Cobre as partes determinísticas: limpeza do corpus, classificação de perfil e
medição. A síntese por LLM não é testada aqui (não-determinística) — o que se
testa é que ela recebe corpus LIMPO, que é onde estava o risco real.
"""
import pytest

from services.voice_policy import (
    BOT_INSTANCE,
    CANAIS,
    DEFAULT_EXCLUDE_FROM_MEMORY,
    MEMORY_TITLE,
    MEMORY_TITLE_EMAIL,
    MIN_CORPUS,
    SELF_CONTACT_IDS,
    classify_recipient,
    clean_email,
    clean_outgoing,
    measure_style,
    memory_title_for,
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

    def test_prestador_vence_familia(self):
        """Dr. Piccino #2869: parente E advogado da reorg das 7 empresas.
        Quem conduz um processo fiscal não recebe rascunho que abre com 'Pai,'
        — o assunto profissional é que pauta o registro (decisão 27/07)."""
        c = {"tags": ["familia"], "cargo": "Advogado", "circulo": 3}
        assert classify_recipient(c) == "prestador"

    def test_companheira_vence_prestador(self):
        """A ordem nova não pode passar por cima do perfil íntimo: a Emma tem
        tag companheira E cargo Massagista."""
        c = {"tags": ["companheira", "familia"], "cargo": "Massagista", "circulo": 1}
        assert classify_recipient(c) == "companheira"

    def test_familia_sem_cargo_de_servico_segue_familia(self):
        assert classify_recipient({"tags": ["filho", "familia"], "cargo": "Software Engineer",
                                   "circulo": 1}) == "familia"

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


# ====================== Fase 2 — e-mail ======================


class TestCleanEmail:
    """Um e-mail enviado carrega muito mais coisa que não é voz do Renato do que
    uma mensagem de WhatsApp: a thread citada, a assinatura e — no caso dos
    encaminhamentos da camada esperta — o corpo inteiro de um e-mail alheio."""

    def test_thread_citada_e_cortada(self):
        """A parte de baixo é fala de TERCEIRO. Mesma classe do bloco de export
        do WhatsApp, mecanismo diferente."""
        bruto = (
            "O que você recomenda?\n\n"
            "Em qui., 16 de jul. de 2026 às 21:58, Andressa Santos <\n"
            "andressa@almeida-prado.com> escreveu:\n\n"
            "> Olá!\n>\n> Segue o retorno da conta Google.\n"
        )
        limpo = clean_email(bruto)
        assert limpo == "O que você recomenda?"
        assert "Andressa" not in limpo

    def test_thread_citada_em_ingles(self):
        bruto = "Thanks, Nick.\n\nOn Fri, Jul 24, 2026 at 3:00 PM Nick <n@x.it> wrote:\n\n> Hi Renato\n"
        assert clean_email(bruto) == "Thanks, Nick."

    def test_encaminhamento_automatico_e_descartado_inteiro(self):
        """Texto de máquina + corpo de terceiro. Não é voz dele em parte alguma —
        devolve "" e o gate de min_len descarta a mensagem."""
        bruto = ("Delegado por Renato via triagem automática.\n\n"
                 "---------- Mensagem encaminhada ----------\n"
                 "De: \"Amazon.com\" <shipment-tracking@amazon.com>\n"
                 "Assunto: Shipped: 1 Shoes item\n")
        assert clean_email(bruto) == ""

    def test_bloco_de_assinatura_sai(self):
        bruto = ("Roger,\n\nSem pressa. Sigo à disposição.\n\n"
                 "Saudações/Regards,\n\nRenato A Prado\n"
                 "+55(11)98415-3337\nrenato.almeida.prado@gmail.com\n"
                 "LinkedIn: https://www.linkedin.com/in/renatoaprado/\n")
        limpo = clean_email(bruto)
        assert limpo == "Roger,\n\nSem pressa. Sigo à disposição."
        assert "LinkedIn" not in limpo and "98415" not in limpo

    def test_despedida_e_preservada(self):
        """'Abraço,\\nRenato' é FECHAMENTO, não assinatura — é exatamente o que a
        métrica de despedida conta. Cortá-lo zeraria o traço que se quer medir."""
        bruto = "Roger, tudo bem?\n\nConseguiu olhar os dois nomes?\n\nAbraço,\nRenato"
        limpo = clean_email(bruto)
        assert limpo.endswith("Abraço,\nRenato")
        assert measure_style([limpo])["com_fechamento"] == 1

    @pytest.mark.parametrize("assinatura", [
        "Renato A Prado",
        "Renato F A Prado",
        "Renato de Faria e Almeida Prado",
    ])
    def test_grafias_de_assinatura_que_ele_usa(self, assinatura):
        assert clean_email(f"Segue o combinado.\n\n{assinatura}\n") == "Segue o combinado."

    def test_nome_no_meio_do_texto_nao_corta(self):
        """O corte de assinatura exige a LINHA inteira ser o nome — senão um
        e-mail que fala dele na terceira pessoa perderia metade do corpo."""
        t = "Falei com o Renato Prado ontem sobre o caso e ele concordou."
        assert clean_email(t) == t

    def test_separador_padrao_de_assinatura(self):
        assert clean_email("Segue em anexo.\n\n-- \nRenato\nCEO") == "Segue em anexo."

    def test_linhas_citadas_soltas_somem(self):
        assert clean_email("Concordo.\n> texto antigo\n> mais texto") == "Concordo."

    def test_email_limpo_passa_intacto(self):
        t = "Pai,\n\nO Club Athletico convocou a compra do título social."
        assert clean_email(t) == t

    def test_vazio_e_none(self):
        assert clean_email(None) == ""
        assert clean_email("") == ""


class TestSeparacaoDeCanal:
    """O canal mora em `conversations.canal`, não em `messages`. Sem o JOIN os
    138 e-mails (1.836 caracteres em média) entravam no corpus de WhatsApp
    (136 de média) e distorciam tamanho e amostra."""

    def test_canais_declarados(self):
        assert CANAIS == ("whatsapp", "email")

    def test_documento_por_canal(self):
        assert memory_title_for("whatsapp") == MEMORY_TITLE
        assert memory_title_for("email") == MEMORY_TITLE_EMAIL
        assert MEMORY_TITLE != MEMORY_TITLE_EMAIL

    def test_canal_invalido_falha_alto(self):
        """Errar o canal em silêncio devolveria corpus vazio e uma política
        vazia gravada por cima da boa."""
        from services.voice_policy import collect_corpus
        with pytest.raises(ValueError, match="canal inválido"):
            collect_corpus(None, canal="telegram")

    def test_gate_de_corpus_menor_no_email(self):
        """E-mails são ~13× mais longos: 12 deles carregam mais sinal de estilo
        que 30 mensagens de WhatsApp."""
        assert MIN_CORPUS["email"] < MIN_CORPUS["whatsapp"]

    def test_render_rotula_o_canal(self):
        destilado = {
            "assistente": {
                "metricas": measure_style(["Segue o pedido. Grato"]),
                "regras": {"abertura": "vocativo", "fechamento": "Grato",
                           "tamanho": "médio", "tom": "executivo",
                           "faça": [], "evite": [], "exemplo_canonico": "Segue."},
            }
        }
        md_email = render_policy_md(destilado, canal="email")
        assert "e-mail" in md_email
        assert "e-mails enviados" in md_email
        md_wa = render_policy_md(destilado, canal="whatsapp")
        assert "WhatsApp" in md_wa
        assert "mensagens enviados" in md_wa
