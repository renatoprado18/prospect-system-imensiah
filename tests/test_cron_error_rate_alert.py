"""
Guarda do alerta de TAXA DE ERRO do monitor-cron-health (31/07/2026).

O QUE ESTE ARQUIVO PROTEGE. Ate 31/07 o `monitor-cron-health` so perguntava
"o job rodou?". Nunca "deu certo?". O `track_cron_run` ja marcava
`status='error'` quando o handler devolvia 200 com erro embutido, e o
`capability_registry` ate agregava a taxa — mas nada ALERTAVA. Resultado
medido: `wa-catchup` acumulou 36 execucoes com erro em 7 dias (10,7%) sem
jamais gerar um aviso, enquanto o monitor o declarava saudavel.

O BUG QUE QUASE ENTROU JUNTO, e que o segundo teste existe pra impedir: o
notificador monta a mensagem de WhatsApp com `if never_fired / else`, e o
ramo `else` assume as chaves de `gap_exceeds_2x` (`gap_min`,
`expected_interval_min`). Um `reason` novo sem ramo proprio cai no else e
estoura `KeyError` DENTRO do notificador — ou seja, o alerta seria detectado
corretamente e morreria calado na hora de avisar. E a pior forma de falha
possivel pra um monitor: ele acha o problema e nao consegue contar.

Por isso o teste nao verifica so o alerta que existe hoje — ele varre TODOS os
`reason` que o endpoint sabe emitir e exige que cada um tenha as chaves que o
notificador vai ler.
"""
import ast
import os
import re

MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "app", "main.py")


def _fonte() -> str:
    with open(MAIN_PY, encoding="utf-8") as fh:
        return fh.read()


class TestLimiarDeErro:
    def test_limiar_e_piso_existem_e_sao_sensatos(self):
        """20% com piso de 5 execucoes. Se alguem afrouxar pra 0 ou apertar pra
        1%, o monitor vira fonte de ruido — que foi o motivo do kill switch de
        notificacao em 13/06 (127 msgs/14d, 1,6% de acao)."""
        src = _fonte()
        taxa = re.search(r"_CRON_ERROR_RATE_ALERT\s*=\s*([\d.]+)", src)
        piso = re.search(r"_CRON_ERROR_MIN_RUNS\s*=\s*(\d+)", src)
        assert taxa, "_CRON_ERROR_RATE_ALERT sumiu de main.py"
        assert piso, "_CRON_ERROR_MIN_RUNS sumiu de main.py"
        assert 0.05 <= float(taxa.group(1)) <= 0.5, (
            f"limiar de erro fora da faixa sensata: {taxa.group(1)}")
        assert int(piso.group(1)) >= 3, (
            "piso de execucoes < 3 alarma por 1 erro em job raro")

    def test_a_query_de_taxa_usa_janela_curta(self):
        """24h de proposito: estes erros vem em BURST. Medido em 31/07 — o
        wa-catchup tinha 10,7% em 7 dias e 0% nas 24h anteriores. Uma media
        de 7 dias diluiria o surto ate ele sumir."""
        src = _fonte()
        trecho = src[src.find("error_rates = {") - 1400: src.find("error_rates = {")]
        assert "INTERVAL '24 hours'" in trecho, (
            "a taxa de erro deixou de usar janela de 24h")
        assert "status IN ('error','timeout')" in trecho, (
            "a taxa parou de contar timeout como falha")


class TestNotificadorCobreTodoReason:
    """A CLASSE. Nenhum `reason` pode cair num ramo que le chave que ele nao tem."""

    def _reasons_emitidos(self) -> set:
        """Literais de `reason` emitidos DENTRO do handler do monitor.

        Escopo restrito de proposito: `main.py` tem ~16k linhas e a palavra
        `reason` aparece em dominios sem relacao nenhuma (proposta de acao usa
        `reason` com texto livre em portugues). Varrer o arquivo inteiro fazia
        este teste reprovar por causa de um dict de outra feature.
        """
        arvore = ast.parse(_fonte(), filename=MAIN_PY)
        alvo = None
        for node in ast.walk(arvore):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    "monitor_cron_health" in node.name:
                alvo = node
                break
        assert alvo is not None, "handler do monitor-cron-health nao encontrado"
        achados = set()
        for node in ast.walk(alvo):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "reason"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    achados.add(v.value)
        return achados

    def test_todo_reason_tem_ramo_proprio_no_notificador(self):
        reasons = self._reasons_emitidos()
        assert {"never_fired", "gap_exceeds_2x", "error_rate"} <= reasons, (
            f"reasons conhecidos sumiram do endpoint: {reasons}")

        src = _fonte()
        ini = src.find('lines = ["[INTEL] Cron health alert:"]')
        assert ini > 0, "bloco de montagem da mensagem nao encontrado"
        bloco = src[ini:ini + 2200]

        # `gap_exceeds_2x` e o ramo `else` (default historico) — os demais
        # precisam de comparacao explicita antes dele.
        for r in sorted(reasons - {"gap_exceeds_2x"}):
            assert f'"{r}"' in bloco or f"'{r}'" in bloco, (
                f"reason '{r}' e emitido mas o notificador nao tem ramo pra ele: "
                f"cairia no else e leria chaves de gap_exceeds_2x (KeyError)")

    def test_error_rate_vem_antes_do_else(self):
        """Ordem importa: `elif error_rate` depois do `else` seria inalcancavel."""
        src = _fonte()
        ini = src.find('lines = ["[INTEL] Cron health alert:"]')
        bloco = src[ini:ini + 2200]
        pos_er = bloco.find('"error_rate"')
        pos_else = bloco.find("\n            else:")
        assert pos_er > 0 and pos_else > 0, "estrutura do notificador mudou"
        assert pos_er < pos_else, (
            "o ramo de error_rate ficou DEPOIS do else — inalcancavel")


class TestRetencaoNaoCegaOsDetectores:
    """A poda de telemetria do mesmo dia nao pode apagar o que estes alertas leem."""

    def test_cron_runs_nunca_perde_linha_na_poda(self):
        ret = os.path.join(os.path.dirname(MAIN_PY), "services", "telemetry_retention.py")
        with open(ret, encoding="utf-8") as fh:
            src = fh.read()
        assert "DELETE FROM cron_runs" not in src, (
            "a poda passou a apagar linhas de cron_runs — 14 jobs tinham a ultima "
            "execucao com sucesso alem de 30 dias e seriam declarados 'nunca disparou'")

    def test_heartbeat_preserva_o_ultimo_de_cada_job(self):
        ret = os.path.join(os.path.dirname(MAIN_PY), "services", "telemetry_retention.py")
        with open(ret, encoding="utf-8") as fh:
            src = fh.read()
        assert "DISTINCT ON (job_id)" in src, (
            "a poda de cron_heartbeats parou de preservar o ultimo batimento de "
            "cada job — job silencioso sumiria do MAX(fired_at), que nao tem janela")
