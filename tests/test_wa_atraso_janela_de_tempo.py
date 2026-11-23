"""A janela do atraso de entrega é de TEMPO, não só de contagem.

Contexto (23/11/26), e o defeito apareceu no minuto seguinte a um conserto que
funcionou. A sessão criptográfica do aparelho do Renato foi recriada por QR:
`Bad MAC` caiu de 15.638/24h para 0, `Failed to decrypt` de 379 para 0, e as
mensagens voltaram a entrar em 0 min — medido em prod, com tráfego real.

E o heartbeat seguiu respondendo `atraso_entrega_min: 1350.8` e
`dias_seguidos: 6`.

A causa: a janela era `ORDER BY criado_em DESC LIMIT 30`, só contagem. Nenhuma
DM nova havia entrado ainda (domingo, 23h), então as 30 últimas continuavam
sendo as do backlog do dia anterior. O medidor que acabara de acertar a pane
passaria a mentir na direção oposta — e o aviso vai para o WhatsApp do Renato a
cada ciclo de 10 min.

"Últimas 30 gravadas" não distingue **"o que chega agora chega velho"** de **"as
últimas 30 são de um episódio encerrado"**. A janela de tempo distingue.

Parâmetros (3h, 5 mensagens) escolhidos por simulação hora a hora sobre 11 dias
de `messages`, não estimados: pegam as panes de 12/11, 14/11 e 17–22/11, ficam
verdes nos dois dias sãos (13/11 com 9 horas verdes, 16/11 com 12) e devolvem
"não medido" nas horas sem amostra.
"""
from datetime import datetime, timedelta, timezone

import pytest

from database import get_db
from services import wa_ingest_heartbeat as hb

UTC = timezone.utc


@pytest.fixture
def conversa():
    """Conversa de WhatsApp isolada, apagada no teardown junto das mensagens."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO conversations (canal, criado_em)
                       VALUES ('whatsapp', now()) RETURNING id""")
        cid = cur.fetchone()["id"]
        conn.commit()
    yield cid
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE conversation_id = %s", (cid,))
        cur.execute("DELETE FROM conversations WHERE id = %s", (cid,))
        conn.commit()


def _msg(cid, gravada_ha_min, atraso_min):
    """Insere uma DM recebida: gravada há N min, com M min de atraso de entrega."""
    agora = datetime.now(UTC)
    criado = agora - timedelta(minutes=gravada_ha_min)
    enviado = criado - timedelta(minutes=atraso_min)
    with get_db() as conn:
        cur = conn.cursor()
        # `canal` vive em `conversations`, não em `messages` — a query do
        # detector filtra por `c.canal ILIKE '%whats%'` via JOIN.
        cur.execute("""INSERT INTO messages
                         (conversation_id, direcao, conteudo, enviado_em, criado_em)
                       VALUES (%s, 'incoming', 'teste', %s, %s)""",
                    (cid, enviado, criado))
        conn.commit()


class TestAmostraInsuficienteNaoViraVeredito:
    def test_janela_vazia_devolve_none(self, conversa):
        assert hb._atraso_de_entrega_min() is None

    def test_abaixo_do_minimo_devolve_none(self, conversa):
        """Mediana de 1 ou 2 mensagens é ruído com cara de medição."""
        for _ in range(hb.ATRASO_MIN_AMOSTRA - 1):
            _msg(conversa, gravada_ha_min=10, atraso_min=600)
        assert hb._atraso_de_entrega_min() is None

    def test_no_minimo_exato_passa_a_medir(self, conversa):
        for _ in range(hb.ATRASO_MIN_AMOSTRA):
            _msg(conversa, gravada_ha_min=10, atraso_min=600)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v == pytest.approx(600, abs=2)


class TestJanelaDeTempo:
    def test_o_backlog_VELHO_nao_prende_mais_o_numero(self, conversa):
        """O defeito exato: 30 mensagens atrasadas gravadas ontem, nada novo.
        Antes, a mediana ficava em 1350 min; agora sai da janela."""
        for _ in range(30):
            _msg(conversa, gravada_ha_min=26 * 60, atraso_min=1350)
        assert hb._atraso_de_entrega_min() is None, (
            "episódio encerrado não pode seguir produzindo veredito"
        )

    def test_mensagens_novas_mandam_no_veredito(self, conversa):
        """Com o canal restaurado, o que chega agora é o que vale — mesmo que o
        backlog velho ainda esteja na tabela."""
        for _ in range(30):
            _msg(conversa, gravada_ha_min=26 * 60, atraso_min=1350)
        for _ in range(10):
            _msg(conversa, gravada_ha_min=5, atraso_min=0.03)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v < hb.ATRASO_ENTREGA_MIN, (
            f"deveria ler a entrega em tempo real, leu {v}"
        )

    def test_borda_da_janela_fica_de_fora(self, conversa):
        dentro = hb.ATRASO_JANELA_H * 60 - 10
        fora = hb.ATRASO_JANELA_H * 60 + 10
        for _ in range(hb.ATRASO_MIN_AMOSTRA):
            _msg(conversa, gravada_ha_min=fora, atraso_min=900)
        assert hb._atraso_de_entrega_min() is None
        for _ in range(hb.ATRASO_MIN_AMOSTRA):
            _msg(conversa, gravada_ha_min=dentro, atraso_min=900)
        assert hb._atraso_de_entrega_min() == pytest.approx(900, abs=2)


class TestSegueDetectandoAPaneReal:
    """O conserto não pode custar a detecção — era este detector, e só ele, que
    viu a pane de 14/11.
    """

    def test_atraso_em_curso_continua_gritando(self, conversa):
        """Lote entrando AGORA com mensagens de horas atrás: é a assinatura da
        pane (entrega atrasada, não parada), e tem de alertar."""
        for _ in range(12):
            _msg(conversa, gravada_ha_min=20, atraso_min=20 * 60)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v > hb.ATRASO_ENTREGA_MIN

    def test_entrega_sa_fica_verde(self, conversa):
        for _ in range(12):
            _msg(conversa, gravada_ha_min=30, atraso_min=0.02)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v <= hb.ATRASO_ENTREGA_MIN

    def test_metade_atrasada_basta_pra_gritar(self, conversa):
        """A mediana é o critério: exige que METADE da janela esteja atrasada,
        o que impede que um único deploy lento acenda o alarme."""
        for _ in range(6):
            _msg(conversa, gravada_ha_min=30, atraso_min=0.02)
        for _ in range(7):
            _msg(conversa, gravada_ha_min=30, atraso_min=20 * 60)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v > hb.ATRASO_ENTREGA_MIN

    def test_um_outlier_sozinho_nao_acende(self, conversa):
        for _ in range(12):
            _msg(conversa, gravada_ha_min=30, atraso_min=0.02)
        _msg(conversa, gravada_ha_min=30, atraso_min=20 * 60)
        v = hb._atraso_de_entrega_min()
        assert v is not None and v <= hb.ATRASO_ENTREGA_MIN


class TestVereditoCarregaODesconhecimento:
    def test_o_detalhe_diz_quanto_faltou(self, monkeypatch):
        """Quem lê precisa saber POR QUE não mediu — "janela vazia" não
        distinguia ausência de tráfego de medidor quebrado.

        O veredito é montado sem tocar a rede: `checar_ingestao` consultaria a
        Evolution e, nos testes, cairia em "Evolution nao respondeu" antes de
        chegar ao ramo que interessa.
        """
        agora = datetime.now(UTC)
        monkeypatch.setattr(hb, "_ultimo_evento_no_banco", lambda: agora - timedelta(minutes=1))
        monkeypatch.setattr(hb, "_atraso_de_entrega_min", lambda: None)

        v = hb.checar_ingestao()

        assert v["alerta"] is False, "não-saber não grita"
        assert v["atraso_entrega_min"] is None
        assert "NAO MEDIDO" in v["detalhe"]
        assert str(hb.ATRASO_MIN_AMOSTRA) in v["detalhe"]
        assert str(hb.ATRASO_JANELA_H) in v["detalhe"]
