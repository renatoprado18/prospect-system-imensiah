"""Contrato das superfícies — caminho C, decidido pelo Renato em 07/08/2026.

Cada superfície responde UMA pergunta, e **nenhuma responde "o que precisa de
mim hoje" duas vezes**:

    ~/cockpit/index.html   → "o que precisa de mim hoje?"  (a ÚNICA)
    Home INTEL             → "quem é essa pessoa, como está esse projeto?"
    tonIAH                 → conversa no WhatsApp
    camada (agente)        → o julgamento que alimenta o cockpit
    ~/cockpit/sistema.html → "o sistema está saudável?"

POR QUE UM TESTE E NÃO SÓ UM MEMO. A divergência de 30/07 não nasceu de
desleixo: nasceu de gente (e sessões) *ajudando* — cada superfície ganhou um
"precisa de você" porque parecia útil. Memo não impede isso; o próximo a mexer
não vai lê-lo. Um teste que quebra, sim.

Ele trava o que é verificável em texto — vocabulário de cobrança na home. Não
substitui julgamento: quem for adicionar julgamento numa tela precisa perguntar
qual das cinco perguntas ela responde. Se for a primeira, ela não existe.

Ver [[project_tres_cockpits_divergentes]] para o contrato e o plano completo.
"""
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(RAIZ, "app", "templates", "rap_dashboard.html")


def _bloco_statcard_projetos(html: str) -> str:
    """O trecho que monta o statcard de projetos — markup E o JS que o preenche.

    São dois lugares distantes no arquivo: o `<div>` no topo e o
    `breakdown.innerHTML` centenas de linhas abaixo. Pegar só o primeiro
    (`find`) fazia o teste passar por não encontrar pílula nenhuma — verde por
    olhar no lugar errado, que é o falso negativo clássico desta casa.
    """
    i = html.find("statProjectsAction")
    j = html.find("breakdown.innerHTML")
    assert i > 0, "statcard de projetos sumiu — se foi renomeado, atualize este teste"
    assert j > 0, "o preenchimento das pílulas sumiu — atualize este teste"
    return html[max(0, i - 600): i + 900] + "\n" + html[j - 900: j + 900]


def test_home_nao_usa_vocabulario_de_cobranca_nos_projetos():
    """A home informa, não cobra.

    `danger`/`warning` nas pílulas é vocabulário de PORTÃO — e o portão mora em
    uma superfície só. Foi com duas telas gritando a mesma urgência sobre listas
    diferentes que se chegou a "cobrar Piccino HOJE, prazo irreversível" ao lado
    de "nada aberto, ela já entregou", no mesmo dia.
    """
    bloco = _bloco_statcard_projetos(open(DASHBOARD, encoding="utf-8").read())
    pilulas = re.findall(r'class="pill ([^"]*)"', bloco)
    assert pilulas, "as pílulas do statcard sumiram — atualize o teste"
    for cls in pilulas:
        assert "danger" not in cls and "warning" not in cls, (
            f'pílula "{cls}" reintroduz vocabulário de urgência na home. '
            "A home responde 'como está esse projeto', não 'o que precisa de mim "
            "hoje' — isso é do cockpit. Ver project_tres_cockpits_divergentes."
        )


def test_home_nao_promete_responder_o_que_precisa_de_voce():
    """Rótulo é contrato com o leitor: 'precisa de atenção' promete portão."""
    bloco = _bloco_statcard_projetos(open(DASHBOARD, encoding="utf-8").read())
    # Só o que é VISÍVEL: title/label. Comentário explicando a decisão pode (e
    # deve) citar os termos — travar comentário faria o teste proibir a própria
    # documentação da regra.
    visivel = re.findall(r'(?:title|stat-label"?>)\s*=?"?([^"<]{3,60})', bloco)
    proibidos = ("precisa de atenção", "precisa de voce", "precisa de você",
                 "precisa de acao", "precisa de ação", "urgente")
    for txt in visivel:
        baixo = txt.lower()
        for p in proibidos:
            assert p not in baixo, (
                f'texto visível "{txt.strip()}" promete portão na home. '
                "Quem responde 'o que precisa de mim hoje' é o cockpit local."
            )
