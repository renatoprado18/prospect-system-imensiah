"""PDF de WhatsApp que o Claude recusa (22/11/26, task #1000688).

`claude_400` era o maior balde de anexo ilegível (41% de 166) e crescia: jul 1 ·
ago 16 · set 1 · out 16 · nov 34. O worker gravava só o código, sem a mensagem
da API, e não tinha plano B — o texto existia no PDF e se perdia mesmo assim.

Rodar: python -m pytest tests/test_wa_pdf_fallback.py -v
"""
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "workers", "audio-transcriber"))

from pypdf import PdfReader, PdfWriter  # noqa: E402

from doc_extract import _extrair_texto_pdf, _motivo_erro_api  # noqa: E402


def _pdf_com_texto(texto: str) -> bytes:
    """PDF mínimo com camada de texto, montado à mão (pypdf não escreve texto)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({texto}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def test_pdf_com_texto_e_resgatado():
    texto, motor, erro = _extrair_texto_pdf(_pdf_com_texto("Ata da reuniao de 28/07"))
    assert erro is None
    assert "Ata da reuniao de 28/07" in texto
    assert motor == "pypdf:1p"


def test_pdf_sem_camada_de_texto_diz_o_motivo():
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    texto, motor, erro = _extrair_texto_pdf(buf.getvalue())
    assert texto is None
    assert erro == "pdf_sem_camada_de_texto"


def test_pdf_com_senha_so_de_permissao_e_lido():
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf_com_texto("Relatorio Vallen"))))
    w.encrypt(user_password="", owner_password="dono")
    buf = io.BytesIO()
    w.write(buf)
    texto, _, erro = _extrair_texto_pdf(buf.getvalue())
    assert erro is None and "Relatorio Vallen" in texto


def test_pdf_com_senha_de_abertura_nao_quebra():
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf_com_texto("x"))))
    w.encrypt(user_password="segredo", owner_password="dono")
    buf = io.BytesIO()
    w.write(buf)
    texto, _, erro = _extrair_texto_pdf(buf.getvalue())
    assert texto is None and erro  # erro explícito, nunca exceção


def test_lixo_nao_levanta():
    texto, _, erro = _extrair_texto_pdf(b"isto nao e um pdf")
    assert texto is None and erro.startswith("pypdf ")


def test_motivo_guarda_mensagem_da_api():
    corpo = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "A maximum of 100 PDF pages may be provided."}})
    assert _motivo_erro_api(400, corpo) == "claude_400: A maximum of 100 PDF pages may be provided."


def test_motivo_sem_corpo_json():
    assert _motivo_erro_api(502, "<html>Bad Gateway</html>").startswith("claude_502: <html>")
    assert _motivo_erro_api(400, "") == "claude_400"
