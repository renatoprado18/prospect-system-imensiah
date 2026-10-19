"""
Vigilancia processual na triagem de e-mail (R3.7, 19/10/26).

O DEFEITO, medido em prod e nao estimado: **86 alertas processuais (Astrea +
Jusbrasil), de 17/06 a 19/10, com `needs_attention=True` em ZERO e 100%
classificados `archive_proposed`**. Nao era amostra — era a populacao inteira.

⚠️ **A CAUSA NAO ERA O R4 (no-reply)** — eu afirmei que era e estava errado. O
`classification_reasons` gravado em prod diz: *"Conta personal: default
archive_proposed (shadow) — must_read exige sinal claro"*. E o **R7 default da
conta PESSOAL**, onde chegam os 92, e cujo default agressivo foi decidido em
09/06/26 por causa de 12.496 promo + 11.921 update naquela caixa. A regra do R7
esta certa; materia processual era o sinal que ela engolia, sem nenhuma regra
`must_read` que o desse.

🔴 **E ISSO QUASE PASSOU PELOS TESTES:** a 1a versao deste arquivo classificava
com `account_type="professional"` (cujo default e `silent`) e 24 testes passavam
VERDES sem exercitar o caminho real. O caso vive em `personal`. Por isso o
default do helper aqui e **personal**, e ha teste explicito nas duas contas
([[feedback_teste_com_ambiente_declarado_nao_aplicado]]).

Alerta judicial vem de no-reply por natureza (tribunal nao espera resposta),
entao a regra tem de vir antes do R4 de todo modo — mas o que a torna
NECESSARIA e o R7.

Prova:
  (a) os assuntos REAIS que estavam sendo arquivados agora sao must_read;
  (b) CONTROLE NEGATIVO com os 5 e-mails de marketing reais do mesmo
      remetente — whitelist de dominio os traria junto, e nao pode;
  (c) "juridico" nao e evidencia: casa com marketing e esta fora da lista;
  (d) remetente de monitoramento SEM materia processual nao passa;
  (e) materia processual de remetente qualquer nao passa (nao e keyword solta);
  (f) a regra vence o R7 (a causa real) e tambem o R4, e o controle positivo
      nomeia DE QUAL regra ela salva o caso;
  (g) vale nas DUAS contas — hoje cai na pessoal, nada garante que siga;
  (h) regressao: financeiro (R3.5), tribunal direto (.jus.br) e no-reply comum
      seguem intactos.

Rodar: .venv/bin/python -m pytest tests/test_triage_processual.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import email_triage as et  # noqa: E402

ACC_PRO = "renato@almeida-prado.com"
ACC_PESSOAL = "renato.almeida.prado@gmail.com"


def _classify(subject, from_email, body="", conta=ACC_PESSOAL, tipo="personal"):
    """Default = conta PESSOAL, que e onde os 92 alertas reais chegam e onde o
    R7 default arquiva. Classificar em `professional` daria `silent` e os testes
    passariam sem tocar o caminho do defeito."""
    svc = et.EmailTriageService()
    return svc.classify_email_cos(
        headers={"from": f"Alerta <{from_email}>", "subject": subject},
        body_text=body, gmail_label_ids=[], account_email=conta,
        account_type=tipo, contact_id=None, enable_calibration=False,
    )


# ------------------------------------------------- (a) os casos reais ---------
# Assuntos colhidos de `copilot.emails` em 19/10 — todos estavam `archive_proposed`.

REAIS_ASTREA = [
    "Seu processo teve 1 Publicação - Escritório Almeida Prado Piccino Advogados",
    "Seu processo teve 3 movimentações - Almeida Prado Piccino Advogados Associados",
    "Seu processo teve 5 andamento(s) automático(s) - Escritório Almeida Prado",
    "Seu processo teve uma movimentação - Almeida Prado Piccino Advogados",
]

REAIS_JUSBRASIL = [
    "Nova decisão no processo",
    "Audiência no processo",
    "Verifique a situação atual do processo",
    "⚖️ Processos de sua empresa",
    "Novo processo encontrado para o seu CPF!",
    "Fictor Invest Ltda identificado em um novo processo",
]


@pytest.mark.parametrize("subject", REAIS_ASTREA)
def test_astrea_real_vira_must_read(subject):
    d = _classify(subject, "astrea@aurum.com.br")
    assert d["classification"] == "must_read", d["rule_hits"]
    assert "R3_7_processual" in d["rule_hits"]
    assert d["priority"] == 9


@pytest.mark.parametrize("subject", REAIS_JUSBRASIL)
def test_jusbrasil_real_vira_must_read(subject):
    d = _classify(subject, "nao-responda@jusbrasil.com.br")
    assert d["classification"] == "must_read", d["rule_hits"]
    assert "R3_7_processual" in d["rule_hits"]


def test_alerta_de_subdominio_de_marketing_tambem_passa():
    """`novidades@descubra.jusbrasil` TEM nome de marketing e mandou alerta
    real — e por isso que a regra olha o assunto, nao o subdominio."""
    d = _classify("FICTOR INVEST LTDA identificado em um novo processo",
                  "novidades@descubra.jusbrasil.com.br")
    assert d["classification"] == "must_read"
    assert "R3_7_processual" in d["rule_hits"]


# ----------------------------- (b)(c) CONTROLE NEGATIVO: marketing real -------
# Os 5 e-mails promocionais reais dos MESMOS remetentes. Whitelist de dominio
# solto os traria pra must_read — e seria o erro que o guard de campanha do R3.5
# ja existia pra evitar.

MARKETING_REAL = [
    ("Voltamos com novidade: dá um Confere 🔍", "nao-responda@jusbrasil.com.br"),
    ("Vai fechar negócio? Confere antes 🔍", "nao-responda@jusbrasil.com.br"),
    ("Conferir leva segundos. Descobrir depois, não.", "ola@descubra.jusbrasil.com.br"),
    ("O que você começou no Jusbrasil ainda está aqui", "ola@descubra.jusbrasil.com.br"),
    ("Renato, sua rotina jurídica merece esse próximo passo ⚖️",
     "ola@descubra.jusbrasil.com.br"),
]


@pytest.mark.parametrize("subject,sender", MARKETING_REAL)
def test_marketing_do_mesmo_remetente_NAO_passa(subject, sender):
    d = _classify(subject, sender)
    assert "R3_7_processual" not in d["rule_hits"], (
        f"marketing entrou como processual: {subject!r}")
    assert d["classification"] != "must_read" or "R3_7_processual" not in d["rule_hits"]


def test_juridico_nao_e_evidencia_de_materia():
    """🔴 A palavra do TEMA aceita o anuncio; a da MATERIA, nao.
    "sua rotina JURIDICA merece esse proximo passo" e marketing puro — foi o
    caso que impediu `juridico` de entrar em `_PROCESSUAL_EVIDENCE`."""
    assert et._has_processual_evidence("sua rotina juridica merece esse passo") is False
    assert et._has_processual_evidence("assessoria juridica especializada") is False
    assert et._has_processual_evidence("nova decisao no processo") is True


# -------------------------------------------- (d)(e) os dois lados do opt-IN --

def test_remetente_de_monitoramento_sem_materia_nao_passa():
    d = _classify("Novidades da plataforma", "nao-responda@jusbrasil.com.br")
    assert "R3_7_processual" not in d["rule_hits"]


def test_materia_processual_de_remetente_qualquer_nao_passa():
    """Nao e keyword solta: "prazo" e "decisao" aparecem em venda B2B."""
    d = _classify("Último prazo! Decisão sobre sua vaga no processo seletivo",
                  "vendas@startupqualquer.com")
    assert "R3_7_processual" not in d["rule_hits"]


def test_evidencia_pode_vir_do_CORPO_nao_so_do_assunto():
    """O Astrea usa assunto genérico e põe o teor no corpo."""
    d = _classify("Resumo semanal", "astrea@aurum.com.br",
                  body="Foi publicado o acórdão no processo 1234-56. Prazo de 15 dias.")
    assert "R3_7_processual" in d["rule_hits"]


# --------------------------------------------------- (f) a ORDEM importa ------

def test_a_regra_vence_o_R4_noreply():
    """`nao-responda@` casa R4 (no-reply, conf 0.95). Se a R3.7 nao viesse
    ANTES, o alerta seguiria arquivado — que e o defeito inteiro."""
    d = _classify("Nova decisão no processo", "nao-responda@jusbrasil.com.br")
    assert d["classification"] == "must_read"
    assert "R4" not in " ".join(d["rule_hits"])


def test_sem_a_regra_o_caso_VOLTA_a_ser_arquivado(monkeypatch):
    """Controle POSITIVO: neutralizando a evidencia, o mesmo e-mail volta a
    `archive_proposed` pelo **R7_personal_default** — que e a causa real. Prova
    que e a R3.7 que o salva, e de qual regra ela o salva
    ([[feedback_controle_positivo_pega_o_furo_real]])."""
    monkeypatch.setattr(et, "_PROCESSUAL_EVIDENCE", ())
    d = _classify("Nova decisão no processo", "nao-responda@jusbrasil.com.br")
    assert "R3_7_processual" not in d["rule_hits"]
    assert d["classification"] == "archive_proposed"
    assert d["rule_hits"] == ["R7_personal_default"], (
        f"a causa nao e a que o teste afirma: {d['rule_hits']}")


def test_na_conta_PROFISSIONAL_o_default_era_silent_nao_archive(monkeypatch):
    """Por que a 1a versao dos testes passou sem provar nada: em
    `professional` o default e `silent`. O alerta tambem nao chegava ao Renato,
    mas por outro caminho — e o assert de `archive_proposed` falharia ali."""
    monkeypatch.setattr(et, "_PROCESSUAL_EVIDENCE", ())
    d = _classify("Nova decisão no processo", "nao-responda@jusbrasil.com.br",
                  conta=ACC_PRO, tipo="professional")
    assert d["classification"] == "silent"


def test_a_regra_vale_nas_DUAS_contas():
    """O alerta hoje cai na pessoal, mas nada garante que siga assim."""
    for conta, tipo in ((ACC_PESSOAL, "personal"), (ACC_PRO, "professional")):
        d = _classify("Nova decisão no processo", "nao-responda@jusbrasil.com.br",
                      conta=conta, tipo=tipo)
        assert d["classification"] == "must_read", (conta, d["rule_hits"])
        assert "R3_7_processual" in d["rule_hits"]


# ------------------------------------------------------ (g) regressao ---------

def test_financeiro_R3_5_segue_intacto():
    d = _classify("Fatura disponível", "noreply@itau.com.br",
                  body="Sua fatura venceu. Boleto em anexo.")
    assert d["classification"] == "must_read"
    assert "R3_5_financial_gov" in d["rule_hits"]


def test_tribunal_direto_segue_pelo_gov():
    """`.jus.br` ja estava em _GOV_DOMAINS — nao regredir isso."""
    d = _classify("Intimação", "noreply@tjsp.jus.br")
    assert d["classification"] == "must_read"


def test_noreply_comum_continua_arquivando():
    d = _classify("Resumo semanal da plataforma", "no-reply@qualquercoisa.com")
    assert d["classification"] == "archive_proposed"
