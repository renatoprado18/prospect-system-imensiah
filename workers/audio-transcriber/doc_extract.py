"""Extração de texto de documento que não é PDF (08/09/26).

Módulo separado do `main.py` de propósito: importar o main sobe o FastAPI e o
scheduler, então o teste do extrator ou seria pesado ou seria pulado — e teste
pulado é verde que não prova nada. Aqui ele roda de verdade.

xlsx/docx/pptx são ZIPs de XML: ler o XML direto evita somar openpyxl,
python-docx e python-pptx ao worker por causa de ~30 arquivos. O texto sai sem
formatação, que é o que os leitores de conversa consomem.
"""
import io
import re
import zipfile


def _extrair_texto_documento(conteudo: bytes, filename: str, mimetype: str = "") -> tuple:
    """Extrai texto de xlsx/docx/pptx/csv/txt. Devolve (texto, motor, erro).

    Sem dependência nova quando dá: xlsx/docx/pptx são ZIPs de XML, e ler o XML
    direto evita somar openpyxl+python-docx+python-pptx ao worker por causa de
    ~30 arquivos. O texto sai sem formatação — é o que os leitores consomem.
    """
    import io
    import re
    import zipfile

    nome = (filename or "").lower()
    mime = (mimetype or "").lower()

    def _por_extensao(*exts):
        return nome.endswith(exts)

    try:
        # Texto puro
        if _por_extensao(".txt", ".md", ".csv") or "text/plain" in mime or "csv" in mime:
            for enc in ("utf-8", "latin-1"):
                try:
                    return conteudo.decode(enc), "decode:" + enc, None
                except UnicodeDecodeError:
                    continue
            return None, None, "nao_decodificou"

        if not zipfile.is_zipfile(io.BytesIO(conteudo)):
            return None, None, f"formato_nao_suportado:{nome[-8:] or mime[:40]}"

        zf = zipfile.ZipFile(io.BytesIO(conteudo))
        nomes = zf.namelist()

        # Quando o WhatsApp manda o documento com LEGENDA em vez de nome de
        # arquivo, `filename` e `mimetype` chegam vazios — e foi justamente o
        # caso dos que mais importavam ("o documento dos processos internos",
        # os relatorios financeiros da Vallen, a ata de 28/07). Decidir so pelo
        # nome deixaria exatamente esses de fora. O conteudo do ZIP diz o
        # formato sem depender de metadado: `xl/` e planilha, `word/` e
        # documento, `ppt/` e apresentacao.
        if not nome and not mime:
            if any(n.startswith("xl/") for n in nomes):
                nome = ".xlsx"
            elif any(n.startswith("word/") for n in nomes):
                nome = ".docx"
            elif any(n.startswith("ppt/") for n in nomes):
                nome = ".pptx"

        # XLSX: strings compartilhadas + células inline.
        if _por_extensao(".xlsx", ".ods") or "spreadsheet" in mime:
            partes = []
            if "xl/sharedStrings.xml" in nomes:
                xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                partes += re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S)
            for n in nomes:
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                    xml = zf.read(n).decode("utf-8", "replace")
                    partes += re.findall(r"<t>(.*?)</t>", xml, re.S)
            texto = " | ".join(_limpar_xml(p) for p in partes if p.strip())
            return (texto or None), "zip:xlsx", (None if texto else "planilha_vazia")

        # DOCX: parágrafos do documento principal.
        if _por_extensao(".docx", ".odt") or "wordprocessing" in mime:
            alvo = "word/document.xml" if "word/document.xml" in nomes else None
            if not alvo:
                alvo = next((n for n in nomes if n.endswith("content.xml")), None)
            if not alvo:
                return None, None, "docx_sem_document_xml"
            xml = zf.read(alvo).decode("utf-8", "replace")
            partes = re.findall(r"<w:t[^>]*>(.*?)</w:t>|<text:p[^>]*>(.*?)</text:p>", xml, re.S)
            planos = [a or b for a, b in partes]
            texto = " ".join(_limpar_xml(p) for p in planos if p and p.strip())
            return (texto or None), "zip:docx", (None if texto else "documento_vazio")

        # PPTX: texto de todos os slides.
        if _por_extensao(".pptx") or "presentation" in mime:
            partes = []
            for n in sorted(n for n in nomes if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
                xml = zf.read(n).decode("utf-8", "replace")
                partes += re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)
            texto = " ".join(_limpar_xml(p) for p in partes if p.strip())
            return (texto or None), "zip:pptx", (None if texto else "apresentacao_vazia")

        return None, None, f"zip_nao_reconhecido:{nome[-8:]}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"[:200]


def _limpar_xml(s: str) -> str:
    import re
    s = re.sub(r"<[^>]+>", "", s or "")
    for de, para in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&quot;", '"'), ("&apos;", "'"), ("&#10;", " ")):
        s = s.replace(de, para)
    return " ".join(s.split())


def _extrair_texto_pdf(conteudo: bytes) -> tuple:
    """Texto de PDF SEM LLM — o plano B do /analyze-pdf (22/11/26). (texto, motor, erro)

    POR QUE EXISTE. Quando o Claude recusava o PDF (400/413), a extração terminava
    em `error='claude_400'` e mais nada: 68 PDFs ilegíveis até 13/11, 34 só em
    novembro (#1000688). Os mesmos arquivos quase sempre têm camada de texto — o
    `wa_media_recover.py` os recuperava com pdftotext, à mão e só dentro da janela
    de ~10 dias do CDN. Aqui o resgate acontece na hora, enquanto o binário ainda
    está em memória.

    Sem OCR: PDF escaneado (só imagem) devolve `pdf_sem_camada_de_texto`, que é
    um erro diferente e honesto — não "vazio" calado.
    """
    import io
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, None, "pypdf_ausente"
    try:
        leitor = PdfReader(io.BytesIO(conteudo))
        if leitor.is_encrypted:
            try:
                leitor.decrypt("")  # muitos PDFs "protegidos" só têm senha de permissão
            except Exception:
                return None, None, "pdf_criptografado"
        partes = []
        for pagina in leitor.pages:
            try:
                partes.append(pagina.extract_text() or "")
            except Exception:
                continue  # página corrompida não derruba as outras
        texto = "\n".join(p.strip() for p in partes if p.strip())
        if not texto:
            return None, "pypdf", "pdf_sem_camada_de_texto"
        return texto, f"pypdf:{len(leitor.pages)}p", None
    except Exception as e:
        return None, None, f"pypdf {type(e).__name__}: {e}"[:200]


def _motivo_erro_api(status: int, corpo: str) -> str:
    """`claude_400` sozinho não diz nada — guarda a mensagem da API junto.

    O código cru foi o que deixou o balde maior de perda sem diagnóstico: o
    worker sabia o motivo (logava `resp.text[:200]`), mas o log some e a linha
    em `wa_attachments` ficava só com o número.
    """
    import json
    msg = ""
    try:
        msg = ((json.loads(corpo or "{}") or {}).get("error") or {}).get("message") or ""
    except Exception:
        msg = (corpo or "")[:160]
    msg = " ".join(msg.split())[:160]
    return f"claude_{status}" + (f": {msg}" if msg else "")
