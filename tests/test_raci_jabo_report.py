"""
Testes do RACI semanal da Governança Jabô (frente 23/07).

A governanca da fazenda NAO vive no ConselhoOS — o "RACI" de facto sao as
tasks do projeto #28 no INTEL. Prova:
  - inferencia de responsavel do prefixo do titulo ([Jabô/Andressa] -> Andressa)
  - strip do prefixo pra nao duplicar com a coluna responsavel
  - bucketing (urgente/atrasada-mov/no-prazo/concluida) espelhando o ConselhoOS
  - concluidas antigas (>7d) nao poluem; on_hold/cancelled ficam de fora
  - format interactive=False nao convida resposta "nº + status"

Rodar: python -m pytest tests/test_raci_jabo_report.py -v
"""
import os
import sys
from datetime import datetime, date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_weekly_report as m  # noqa: E402
from services.raci_weekly_report import (  # noqa: E402
    _infer_task_responsavel,
    _strip_task_prefix,
    generate_jabo_report,
    format_raci_whatsapp,
)


# ───────────────────── inferencia de responsavel ─────────────────────
class TestInferResponsavel:
    def test_prefixo_com_pessoa(self):
        assert _infer_task_responsavel("[Jabô/Andressa] Enviar onboarding") == "Andressa"

    def test_prefixo_so_projeto_sem_pessoa(self):
        assert _infer_task_responsavel("[Jabô] Classificar safra") == "—"

    def test_sem_prefixo(self):
        assert _infer_task_responsavel("Investigar Fiama") == "—"

    def test_prefixo_com_barra_e_espacos(self):
        assert _infer_task_responsavel("[Jabô / Reginaldo]  Irrigação") == "Reginaldo"

    def test_titulo_vazio(self):
        assert _infer_task_responsavel("") == "—"
        assert _infer_task_responsavel(None) == "—"


class TestStripPrefix:
    def test_remove_prefixo(self):
        assert _strip_task_prefix("[Jabô/Andressa] Enviar onboarding") == "Enviar onboarding"

    def test_sem_prefixo_mantem(self):
        assert _strip_task_prefix("Investigar Fiama") == "Investigar Fiama"

    def test_so_prefixo_nao_esvazia(self):
        # se sobrar nada apos o strip, mantem o titulo original
        assert _strip_task_prefix("[Jabô]") == "[Jabô]"


# ───────────────────── bucketing (fake cursor) ─────────────────────
class _FakeCursor:
    """Devolve `rows` na primeira execute; RealDictCursor-like (dict rows)."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


def _task(id, titulo, status, prazo=None, atualizado_em=None, data_conclusao=None):
    return {
        "id": id,
        "titulo": titulo,
        "status": status,
        "prioridade": 5,
        "data_vencimento": prazo,
        "data_conclusao": data_conclusao,
        "atualizado_em": atualizado_em,
    }


class TestBucketing:
    def _report(self, rows):
        return generate_jabo_report(_FakeCursor(rows))

    def test_vazio_retorna_none(self):
        assert self._report([]) is None

    def test_vencido_sem_update_recente_e_urgente(self):
        old = datetime.now() - timedelta(days=10)
        prazo = date.today() - timedelta(days=5)
        r = self._report([_task(1, "[Jabô] Atrasada fria", "pending", prazo, atualizado_em=old)])
        assert len(r["urgentes"]) == 1
        assert len(r["atrasadas_mov"]) == 0

    def test_vencido_com_update_recente_vai_pra_atrasadas_mov(self):
        recent = datetime.now() - timedelta(hours=2)
        prazo = date.today() - timedelta(days=5)
        r = self._report([_task(2, "[Jabô] Atrasada com movimento", "pending", prazo, atualizado_em=recent)])
        assert len(r["urgentes"]) == 0
        assert len(r["atrasadas_mov"]) == 1

    def test_prazo_futuro_e_no_prazo(self):
        prazo = date.today() + timedelta(days=5)
        r = self._report([_task(3, "[Jabô] Futura", "pending", prazo)])
        assert len(r["no_prazo"]) == 1

    def test_sem_prazo_e_no_prazo(self):
        r = self._report([_task(4, "[Jabô] Sem prazo", "pending", None)])
        assert len(r["no_prazo"]) == 1

    def test_concluida_recente_aparece(self):
        done = datetime.now() - timedelta(days=2)
        r = self._report([_task(5, "[Jabô] Feita ontem", "completed", data_conclusao=done)])
        assert len(r["concluidas"]) == 1

    def test_concluida_antiga_some(self):
        done = datetime.now() - timedelta(days=30)
        r = self._report([_task(6, "[Jabô] Feita mês passado", "completed", data_conclusao=done)])
        # unica task e concluida-antiga -> nada acionavel -> None
        assert r is None

    def test_on_hold_e_cancelled_ficam_de_fora(self):
        # o filtro esta no SQL (status NOT IN), mas garantimos que o fake nao
        # injeta esses estados: passamos so pending; total reflete o visivel
        prazo = date.today() + timedelta(days=3)
        r = self._report([_task(7, "[Jabô] Ativa", "pending", prazo)])
        assert r["total"] == 1

    def test_responsavel_inferido_no_entry(self):
        prazo = date.today() + timedelta(days=3)
        r = self._report([_task(8, "[Jabô/Andressa] Pipeline", "pending", prazo)])
        assert r["no_prazo"][0]["responsavel"] == "Andressa"
        assert r["no_prazo"][0]["acao"] == "Pipeline"


# ───────────────────── formato nao-interativo ─────────────────────
class TestFormatNaoInterativo:
    def _report(self):
        prazo = date.today() + timedelta(days=3)
        return generate_jabo_report(_FakeCursor([_task(9, "[Jabô/Andressa] X", "pending", prazo)]))

    def test_interactive_false_sem_convite_de_resposta(self):
        msg = format_raci_whatsapp(self._report(), interactive=False)
        assert "Responda com o nº" not in msg
        assert "_Total: 1_" in msg

    def test_interactive_true_mantem_convite(self):
        msg = format_raci_whatsapp(self._report(), interactive=True)
        assert "Responda com o nº" in msg


# ── Preview semanal dos conselhos desligado (29/07) ──────────────────────────
# Decisao do Renato: "agora que temos o RACI dentro do projeto, com botao de
# disparo, nao faz sentido continuar mandando o preview". O Jabo NAO sai junto —
# o RACI dele sao as tasks do #28 e a pagina nao as le (zero raci_itens), entao
# desligar os dois deixaria a fazenda sem superficie nenhuma.
class TestPreviewConselhosDesligado:
    def test_default_desligado(self, monkeypatch):
        monkeypatch.delenv("RACI_WEEKLY_PREVIEW_CONSELHOS", raising=False)
        assert m._preview_conselhos_enabled() is False

    def test_liga_por_env(self, monkeypatch):
        for valor in ("on", "1", "true", "YES"):
            monkeypatch.setenv("RACI_WEEKLY_PREVIEW_CONSELHOS", valor)
            assert m._preview_conselhos_enabled() is True, valor

    def test_whitespace_do_railway_nao_quebra(self, monkeypatch):
        """Railway/Vercel colam '\\n' no valor — padrao anti-whitespace do repo."""
        monkeypatch.setenv("RACI_WEEKLY_PREVIEW_CONSELHOS", " on\n")
        assert m._preview_conselhos_enabled() is True

    def test_valor_qualquer_mantem_desligado(self, monkeypatch):
        monkeypatch.setenv("RACI_WEEKLY_PREVIEW_CONSELHOS", "talvez")
        assert m._preview_conselhos_enabled() is False


# ═══════════════ 16/08/2026 — o preview passou a mostrar o SILÊNCIO ═══════════
# Pedido do Renato: "a Andressa tem enviado diversas atualizações e não estamos
# fazendo o RACI semanal; não quero desmotivá-la". O preview chegava toda segunda
# sem dizer quantas vezes ela escreveu sem receber nada — que é o dado que decide
# se ele para tudo e dispara.

class _CursorReportes:
    """Cursor de 2 tempos: 1ª execute devolve a âncora (último RACI), 2ª os
    reportes. Espelha a ordem real de `jabo_reportes_pendentes`."""
    def __init__(self, desde, reportes, jid="jid@g.us"):
        self._respostas = [{"group_jid": jid}, {"ts": desde}]
        self._reportes = reportes
        self._n = 0

    def execute(self, *a, **k):
        self._n += 1

    def fetchone(self):
        return self._respostas.pop(0) if self._respostas else None

    def fetchall(self):
        return self._reportes


def _msg(dia, nome, texto):
    return {"timestamp": datetime(2026, 8, dia, 12, 0), "sender_name": nome,
            "content": texto}


class TestReportesPendentes:
    def test_conta_e_data_do_ultimo_raci(self):
        desde = datetime(2026, 8, 3, 14, 53)
        ctx = m.jabo_reportes_pendentes(
            _CursorReportes(desde, [_msg(4, "Andressa Santos", "Amostra enviada ao Coffee Lab")]))
        assert ctx["desde"] == desde
        assert len(ctx["reportes"]) == 1
        assert ctx["dias"] >= 0

    def test_sem_raci_anterior_nao_quebra(self):
        ctx = m.jabo_reportes_pendentes(_CursorReportes(None, []))
        assert ctx["dias"] is None and ctx["reportes"] == []


class TestBlocoReportes:
    def test_conta_todos_e_nomeia_quem(self):
        ctx = {"dias": 12, "desde": None, "reportes": [
            _msg(4, "Andressa Santos", "Amostra enviada ontem para o Coffee Lab, aguardo retorno"),
            _msg(5, "Andressa Santos", "Nova amostra enviada para a Garagem do Café hoje"),
        ]}
        b = m._bloco_reportes(ctx)
        assert "2 atualizações no grupo sem retorno" in b
        assert "*Andressa* 2×" in b
        assert "12 dias" in b

    def test_saudacao_nao_ocupa_a_amostra_mas_conta(self):
        """'Olá! Bom dia!' é toque sem retorno igual — entra na contagem — mas
        não pode roubar a vaga de uma atualização real na amostra."""
        ctx = {"dias": 5, "desde": None, "reportes": [
            _msg(4, "Andressa Santos", "Follow-up enviado às cinco torrefações da lista"),
            _msg(5, "Andressa Santos", "Olá!"),
        ]}
        b = m._bloco_reportes(ctx)
        assert "2 atualizações" in b          # contagem inclui a saudação
        assert "Follow-up enviado" in b       # amostra mostra a substantiva
        assert "Olá!" not in b

    def test_so_saudacoes_ainda_mostra_algo(self):
        """Se TUDO for curto, mostrar as curtas — melhor que bloco mudo."""
        ctx = {"dias": 3, "desde": None, "reportes": [_msg(4, "Andressa Santos", "Bom dia!")]}
        assert "Bom dia!" in m._bloco_reportes(ctx)

    def test_silencio_dos_dois_lados_avisa(self):
        b = m._bloco_reportes({"dias": 30, "desde": None, "reportes": []})
        assert "30 dias" in b and "ninguém escreveu" in b

    def test_sem_reportes_e_raci_recente_fica_quieto(self):
        assert m._bloco_reportes({"dias": 2, "desde": None, "reportes": []}) == ""


class TestResponsavelPorVinculo:
    """O vínculo só vale para quem o projeto declara EXECUTOR — senão o RACI
    manda ao grupo uma matriz dando tarefa a quem não a tem."""

    def test_executor_declarado_assume_a_linha(self):
        assert _infer_task_responsavel("Ligar para a Fora da Lei", "Andressa Santos") == "Andressa"

    def test_prefixo_vence_o_vinculo(self):
        assert _infer_task_responsavel("[Jabô/Reginaldo] Classificar", "Andressa Santos") == "Reginaldo"

    def test_papel_de_contraparte_nao_e_executor(self):
        assert m._PAPEL_EXECUTOR.search("contraparte") is None
        assert m._PAPEL_EXECUTOR.search("Prospect canal USA") is None
        assert m._PAPEL_EXECUTOR.search("Decisor — classificação da safra") is None

    def test_papel_de_executor_reconhecido(self):
        assert m._PAPEL_EXECUTOR.search("Executora operacional (R: prospecção/amostras)")
        assert m._PAPEL_EXECUTOR.search("Gerente da fazenda — executa classificação")

    def test_caso_real_rodrigo_nao_vira_responsavel(self):
        """'Convidar Rodrigo a visitar a fazenda' tem contact_id do Rodrigo, que
        é o ASSUNTO. Ele é 'contraparte'/'Responsável' sem execução declarada —
        o filtro de papel zera o vínculo antes de chegar aqui."""
        assert _infer_task_responsavel("Convidar Rodrigo a visitar a fazenda", None) == "—"


# ═══════════════════ o preview mostra o que o botão envia (17/08/2026) ═══════════════════
#
# O DEFEITO QUE ESTES TESTES GUARDAM. Até 17/08 o corpo do preview vinha das
# *tasks* do #28 enquanto o "Enviar com 1 clique" — a página `/projetos/28/raci` —
# monta o texto com `raci_matrix.get_matrix` sobre os 10 itens de `raci_itens`.
# Medido naquele dia: os dois conjuntos não tinham UM item em comum. O Renato
# revisava um texto e disparava outro, e o gate humano aprovava uma peça que nunca
# chegava a ninguém.
#
# Piorava por desenho: o auto-update do grupo escreve em `raci_itens`, então
# quanto melhor ele funcionasse, mais o que a Andressa recebia se afastava do
# aprovado. Um teste sobre "o preview não está vazio" não pegaria nada disso —
# ele passava com a fonte errada. O que pega é comparar com o texto do BOTÃO.

class _CursorPreview:
    """Cursor do wrapper: tasks, nome do grupo, e a âncora/reportes do grupo."""

    def __init__(self, tasks, group_name="🌱 Governança Jabô"):
        self._tasks = tasks
        self._fetchone = [
            {"group_name": group_name},          # destino
            {"group_jid": "jid@g.us"},           # jabo_reportes_pendentes: jid
            {"ts": None},                        # âncora: sem RACI anterior
        ]
        self._ultimo = None

    def execute(self, sql, *a, **k):
        self._ultimo = sql

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        # generate_jabo_report é o primeiro a chamar fetchall; reportes vêm vazios
        if self._tasks is not None:
            t, self._tasks = self._tasks, None
            return t
        return []


class _ConnPreview:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_MATRIZ_FAKE = {
    "project": {"id": 28, "nome": "Exportação Direta — Café Jabô"},
    "itens": [{"id": 10, "acao": "ITEM CURADO DO RACI", "status_efetivo": "em_andamento",
               "responsavel_r": "Andressa", "prazo": None, "fonte": "intel"}],
    "resumo": {"em_andamento": 1}, "acumulado": {}, "total": 1,
}


def _preview_com(monkeypatch, tasks, matriz=_MATRIZ_FAKE):
    from services import raci_matrix
    cur = _CursorPreview(tasks)
    monkeypatch.setattr(m, "get_db", lambda: _ConnPreview(cur), raising=False)
    monkeypatch.setattr("database.get_db", lambda: _ConnPreview(cur), raising=False)
    monkeypatch.setattr(raci_matrix, "get_matrix", lambda pid, **k: matriz)
    monkeypatch.setattr(m, "_intel_base_url", lambda: "https://intel.exemplo")
    return m.build_jabo_preview()


class TestPreviewUsaAFonteDoBotao:
    def test_corpo_e_o_texto_que_o_botao_enviaria(self, monkeypatch):
        """O controle positivo: o corpo tem que ser IDÊNTICO ao que
        `format_for_whatsapp` produz — a mesma função que a página usa."""
        from services.raci_matrix import format_for_whatsapp
        p = _preview_com(monkeypatch, [_task(1, "[Jabô/Andressa] TASK DO BACKLOG", "pending",
                                             date.today() - timedelta(days=5))])
        assert p is not None
        esperado = format_for_whatsapp(_MATRIZ_FAKE)
        assert esperado in p, "o corpo do preview não é o texto que o botão envia"
        assert "ITEM CURADO DO RACI" in p

    def test_task_nao_entra_no_corpo_que_vai_ao_grupo(self, monkeypatch):
        """A task pode aparecer no bloco de backlog, nunca no texto pra copiar —
        senão volta o desalinhamento entre o que se aprova e o que se envia."""
        p = _preview_com(monkeypatch, [_task(1, "[Jabô/Andressa] TASK DO BACKLOG", "pending",
                                             date.today() - timedelta(days=5))])
        corpo = p.split("━━━━━━━━━━━━━━━━━━━━")[1]
        assert "TASK DO BACKLOG" not in corpo
        assert "ITEM CURADO DO RACI" in corpo

    def test_backlog_aparece_rotulado_como_fora_do_grupo(self, monkeypatch):
        """As tasks eram o corpo do preview até 17/08. Tirá-las sem substituto
        removeria informação que ele vinha recebendo — ficam, nomeadas."""
        p = _preview_com(monkeypatch, [_task(1, "[Jabô/Andressa] TASK DO BACKLOG", "pending",
                                             date.today() - timedelta(days=5))])
        assert "TASK DO BACKLOG" in p
        assert "não vai ao grupo" in p

    def test_sem_task_atrasada_nao_inventa_bloco(self, monkeypatch):
        p = _preview_com(monkeypatch, [_task(2, "[Jabô] Futura", "pending",
                                             date.today() + timedelta(days=30))])
        assert "não vai ao grupo" not in p
        assert "ITEM CURADO DO RACI" in p

    def test_sem_task_nenhuma_o_preview_ainda_sai(self, monkeypatch):
        """Quem manda em existir preview é o RACI, não o backlog: as tasks são
        contexto, e contexto ausente não pode calar a peça."""
        p = _preview_com(monkeypatch, [])
        assert p is not None and "ITEM CURADO DO RACI" in p

    def test_raci_vazio_nao_manda_preview(self, monkeypatch):
        """Sem item curado não há peça pra revisar — e um preview com corpo vazio
        treina a pessoa a ignorar o preview."""
        vazia = {**_MATRIZ_FAKE, "itens": [], "resumo": {}, "total": 0}
        assert _preview_com(monkeypatch, [], matriz=vazia) is None

    def test_matriz_com_erro_nao_manda_preview_meio_pronto(self, monkeypatch):
        assert _preview_com(monkeypatch, [], matriz={"error": "projeto não encontrado"}) is None
