"""O `create_draft` do gmail-proxy descartava `cc` em silêncio (10/10/2026).

O incidente: a CoS passou `cc` em `params`, o proxy montava um `MIMEText` com
só `To` e `Subject`, e o endereço era jogado fora SEM erro. O e-mail à Fernanda
saiu sem o Orestes em cópia e o corpo terminava com "Pai, consegue mandar para
ela?" — dirigido a quem não recebeu. Teve de ser reenviado à mão.

Parâmetro aceito e descartado em silêncio é pior que parâmetro recusado: quem
chama não tem como saber que não funcionou. Por isso estes testes olham o MIME
REAL que vai para o Gmail, não o retorno do endpoint — era justamente o retorno
que parecia certo enquanto a cópia se perdia.

A montagem foi EXTRAÍDA para `services.gmail_mime` para que estes testes
exercitem o código de produção. A 1ª versão deles tinha um espelho da lógica
aqui dentro — e, no stash, 8 dos 10 passaram verdes com o `main.py` revertido,
porque testavam a si mesmos. Espelho de lógica é o fake frouxo de
[[feedback_fake_frouxo_da_verde_a_bug_real]]: dá a sensação de cobertura
exatamente onde não há. As duas guardas de classe no fim cobrem o que o teste
de comportamento não alcança: que o endpoint DELEGA em vez de remontar.

Rodar: .venv/bin/python -m pytest tests/test_gmail_proxy_create_draft.py -v
"""
import base64
import os
import sys
from email import message_from_bytes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)


# ------------------------------------------------- o CÓDIGO REAL, sem espelho --
#
# A 1ª versão destes testes tinha um espelho de `_monta_mime` aqui dentro, e
# passava verde com o `main.py` revertido: testava a si mesma. A montagem foi
# extraída para `services.gmail_mime` justamente para que isto exercite o que
# roda em produção ([[feedback_fake_frouxo_da_verde_a_bug_real]]).

from services.gmail_mime import monta_mensagem, monta_raw, normaliza_enderecos  # noqa: E402


def _monta_mime(params):
    return monta_mensagem(
        to=params.get("to"), subject=params.get("subject", ""),
        body=params.get("body", ""), cc=params.get("cc"),
        bcc=params.get("bcc"), attachments=params.get("attachments"),
    ).as_bytes()


def _lido(params):
    return message_from_bytes(_monta_mime(params))


# ------------------------------------------------------- o caso de 10/10 --

def test_cc_chega_no_mime():
    """O caso real. Sem isto o Orestes não recebe e ninguém é avisado."""
    m = _lido({
        "to": "fernanda@exemplo.com",
        "cc": "orestes@exemplo.com",
        "subject": "Planilha 2023-24",
        "body": "Pai, consegue mandar para ela?",
    })

    assert m["Cc"] == "orestes@exemplo.com", "o cc foi descartado outra vez"
    assert m["To"] == "fernanda@exemplo.com"


def test_bcc_chega_no_mime():
    m = _lido({"to": "a@x.com", "bcc": "oculto@x.com", "subject": "s", "body": "b"})

    assert m["Bcc"] == "oculto@x.com"


def test_cc_como_LISTA_nao_vira_repr_de_python():
    """A camada manda ora string, ora lista.

    Uma lista crua no cabeçalho viraria `['a@x.com']` e o Gmail silenciaria o
    endereço — o MESMO modo de falha do incidente, uma camada acima: aceito,
    não recusado, e perdido.
    """
    m = _lido({"to": ["a@x.com", "b@x.com"], "cc": ["c@x.com"],
               "subject": "s", "body": "b"})

    assert m["To"] == "a@x.com, b@x.com"
    assert m["Cc"] == "c@x.com"
    assert "[" not in (m["To"] or ""), "lista virou repr de Python no cabeçalho"


def test_sem_cc_nao_cria_cabecalho_vazio():
    """`Cc:` vazio em rascunho é lixo visível para quem abre o e-mail."""
    m = _lido({"to": "a@x.com", "subject": "s", "body": "b"})

    assert m["Cc"] is None
    assert m["Bcc"] is None


# ------------------------------------------------------------------ anexo --

def test_anexo_entra_com_nome_e_tipo():
    conteudo = b"PK\x03\x04conteudo-falso-de-docx"
    m = _lido({
        "to": "glaucia@exemplo.com", "subject": "Contrato", "body": "Segue.",
        "attachments": [{
            "filename": "contrato.docx",
            "content_b64": base64.urlsafe_b64encode(conteudo).decode(),
            "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }],
    })

    assert m.is_multipart()
    anexos = [p for p in m.walk() if p.get_filename()]
    assert [p.get_filename() for p in anexos] == ["contrato.docx"]
    assert anexos[0].get_payload(decode=True) == conteudo


def test_dois_anexos():
    """O caso que a CoS fez à mão: rascunho com cópia e dois .docx."""
    a = base64.urlsafe_b64encode(b"um").decode()
    b = base64.urlsafe_b64encode(b"dois").decode()
    m = _lido({
        "to": "a@x.com", "cc": "b@x.com", "subject": "s", "body": "b",
        "attachments": [
            {"filename": "um.docx", "content_b64": a},
            {"filename": "dois.docx", "content_b64": b},
        ],
    })

    assert sorted(p.get_filename() for p in m.walk() if p.get_filename()) == \
        ["dois.docx", "um.docx"]
    assert m["Cc"] == "b@x.com", "anexo não pode atropelar a cópia"


def test_base64_do_gmail_sem_padding_e_aceito():
    """O `get_attachment` deste mesmo proxy devolve base64URL SEM padding.

    Reanexar o que o proxy acabou de baixar não pode exigir recompor padding
    na mão — senão o caminho natural (baixar de uma thread, anexar noutra)
    falha por um detalhe de alfabeto.
    """
    conteudo = b"cinco"  # 5 bytes => base64 com padding, que removemos
    sem_padding = base64.urlsafe_b64encode(conteudo).decode().rstrip("=")
    assert sem_padding.endswith("=") is False

    m = _lido({"to": "a@x.com", "subject": "s", "body": "b",
               "attachments": [{"filename": "f.bin", "content_b64": sem_padding}]})

    anexos = [p for p in m.walk() if p.get_filename()]
    assert anexos and anexos[0].get_payload(decode=True) == conteudo


def test_anexo_incompleto_nao_derruba_o_rascunho():
    """Anexo sem nome/conteúdo é ignorado com log — o rascunho ainda sai.

    Perder o anexo é ruim; perder o rascunho inteiro (e o texto já escrito) é
    pior, e é o que uma exceção aqui causaria.
    """
    m = _lido({"to": "a@x.com", "subject": "s", "body": "corpo",
               "attachments": [{"filename": "sem_conteudo.pdf"}, {}]})

    assert m["To"] == "a@x.com"
    assert [p.get_filename() for p in m.walk() if p.get_filename()] == []


# --------------------------------------------------------- guardas de classe --
#
# 🔴 POR AST, NÃO POR SUBSTRING — e isto custou três falsos positivos no mesmo
# dia. Uma guarda que faz `"MIMEText" not in fonte` casa com a palavra dentro
# do comentário que EXPLICA o defeito, e reprova código certo: aconteceu aqui,
# aconteceu na guarda do heartbeat (`atraso_min` no comentário) e aconteceu de
# novo quando a docstring do serviço passou a dizer *procurar `MIMEText(`* —
# a frase que descreve a guarda quebrou a guarda. Três vezes não é descuido, é
# o método errado: prosa sobre código vive no mesmo arquivo que o código, e
# substring não sabe distinguir os dois. O AST só vê o programa.


def _nomes_chamados(caminho):
    """Todos os nomes de função efetivamente CHAMADOS no arquivo.

    Comentário e docstring não produzem nó `Call`, então ficam de fora por
    construção — que é exatamente o ponto.
    """
    import ast

    with open(caminho, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())

    nomes = set()
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        f = no.func
        if isinstance(f, ast.Name):
            nomes.add(f.id)
        elif isinstance(f, ast.Attribute):
            nomes.add(f.attr)
    return nomes


def test_montagem_do_mime_nao_usa_mimetext_em_lugar_nenhum():
    """`MIMEText` não tem onde pôr cópia nem anexo: se for CHAMADO, o bug voltou.

    Vale para os dois arquivos de uma vez — não importa onde alguém remonte o
    MIME, importa que ninguém o remonte com a classe que perdeu o `cc`.
    """
    for rel in (("app", "main.py"), ("app", "services", "gmail_mime.py")):
        caminho = os.path.join(_ROOT, *rel)
        assert "MIMEText" not in _nomes_chamados(caminho), (
            f"{rel[-1]} voltou a chamar MIMEText — cc e anexo somem calados"
        )


def test_o_servico_monta_com_emailmessage_e_anexa():
    """A lógica tem de estar NO serviço, e usar a classe que suporta cópia."""
    chamados = _nomes_chamados(os.path.join(_ROOT, "app", "services", "gmail_mime.py"))

    assert "EmailMessage" in chamados
    assert "add_attachment" in chamados
    assert "urlsafe_b64decode" in chamados

def test_main_delega_a_montagem_e_nao_remonta_o_mime():
    """`main.py` tem de DELEGAR, não remontar.

    Os testes de comportamento exercitam `services.gmail_mime`. Se alguém
    voltar a montar o MIME inline no endpoint, eles seguem verdes e a
    regressão passa — é a única coisa que eles não conseguem ver.
    """
    with open(os.path.join(_ROOT, "app", "main.py"), encoding="utf-8") as fh:
        fonte = fh.read()

    i = fonte.index('if action == "create_draft":')
    assert "monta_raw" in fonte[i:i + 4000], "o endpoint parou de delegar"


def test_docstring_lista_todas_as_acoes_reais():
    """A lista incompleta fez a CoS concluir que não dava pra baixar anexo.

    Capacidade que existe e não está escrita é, na prática, capacidade que não
    existe. Este teste descobre as ações pelo CÓDIGO e exige que cada uma esteja
    na docstring — inclusive as declaradas na forma `action in (...)`, que um
    grep por `action ==` não enxerga (foi o que me fez contar 13 em vez de 14).
    """
    import re

    with open(os.path.join(_ROOT, "app", "main.py"), encoding="utf-8") as fh:
        fonte = fh.read()

    ini = fonte.index('@app.post("/api/admin/gmail-proxy")')
    corpo = fonte[ini:ini + 30000]
    fim = corpo.index('@app.', 10)
    corpo = corpo[:fim]

    acoes = set(re.findall(r'if action == "([a-z_]+)"', corpo))
    for grupo in re.findall(r'if action in \(([^)]*)\)', corpo):
        acoes |= set(re.findall(r'"([a-z_]+)"', grupo))

    assert len(acoes) >= 14, f"achei só {len(acoes)}: {sorted(acoes)}"

    docstring = corpo[:corpo.index('"""', corpo.index('"""') + 3) + 3]
    faltando = sorted(a for a in acoes if a not in docstring)
    assert not faltando, f"ações reais fora da docstring: {faltando}"
