"""O aviso de atraso que repetiu 42 vezes em 9 dias sem produzir uma acao.

Contexto (22/11/26). A entrega do WhatsApp no INTEL ficou atrasada em regime
permanente, medida por dia de gravacao: 16/11 0,0 min (o conserto de 15/11
funcionou por UM dia), 17/11 134 min, 18/11 20h, 19/11 24h, 20/11 24h, 21/11
20h, 22/11 21h. O heartbeat detectou corretamente — 867 runs vermelhos — e
avisou 4 a 6 vezes por dia, sempre com o mesmo texto: "as mensagens estao
presas na fila, vao aparecer depois, nao concluir que alguem nao respondeu
enquanto isto durar".

No primeiro dia aquele texto descrevia um incidente. No quinto, descrevia o
regime normal, e seguia escrito como transitorio. Tres defeitos somados:

① a escala de faixas SATURAVA em 12h, e o atraso real passou dela — logo a
   degradacao de 4h para 24h contou uma vez e uma piora para 48h nao contaria
   nenhuma;
② o aviso nao dizia HA QUANTO TEMPO durava, o unico eixo em que a noticia
   muda quando o atraso se estabiliza no alto;
③ `notificado: true` era fixo no payload: 871 runs diziam ter notificado
   contra 42 mensagens realmente enviadas.
"""
from main import _faixa_atraso


class TestFaixaAlcancaOFenomeno:
    def test_patamares_de_baixo_preservados(self):
        """A degradacao fina continua contando: era o conserto de 14/11."""
        assert _faixa_atraso(1) == "15min"
        assert _faixa_atraso(59) == "15min"
        assert _faixa_atraso(60) == "1h"
        assert _faixa_atraso(239) == "1h"
        assert _faixa_atraso(240) == "4h"
        assert _faixa_atraso(719) == "4h"

    def test_doze_horas_deixa_de_ser_o_teto(self):
        assert _faixa_atraso(720) == "12h"
        assert _faixa_atraso(1439) == "12h"

    def test_o_atraso_observado_em_novembro_NAO_muda_de_faixa(self):
        """Honestidade sobre o alcance do conserto: o nome da faixa e o PISO
        dela, entao os 20h-24h medidos de 18 a 22/11 seguem em "12h", a mesma
        faixa do teto antigo. Alargar a escala nao altera o aviso de hoje — quem
        altera a cadencia de hoje e a duracao na chave de dedup
        (TestDedupPorDuracao). O que a escala passa a cobrir e a PIORA daqui pra
        cima, que antes era invisivel."""
        assert _faixa_atraso(1223) == "12h"
        assert _faixa_atraso(1350) == "12h"
        assert _faixa_atraso(1437) == "12h"

    def test_passar_de_24h_volta_a_avisar(self):
        """O ponto da faixa e que PIORA gere aviso novo. Com teto em 12h,
        qualquer coisa acima de 12h era a mesma chave: dobrar o atraso nao
        mudava nada e o Renato nao saberia."""
        assert _faixa_atraso(1400) != _faixa_atraso(1500)
        assert _faixa_atraso(1500) == "24h"
        assert _faixa_atraso(2879) == "24h"
        assert _faixa_atraso(2880) == "48h+"
        assert _faixa_atraso(5000) == "48h+"

    def test_cada_degrau_muda_a_chave(self):
        faixas = [_faixa_atraso(m) for m in (10, 100, 300, 800, 2000, 4000)]
        assert len(set(faixas)) == len(faixas)


class TestTextoDoAvisoEscala:
    """O corpo do aviso muda quando o problema deixa de ser transitorio.

    Reproduz o ramo de `cron_wa_ingest_heartbeat` sem subir o app: o que se
    trava aqui e a REGRA (>= 2 dias muda a mensagem), nao a string.
    """

    @staticmethod
    def _corpo(dias):
        return "cronico" if (dias or 0) >= 2 else "transitorio"

    def test_primeiro_dia_segue_sendo_tratado_como_incidente(self):
        assert self._corpo(0) == "transitorio"
        assert self._corpo(1) == "transitorio"

    def test_a_partir_do_segundo_dia_o_aviso_muda(self):
        assert self._corpo(2) == "cronico"
        assert self._corpo(5) == "cronico"

    def test_ausencia_do_campo_nao_quebra(self):
        """Veredito antigo (sem `dias_seguidos`) cai no texto de sempre, nunca
        em TypeError: o heartbeat roda a cada 10 min e nao pode morrer por causa
        de uma chave nova."""
        assert self._corpo(None) == "transitorio"


class TestDedupPorDuracao:
    """A chave de dedup passa a ter faixa + duracao.

    Com faixa parada em "12h" por 5 dias, a cadencia observada foi de 4 a 6
    avisos/dia, todos iguais. Com a duracao na chave, a noticia sai quando ela
    muda — uma vez por dia de problema.
    """

    @staticmethod
    def _chave(atraso_min, dias):
        return f"wa_ingest:atraso:{_faixa_atraso(atraso_min)}:d{dias or 0}"

    def test_mesmo_dia_e_mesma_faixa_nao_repete(self):
        assert self._chave(1400, 5) == self._chave(1420, 5)

    def test_virar_o_dia_avisa_de_novo(self):
        assert self._chave(1400, 5) != self._chave(1400, 6)

    def test_piorar_a_faixa_avisa_de_novo_no_mesmo_dia(self):
        assert self._chave(1400, 5) != self._chave(3000, 5)

    def test_a_chave_dos_5_dias_de_novembro_nao_colide_com_a_do_primeiro_dia(self):
        assert self._chave(1253, 5) != self._chave(1253, 1)
