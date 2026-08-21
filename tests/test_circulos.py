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
    TAG_PROFISSIONAL_OVERRIDES,
    calcular_circulo_profissional,
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

    def test_tag_conselho_e_do_eixo_profissional(self, contact_conselho):
        """`conselho` migrou pro eixo PROFISSIONAL quando os círculos viraram
        dois (pessoal × profissional). Este teste esperava círculo 2 do
        `calcular_score_circulo`, que hoje só olha as tags pessoais — e por isso
        acusava regressão onde houve redesenho.

        O círculo NÃO é cravado aqui: sai do próprio `TAG_PROFISSIONAL_OVERRIDES`
        (hoje R3 = Networking, junto com prospect e fornecedor — taxonomia
        deliberada, documentada acima da tabela). Cravar o número foi o erro que
        quebrou a versão anterior deste teste, e eu o repeti na primeira tentativa
        do conserto. O que se testa é a PROPRIEDADE: a tag é reconhecida como
        override e não cai no caminho de frequência.

        ⚠️ Os syncs (whatsapp/gmail) continuam chamando `calcular_score_circulo`,
        que só olha as tags pessoais — quem tem só tag de conselho NÃO recebe o
        override profissional por aquele caminho. É decisão de produto em aberto,
        não defeito deste teste.
        """
        esperado = next(c for c, tags in TAG_PROFISSIONAL_OVERRIDES.items()
                        if "conselho" in tags)
        circulo, reasons = calcular_circulo_profissional(contact_conselho)
        assert circulo == esperado, (
            f"tag `conselho` está declarada no círculo {esperado} profissional "
            f"mas o cálculo devolveu {circulo} — o override não foi aplicado")
        assert any("tag" in r.lower() for r in reasons), (
            f"veio por frequência, não por override: {reasons}")

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
        """Atraso é RELATIVO à frequência do círculo — o número vem do config.

        A versão anterior cravava "círculo 2 tem frequência de 14 dias" e passou
        a falhar quando o config virou 30. O que importa não é o número, é a
        propriedade: passar bem do prazo derruba o health.
        """
        freq = CIRCULO_CONFIG[2]["frequencia_dias"]
        contact = {
            "circulo": 2,
            "ultimo_contato": (datetime.now() - timedelta(days=freq * 2 - 1)).isoformat()
        }
        health = calcular_health_score(contact, 2)
        assert health < 50, f"quase 2x a frequência ({freq}d) deveria derrubar o health"

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
        """Propriedade, não número: o arquivo é o mais tolerante de todos.
        Cravar 365 quebrou quando o config pessoal passou a usar 180."""
        freqs = [CIRCULO_CONFIG[i]["frequencia_dias"] for i in range(1, 5)]
        assert CIRCULO_CONFIG[5]["frequencia_dias"] >= max(freqs)


class TestTagOverrides:
    """Testes para tags de override"""

    def test_familia_tags_exist(self):
        """O eixo pessoal usa o VÍNCULO, não o rótulo genérico: "familia" e
        "family" saíram quando as tags viraram específicas (filho/pai/mae)."""
        assert "filho" in TAG_OVERRIDES[1]
        assert "mae" in TAG_OVERRIDES[1]
        assert "esposa" in TAG_OVERRIDES[1]

    def test_conselho_tags_exist(self):
        """`conselho` vive no eixo PROFISSIONAL — procurar no pessoal foi o que
        fez este teste acusar regressão inexistente."""
        profissionais = {t for tags in TAG_PROFISSIONAL_OVERRIDES.values() for t in tags}
        assert "conselho" in profissionais
        assert "board" in profissionais


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
