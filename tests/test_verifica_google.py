"""O verificador de divergência INTEL⇄Google não pode inventar passivo.

DOIS FALSOS POSITIVOS PEGOS NA PRIMEIRA RODADA (18/08/2026), ambos capazes de
transformar a base inteira em "passivo" e mandar alguém limpar o que está certo:

1. **3.949 órfãs cross-conta.** A mesma pessoa tem `resourceName` DIFERENTE em
   cada conta Google e `contacts.google_contact_id` é escalar — guarda um dos
   dois. A ficha da outra conta é, por construção, "não apontada por ninguém".
   Comparar entre contas acusava metade da agenda. Restrito à conta do id
   vinculado: 232, e conferidas na fonte (a "Carla" #707 está 4× no Google, duas
   vezes na MESMA conta).

2. **`#23360+#23360` como duplicata de si mesmo.** Um contato com
   "+5511988887777" e "11988887777" cadastrados tem dois telefones que
   normalizam pro mesmo sufixo de 8 dígitos, e o agrupamento por telefone o
   listava duas vezes.

Um verificador que exagera o passivo é pior que nenhum: manda limpar o que está
certo, e queima a confiança no número quando ele estiver certo.

Rodar: .venv/bin/python -m pytest tests/test_verifica_google.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "verifica_google", os.path.join(_ROOT, "scripts", "verifica_google.py"))
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

CONTA_A = "renato@almeida-prado.com"
CONTA_B = "renato.almeida.prado@gmail.com"


def _g(nome, rid, conta, tel):
    return {"nome": nome, "rid": rid, "conta": conta, "tels": [tel]}


def _c(cid, nome, tel, gid=None, msgs=0):
    return {"id": cid, "nome": nome, "telefones": [{"number": tel}],
            "google_contact_id": gid, "msgs": msgs}


def test_mesma_pessoa_nas_duas_contas_nao_e_orfa():
    """O caso que acusou 3.949: cada conta tem um resourceName próprio."""
    google = [_g("Thalita Mendes", "cAAA", CONTA_A, "+5511988887777"),
              _g("Thalita Mendes", "cBBB", CONTA_B, "+5511988887777")]
    contatos = [_c(1, "Thalita Mendes", "+5511988887777", gid="cAAA")]
    b, _ = vg.classificar(google, contatos)
    assert b["orfa"] == [], "ficha da outra conta contada como órfã"


def test_ficha_nao_apontada_na_mesma_conta_e_orfa():
    """Controle positivo: sem ele, o teste acima passa com a detecção desligada."""
    google = [_g("Carla", "cAAA", CONTA_A, "+5511976900869"),
              _g("Carla", "cDUP", CONTA_A, "+5511976900869")]
    contatos = [_c(707, "Carla", "+5511976900869", gid="cAAA")]
    b, _ = vg.classificar(google, contatos)
    assert len(b["orfa"]) == 1
    assert b["orfa"][0]["orfa_rid"] == "cDUP"


def test_contato_com_dois_formatos_do_mesmo_numero_nao_duplica_consigo():
    """O caso `#23360+#23360`."""
    contatos = [{"id": 23360, "nome": "Jose Olimpio", "google_contact_id": None, "msgs": 3,
                 "telefones": [{"number": "+5511988887777"}, {"number": "11988887777"}]}]
    b, _ = vg.classificar([], contatos)
    assert b["dupe_intel"] == [], "contato listado como duplicata de si mesmo"


def test_duas_fichas_intel_no_mesmo_telefone_sao_duplicata():
    """Controle positivo do balde 🟤 — "Leo" #16072 e "Leo" #21985."""
    contatos = [_c(16072, "Léo", "+5511988887777"), _c(21985, "Leo", "+5511988887777")]
    b, _ = vg.classificar([], contatos)
    assert len(b["dupe_intel"]) == 1
    assert b["dupe_intel"][0]["ids"] == [16072, 21985]


def test_telefone_compartilhado_nao_e_divergencia():
    """`Copersucar` no INTEL e `Sidnei Rosa` no Google: a empresa e quem atende."""
    google = [_g("Copersucar", "cAAA", CONTA_A, "+551133334444"),
              _g("Sidnei Rosa", "cBBB", CONTA_A, "+551133334444")]
    contatos = [_c(1, "Copersucar", "+551133334444", gid="cAAA")]
    b, _ = vg.classificar(google, contatos)
    assert b["divergencia"] == []
    assert len(b["compartilhado"]) == 1


def test_grafia_diferente_da_mesma_pessoa_e_decisao():
    """O único balde que vai pra tela do Renato."""
    google = [_g("Prof Dr Gabriel Cogo", "cAAA", CONTA_A, "+5511977776666")]
    contatos = [_c(2180, "Gabriel Cogo", "+5511977776666", gid="cAAA", msgs=166)]
    b, _ = vg.classificar(google, contatos)
    assert len(b["divergencia"]) == 1
    assert b["divergencia"][0]["id"] == 2180


def test_nome_igual_nao_vira_decisao():
    """Quem já bate dos dois lados não pode aparecer como pendência."""
    google = [_g("Gabriel Cogo", "cAAA", CONTA_A, "+5511977776666")]
    contatos = [_c(2180, "Gabriel Cogo", "+5511977776666", gid="cAAA")]
    b, _ = vg.classificar(google, contatos)
    assert b["divergencia"] == []


def test_sem_contraparte_no_google_cai_em_ausente():
    contatos = [_c(1, "Fulano", "+5511911112222")]
    b, _ = vg.classificar([], contatos)
    assert len(b["ausente"]) == 1


def test_par_ja_decidido_nao_volta_a_ser_perguntado():
    """O caso Douglas Bassi × Orestes (22/08/26).

    Os dois trabalham na Virtus BR Partners e compartilham o fixo
    +55 11 3576-1505 — `home` num, `workfax` no outro. O Renato já tinha decidido
    que NÃO são a mesma pessoa, e a decisão voltou a ser perguntada porque não
    havia onde escrevê-la: prosa em memo nenhum script lê. Ferramenta que
    repropõe o decidido vira tarefa dele.
    """
    contatos = [_c(1360, "Douglas Bassi", "+551135761505"),
                _c(4376, "Orestes Alves de Almeida Prado", "+551135761505")]
    b, _x = vg.classificar([], contatos, nao_fundir={(1360, 4376)})
    assert b["dupe_intel"] == [] and b["tel_compartilhado_intel"] == [], (
        "par decidido voltou a ser proposto")


def test_par_nao_decidido_continua_aparecendo():
    """Controle positivo: sem ele, o teste acima passa com o balde desligado.

    Douglas e Orestes têm nomes sem nada em comum, então caem no balde de
    telefone de empresa — não no de ficha repetida. O que importa aqui é que
    aparecem em ALGUM lugar enquanto ninguém decidiu."""
    contatos = [_c(1360, "Douglas Bassi", "+551135761505"),
                _c(4376, "Orestes Alves de Almeida Prado", "+551135761505")]
    b, _x = vg.classificar([], contatos, nao_fundir=set())
    assert len(b["dupe_intel"]) + len(b["tel_compartilhado_intel"]) == 1


def test_ficha_repetida_e_telefone_de_empresa_sao_baldes_diferentes():
    """`Bettina Berman` 3× é limpeza; `Carla / Vania Leister` no fixo da empresa
    pede o olho de quem conhece a relação. Misturar os dois faz a lista pedir
    julgamento onde bastava limpeza."""
    repetida = [_c(491, "Bettina Berman", "+5511911112222"),
                _c(21428, "Bettina Berman", "+5511911112222")]
    b1, _ = vg.classificar([], repetida)
    assert len(b1["dupe_intel"]) == 1 and b1["tel_compartilhado_intel"] == []

    empresa = [_c(707, "Carla", "+551133334444"),
               _c(26483, "Vania Leister", "+551133334444")]
    b2, _ = vg.classificar([], empresa)
    assert b2["dupe_intel"] == [] and len(b2["tel_compartilhado_intel"]) == 1


def test_ordem_do_par_nao_muda_a_decisao():
    """A tabela guarda em ordem canônica (a < b); o filtro tem que casar
    independentemente da ordem em que os contatos aparecem na varredura."""
    contatos = [_c(4376, "Orestes", "+551135761505"),
                _c(1360, "Douglas Bassi", "+551135761505")]
    b, _x = vg.classificar([], contatos, nao_fundir={(1360, 4376)})
    assert b["dupe_intel"] == [] and b["tel_compartilhado_intel"] == []


def test_colega_de_trabalho_no_telefone_da_empresa_nao_e_orfa():
    """O FALSO POSITIVO QUE QUASE APAGOU A AGENDA (22/08/26).

    Pedido: "limpa as 231 órfãs no Google". Ao baixar as duas pontas antes de
    apagar, a maioria não era duplicata: a ficha #697 ("Vanessa Ikeno") arrastava
    Magda Glasser, Lucca Najar, Camila Lisbôa e mais quatro — todos com e-mail
    @carambola.com.vc, colegas no telefone da empresa. A #2856 arrastava meia
    Amcham. Apagar teria destruído dezenas de contatos reais.

    É a mesma premissa falsa que já custou dois baldes neste mesmo dia: "mesmo
    telefone = mesma pessoa". Órfã de merge é ficha REPETIDA.
    """
    google = [_g("Vanessa Ikeno", "cPRINC", CONTA_A, "+551133334444"),
              _g("Magda Glasser", "cCOLEGA", CONTA_A, "+551133334444")]
    contatos = [_c(697, "Vanessa Ikeno", "+551133334444", gid="cPRINC")]
    b, _ = vg.classificar(google, contatos)
    assert b["orfa"] == [], "colega de trabalho contado como órfã de merge"


def test_ficha_repetida_da_mesma_pessoa_continua_sendo_orfa():
    """Controle positivo do corte acima — sem ele, a detecção morre inteira."""
    google = [_g("Glaucia Parizotto", "cPRINC", CONTA_A, "+5511977776666"),
              _g("Glaucia Parizotto", "cDUP", CONTA_A, "+5511977776666")]
    contatos = [_c(26688, "Glaucia Parizotto", "+5511977776666", gid="cPRINC")]
    b, _ = vg.classificar(google, contatos)
    assert len(b["orfa"]) == 1
    assert b["orfa"][0]["orfa_rid"] == "cDUP"


def test_ficha_sem_nome_no_google_continua_sendo_orfa():
    """Ficha vazia não tem nome pra ter parentesco, e é justamente o resto de
    merge mais provável — não pode escapar pelo critério de nome."""
    google = [_g("Shirley", "cPRINC", CONTA_A, "+5511955554444"),
              _g("", "cVAZIA", CONTA_A, "+5511955554444")]
    contatos = [_c(23280, "Shirley", "+5511955554444", gid="cPRINC")]
    b, _ = vg.classificar(google, contatos)
    assert len(b["orfa"]) == 1
