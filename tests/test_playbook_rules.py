"""
Testes dos helpers puros do Playbook Andressa (frente 23/07).

Cobre a lógica determinística (sem LLM/DB/Drive): detecção de reunião Jabô,
parse do JSON do extractor, kill-switch, formato da proposta WA.
A qualidade da extração/merge (LLM) é validada por dry-run manual contra prod.

Rodar: python -m pytest tests/test_playbook_rules.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import playbook_rules as pb  # noqa: E402


class TestKillSwitch:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("PLAYBOOK_AUTO_EXTRACT", raising=False)
        assert pb.is_enabled() is False

    def test_on_variants(self, monkeypatch):
        for v in ("on", "1", "true", "YES"):
            monkeypatch.setenv("PLAYBOOK_AUTO_EXTRACT", v)
            assert pb.is_enabled() is True

    def test_off_variant(self, monkeypatch):
        monkeypatch.setenv("PLAYBOOK_AUTO_EXTRACT", "off")
        assert pb.is_enabled() is False


class TestIsJaboMeeting:
    def test_keyword_no_titulo(self):
        assert pb.is_jabo_meeting("Reunião sobre exportação de café", "", []) is True

    def test_keyword_no_resumo(self):
        assert pb.is_jabo_meeting("Call", "Falamos do lote de peneira alta pra Guaxupé", []) is True

    def test_acento_insensivel(self):
        # 'café' com e sem acento deve bater 'cafe'
        assert pb.is_jabo_meeting("Cafe verde", "", []) is True
        assert pb.is_jabo_meeting("Café verde", "", []) is True

    def test_membro_do_projeto_casa(self):
        # sem keyword, mas contato casado é membro do #28
        assert pb.is_jabo_meeting("Sync geral", "assunto diverso", [999], {999, 123}) is True

    def test_nao_jabo(self):
        assert pb.is_jabo_meeting("Reunião de conselho Vallen", "clínica e RACI", [7]) is False

    def test_nao_jabo_sem_membro(self):
        assert pb.is_jabo_meeting("Papo aleatório", "nada a ver", [7], {1, 2}) is False


class TestParseJsonArray:
    def test_array_limpo(self):
        raw = '[{"tema":"EXPORTAÇÃO","titulo":"X","texto":"Y"}]'
        out = pb._parse_json_array(raw)
        assert len(out) == 1
        assert out[0]["tema"] == "EXPORTAÇÃO" and out[0]["titulo"] == "X"

    def test_array_com_cercas_e_texto(self):
        raw = 'Claro! Aqui:\n```json\n[{"tema":"amostras","titulo":"A","texto":"B"}]\n```'
        out = pb._parse_json_array(raw)
        assert len(out) == 1
        assert out[0]["tema"] == "AMOSTRAS"  # upcase

    def test_vazio(self):
        assert pb._parse_json_array("[]") == []
        assert pb._parse_json_array(None) == []
        assert pb._parse_json_array("sem json aqui") == []

    def test_descarta_regra_incompleta(self):
        raw = '[{"tema":"X","titulo":"só titulo"}, {"tema":"Y","titulo":"ok","texto":"tem"}]'
        out = pb._parse_json_array(raw)
        assert len(out) == 1  # a sem 'texto' cai fora
        assert out[0]["titulo"] == "ok"

    def test_tema_default_outro(self):
        raw = '[{"titulo":"sem tema","texto":"corpo"}]'
        out = pb._parse_json_array(raw)
        assert out[0]["tema"] == "OUTRO"


class TestFormatProposalWa:
    def test_formata_com_numeracao_e_footer(self):
        rules = [
            {"tema": "EXPORTAÇÃO", "titulo": "EUDR", "texto": "Geoloc GPS."},
            {"tema": "AMOSTRAS", "titulo": "Peso", "texto": "250g."},
        ]
        msg = pb.format_proposal_wa(rules, "Reunião X", 42)
        assert "2 regra(s) nova(s)" in msg
        assert "1. [EXPORTAÇÃO] EUDR" in msg
        assert "2. [AMOSTRAS] Peso" in msg
        assert "#42" in msg
        assert "aprovo playbook" in msg
