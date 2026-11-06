"""`_normalize_phone` só acertava DDD de São Paulo — 597 de 713 saíam sem o `55`.

O defeito (medido 06/11/2026) era o `if` de 11 dígitos exigir `startswith('11')`:

    if len(digits) == 11 and digits.startswith('11'): digits = '55' + digits
    elif len(digits) == 10: digits = '5511' + digits
    elif len(digits) == 9:  digits = '55119' + digits

`41991064663` (Curitiba) não casava com ramo nenhum e ia para a Evolution sem
DDI. Falha calada: a API aceita e devolve sucesso.

Estes testes travam as TRÊS classes de erro que apareceram ao consertar:

1. **DDD fora de SP sem DDI** — o defeito relatado.
2. **DDD de SP continua funcionando** — regressão: 113 números já estavam
   certos e não podem mudar.
3. **Número internacional NÃO leva `55`** — a classe que o conserto "óbvio"
   (convergir para a regra `not startswith('55') and len <= 11` do
   `evolution_api`) introduziria: `+33624553002` viraria `5533624553002` e a
   mensagem iria para um brasileiro em Governador Valadares. A base tem 192
   números assim no bucket de 11 dígitos.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.phone_br import DDD_VALIDOS, normalize_br_phone  # noqa: E402


# ---------------------------------------------------------------- o defeito
class TestDDDForaDeSaoPaulo:
    """Classe 1 — o que estava quebrado."""

    def test_curitiba_o_caso_medido(self):
        # O número do relato. Antes: '41991064663' (sem DDI).
        assert normalize_br_phone("41991064663") == "5541991064663"

    @pytest.mark.parametrize("entrada,esperado", [
        ("41991064663", "5541991064663"),  # PR
        ("21987654321", "5521987654321"),  # RJ
        ("31988887777", "5531988887777"),  # MG
        ("51999998888", "5551999998888"),  # RS
        ("61985665401", "5561985665401"),  # DF
        ("71991112222", "5571991112222"),  # BA
        ("85988776655", "5585988776655"),  # CE
        ("92981234567", "5592981234567"),  # AM
        ("13991234567", "5513991234567"),  # Santos
        ("19998887766", "5519998887766"),  # Campinas
    ])
    def test_celular_de_todo_ddd_ganha_o_55(self, entrada, esperado):
        assert normalize_br_phone(entrada) == esperado

    def test_ddd_55_nao_e_confundido_com_o_ddi(self):
        # Santa Maria/RS é DDD 55. A regra do `evolution_api`
        # (`not startswith('55')`) deixava este número SEM DDI, porque os dois
        # primeiros dígitos já são "55". Decidir por FORMA resolve: 11 dígitos
        # + DDD válido + nono dígito 9 é celular nacional.
        assert normalize_br_phone("55991234567") == "5555991234567"


# ------------------------------------------------------------- a regressão
class TestRegressaoDDD11:
    """Classe 2 — os 113 números que JÁ funcionavam não podem mudar."""

    @pytest.mark.parametrize("entrada", [
        "11984153337",
        "(11) 99504-3148",
        "(11) 95210-5262",
        "11 97621 4041",
        "+55 11 98415-3337",
        "11981006600",
    ])
    def test_sao_paulo_continua_saindo_igual(self, entrada):
        saida = normalize_br_phone(entrada)
        assert saida.startswith("5511")
        assert len(saida) == 13

    def test_o_exemplo_do_docstring_original(self):
        assert normalize_br_phone("+55 11 98415-3337") == "5511984153337"
        assert normalize_br_phone("(11) 98415-3337") == "5511984153337"


# ------------------------------------------------------ a classe introduzida
class TestInternacionalNaoLevaDDIBrasileiro:
    """Classe 3 — o conserto "óbvio" mandaria a mensagem para outra pessoa.

    Todos os números abaixo existem na base de contatos do INTEL.
    """

    @pytest.mark.parametrize("entrada,esperado", [
        ("+33624553002", "33624553002"),      # França  — DDD 33 (Gov. Valadares) existe!
        ("+12122706000", "12122706000"),      # EUA     — DDD 12 (SJCampos) existe
        ("+13102183219", "13102183219"),      # EUA     — DDD 13 (Santos) existe
        ("+14157588419", "14157588419"),      # EUA     — DDD 14 (Bauru) existe
        ("+1 (207) 613-7334", "12076137334"), # EUA com máscara
        ("19292132613", "19292132613"),       # EUA     — DDD 19 (Campinas) existe
        ("16266176338", "16266176338"),       # EUA     — DDD 16 (Ribeirão) existe
    ])
    def test_nao_ganha_55(self, entrada, esperado):
        saida = normalize_br_phone(entrada)
        assert saida == esperado
        assert not saida.startswith("55"), (
            f"{entrada} ganhou DDI brasileiro — a mensagem iria para um estranho"
        )

    def test_o_que_separa_e_o_nono_digito(self):
        # Mesmo DDD (13), mesma quantidade de dígitos. O que decide é a FORMA
        # do assinante: celular brasileiro tem 9 na terceira posição.
        assert normalize_br_phone("13991234567") == "5513991234567"  # Santos
        assert normalize_br_phone("13102183219") == "13102183219"    # +1 310


# ------------------------------------------------------- formatos da vida real
class TestFormatosDaVidaReal:

    @pytest.mark.parametrize("entrada,esperado", [
        # com e sem máscara
        ("(41) 99106-4663", "5541991064663"),
        ("41 99106-4663", "5541991064663"),
        ("41 9 9106 4663", "5541991064663"),
        # com e sem +
        ("+5541991064663", "5541991064663"),
        ("5541991064663", "5541991064663"),
        ("+55 (41) 99106-4663", "5541991064663"),
        # fixo com DDD (8 dígitos de assinante, começa em 2-5)
        ("1133334444", "551133334444"),
        ("(11) 3258-8699", "551132588699"),
        ("5137782929", "555137782929"),
        ("+551133334444", "551133334444"),
        # prefixo de tronco interurbano — 346 ocorrências na base
        ("(011) 3258-8699", "551132588699"),
        ("(011) 5090-9009", "551150909009"),
        ("041 3333-4444", "554133334444"),
        # celular local, sem DDD: assume 11 (não há o que inferir)
        ("99106-4663", "5511991064663"),
        ("991064663", "5511991064663"),
    ])
    def test_formato(self, entrada, esperado):
        assert normalize_br_phone(entrada) == esperado

    def test_nono_digito_nao_e_duplicado(self):
        # O ramo antigo era `'55119' + digits` para 9 dígitos: '991064663'
        # virava '55119991064663' — 14 dígitos, com o 9 duas vezes.
        saida = normalize_br_phone("991064663")
        assert len(saida) == 13
        assert saida == "5511991064663"

    def test_fixo_de_dez_digitos_nao_recebe_5511_sobre_o_ddd_proprio(self):
        # O ramo antigo era `'5511' + digits` para QUALQUER 10 dígitos:
        # '5137782929' (Porto Alegre) virava '55115137782929' — 14 dígitos,
        # com dois DDDs.
        saida = normalize_br_phone("5137782929")
        assert len(saida) == 12
        assert saida == "555137782929"


# ------------------------------------------------------------------ abstenção
class TestNaoInventa:
    """Quando a forma não permite afirmar, devolve os dígitos como entraram."""

    @pytest.mark.parametrize("entrada", [
        "",
        None,
        "   ",
        "-",
    ])
    def test_vazio(self, entrada):
        assert normalize_br_phone(entrada) == ""

    def test_celular_sp_pre_2012_fica_intocado(self):
        # '(011) 8396-0550' é celular de SP do formato pré-nono-dígito. Inserir
        # o 9 aqui fabricaria um número DIFERENTE, e a mensagem sairia para um
        # estranho. `contact_identity.canonical_br_phone` faz essa inserção —
        # mas lá é para COMPARAR fichas, onde o erro custa um falso positivo
        # revisável, não um envio.
        assert normalize_br_phone("(011) 8396-0550") == "01183960550"

    def test_fixo_de_oito_digitos_nao_ganha_ddd(self):
        # A base tem campo corrompido por notação científica do Excel
        # ('5.51139E+16' -> '55113916'). Oito dígitos ganhando '5511' viraria
        # '551155113916' — plausível e errado.
        assert normalize_br_phone("5.51139E+16") == "55113916"
        assert normalize_br_phone("7169-8931") == "71698931"

    def test_ddd_inexistente_nao_e_tratado_como_brasileiro(self):
        # 20, 23, 25, 26, 29, 30, 36, 39, 40, 50, 52, 56..60, 70, 72, 76, 78,
        # 80, 90 não existem no plano da Anatel.
        for ddd in ("20", "23", "30", "40", "52", "60", "72", "80", "90"):
            assert ddd not in {str(d) for d in DDD_VALIDOS}
            assert normalize_br_phone(f"{ddd}991234567") == f"{ddd}991234567"

    def test_ddd_11_com_assinante_malformado_tambem_se_abstem(self):
        """Mudança de comportamento ASSUMIDA, não acidental.

        6 dos 113 telefones de DDD 11 da base têm assinante que não existe no
        plano brasileiro (nem `9` de celular, nem `2-5` de fixo) — campo com
        dois números (`'11 3149 7435/ 6'`), dígito sobrando, dado sujo. O
        código antigo prefixava `55` neles e produzia 13 dígitos que *parecem*
        um número válido e não são de ninguém. Abster-se deixa o número
        visível para quem for consertar o cadastro.
        """
        for sujo in ("11419118181", "11387132332", "11408111111", "11086555657"):
            assert normalize_br_phone(sujo) == sujo

    def test_jid_de_grupo_atravessa_sem_virar_telefone(self):
        # `POST /api/projects/{id}/whatsapp/grupo/enviar` passa o `group_jid`
        # por `send_text`. 18 dígitos não casam com ramo nenhum.
        assert normalize_br_phone("120363041234567890@g.us") == "120363041234567890"


# --------------------------------------------------- as duas implementações
class TestUmaSoImplementacao:
    """O defeito existia em DOIS lugares, errados em sentidos opostos. Uma
    segunda cópia da regra é a porta por onde ele volta."""

    def test_whatsapp_integration_delega(self):
        from integrations.whatsapp import WhatsAppIntegration

        wa = WhatsAppIntegration()
        assert wa._normalize_phone("41991064663") == "5541991064663"
        assert wa._normalize_phone("11984153337") == "5511984153337"
        assert wa._normalize_phone("+33624553002") == "33624553002"

    def test_nenhum_cliente_prefixa_ddi_em_linha(self):
        """Guarda de classe: o que volta é alguém PREPENDAR o DDI à mão.

        Não basta proibir `startswith('55')` — `format_phone_display` o usa
        legitimamente, para LER um número já normalizado. O que não pode
        voltar é a ESCRITA: montar o destino concatenando `'55'`/`'5511'` na
        frente dos dígitos.
        """
        import pathlib
        import re as _re

        raiz = pathlib.Path(__file__).resolve().parent.parent
        prefixacao = _re.compile(r"""=\s*['"]55\d*['"]\s*\+""")
        for arquivo in (
            "app/integrations/evolution_api.py",
            "app/integrations/whatsapp.py",
            "app/main.py",
        ):
            fonte = (raiz / arquivo).read_text(encoding="utf-8")
            achados = [
                linha.strip()
                for linha in fonte.splitlines()
                if prefixacao.search(linha) and not linha.lstrip().startswith("#")
            ]
            assert not achados, (
                f"{arquivo} voltou a prefixar DDI em linha ({achados}) — "
                f"use services.phone_br.normalize_br_phone"
            )


# ----------------------------------------------------- controle sobre a base
class TestContraOsDadosReais:
    """Controle positivo: a regra roda sobre os telefones da base, não só sobre
    exemplos escolhidos. Assertivas DIRECIONAIS (não contagens fixas), porque
    a base muda — o que não pode mudar é o sentido.

    Medição de 06/11/2026 no Postgres local: 713 telefones de 11 dígitos, 113
    de DDD 11 e 600 de outros DDDs; 597 saíam SEM o `55`, 214 passam a sair
    com DDI correto e 383 ficam intocados de propósito (189 internacionais +
    194 no formato de tronco 0 / celular SP pré-2012).
    """

    @staticmethod
    def _regra_antiga(phone: str) -> str:
        import re as _re

        d = _re.sub(r"\D", "", phone)
        if len(d) == 11 and d.startswith("11"):
            d = "55" + d
        elif len(d) == 10:
            d = "5511" + d
        elif len(d) == 9:
            d = "55119" + d
        return d

    @staticmethod
    def _forma_de_jid_br(d: str) -> bool:
        """13 dígitos = 55+DDD+9XXXXXXXX (celular); 12 = 55+DDD+[2-5]XXXXXXX."""
        if not d.startswith("55"):
            return False
        if len(d) == 13:
            return int(d[2:4]) in DDD_VALIDOS and d[4] == "9"
        if len(d) == 12:
            return int(d[2:4]) in DDD_VALIDOS and d[4] in "2345"
        return False

    @pytest.fixture(scope="class")
    def telefones(self):
        import json as _json
        import re as _re

        try:
            from database import get_db

            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT telefones FROM contacts WHERE telefones IS NOT NULL")
                linhas = cur.fetchall()
        except Exception as e:  # noqa: BLE001 — sem banco local a medição não roda
            pytest.skip(f"Postgres local indisponível: {e}")

        saida = []
        for linha in linhas:
            t = linha["telefones"]
            if isinstance(t, str):
                try:
                    t = _json.loads(t)
                except Exception:  # noqa: BLE001
                    t = [t]
            if isinstance(t, dict):
                t = list(t.values())
            if not isinstance(t, list):
                t = [t]
            for p in t:
                if isinstance(p, dict):
                    p = p.get("numero") or p.get("number") or p.get("telefone") or ""
                if p:
                    saida.append(str(p))
        if not saida:
            pytest.skip("base local sem telefones")
        return [p for p in saida if len(_re.sub(r"\D", "", p)) == 11]

    def test_nenhum_numero_entregavel_deixa_de_ser(self, telefones):
        """A guarda que importa: o conserto não pode tirar DDI de quem já tinha."""
        quebrados = [
            (p, self._regra_antiga(p), normalize_br_phone(p))
            for p in telefones
            if self._forma_de_jid_br(self._regra_antiga(p))
            and not self._forma_de_jid_br(normalize_br_phone(p))
        ]
        assert not quebrados, f"regressão de entregabilidade: {quebrados[:10]}"

    def test_a_maioria_dos_nao_sp_passa_a_ganhar_ddi(self, telefones):
        """Conta DDI ACRESCENTADO, não número que já começava com 55.

        A base tem 3 telefones de 11 dígitos escritos `+55119741344` — já
        começam com `55` sem ter DDI de verdade. Contar `startswith('55')` na
        saída confundiria os dois: o que se mede é a regra ter MUDADO o número.
        """
        import re as _re

        nao_sp = [p for p in telefones if not _re.sub(r"\D", "", p).startswith("11")]
        antes = sum(
            1 for p in nao_sp if self._regra_antiga(p) != _re.sub(r"\D", "", p)
        )
        depois = sum(
            1 for p in nao_sp if normalize_br_phone(p) != _re.sub(r"\D", "", p)
        )
        assert antes == 0, "a regra antiga nunca acrescentava DDI em DDD != 11"
        assert depois > 100, f"esperado centenas de conserto, veio {depois}"

    def test_sp_continua_com_ddi(self, telefones):
        import re as _re

        sp = [p for p in telefones if _re.sub(r"\D", "", p).startswith("11")]
        bem_formados = [p for p in sp if self._forma_de_jid_br(self._regra_antiga(p))]
        assert bem_formados, "controle vazio — a amostra de DDD 11 não serve de regressão"
        for p in bem_formados:
            assert normalize_br_phone(p) == self._regra_antiga(p), (
                f"{p} mudou e já estava certo"
            )
