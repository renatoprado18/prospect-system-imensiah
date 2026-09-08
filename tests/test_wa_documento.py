"""Documento de WhatsApp que não é PDF (08/09/26).

O detector do INTEL só reconhecia PDF entre os `documentMessage`: dos 110
documentos de grupo com anexo gravado, **110 eram pdf e ZERO xlsx/docx**.
Planilha e Word não davam erro — `dispatch_attachment_to_worker` devolvia
"no_attachment" e a mensagem seguia como se não tivesse anexo.

Custo medido: "o documento dos processos internos" (31/08) e o
`Vallen_Registro_Indicadores.xlsx` ficaram invisíveis, e a camada CoS chegou a
afirmar que o mapeamento de processos NÃO EXISTIA — conclusão tirada da
própria cegueira. Junto foram a ata da reunião extraordinária de 28/07 e os
relatórios financeiros mensais da Vallen.

Rodar: PYTHONPATH=app python -m pytest tests/test_wa_documento.py -v
"""
import importlib.util
import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.wa_attachment_dispatch import (  # noqa: E402
    EXTENSOES_DOCUMENTO,
    _detect_attachment_kind,
)


def _carregar_worker():
    """O extrator vive no worker (deploy separado), em `doc_extract.py`.

    Módulo próprio de propósito: a primeira versão importava o `main.py`, que
    sobe FastAPI e scheduler — o import falhava por dependência do worker e os
    8 testes do extrator viravam SKIP. Passavam em verde sem testar nada, que é
    o mesmo modo de falha que este arquivo existe para cobrir. Sem try/except
    aqui: se o extrator não importar, o teste tem de FALHAR, não sumir.
    """
    caminho = os.path.join(_ROOT, "workers", "audio-transcriber", "doc_extract.py")
    spec = importlib.util.spec_from_file_location("worker_doc_extract", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(fname: str, mime: str = "") -> dict:
    return {"documentMessage": {"fileName": fname, "mimetype": mime}}


class TestDetector:
    def test_pdf_continua_pdf(self):
        assert _detect_attachment_kind(_doc("contrato.pdf")) == "pdf"
        assert _detect_attachment_kind(_doc("x", "application/pdf")) == "pdf"

    def test_planilha_e_word_agora_entram(self):
        """Os dois arquivos que sumiram em 31/08."""
        assert _detect_attachment_kind(_doc("Vallen_Registro_Indicadores.xlsx")) == "documento"
        assert _detect_attachment_kind(_doc("processos internos.docx")) == "documento"

    def test_reconhece_por_mimetype_quando_o_nome_nao_ajuda(self):
        """O WhatsApp nem sempre manda `fileName` com extensão."""
        assert _detect_attachment_kind(_doc(
            "arquivo", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )) == "documento"
        assert _detect_attachment_kind(_doc("arquivo", "text/csv")) == "documento"

    def test_video_e_zip_ficam_de_fora(self):
        """Não há extrator para eles. Devolver um kind que o worker não sabe
        tratar trocaria o silêncio por um erro gravado a cada mensagem — pior,
        porque enche a tabela de falha que ninguém pode consertar."""
        assert _detect_attachment_kind(_doc("VIDEO_FINAL.mp4")) is None
        assert _detect_attachment_kind(_doc("IMG_1794.MOV")) is None
        assert _detect_attachment_kind(_doc("backup.zip")) is None

    def test_audio_e_imagem_nao_regridem(self):
        assert _detect_attachment_kind({"audioMessage": {}}) == "audio"
        assert _detect_attachment_kind({"imageMessage": {}}) == "image"
        assert _detect_attachment_kind({}) is None
        assert _detect_attachment_kind(None) is None

    def test_extensoes_cobrem_o_que_apareceu_em_prod(self):
        """Medido nos 281 documentos de grupo sem anexo: xlsx, docx, doc, pptx."""
        for ext in (".xlsx", ".docx", ".doc", ".pptx"):
            assert ext in EXTENSOES_DOCUMENTO


class TestExtrator:
    """O extrator lê xlsx/docx/pptx como ZIP de XML — sem somar openpyxl,
    python-docx e python-pptx ao worker por causa de ~30 arquivos."""

    def setup_method(self):
        self.w = _carregar_worker()

    def _xlsx(self, valores) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/sharedStrings.xml",
                       "<sst>" + "".join(f"<t>{v}</t>" for v in valores) + "</sst>")
            z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        return buf.getvalue()

    def _docx(self, paragrafos) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml",
                       "<w:document><w:body>"
                       + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragrafos)
                       + "</w:body></w:document>")
        return buf.getvalue()

    def test_xlsx_devolve_as_celulas(self):
        dados = self._xlsx(["Indicador", "Meta", "Faturamento", "120000"])
        texto, motor, erro = self.w._extrair_texto_documento(dados, "Vallen_Registro_Indicadores.xlsx")
        assert erro is None and motor == "zip:xlsx"
        assert "Faturamento" in texto and "120000" in texto

    def test_docx_devolve_os_paragrafos(self):
        dados = self._docx(["Mapeamento de processos internos", "Etapa 1: triagem"])
        texto, motor, erro = self.w._extrair_texto_documento(dados, "processos.docx")
        assert erro is None and motor == "zip:docx"
        assert "Mapeamento de processos internos" in texto
        assert "Etapa 1: triagem" in texto

    def test_csv_e_txt_sem_zip(self):
        texto, motor, erro = self.w._extrair_texto_documento(
            "nome;valor\nAndressa;100\n".encode(), "relacao.csv")
        assert erro is None and "Andressa" in texto and motor.startswith("decode:")

    def test_acentuacao_sobrevive(self):
        """Nome de arquivo e conteúdo em português — perder acento aqui
        estragaria a busca por texto depois."""
        texto, _, erro = self.w._extrair_texto_documento(
            "relatório de repasses — junho\n".encode("utf-8"), "financeiro.txt")
        assert erro is None and "relatório" in texto and "—" in texto

    def test_entidade_xml_vira_caractere(self):
        dados = self._docx(["Receita &amp; Despesa &lt;consolidado&gt;"])
        texto, _, _ = self.w._extrair_texto_documento(dados, "x.docx")
        assert "Receita & Despesa <consolidado>" in texto

    def test_formato_desconhecido_devolve_MOTIVO(self):
        """Erro nomeado, não None silencioso: o worker grava o motivo em
        `wa_attachments.error`, senão a mensagem volta a parecer 'sem anexo' —
        que é exatamente o defeito original."""
        texto, motor, erro = self.w._extrair_texto_documento(b"\x00\x01binario", "coisa.bin")
        assert texto is None and erro and "formato_nao_suportado" in erro

    def test_zip_corrompido_nao_estoura(self):
        texto, _, erro = self.w._extrair_texto_documento(b"PK\x03\x04lixo", "quebrado.xlsx")
        assert texto is None and erro

    def test_planilha_vazia_e_reportada(self):
        dados = self._xlsx([])
        texto, _, erro = self.w._extrair_texto_documento(dados, "vazia.xlsx")
        assert texto is None and erro == "planilha_vazia"


class TestWiring:
    def test_endpoint_do_documento_esta_mapeado(self):
        """Guard: kind novo sem entrada no `endpoint_map` levantaria KeyError
        no dispatch — e o dispatch roda dentro de try/except no chamador, então
        o erro sumiria."""
        src = open(os.path.join(_ROOT, "app", "services", "wa_attachment_dispatch.py"),
                   encoding="utf-8").read()
        assert '"documento": "/analyze-document"' in src

    def test_worker_expoe_a_rota(self):
        src = open(os.path.join(_ROOT, "workers", "audio-transcriber", "main.py"),
                   encoding="utf-8").read()
        assert '@app.post("/analyze-document")' in src

    def test_filename_e_mimetype_vao_no_payload(self):
        """O extrator decide o formato pelo nome/mime. Sem eles no payload, o
        worker recebe o arquivo e não sabe como abrir."""
        src = open(os.path.join(_ROOT, "app", "services", "wa_attachment_dispatch.py"),
                   encoding="utf-8").read()
        assert 'if kind in ("pdf", "documento")' in src
        assert 'payload["mimetype"]' in src
