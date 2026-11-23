"""O contador de erro escondido um nivel abaixo do topo do payload.

Contexto (22/11/26): o `sync-tasks` vinha gravando `status='success'` com
`{"push": {"errors": 7}}` no MESMO corpo da resposta, duas vezes por dia. As
sete tasks falhavam desde abril e ninguem via.

A causa nao era do job: `_has_embedded_errors` olha `errors` no TOPO do payload
(regua 1) e desce um nivel apenas em `results` e `steps` (reguas 5 e 6). Um cron
que agrupa o proprio resultado por etapa — `push`, `por_status`, `snapshot` —
esconde o contador fora de todas elas e passa verde.

Medido sobre 30 dias de `cron_runs` (29.727 runs) antes do conserto: 854 runs
marcados `success` com erro real em sub-dict, em 5 jobs, e ZERO regressao (nenhum
run que era `error` passa a verde). Os payloads daqui sao os reais desses jobs.
"""
from services.cron_telemetry import _has_embedded_errors


class TestPegaOContadorEmSubDict:
    def test_sync_tasks_errors_dentro_de_push(self):
        """O caso que abriu a investigacao: 7 falhas e `status: success` juntos."""
        has, resumo = _has_embedded_errors({
            "job": "sync-tasks",
            "status": "success",
            "push": {"pushed": 1, "errors": 7, "error_samples": []},
            "pull": {"created": 0, "updated": 100},
        })
        assert has
        assert "push.errors=7" in resumo

    def test_push_google_contacts_erro_em_portugues(self):
        """720 runs em 30 dias, e o corpo se declarava `"status": "ok"`."""
        has, resumo = _has_embedded_errors({
            "job": "push-google-contacts",
            "status": "ok",
            "candidatas": 1,
            "por_status": {"erro": 1},
            "contatos_no_google": 4737,
        })
        assert has
        assert "por_status.erro=1" in resumo

    def test_lista_de_erros_entra_com_a_causa_no_resumo(self):
        """`sync-conselhoos-raci`: a FK aborta a transacao e leva 2 clientes."""
        has, resumo = _has_embedded_errors({
            "synced_at": "2026-11-22T06:00:50",
            "raci_to_tasks": {"errors": [], "created": 0},
            "pessoas_to_members": {"added": 0, "errors": [
                'Error syncing pessoas for Vallen Clinic: violates foreign key constraint',
                "Error syncing pessoas for Alba Consultoria: current transaction is aborted",
            ]},
        })
        assert has
        assert "pessoas_to_members.errors[2]" in resumo
        assert "Vallen" in resumo

    def test_failed_como_lista_de_dicts(self):
        """`platform-costs-daily`: o custo do Railway sem coletar."""
        has, _ = _has_embedded_errors({
            "job": "platform-costs-daily",
            "snapshot": {"failed": [{"provider": "railway", "error": "Not Authorized"}],
                         "auto_filled": [{"id": 1249}]},
        })
        assert has


class TestNaoAcendeAToa:
    def test_contador_zerado_e_lista_vazia_passam_verde(self):
        has, _ = _has_embedded_errors({
            "job": "sync-tasks",
            "push": {"pushed": 8, "errors": 0, "error_samples": []},
            "pull": {"created": 0},
        })
        assert not has

    def test_last_error_nao_e_falha_DESTE_run(self):
        """`result.last_error` guarda o erro mais recente conhecido (714
        ocorrencias na base) — e historico, nao resultado. Casar por substring
        em vez de nome exato acenderia alarme sobre ele."""
        has, _ = _has_embedded_errors({
            "job": "qualquer",
            "result": {"ok": 5, "errors": 0, "last_error": "timeout de 03/11"},
        })
        assert not has

    def test_sub_dict_sem_nenhuma_chave_de_erro(self):
        has, _ = _has_embedded_errors({
            "job": "x",
            "pull": {"created": 3, "updated": 100, "skipped": 0},
        })
        assert not has

    def test_booleano_nao_e_contagem(self):
        """`failed: False` e um flag, e `True` seria contado como 1 por int."""
        has, _ = _has_embedded_errors({"job": "x", "etapa": {"failed": False}})
        assert not has

    def test_sub_dict_aninhado_mais_fundo_nao_e_varrido(self):
        """A varredura e de UM nivel de proposito: descer sem limite leria
        contador de erro dentro de payload de terceiro (ex.: resposta de API
        guardada no resultado) como falha do job."""
        has, _ = _has_embedded_errors({
            "job": "x",
            "etapa": {"resposta_da_api": {"errors": 3}},
        })
        assert not has


class TestNaoQuebraAsReguasAntigas:
    def test_errors_no_topo_continua_pegando(self):
        has, resumo = _has_embedded_errors({"errors": 2604, "contacts_updated": 0})
        assert has and "2604" in resumo

    def test_status_error_continua_pegando(self):
        has, _ = _has_embedded_errors({"status": "error", "error": "token expirado"})
        assert has

    def test_steps_continua_pegando(self):
        has, resumo = _has_embedded_errors({
            "steps": {"wa": {"status": "ok"}, "gmail": {"status": "timeout"}},
        })
        assert has and "gmail" in resumo

    def test_payload_limpo_segue_verde(self):
        has, _ = _has_embedded_errors({"job": "x", "status": "success", "rows": 12})
        assert not has
