"""Merge não pode FABRICAR no Google a duplicata que veio limpar.

O DEFEITO (medido em 22/08/2026, fundindo de verdade). `contact_dedup` criava
ficha nova no Google sempre que o contato mantido não tinha
`google_contact_id` PARA AQUELA CONTA — e a coluna é escalar enquanto as contas
são duas, então em pelo menos uma delas ela é sempre None.

Ao fundir as três fichas de "Bettina Berman": 2 apagadas no Google e **4
criadas**. Piora líquida, com o log dizendo `success: True`.

Era o GERADOR do passivo que o `verifica_google.py` mede, não o rescaldo dele:
quem apaga duplicata de um lado e cria de outro fica em disputa consigo mesmo, e
o mutirão seguinte reencontra o mesmo trabalho.

Rodar: .venv/bin/python -m pytest tests/test_merge_nao_cria_duplicata.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.contact_dedup import _achar_ou_criar  # noqa: E402


class _GcFake:
    """Dublê do módulo google_contacts, contando o que foi chamado."""

    def __init__(self, achado=None, update_ok=True):
        self._achado = achado
        self._update_ok = update_ok
        self.criou = 0
        self.atualizou = []

    async def buscar_ficha_existente(self, token, nome, telefones):
        return self._achado

    async def update_google_contact(self, token, rid, dados):
        self.atualizou.append(rid)
        return self._update_ok

    async def create_google_contact(self, token, dados):
        self.criou += 1
        return "cNOVA"


DADOS = {"nome": "Bettina Berman",
         "telefones": [{"number": "+55 (11) 99192-1788"}]}


@pytest.mark.asyncio
async def test_acha_a_ficha_existente_e_atualiza_em_vez_de_criar():
    gc = _GcFake(achado="cEXISTENTE")
    r = await _achar_ou_criar(gc, "tok", DADOS)
    assert gc.criou == 0, "criou ficha nova havendo uma existente — é o bug de origem"
    assert gc.atualizou == ["cEXISTENTE"]
    assert r["status"] == "updated"
    assert r["google_id"] == "cEXISTENTE"


@pytest.mark.asyncio
async def test_sem_ficha_na_conta_cria_como_antes():
    """Controle positivo: o conserto não pode ser 'nunca mais criar'."""
    gc = _GcFake(achado=None)
    r = await _achar_ou_criar(gc, "tok", DADOS)
    assert gc.criou == 1
    assert r["status"] == "created"
    assert r["google_id"] == "cNOVA"


@pytest.mark.asyncio
async def test_busca_inconclusiva_cai_no_create():
    """`None` da busca significa "não sei", e aí criar duplicata é menos grave
    que escrever por cima da ficha de outra pessoa."""
    gc = _GcFake(achado=None)
    r = await _achar_ou_criar(gc, "tok", DADOS)
    assert r["status"] == "created"


@pytest.mark.asyncio
async def test_update_que_falha_e_reportado_e_nao_vira_create_silencioso():
    """Se a ficha existe e o update falha, o resultado precisa DIZER isso —
    cair no create escondendo a falha é como o passivo nasceu."""
    gc = _GcFake(achado="cEXISTENTE", update_ok=False)
    r = await _achar_ou_criar(gc, "tok", DADOS)
    assert r["status"] == "update_falhou"
    assert gc.criou == 0


@pytest.mark.asyncio
async def test_modulo_sem_a_busca_nao_quebra():
    """Compatibilidade: chamador antigo sem `buscar_ficha_existente` segue
    funcionando, criando como antes."""
    class _Antigo:
        def __init__(self):
            self.criou = 0

        async def create_google_contact(self, token, dados):
            self.criou += 1
            return "cNOVA"

        async def update_google_contact(self, token, rid, dados):
            return True

    gc = _Antigo()
    r = await _achar_ou_criar(gc, "tok", DADOS)
    assert r["status"] == "created" and gc.criou == 1


@pytest.mark.asyncio
async def test_nao_escreve_na_ficha_que_o_merge_vai_apagar():
    """O SEGUNDO defeito, que só apareceu depois de consertar o primeiro
    (22/08). A busca acha pelo telefone — e o telefone é o MESMO nas fichas do
    contato absorvido. Sem filtrar os condenados, o merge atualizava a ficha
    prestes a ser apagada e a apagava no passo seguinte da mesma função:
    `updates` e `deletions` traziam o mesmo resourceName, e o trabalho ia junto.
    """
    gc = _GcFake(achado="cCONDENADA")
    r = await _achar_ou_criar(gc, "tok", DADOS, ignorar_rids={"cCONDENADA"})
    assert gc.atualizou == [], "escreveu na ficha que seria apagada"
    assert r["status"] == "created", "devia criar, já que a achada vai sumir"


@pytest.mark.asyncio
async def test_ficha_existente_que_NAO_sera_apagada_e_usada():
    """Controle positivo: filtrar demais desligaria o conserto do primeiro bug."""
    gc = _GcFake(achado="cBOA")
    r = await _achar_ou_criar(gc, "tok", DADOS, ignorar_rids={"cOUTRA"})
    assert gc.atualizou == ["cBOA"]
    assert r["status"] == "updated"
