"""Testes do heartbeat da ingestao WA (incidente de 03-04/08/2026).

O webhook ficou 3h calado e nada avisou -- 93 mensagens fora, entre elas o
endereco de uma reuniao presencial. O heartbeat existe pra isso nao repetir.

O risco do remedio e ser pior que a doenca: "alertar se nao chega mensagem ha
20min" grita toda madrugada, e alerta que grita a toa e desligado -- ai a
proxima queda passa de novo. Por isso o veredito e COMPARATIVO. Estes testes
cobrem os dois erros possiveis:

  (a) FALSO NEGATIVO -- cano parado e ninguem avisa (o incidente);
  (b) FALSO POSITIVO -- silencio legitimo virando alarme (o que mata o alerta).

Rodar: .venv/bin/python -m pytest tests/test_wa_ingest_heartbeat.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import wa_ingest_heartbeat as hb  # noqa: E402


@pytest.fixture
def relogio(monkeypatch):
    agora = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(hb, "now_utc", lambda: agora)
    return agora


def _arma(monkeypatch, ultimo_banco, epoch_evolution, estado="open"):
    """Arma o cenario. `estado` default "open" NAO e cosmetico.

    Desde 15/10/26 `checar_ingestao` consulta `connectionState`. Sem este mock,
    todo teste acima faria chamada HTTP real -- e o veredito passaria a depender
    do estado da instancia do Renato na hora de rodar a suite. Com a instancia
    caida (como em 15/10) a suite ficaria vermelha sem nenhuma regressao:
    a classe de [[feedback_teste_com_ambiente_declarado_nao_aplicado]].
    """
    monkeypatch.setattr(hb, "_ultimo_evento_no_banco", lambda: ultimo_banco)
    monkeypatch.setattr(hb, "_ultima_mensagem_na_evolution", lambda: epoch_evolution)
    monkeypatch.setattr(hb, "_estado_da_conexao", lambda: estado)


# ------------------------------------------------------- o incidente de 03/08 --

def test_alerta_quando_evolution_tem_o_que_o_intel_nao_tem(relogio, monkeypatch):
    """O caso real: banco parado as 17:30, Evolution recebendo ate agora."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int((relogio - timedelta(minutes=2)).timestamp()))

    v = hb.checar_ingestao()

    assert v["alerta"] is True
    assert v["status"] == "parado"
    assert v["atraso_min"] > 170


def test_janela_perdida_cobre_o_buraco(relogio, monkeypatch):
    """O catchup tem que pedir horas suficientes pra alcancar o inicio do gap."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int(relogio.timestamp()))

    janela = hb.janela_perdida(hb.checar_ingestao())

    assert janela["horas"] >= 3, "pediria menos horas que o tamanho do buraco"


# ------------------------------------------ o alerta que se mataria sozinho --

def test_madrugada_silenciosa_nao_alerta(relogio, monkeypatch):
    """ZERO evento por horas e NORMAL de madrugada -- 06h UTC teve zero no dia
    medido e estava tudo certo. Se isto alertar, o Renato desliga o aviso e a
    proxima queda de verdade passa batido."""
    ultimo = relogio - timedelta(hours=4)
    # A Evolution tambem nao tem nada novo: o mundo esta quieto, nao o cano.
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()))

    v = hb.checar_ingestao()

    assert v["alerta"] is False
    assert v["status"] == "mundo_quieto"


def test_mensagem_em_transito_nao_alerta(relogio, monkeypatch):
    """A mensagem que chegou na Evolution neste segundo ainda nao teve tempo de
    virar linha aqui. Sem tolerancia, todo pico viraria alerta."""
    ultimo = relogio - timedelta(minutes=25)
    _arma(monkeypatch, ultimo, int((ultimo + timedelta(seconds=30)).timestamp()))

    assert hb.checar_ingestao()["alerta"] is False


def test_fluxo_normal_nem_pergunta_pra_evolution(relogio, monkeypatch):
    """Custo: no caminho feliz nao ha chamada externa nenhuma."""
    monkeypatch.setattr(hb, "_ultimo_evento_no_banco", lambda: relogio - timedelta(minutes=3))

    def _explode():
        raise AssertionError("chamou a Evolution com o banco em dia")

    monkeypatch.setattr(hb, "_ultima_mensagem_na_evolution", _explode)

    assert hb.checar_ingestao()["status"] == "ok"


def test_evolution_muda_nao_vira_alerta(relogio, monkeypatch):
    """Se a Evolution nao responde, nao sabemos -- e nao-saber nao e alerta.
    Senao a instabilidade DELA viraria alarme de ingestao parada, e o aviso
    perderia o sentido."""
    _arma(monkeypatch, relogio - timedelta(hours=2), 0)

    v = hb.checar_ingestao()

    assert v["alerta"] is False
    assert v["status"] == "indeterminado"


def test_banco_vazio_nao_alerta(relogio, monkeypatch):
    _arma(monkeypatch, None, 0)

    assert hb.checar_ingestao()["alerta"] is False


# ------------------------------ a queda de 14-15/10/2026: instancia caida --
#
# O Renato re-registrou o numero no WhatsApp comum depois de um bug no Business.
# Isso invalida todo dispositivo vinculado: `rap-whatsapp` foi a `close`/403 as
# 18h59. O heartbeat rodou 96x e devolveu `mundo_quieto` em todas, porque o
# comparativo so enxerga webhook caido com a Evolution VIVA -- caindo a
# instancia, os dois lados silenciam juntos e nao existe gap pra achar.


def test_instancia_caida_alerta_com_os_dois_lados_quietos(relogio, monkeypatch):
    """O caso real, e o teste que fica VERMELHO sem o fix.

    Cenario identico ao `test_madrugada_silenciosa_nao_alerta` -- banco quieto,
    Evolution sem nada novo -- mudando UMA coisa: a instancia esta `close`. O
    veredito tem de inverter. E a unica diferenca entre "gente dormindo" e "cano
    arrancado", e e por nao olhar pra ela que se perderam 15,8h.
    """
    ultimo = relogio - timedelta(hours=15, minutes=48)
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()), estado="close")

    v = hb.checar_ingestao()

    assert v["alerta"] is True
    assert v["status"] == "desconectado"
    assert v["estado_instancia"] == "close"
    assert v["exige_qr"] is True, "close exige QR novo -- nao volta sozinho"


def test_veredito_desconectado_nao_promete_atraso_min(relogio, monkeypatch):
    """Instancia caida nao tem com o que comparar: a fonte parou junto.

    Quem formata o aviso NAO pode supor `atraso_min`. Em 15/10 o texto do
    `notify` lia essa chave direto, e um KeyError ali cai no `except` que so
    loga warning -- o alarme falharia calado justamente no caso que ele passou
    a detectar. O teste de fonte abaixo guarda o ramo.
    """
    ultimo = relogio - timedelta(hours=4)
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()), estado="close")

    v = hb.checar_ingestao()

    assert "atraso_min" not in v
    assert "ultima_msg_evolution" not in v


def test_connecting_curto_nao_alerta(relogio, monkeypatch):
    """`connecting` e transitorio -- religamento normal passa por ele.

    Gritar aqui seria a fabrica de falso positivo que este modulo existe pra
    evitar. So `close`, que e terminal, dispensa carencia.
    """
    ultimo = relogio - timedelta(minutes=25)
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()), estado="connecting")

    v = hb.checar_ingestao()

    assert v["alerta"] is False
    assert v["status"] == "mundo_quieto"


def test_connecting_prolongado_alerta(relogio, monkeypatch):
    """Religamento que nao termina deixou de ser transitorio."""
    ultimo = relogio - timedelta(minutes=hb.CONNECTING_MIN + 5)
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()), estado="connecting")

    v = hb.checar_ingestao()

    assert v["alerta"] is True
    assert v["status"] == "desconectado"
    assert v["exige_qr"] is False, "connecting ainda pode voltar sozinho"


def test_estado_desconhecido_cai_no_comparativo(relogio, monkeypatch):
    """Nao-saber o estado nao pode virar alerta nem apagar o alarme antigo:
    tem de cair exatamente no comportamento comparativo de antes."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int((relogio - timedelta(minutes=2)).timestamp()),
          estado="")

    v = hb.checar_ingestao()

    assert v["alerta"] is True
    assert v["status"] == "parado", "sem estado, o veredito antigo tem de valer"


def test_instancia_de_pe_nao_encurta_o_comparativo(relogio, monkeypatch):
    """Com a instancia `open`, nada muda: o gap real continua virando alerta."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int((relogio - timedelta(minutes=2)).timestamp()),
          estado="open")

    assert hb.checar_ingestao()["status"] == "parado"


# ---------------- o defeito que INVERTEU o sinal: medir a instancia errada --


def test_ultimo_evento_filtra_por_instancia(monkeypatch):
    """`MAX(received_at)` sem `WHERE instance` deixa uma instancia viva atestar
    o frescor de uma morta -- e foi pior que encurtar.

    Em 15/10 o `intel-bot-v2` (outro numero, o do bot) gravou evento as 07h00 e
    o silencio de 15,8h apareceu como 3,8h. Pior: esse `ultimo` contaminado
    ficou MAIS NOVO que a ultima mensagem conhecida pela Evolution (14/10
    19h00), entao `atraso_seg` deu negativo e nunca passaria da tolerancia. O
    `mundo_quieto` nao foi leitura infeliz: era o unico resultado possivel.
    """
    capturado = {}

    class _Cur:
        def execute(self, sql, params=None):
            capturado["sql"] = sql
            capturado["params"] = params

        def fetchone(self):
            return {"ultimo": None}

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(hb, "get_db", lambda: _Conn())

    hb._ultimo_evento_no_banco()

    sql = " ".join(capturado["sql"].split()).lower()
    assert "where instance" in sql, (
        "sem filtro por instancia, uma instancia viva zera o alarme da morta"
    )
    assert capturado["params"] == (hb.INSTANCIA,)


def test_ramo_desconectado_existe_antes_de_ler_atraso_min():
    """Guarda do formatador em main.py (ver o teste do payload acima).

    Verifica a ORDEM: o ramo `desconectado` tem de aparecer antes do texto que
    le `atraso_min`, senao o veredito novo estoura KeyError no notify.
    """
    caminho = os.path.join(_ROOT, "app", "main.py")
    with open(caminho, encoding="utf-8") as fh:
        fonte = fh.read()

    i = fonte.find('@app.post("/api/cron/wa-ingest-heartbeat")')
    assert i > 0
    bloco = fonte[i:i + 4500]

    pos_ramo = bloco.find("if desconectado:")
    # Procura o USO, nao a mencao: a 1a versao deste teste casou com a palavra
    # dentro do proprio comentario que explica o ramo, e reprovou codigo certo.
    pos_atraso = bloco.find("veredito['atraso_min']")
    assert pos_ramo > 0, "o formatador voltou a ter um unico texto"
    assert pos_atraso > 0, "o ramo comparativo perdeu o atraso_min do texto"
    assert pos_ramo < pos_atraso, "le atraso_min antes de ramificar -> KeyError"
    assert "not desconectado" in bloco, (
        "catchup com a instancia caida varre 200 contatos pra achar zero"
    )


# --------------------------------------------------------------- integracao --

def test_job_esta_agendado_no_worker():
    """Servico + endpoint sem agendador seria um monitor que nunca roda --
    exatamente o tipo de 'wirado mas nao vivo' que ja mordeu antes."""
    caminho = os.path.join(_ROOT, "workers", "audio-transcriber", "main.py")
    with open(caminho, encoding="utf-8") as fh:
        fonte = fh.read()

    assert "/api/cron/wa-ingest-heartbeat" in fonte


def test_endpoint_existe_e_exige_cron_auth():
    caminho = os.path.join(_ROOT, "app", "main.py")
    with open(caminho, encoding="utf-8") as fh:
        fonte = fh.read()

    i = fonte.find('@app.post("/api/cron/wa-ingest-heartbeat")')
    assert i > 0, "endpoint nao existe"
    assert "verify_cron_auth" in fonte[i:i + 2000]


# ------------------------------- diagnóstico da falha do agente CoS --------

def test_motivo_da_falha_vai_pro_payload():
    """Até 06/08 o `cos_daily_review` gravava `falhas: 3` e o PORQUÊ ia só pro
    stderr (`~/.cos-agent/run.err`), que ninguém abre. O `pdca.py` mostrava o
    número sem diagnóstico e cada investigação recomeçava do zero.

    Naquele dia foram 2 falhas de DNS do Neon e 1 timeout — e só se soube lendo
    um arquivo local por acaso. Oitava aparição do padrão do dia: o mecanismo
    registra QUE falhou, não O QUE falhou.
    """
    import pathlib

    src = pathlib.Path(_ROOT).joinpath("scripts", "cos_agent", "run.py").read_text()
    assert '"falhas_detalhe"' in src, "o motivo da falha voltou a morrer no stderr"
    assert '"erro": str(d.get("error"))' in src


def test_retry_so_pra_falha_de_rede():
    """DNS que falha é transitório e some sozinho — repetir salva a frente.
    Timeout NÃO: se estourou 420s, repetir custa mais 420s e estoura de novo.
    """
    import pathlib

    src = pathlib.Path(_ROOT).joinpath("scripts", "cos_agent", "run.py").read_text()
    i = src.index("RETRY PRA FALHA DE REDE")
    trecho = src[i:i + 1800]
    assert "translate host name" in trecho, "não reconhece o erro real que ocorreu"
    # o retorno de timeout tem que continuar saindo direto, sem 2a tentativa
    assert 'error": f"timeout > {AGENT_TIMEOUT_S}s"' in trecho
    assert "time.sleep(30)" in trecho
