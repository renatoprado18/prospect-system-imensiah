"""O importador do Fathom perdia os encaminhamentos, e a causa era o PLANO.

O BUG (medido pela sessão CoS em 19/08/2026, confirmado no código em 20/08):
`process_fathom_meeting` lia só `action_items[]`, e a conta é FREE — o próprio
e-mail do recap diz "Upgrade to Premium to unlock AI generated action items". Em
5 reuniões Alba de 60 dias, **3 vieram com `action_items` vazio** e produziram
zero task e zero nota, apesar de o resumo trazer `## Próximos Passos` com dono
nomeado em cada item.

Placar das 6 atribuições ao Renato: 1 virou task pelo Fathom, 3 saíram no
trabalho manual da CoS, **2 se perderam**. E era falha calada — nada distinguia
"reunião sem encaminhamento" de "importador não achou o encaminhamento".

O markdown abaixo é o formato REAL, copiado de uma memória gravada em produção
(Reunião Vibra, recording dA4d9g5uV3QKo9ecabZSZsPfz3LKYouC).

Rodar: .venv/bin/python -m pytest tests/test_fathom_proximos_passos.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from integrations.fathom import _proximos_passos_do_resumo  # noqa: E402

_URL = "https://fathom.video/share/dA4d9g5uV3QKo9ecabZSZsPfz3LKYouC?tab=summary&timestamp=26.0"

RESUMO_REAL = f"""## Objetivo da Reunião

[Alinhar a abordagem do projeto Vibra e definir os próximos passos.]({_URL})

## Principais Conclusões

  - [**Estratégia de Projeto:** Adotar uma abordagem modular.]({_URL})

## Tópicos

### Desafios do Projeto Vibra

  - [Algo que NÃO é encaminhamento.]({_URL})

## Próximos Passos

  - [**Renato:** Consolidar os questionários societários até a próxima quarta-feira.]({_URL})
  - [**Alba:** Agendar a reunião de alinhamento com Renato e Medeiros.]({_URL})
  - [**Alba:** Buscar um consultor especializado em agronegócio.]({_URL})


---
Gravacao Fathom: https://fathom.video/share/dA4d9g5uV3QKo9ecabZSZsPfz3LKYouC
"""


def test_extrai_os_tres_encaminhamentos():
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    assert len(itens) == 3, [i["description"] for i in itens]


def test_preserva_o_dono_no_inicio_da_descricao():
    """É o que o raci_parser lê pra nascer `delegated` quando a bola não é dele.
    Sem o prefixo, tarefa da Alba cairia na caixa do Renato."""
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    assert itens[0]["description"].startswith("Renato: ")
    assert itens[1]["description"].startswith("Alba: ")
    assert "**" not in itens[0]["description"], "negrito quebra o _PREFIX_PATTERN"


def test_o_dono_extraido_chega_no_raci_parser():
    """Controle de ponta a ponta: não basta o texto sair bonito, o parser de
    RACI precisa concordar — é ele que decide se a task polui a caixa dele."""
    from services.raci_parser import parse_raci
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    r_renato = parse_raci(itens[0]["description"][:200], itens[0]["description"])
    r_alba = parse_raci(itens[1]["description"][:200], itens[1]["description"])
    assert r_renato.is_renato is True
    assert r_alba.is_renato is False, "tarefa da Alba nasceria na caixa do Renato"


def test_guarda_o_link_do_momento_na_gravacao():
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    assert itens[0]["recording_playback_url"] == _URL


def test_nao_captura_bullets_de_outras_secoes():
    """`## Principais Conclusões` e `### Desafios` têm bullets no mesmo formato.
    Varrer o documento inteiro viraria task de tudo que foi conversado."""
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    juntos = " ".join(i["description"] for i in itens)
    assert "Estratégia de Projeto" not in juntos
    assert "NÃO é encaminhamento" not in juntos


def test_para_no_rodape():
    itens = _proximos_passos_do_resumo(RESUMO_REAL)
    assert not any("Gravacao Fathom" in i["description"] for i in itens)


def test_resumo_sem_a_secao_devolve_vazio():
    """Reunião sem encaminhamento tem que sair vazia — o fallback não pode
    inventar task pra justificar a própria existência."""
    assert _proximos_passos_do_resumo("## Objetivo\n\n  - [Conversa.](http://x)") == []
    assert _proximos_passos_do_resumo("") == []
    assert _proximos_passos_do_resumo(None) == []


def test_aceita_variantes_do_titulo_da_secao():
    for cab in ("## Next Steps", "## Ações", "## Encaminhamentos", "## PRÓXIMOS PASSOS"):
        md = f"{cab}\n\n  - [**Renato:** Fazer algo.](http://x)\n"
        itens = _proximos_passos_do_resumo(md)
        assert len(itens) == 1, f"não pegou {cab}"


class _CurFake:
    """Cursor que devolve os candidatos declarados no teste."""

    def __init__(self, candidatos):
        self._c = candidatos

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._c


def _proj(pid, nome, quantos):
    return {"id": pid, "nome": nome, "quantos": quantos}


def test_um_participante_num_projeto_nao_decide():
    """O FALSO POSITIVO medido em 20/08 antes de subir: "Renatão, Rodrigo - Des.
    SW" caía em "Originação Conselho — Canal Orbiz" só por ser o único projeto
    ativo daquele participante. Gente séria participa de mais de uma coisa."""
    from integrations.fathom import _projeto_dos_participantes
    cur = _CurFake([_proj(59, "Originação Conselho — Canal Orbiz", 1)])
    pid, motivo = _projeto_dos_participantes(cur, [{"id": 1}], titulo="Renatão, Rodrigo - Des. SW")
    assert pid is None, f"escreveria no projeto errado ({motivo})"


def test_titulo_que_nomeia_o_projeto_decide():
    from integrations.fathom import _projeto_dos_participantes
    cur = _CurFake([_proj(26, "Alba Consultoria", 1), _proj(59, "Canal Orbiz", 1)])
    pid, motivo = _projeto_dos_participantes(
        cur, [{"id": 1}, {"id": 2}], titulo="Reunião de Conselho (Alba) - Online")
    assert pid == 26
    assert motivo == "titulo_nomeia_o_projeto"


def test_dois_participantes_no_mesmo_projeto_decide():
    from integrations.fathom import _projeto_dos_participantes
    cur = _CurFake([_proj(26, "Alba Consultoria", 3), _proj(59, "Canal Orbiz", 1)])
    pid, _ = _projeto_dos_participantes(cur, [{"id": 1}, {"id": 2}, {"id": 3}], titulo="Sync semanal")
    assert pid == 26


def test_empate_nao_vira_palpite():
    """Nota no projeto errado é pior que nota nenhuma: passa a contar como
    registro daquela frente, e escrever no alvo errado não dá erro nenhum."""
    from integrations.fathom import _projeto_dos_participantes
    cur = _CurFake([_proj(26, "Alba Consultoria", 2), _proj(59, "Canal Orbiz", 2)])
    pid, motivo = _projeto_dos_participantes(cur, [{"id": 1}, {"id": 2}], titulo="Sync")
    assert pid is None
    assert "ambiguo" in motivo


def test_titulo_que_nomeia_dois_candidatos_nao_decide():
    from integrations.fathom import _projeto_dos_participantes
    cur = _CurFake([_proj(26, "Alba Consultoria", 1), _proj(59, "Orbiz Turnaround", 1)])
    pid, _ = _projeto_dos_participantes(cur, [{"id": 1}], titulo="Alba x Orbiz — parceria")
    assert pid is None


def test_sem_participante_casado_nao_infere():
    from integrations.fathom import _projeto_dos_participantes
    pid, motivo = _projeto_dos_participantes(_CurFake([]), [], titulo="Qualquer coisa")
    assert pid is None
    assert motivo == "sem_participante_casado"


def test_bullet_sem_link_tambem_conta():
    """Nem todo resumo vem com o link do timestamp."""
    itens = _proximos_passos_do_resumo("## Próximos Passos\n\n- **Renato:** Ligar pro Israel.\n")
    assert len(itens) == 1
    assert itens[0]["description"] == "Renato: Ligar pro Israel."
    assert itens[0]["recording_playback_url"] == ""
