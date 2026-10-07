"""A guarda do `send-to-group` — o botão que publica no grupo do CLIENTE.

POR QUE ESTE ARQUIVO EXISTE. A guarda de 409 foi escrita em 05/10/2026 porque a
proteção anterior era VERBAL: a sessão CoS disse ao Renato "não aperte o botão".
Instrução verbal não é guarda — depende de alguém lembrar, num botão que publica
na governança de uma empresa que não é dele.

Só que a própria guarda nasceu sem teste. Foi "provada em prod com um group_jid
falso" e mais nada. Em 07/10, com a fonte única no ar, ela passou a LIBERAR o
envio (duplicatas = 0, que é o resultado correto) — e é exatamente aí que a
ausência de teste fica perigosa: uma regressão reabriria o caminho em silêncio, e
o sinal de que algo quebrou seria uma mensagem duplicada no grupo de um conselho.

O que se prova aqui é a decisão de BLOQUEAR ou LIBERAR. O envio em si (Evolution)
não é exercitado: nenhum teste desta casa manda mensagem de verdade.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def sem_envio_real(monkeypatch):
    """Trava o envio NA PORTA DE SAÍDA REAL — e esta fixture tem história.

    🔴 07/10/2026: a primeira versão travava `main.send_group_message` e dois
    outros nomes que NÃO EXISTEM nesse módulo. `hasattr` devolvia False para os
    três, nada era trocado, e a trava era decorativa. Ao sabotar a guarda para
    conferir se o teste a alcançava, o endpoint seguiu até o fim e **mandou
    "RACI de teste" no grupo Conselho Vallen, o grupo real do cliente** — e a
    Evolution confirmou a entrega.

    Duas lições que ficam no código:
      · trava por nome com `hasattr` FALHA EM SILÊNCIO quando o nome muda ou
        nunca existiu. O caminho real é `integrations.evolution_api
        .get_evolution_client().send_text(...)`, resolvido DENTRO da função —
        então é o cliente que precisa ser substituído, não um atalho no módulo;
      · a segurança de um teste JAMAIS pode depender da guarda que ele testa.
        Sabotar a guarda é parte do método desta casa; se a única coisa entre o
        teste e o grupo do cliente for essa guarda, o método vira o risco.

    `autouse=True` porque esquecer de pedir a trava não pode ser possível.
    """
    import integrations.evolution_api as ev

    class _ClienteProibido:
        async def send_text(self, *a, **k):
            raise AssertionError(
                "ENVIO REAL BLOQUEADO: um teste tentou publicar no WhatsApp. "
                "Nenhum teste desta suíte manda mensagem — nem com a guarda "
                "sabotada."
            )
        def __getattr__(self, nome):
            async def _qualquer(*a, **k):
                raise AssertionError(f"ENVIO REAL BLOQUEADO: client.{nome}()")
            return _qualquer

    monkeypatch.setattr(ev, "get_evolution_client", lambda *a, **k: _ClienteProibido())


PATH = "/api/projects/24/raci/send-to-group"
CORPO = {"group_jid": "120363408325592607@g.us", "text": "RACI de teste"}

# A rota exige `require_scaffold_auth` (sessão admin OU X-API-Key). SEM esta
# credencial a resposta é 401 e o teste NUNCA ALCANÇA A GUARDA — foi assim que a
# primeira versão deste arquivo passou com quatro verdes medindo nada. Um teste
# que aceita "401 ou 409" aprova o endpoint por recusar a entrada, não por
# bloquear o envio: é o mesmo erro de instrumento que esta frente achou três
# vezes em 06/10.
AUTH = {"X-API-Key": "chave-de-teste"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("INTEL_API_KEY", "chave-de-teste")


def _matriz(duplicatas, checadas=True):
    return {
        "project": {"id": 24, "nome": "Vallen Clinic"},
        "itens": [],
        "duplicatas": duplicatas,
        "duplicatas_total": len(duplicatas),
        "duplicatas_checadas": checadas,
    }


def test_duplicata_BLOQUEIA_o_envio(client, monkeypatch):
    """O caso de 05/10: o grupo do cliente receberia a mesma linha duas vezes."""
    import main
    monkeypatch.setattr(
        "services.raci_matrix.get_matrix",
        lambda pid, status=None: _matriz([
            {"acao": "x", "intel_uid": "intel:1", "conselhoos_uid": "conselhoos:a",
             "status_divergente": True},
        ]))
    r = client.post(PATH, json=CORPO, headers=AUTH)
    assert r.status_code == 409, (
        f"esperado 409 (bloqueio da guarda), veio {r.status_code}: {r.text[:200]}"
    )
    assert "DUAS" in r.text or "duplicat" in r.text.lower()


def test_NAO_PODER_CONFERIR_tambem_bloqueia(client, monkeypatch):
    """O buraco fechado em 06/10, e o mais perigoso dos dois.

    Com o Neon do ConselhoOS fora, `_fetch_conselhoos_status` devolve ([], erro)
    SEM levantar exceção — então `duplicatas` vinha vazio e o envio passava como
    "limpo" tendo conferido NADA. Lista vazia por ignorância e lista vazia por
    conferência são idênticas na tela e opostas no risco.
    """
    monkeypatch.setattr(
        "services.raci_matrix.get_matrix",
        lambda pid, status=None: _matriz([], checadas=False))
    r = client.post(PATH, json=CORPO, headers=AUTH)
    assert r.status_code == 409, (
        f"esperado 409 (não pôde conferir), veio {r.status_code}: {r.text[:200]}"
    )
    assert "conferir" in r.text.lower()


def test_override_e_explicito_nunca_default(client, monkeypatch):
    """Existe caso legítimo de mandar mesmo assim — mas DECLARADO. Um default
    que liberasse seria a instrução verbal de volta, só que invisível."""
    import inspect
    import main
    src = inspect.getsource(main.api_raci_send_to_group)
    assert 'data.get("confirmar_duplicatas")' in src
    assert "confirmar_duplicatas=true" in src, (
        "a mensagem do 409 tem de dizer COMO sobrepor, senão a guarda vira beco"
    )


def test_jid_arbitrario_nao_vira_relay(client):
    """O endpoint valida o grupo contra os vinculados ao PROJETO. Sem isso ele
    seria um relay para qualquer grupo da conta do Renato."""
    import inspect
    import main
    src = inspect.getsource(main.api_raci_send_to_group)
    assert "project_whatsapp_groups" in src
