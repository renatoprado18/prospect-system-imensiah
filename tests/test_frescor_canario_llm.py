"""O SUBSISTEMA MAIS CARO CAIU E A ABERTURA PASSOU VERDE (05/10/2026).

Em 04/10/26 às ~08h UTC a conta Anthropic ficou SEM SALDO. O sistema passou ~30h
com TODA função LLM parada — `task-reconciler` (41% dos fechamentos de tarefa),
triagem, visão, signal router, briefing. A abertura `/dev` daquela manhã e a de
05/10 passaram verdes em `verifica_modelo`, `verifica_boards` e `verifica_frescor`:
nenhum dos três olhava LLM. A queda foi descoberta por acidente, quando uma chamada
de validação voltou 400.

O `anthropic-canary` FEZ o trabalho dele — detectou e alertou por WhatsApp às 08:33
(mensagem #36194). O furo não é de detecção, é de SUPERFÍCIE: o alerta sai UMA vez
(depois o canário só repete `already_alerted` até recuperar) e esse único aviso caiu
no meio de uma viagem. A abertura é onde ele reaparece
([[feedback_superficie_nova_mata_o_aviso]]).

DOIS DETECTORES, E É DE PROPÓSITO. O flag (`CANARIOS`) é direto e imediato, mas
depende do canário estar de pé e do modo de falha ser um que ele modele — chave
rotacionada, rate limit ou worker fora não levantam flag. O frescor de
`tonia_llm_usage` (`FONTES`) é indireto e mais lento, e por não depender do canário
é o que sobra quando o próprio canário quebra
([[feedback_medidor_que_nao_mede_a_si_mesmo]]).

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_frescor_canario_llm.py -q
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

_SCRIPT = _ROOT / "scripts" / "verifica_frescor.py"


@pytest.fixture(scope="module")
def vf():
    spec = importlib.util.spec_from_file_location("verifica_frescor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fonte_llm(vf):
    achadas = [f for f in vf.FONTES if "tonia_llm_usage" in f["sql"]]
    assert len(achadas) == 1, "esperava exatamente 1 fonte lendo tonia_llm_usage"
    return achadas[0]


# ===========================================================================
# 1. A decisão que faz o detector funcionar: régua CORRIDA, não útil
# ===========================================================================

def test_llm_e_medido_em_horas_corridas_nao_uteis(vf):
    """Se alguém "normalizar" esta fonte para `uteis=True`, o detector fica cego
    exatamente no caso que o criou — ver o teste seguinte, que mede o preço."""
    assert _fonte_llm(vf)["uteis"] is False, (
        "LLM tem de ser medido em horas CORRIDAS: horas úteis existem porque "
        "conversa HUMANA pausa no fim de semana, e consumo de LLM é de MÁQUINA "
        "— o cron roda 24/7 e o reconciler das 15h UTC é perdido no sábado igual."
    )


def test_a_regua_de_dia_util_nao_enxergaria_a_queda_de_04_10(vf):
    """O controle positivo da decisão acima, com as datas reais do incidente.

    Última chamada: sábado 04/10 05:35 UTC. Medição: domingo 05/10 ~14h UTC.
    Em horas corridas isso é ~32h e estoura o limiar de 12h. Em horas úteis é ~3h
    — abaixo até do intervalo NORMAL máximo observado (4,25h em 30 dias) —, e o
    alarme só acordaria na segunda de manhã.
    """
    ultima = datetime(2026, 10, 4, 5, 35, tzinfo=timezone.utc)   # sábado
    agora = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)    # domingo

    corridas = (agora - ultima).total_seconds() / 3600
    uteis = vf.horas_uteis(ultima, agora)
    limiar = _fonte_llm(vf)["limiar"]

    assert corridas > limiar, "a régua corrida tem de pegar a queda"
    assert uteis < limiar, (
        f"em horas úteis a mesma queda dá {uteis:.1f}h e passaria despercebida — "
        "é por isso que esta fonte não usa a régua de dia útil"
    )


def test_limiar_fica_acima_do_pior_intervalo_real_e_abaixo_de_um_dia(vf):
    """12h foi medido, não estimado: em 60 dias e 17.180 intervalos o p99 é 1,58h
    e o MÁXIMO 7,17h (a cauda é a janela morta 00h–05h UTC entre crons).

    Os dois lados importam. Abaixo de ~8h o alarme dispara em noite normal e vira
    ruído que se aprende a ignorar; acima de 24h ele deixa de ser achado da
    abertura do dia, que é a função dele.
    """
    limiar = _fonte_llm(vf)["limiar"]
    assert limiar > 7.17, "abaixo do máximo real observado = alarme em noite normal"
    assert limiar <= 24, "acima de um dia deixa de servir à abertura diária"


# ===========================================================================
# 2. O canário de estado — e a prova de que ele não acende sozinho
# ===========================================================================

def test_canario_da_anthropic_esta_declarado(vf):
    c = [c for c in vf.CANARIOS if "anthropic_credit_down" in c["sql"]]
    assert len(c) == 1
    assert "credit_canary" in c[0]["sql"], (
        "sem o filtro por tipo, qualquer system_memories com esse título acende o canário"
    )


class _CursorFake:
    """Responde cada SQL do script sem banco.

    `flag_aceso=False` devolve None na query do canário (estado saudável).
    `llm_parado=True` envelhece SÓ a fonte de LLM — é o que separa queda real
    (flag + silêncio) de flag obsoleto (flag + chamada recente).
    """

    def __init__(self, agora, flag_aceso, llm_parado):
        self._agora, self._flag, self._llm_parado = agora, flag_aceso, llm_parado
        self._ultimo = None

    def execute(self, sql, *a):
        if "credit_canary" in sql:
            self._ultimo = {"max": datetime(2026, 10, 4, 8, 33) if self._flag else None}
        elif "tonia_llm_usage" in sql:
            quando = (datetime(2026, 10, 4, 5, 35) if self._llm_parado
                      else self._agora.replace(tzinfo=None))
            self._ultimo = {"max": quando}
        else:
            # as outras fontes voltam frescas: isolam o LLM como única variável
            self._ultimo = {"max": self._agora.replace(tzinfo=None)}

    def fetchone(self):
        return self._ultimo


class _ConnFake:
    def __init__(self, agora, flag_aceso, llm_parado):
        self._c = _CursorFake(agora, flag_aceso, llm_parado)

    def cursor(self):
        return self._c

    def rollback(self):
        pass

    def close(self):
        pass


def _roda_main(vf, monkeypatch, capsys, *, flag_aceso, llm_parado=False):
    """Executa o `main()` DE VERDADE — é o código que a abertura roda."""
    agora = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("DB_TARGET", "prod")
    monkeypatch.setattr(vf, "get_connection",
                        lambda: _ConnFake(agora, flag_aceso, llm_parado))
    monkeypatch.setattr(vf, "now_utc", lambda: agora)
    monkeypatch.setattr(sys, "argv", ["verifica_frescor.py", "--quiet"])
    code = vf.main()
    return code, capsys.readouterr().out


def test_queda_real_flag_mais_silencio_diz_SEM_SALDO(vf, monkeypatch, capsys):
    """O cenário de 04/10: flag aceso E nenhuma chamada de LLM. Aqui o recado é
    de queda, e é o que tem de chegar ao Renato."""
    code, saida = _roda_main(vf, monkeypatch, capsys, flag_aceso=True, llm_parado=True)
    assert code == 1, "canário aceso tem de reprovar a abertura, não só imprimir"
    assert "CANÁRIO ACESO" in saida and "OBSOLETO" not in saida
    assert "SEM SALDO" in saida
    assert "04/10" in saida, "tem de dizer DESDE QUANDO, senão não se sabe se é novo"
    assert "Chamadas LLM" in saida, "a fonte parada tem de aparecer junto"


def test_flag_aceso_com_llm_fresco_diz_OBSOLETO_nao_queda(vf, monkeypatch, capsys):
    """O caso real que apareceu UMA HORA depois de armar o detector.

    O saldo foi recarregado ~15h UTC de 05/10 e as chamadas voltaram no mesmo
    minuto; mas o canário roda 1×/h (:33) e só limpa o flag quando RODA — então
    por até uma hora a abertura dizia "toda função LLM está caída" com o sistema
    funcionando. Dois detectores pagaram na direção OPOSTA à prevista: foram
    postos para cobrir a falha um do outro na QUEDA, e a primeira divergência
    apareceu na VOLTA.

    O veredito é do frescor, não do flag: dado fresco é evidência de AGORA, flag
    é evidência de quando foi escrito. Alarme que grita depois de resolvido é
    como se aprende a ignorar alarme.
    """
    # o _CursorFake devolve fontes frescas + flag presente = exatamente o caso
    code, saida = _roda_main(vf, monkeypatch, capsys, flag_aceso=True)
    assert "OBSOLETO" in saida
    assert "voltou" in saida
    assert "toda função LLM está caída" not in saida, (
        "com chamada de LLM recente, afirmar queda é afirmação confiante e errada"
    )
    assert code == 1, (
        "ainda sai 1: o flag de pé é divergência que alguém precisa confirmar, "
        "não silêncio — só o veredito muda, não o fato de haver algo a ver"
    )


def test_canario_apagado_sai_0_e_fica_quieto(vf, monkeypatch, capsys):
    """O lado negativo, que é o que separa detector de alarme preso: com o flag
    ausente e as fontes frescas, a abertura não pode imprimir nada nem reprovar.
    Sem este teste, um `acesos` sempre-verdadeiro passaria pelos outros oito."""
    code, saida = _roda_main(vf, monkeypatch, capsys, flag_aceso=False)
    assert code == 0, f"sem flag e com fontes frescas a saída tem de ser 0; veio:\n{saida}"
    assert saida.strip() == "", f"--quiet com tudo saudável tem de ser silencioso; veio:\n{saida}"


def test_canario_sai_de_FONTES_porque_o_sinal_e_invertido(vf):
    """Em FONTES a lógica é "antigo = ruim"; no canário é a PRESENÇA de um registro
    recente que é a má notícia. Misturar os dois inverteria o sinal: um flag de
    queda recém-criado seria lido como "fresco", ou seja, saudável."""
    for f in vf.FONTES:
        assert "credit_canary" not in f["sql"], (
            "canário na tabela de frescor inverte o sinal — flag novo viraria 'fresco'"
        )


# ===========================================================================
# 3. O medidor tem de medir a si mesmo
# ===========================================================================

def test_nao_medir_nunca_conta_como_saudavel(vf):
    """Tanto fonte quanto canário têm caminho de `NÃO MEDIDO` e ele conta como
    estouro. Guarda do texto porque é a propriedade, não a implementação, que não
    pode regredir ([[feedback_guarda_abstencao_vira_fabrica]])."""
    fonte = _SCRIPT.read_text()
    assert "canarios_nao_medidos" in fonte, "canário sem caminho de falha de medição"
    assert "canário que não pôde ser lido não é canário apagado" in fonte
    # o return 1 tem de considerar os canários, senão exit 0 com alarme aceso
    assert "not acesos and not canarios_nao_medidos" in fonte, (
        "o caminho de saída verde precisa exigir canário apagado, senão a abertura "
        "sai 0 com a conta Anthropic caída"
    )


def test_quiet_nao_engole_canario_aceso(vf):
    """`--quiet` é o modo que a abertura roda. Se ele filtrasse o canário junto com
    as fontes verdes, todo o conserto seria inútil no único lugar onde importa."""
    fonte = _SCRIPT.read_text()
    assert "if a.quiet and not estouros and not nao_medidas and not acesos" in fonte
