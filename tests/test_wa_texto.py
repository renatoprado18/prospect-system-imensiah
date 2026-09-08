"""Texto efetivo da mensagem: transcricao/OCR no lugar do `[Áudio]` (08/09/26).

O defeito: o pipeline transcrevia audio e fazia OCR de imagem ha meses (386 de
412 audios, 3.027 imagens em `wa_attachments.extracted_text`) e NENHUM leitor
de conversa usava esse texto — todos liam `messages.conteudo`, que para midia
guarda so o literal. 2.982 mensagens tinham conteudo real invisivel.

O custo: os 3 audios da Andressa de 24/08 (Itau e BB recusando os extratos da
Carambola, prazo 11/09) estavam transcritos no banco e foram tratados como
canal cego por duas semanas.

Rodar: PYTHONPATH=app python -m pytest tests/test_wa_texto.py -v
"""
import os
import pathlib
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.wa_texto import (  # noqa: E402
    PLACEHOLDERS_MIDIA,
    e_placeholder_midia,
    texto_efetivo_grupo_sql,
    texto_efetivo_sql,
)


class TestExpressaoSQL:
    def test_usa_subquery_escalar_e_nao_join(self):
        """LEFT JOIN duplicaria a mensagem quando ela tem mais de um anexo:
        `wa_attachments` e UNIQUE por (message_id, kind), NAO por message_id.
        Hoje nenhuma tem dois, mas isso nao e invariante — e a duplicacao
        seria silenciosa, inflando qualquer contagem de mensagens."""
        sql = texto_efetivo_sql("m")
        assert "SELECT wa_x.extracted_text" in sql
        assert "LIMIT 1" in sql
        assert "JOIN" not in sql.upper()

    def test_cai_pro_conteudo_quando_nao_ha_anexo(self):
        sql = texto_efetivo_sql("m")
        assert sql.startswith("COALESCE(")
        assert sql.rstrip().endswith("m.conteudo)")

    def test_extracao_vazia_nao_apaga_o_placeholder(self):
        """Extracao que falhou grava string vazia. Sem o NULLIF, a mensagem
        viraria texto vazio — pior que `[Áudio]`, que ao menos diz que houve
        audio ali."""
        assert "NULLIF(" in texto_efetivo_sql("m")
        assert "wa_x.extracted_text <> ''" in texto_efetivo_sql("m")

    def test_alias_e_colunas_configuraveis(self):
        """DM casa por `external_id`; grupo, por `message_id`. Errar a coluna
        nao da erro de SQL — casa com nada e devolve o placeholder, calado."""
        dm = texto_efetivo_sql("m")
        assert "m.external_id" in dm and "m.conteudo" in dm

        gr = texto_efetivo_grupo_sql("g")
        assert "g.message_id" in gr and "g.content" in gr

    def test_grupo_nao_usa_colunas_de_dm(self):
        gr = texto_efetivo_grupo_sql("g")
        assert "conteudo" not in gr
        assert "external_id" not in gr


class TestPlaceholder:
    def test_reconhece_as_duas_grafias_de_audio(self):
        """`[Áudio]` (DM) e `[Audio]` (grupo) convivem no banco: 390 e 160.
        Casar por so uma acha metade e conclui ausencia — foi assim que a
        medicao de 08/09 se enganou nos dois sentidos."""
        assert e_placeholder_midia("[Áudio]")
        assert e_placeholder_midia("[Audio]")
        assert "[Áudio]" in PLACEHOLDERS_MIDIA and "[Audio]" in PLACEHOLDERS_MIDIA

    def test_documento_e_contato_sao_variaveis(self):
        assert e_placeholder_midia("[Documento: Edital.pdf]")
        assert e_placeholder_midia("[Contato: Cy Baldo]")

    def test_texto_de_verdade_nao_e_placeholder(self):
        assert not e_placeholder_midia("No Itaú, tá um pouco mais complicado")
        assert not e_placeholder_midia("[não é mídia] mas começa com colchete")

    def test_vazio_e_none_nao_sao_placeholder(self):
        """Mensagem vazia e mensagem-nao-avaliada sao coisas diferentes:
        tratar as duas como iguais e o que fazia audio decisivo contar como
        silencio."""
        assert not e_placeholder_midia("")
        assert not e_placeholder_midia(None)


class TestLeitoresLigados:
    """Guard de wiring: o texto so serve se os leitores o usarem.

    Cada arquivo aqui teve um `SELECT m.conteudo` trocado pelo helper. Se
    alguem reverter, some texto da tela sem nenhum erro — exatamente o modo
    de falha original. [[feedback_consumidor_morto_wiring]]
    """

    LEITORES = (
        "services/frente_review.py",       # portoes que o Renato ve no cockpit
        "services/signal_router.py",       # o que vira sinal de frente
        "services/timeline.py",            # leitura de conversa por contato
        "services/pre_meeting_briefing.py",  # dossie antes da reuniao
        "services/task_reconciler.py",     # decide se a espera de uma task acabou
        "services/message_classifier.py",
        "services/export.py",
    )

    def test_todo_leitor_de_conversa_usa_o_helper(self):
        faltando = []
        for rel in self.LEITORES:
            src = (pathlib.Path(_ROOT) / "app" / rel).read_text(encoding="utf-8")
            if "texto_efetivo_sql" not in src:
                faltando.append(rel)
        assert not faltando, (
            "leitor de conversa voltou a ler `conteudo` cru — audio e imagem "
            "ficam invisiveis de novo: " + ", ".join(faltando)
        )

    def test_filtro_de_comprimento_nao_descarta_midia(self):
        """`[Áudio]` tem 7 caracteres. `frente_review` filtrava LENGTH > 10 e
        `signal_router`, > 20 — a midia era descartada no WHERE antes de
        chegar na projecao, entao trocar so a SELECT list nao resolveria."""
        for rel in ("services/frente_review.py", "services/signal_router.py"):
            src = (pathlib.Path(_ROOT) / "app" / rel).read_text(encoding="utf-8")
            assert "LENGTH(m.conteudo)" not in src, (
                f"{rel} voltou a medir o comprimento do conteudo cru: "
                "midia volta a ser descartada no WHERE"
            )
