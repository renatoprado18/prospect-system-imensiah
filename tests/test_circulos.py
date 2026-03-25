"""
Testes para o sistema de Circulos.

Rodar: python -m pytest tests/test_circulos.py -v
"""
import pytest
from datetime import datetime, timedelta
import sys
import os

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import functions to test (without database dependency)
from app.services.circulos import (
    parse_tags,
    has_tag,
    get_matching_tags,
    calcular_dias_sem_contato,
    calcular_score_circulo,
    calcular_health_score,
    CIRCULO_CONFIG,
    TAG_OVERRIDES,
    BONUS_TAGS
)


class TestParseTags:
    """Testes para parse_tags()"""

    def test_empty_list(self):
        assert parse_tags([]) == []

    def test_empty_string(self):
        assert parse_tags("") == []

    def test_none(self):
        assert parse_tags(None) == []

    def test_list_of_strings(self):
        assert parse_tags(["Familia", "Amigo"]) == ["familia", "amigo"]

    def test_json_string(self):
        assert parse_tags('["test", "TAG"]') == ["test", "tag"]

    def test_comma_separated(self):
        assert parse_tags("cliente, vip, importante") == ["cliente", "vip", "importante"]

    def test_mixed_case(self):
        assert parse_tags(["FAMILIA", "Conselho", "vip"]) == ["familia", "conselho", "vip"]


class TestHasTag:
    """Testes para has_tag()"""

    def test_has_matching_tag(self):
        assert has_tag(["familia", "amigo"], ["familia", "family"]) is True

    def test_no_matching_tag(self):
        assert has_tag(["cliente", "vip"], ["familia", "family"]) is False

    def test_empty_contact_tags(self):
        assert has_tag([], ["familia"]) is False

    def test_empty_target_tags(self):
        assert has_tag(["familia"], []) is False


class TestCalcularDiasSemContato:
    """Testes para calcular_dias_sem_contato()"""

    def test_hoje(self):
        hoje = datetime.now().isoformat()
        assert calcular_dias_sem_contato(hoje) == 0

    def test_uma_semana(self):
        semana_atras = (datetime.now() - timedelta(days=7)).isoformat()
        assert calcular_dias_sem_contato(semana_atras) == 7

    def test_none(self):
        assert calcular_dias_sem_contato(None) is None

    def test_string_vazia(self):
        assert calcular_dias_sem_contato("") is None


class TestCalcularScoreCirculo:
    """Testes para calcular_score_circulo()"""

    def test_tag_familia_circulo_1(self, contact_familia):
        circulo, score, reasons = calcular_score_circulo(contact_familia)
        assert circulo == 1
        assert "Tag especial" in reasons[0]

    def test_tag_conselho_circulo_2(self, contact_conselho):
        circulo, score, reasons = calcular_score_circulo(contact_conselho)
        assert circulo == 2
        assert "Tag especial" in reasons[0]

    def test_contato_ativo_circulo_2_ou_3(self, contact_ativo):
        circulo, score, reasons = calcular_score_circulo(contact_ativo)
        assert circulo in [2, 3]
        assert score >= 50

    def test_contato_conhecido_circulo_4(self, contact_conhecido):
        circulo, score, reasons = calcular_score_circulo(contact_conhecido)
        assert circulo in [4, 5]
        assert score >= 10

    def test_contato_arquivo_circulo_5(self, contact_arquivo):
        circulo, score, reasons = calcular_score_circulo(contact_arquivo)
        assert circulo == 5
        assert score < 25

    def test_muitas_interacoes_aumenta_score(self):
        contact_low = {"total_interacoes": 3, "tags": []}
        contact_high = {"total_interacoes": 50, "tags": []}

        _, score_low, _ = calcular_score_circulo(contact_low)
        _, score_high, _ = calcular_score_circulo(contact_high)

        assert score_high > score_low

    def test_contato_recente_aumenta_score(self):
        contact_old = {
            "total_interacoes": 10,
            "tags": [],
            "ultimo_contato": (datetime.now() - timedelta(days=100)).isoformat()
        }
        contact_recent = {
            "total_interacoes": 10,
            "tags": [],
            "ultimo_contato": datetime.now().isoformat()
        }

        _, score_old, _ = calcular_score_circulo(contact_old)
        _, score_recent, _ = calcular_score_circulo(contact_recent)

        assert score_recent > score_old

    def test_bonus_tags_aumenta_score(self):
        contact_normal = {"total_interacoes": 10, "tags": []}
        contact_vip = {"total_interacoes": 10, "tags": ["vip", "cliente"]}

        _, score_normal, _ = calcular_score_circulo(contact_normal)
        _, score_vip, _ = calcular_score_circulo(contact_vip)

        assert score_vip > score_normal


class TestCalcularHealthScore:
    """Testes para calcular_health_score()"""

    def test_contato_em_dia_health_100(self):
        contact = {
            "circulo": 2,
            "ultimo_contato": datetime.now().isoformat()
        }
        health = calcular_health_score(contact, 2)
        assert health == 100

    def test_contato_atrasado_health_baixo(self):
        # Circulo 2 tem frequencia de 14 dias
        # 30 dias sem contato = muito atrasado
        contact = {
            "circulo": 2,
            "ultimo_contato": (datetime.now() - timedelta(days=30)).isoformat()
        }
        health = calcular_health_score(contact, 2)
        assert health < 50

    def test_sem_contato_health_minimo(self):
        contact = {"circulo": 1, "ultimo_contato": None}
        health = calcular_health_score(contact, 1)
        assert health == 20  # Valor minimo para sem contato

    def test_circulo_5_mais_tolerante(self):
        # Circulo 5 tem frequencia de 365 dias
        # 100 dias sem contato ainda esta ok
        contact = {
            "circulo": 5,
            "ultimo_contato": (datetime.now() - timedelta(days=100)).isoformat()
        }
        health = calcular_health_score(contact, 5)
        assert health == 100

    def test_frequencia_personalizada(self):
        contact = {
            "circulo": 3,
            "frequencia_ideal_dias": 7,  # Personalizado para 7 dias
            "ultimo_contato": (datetime.now() - timedelta(days=14)).isoformat()
        }
        # Com frequencia de 7 dias, 14 dias = 100% excesso = health 0
        health = calcular_health_score(contact, 3)
        assert health == 0


class TestCirculoConfig:
    """Testes para configuracao dos circulos"""

    def test_todos_circulos_definidos(self):
        for i in range(1, 6):
            assert i in CIRCULO_CONFIG

    def test_frequencias_crescentes(self):
        freqs = [CIRCULO_CONFIG[i]["frequencia_dias"] for i in range(1, 6)]
        assert freqs == sorted(freqs)

    def test_circulo_1_mais_frequente(self):
        assert CIRCULO_CONFIG[1]["frequencia_dias"] <= 7

    def test_circulo_5_menos_frequente(self):
        assert CIRCULO_CONFIG[5]["frequencia_dias"] >= 365


class TestTagOverrides:
    """Testes para tags de override"""

    def test_familia_tags_exist(self):
        assert "familia" in TAG_OVERRIDES[1]
        assert "family" in TAG_OVERRIDES[1]

    def test_conselho_tags_exist(self):
        assert "conselho" in TAG_OVERRIDES[2]
        assert "board" in TAG_OVERRIDES[2]


class TestBonusTags:
    """Testes para tags de bonus"""

    def test_cliente_has_bonus(self):
        assert "cliente" in BONUS_TAGS
        assert BONUS_TAGS["cliente"] > 0

    def test_vip_has_highest_bonus(self):
        vip_bonus = BONUS_TAGS.get("vip", 0)
        for tag, bonus in BONUS_TAGS.items():
            if tag != "vip":
                assert vip_bonus >= bonus
