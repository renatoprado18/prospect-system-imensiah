"""
Testes da reconciliação de RACI por ata (3ª peça, 29/07).

O que estes testes protegem, em ordem de importância:

  1. A REGRA (a) do Renato — item aberto que a ata não menciona sai em
     `nao_tratados` e NUNCA vira proposta. Se algum dia alguém "melhorar" o
     prompt e o parser passar a inferir fechamento por ausência, este teste cai.
  2. Retomada não vira item novo — o defeito que a peça existe pra matar.
  3. Alucinação de id não passa (o caminho de grupo já foi mordido por isso).
  4. Prazo de item novo nunca é nulo (coluna NOT NULL lá) e o motivo é honesto.

Rodar: python -m pytest tests/test_raci_ata_reconciler.py -v
"""
import asyncio
import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import raci_ata_reconciler as rec  # noqa: E402

ITEM_A = "aaaaaaaa-1111-2222-3333-444444444444"
ITEM_B = "bbbbbbbb-1111-2222-3333-444444444444"
ITEM_C = "cccccccc-1111-2222-3333-444444444444"

ITENS = [
    {"id": ITEM_A, "acao": "Contratar recepção nova", "area": "RH",
     "status": "pendente", "prazo": date(2026, 8, 15), "responsavel_r": "Jéssica",
     "reuniao_data": date(2026, 7, 1), "desta_reuniao": False},
    {"id": ITEM_B, "acao": "Modelagem financeira das metas", "area": "Financeiro",
     "status": "em_andamento", "prazo": date(2026, 8, 30), "responsavel_r": "Amadeo",
     "reuniao_data": date(2026, 7, 1), "desta_reuniao": False},
    {"id": ITEM_C, "acao": "Site Vallen — definir data de apresentação", "area": "Marketing",
     "status": "em_andamento", "prazo": date(2026, 5, 1), "responsavel_r": "Renata",
     "reuniao_data": date(2026, 4, 8), "desta_reuniao": False},
]

VALID_IDS = {ITEM_A, ITEM_B, ITEM_C}


# ── parse_llm_output ─────────────────────────────────────────────────────────
def test_parse_aceita_atualizacao_valida():
    raw = json.dumps({
        "atualizacoes": [{"item_id": ITEM_A, "action": "complete", "new_status": "concluido",
                          "notes": "recepcionista contratada", "evidencia": "a nova recepcionista começou dia 20",
                          "confianca": "alta"}],
        "novos": [],
    })
    out = rec.parse_llm_output(raw, VALID_IDS)
    assert len(out["atualizacoes"]) == 1
    assert out["atualizacoes"][0]["item_id"] == ITEM_A


def test_parse_derruba_id_alucinado():
    """O modelo inventando um UUID inédito não pode virar proposta — a linha
    apontaria pra um item que não existe e o apply falharia no cliente."""
    raw = json.dumps({
        "atualizacoes": [{"item_id": "99999999-9999-9999-9999-999999999999",
                          "action": "complete", "new_status": "concluido",
                          "evidencia": "x", "confianca": "alta"}],
        "novos": [],
    })
    assert rec.parse_llm_output(raw, VALID_IDS)["atualizacoes"] == []


def test_parse_derruba_id_numerico():
    """Regressão observada no caminho de grupo: item_id="13" em vez de UUID."""
    raw = json.dumps({"atualizacoes": [{"item_id": "13", "action": "complete",
                                        "evidencia": "x", "confianca": "alta"}], "novos": []})
    assert rec.parse_llm_output(raw, VALID_IDS)["atualizacoes"] == []


def test_parse_exige_evidencia():
    """Sem trecho da ata não há como o Renato conferir a proposta — e proposta
    que não dá pra conferir é pior que proposta nenhuma."""
    raw = json.dumps({
        "atualizacoes": [{"item_id": ITEM_A, "action": "complete", "new_status": "concluido",
                          "evidencia": "  ", "confianca": "alta"}],
        "novos": [{"acao": "Fazer X", "responsavel_r": "Fulano", "evidencia": ""}],
    })
    out = rec.parse_llm_output(raw, VALID_IDS)
    assert out["atualizacoes"] == []
    assert out["novos"] == []


def test_parse_derruba_item_novo_sem_responsavel():
    """Tema discutido sem dono não é item de RACI — vira ruído com cara de pendência."""
    raw = json.dumps({"atualizacoes": [], "novos": [
        {"acao": "Discutir posicionamento de marca", "responsavel_r": "",
         "evidencia": "falou-se sobre marca", "confianca": "media"},
        {"acao": "Enviar proposta ao fornecedor", "responsavel_r": "Jéssica",
         "evidencia": "Jéssica envia até sexta", "confianca": "alta"},
    ]})
    out = rec.parse_llm_output(raw, VALID_IDS)
    assert len(out["novos"]) == 1
    assert out["novos"][0]["acao"] == "Enviar proposta ao fornecedor"


def test_parse_resposta_sem_json_nao_explode():
    assert rec.parse_llm_output("desculpe, não consegui", VALID_IDS) == {"atualizacoes": [], "novos": []}


def test_parse_json_quebrado_nao_explode():
    assert rec.parse_llm_output('{"atualizacoes": [', VALID_IDS) == {"atualizacoes": [], "novos": []}


# ── prazo ────────────────────────────────────────────────────────────────────
def test_prazo_da_ata_vence():
    prazo, motivo = rec.resolve_prazo_novo("2026-09-10", date(2026, 7, 28), date(2026, 8, 25))
    assert prazo == date(2026, 9, 10)
    assert "definido na ata" in motivo


def test_prazo_cai_na_proxima_reuniao():
    prazo, motivo = rec.resolve_prazo_novo(None, date(2026, 7, 28), date(2026, 8, 25))
    assert prazo == date(2026, 8, 25)
    assert "próxima reunião" in motivo


def test_prazo_fallback_quando_nao_ha_proxima_reuniao():
    prazo, motivo = rec.resolve_prazo_novo(None, date(2026, 7, 28), None)
    assert prazo == date(2026, 8, 27)  # +30d
    assert "sem próxima reunião" in motivo


def test_prazo_invalido_do_llm_nao_quebra():
    """'até o fim do mês' não é data — cai na convenção em vez de estourar."""
    prazo, motivo = rec.resolve_prazo_novo("ate o fim do mes", date(2026, 7, 28), None)
    assert prazo == date(2026, 8, 27)
    assert "não definido na ata" in motivo


# ── prompt ───────────────────────────────────────────────────────────────────
def test_prompt_lista_itens_com_uuid_e_data_de_origem():
    p = rec.build_prompt("Vallen Clinic", "2026-07-28", "# Ata\nconteúdo", ITENS)
    assert ITEM_A in p and ITEM_C in p
    assert "Contratar recepção nova" in p
    assert "2026-04-08" in p  # de qual reunião o item veio
    assert "RETOMADA NÃO É ITEM NOVO" in p


def test_prompt_sem_itens_abertos():
    p = rec.build_prompt("Phisalia", "2026-07-28", "# Ata", [])
    assert "nenhum item aberto" in p


def test_prompt_corta_ata_gigante():
    p = rec.build_prompt("X", "2026-07-28", "z" * 100_000, [])
    assert len(p) < 60_000


# ── reconcile_ata (deps injetadas — sem banco, sem rede) ─────────────────────
def _fake_llm(payload: dict):
    async def _call(_prompt: str) -> str:
        return json.dumps(payload)
    return _call


@pytest.fixture
def stub_db(monkeypatch):
    """Neutraliza tudo que fala com banco; grava as propostas numa lista."""
    gravadas = []
    monkeypatch.setattr(rec, "load_reuniao", lambda rid: {
        "id": rid, "empresa_id": "emp-1", "empresa_nome": "Vallen Clinic",
        "data": date(2026, 7, 28), "ata_md": "# Ata da reunião\nTexto suficiente.",
        "transcricao": "",
    })
    monkeypatch.setattr(rec, "open_items", lambda e, r: list(ITENS))
    monkeypatch.setattr(rec, "existing_proposals_for", lambda r: 0)
    monkeypatch.setattr(rec, "next_meeting_date", lambda e, d: date(2026, 8, 25))

    def _store(**kw):
        gravadas.append(kw)
        return len(gravadas)
    monkeypatch.setattr(rec, "_store_ata_proposal", _store)
    return gravadas


def test_item_nao_mencionado_fica_aberto_e_aparece_em_nao_tratados(stub_db):
    """A REGRA (a), que é a decisão de negócio do Renato.

    A ata só fala do item B. A e C não são mencionados: não pode sair proposta
    nenhuma sobre eles — nem de fechamento, nem de cancelamento — e os dois
    precisam aparecer em `nao_tratados` pra que o silêncio seja visível.
    """
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({
        "atualizacoes": [{"item_id": ITEM_B, "action": "update_status",
                          "new_status": "em_andamento", "notes": "modelagem em curso",
                          "evidencia": "Amadeo apresentou a base de cálculo",
                          "confianca": "alta"}],
        "novos": [],
    })))

    assert out["atualizacoes"] == 1
    ids_nao_tratados = {n["item_id"] for n in out["nao_tratados"]}
    assert ids_nao_tratados == {ITEM_A, ITEM_C}
    # nenhuma proposta tocou A ou C
    tocados = {g.get("item_id") for g in stub_db}
    assert ITEM_A not in tocados and ITEM_C not in tocados


def test_nada_mencionado_nao_gera_proposta_e_lista_todos(stub_db):
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({"atualizacoes": [], "novos": []})))
    assert out["atualizacoes"] == 0 and out["novos"] == 0
    assert len(out["nao_tratados"]) == 3
    assert stub_db == []


def test_item_novo_vira_proposta_create_item_com_payload(stub_db):
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({
        "atualizacoes": [],
        "novos": [{"area": "Jurídico", "acao": "Consultar advogada sobre PJ vs CLT",
                   "responsavel_r": "Thalita", "responsavel_a": "Conselho",
                   "prazo": None, "evidencia": "ficou de consultar a Dra. Glaucia",
                   "confianca": "alta"}],
    })))
    assert out["novos"] == 1
    prop = stub_db[0]
    assert prop["action"] == "create_item"
    assert prop["item_id"] is None
    assert prop["item_payload"]["acao"] == "Consultar advogada sobre PJ vs CLT"
    # prazo resolvido pela próxima reunião, e o motivo dito na proposta
    assert prop["item_payload"]["prazo"] == "2026-08-25"
    assert "próxima reunião" in prop["item_payload"]["prazo_motivo"]


def test_idempotencia_nao_reprocessa_reuniao_ja_reconciliada(stub_db, monkeypatch):
    """A ata pode ser regerada (o Renato dispara o POST de novo quando não gosta
    do resultado). Sem esta guarda, cada regeração duplicaria a fila de revisão."""
    monkeypatch.setattr(rec, "existing_proposals_for", lambda r: 4)
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({"atualizacoes": [], "novos": []})))
    assert out["skipped"] == "ja_reconciliada"
    assert out["propostas_existentes"] == 4
    assert stub_db == []


def test_force_repropoe(stub_db, monkeypatch):
    monkeypatch.setattr(rec, "existing_proposals_for", lambda r: 4)
    out = asyncio.run(rec.reconcile_ata("r-1", force=True, call_llm=_fake_llm({
        "atualizacoes": [], "novos": [{"area": "RH", "acao": "Fazer Y", "responsavel_r": "X",
                                       "evidencia": "combinado", "confianca": "alta"}],
    })))
    assert out.get("skipped") is None
    assert out["reproposta"] is True
    assert out["novos"] == 1


def test_dry_run_nao_grava(stub_db):
    out = asyncio.run(rec.reconcile_ata("r-1", dry_run=True, call_llm=_fake_llm({
        "atualizacoes": [{"item_id": ITEM_A, "action": "complete", "new_status": "concluido",
                          "evidencia": "contratada", "confianca": "alta"}],
        "novos": [],
    })))
    assert out["dry_run"] is True
    assert stub_db == []
    assert "detalhe" in out


def test_guardrail_de_julgamento_ainda_vale_na_ata(stub_db, monkeypatch):
    """Reusa a rede determinística do caminho de grupo: item de REVISÃO só fecha
    com referência ao entregável. Numa ata isso importa igual — 'o conselho
    discutiu o documento' não é 'o documento foi revisado'."""
    item_rev = "dddddddd-1111-2222-3333-444444444444"
    monkeypatch.setattr(rec, "open_items", lambda e, r: [
        {"id": item_rev, "acao": "Revisar o questionário dos sócios", "area": "Jurídico",
         "status": "pendente", "prazo": date(2026, 8, 1), "responsavel_r": "Renato",
         "reuniao_data": date(2026, 6, 9), "desta_reuniao": False},
    ])
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({
        "atualizacoes": [{"item_id": item_rev, "action": "complete", "new_status": "concluido",
                          "evidencia": "agradecemos a todos pela reunião produtiva",
                          "confianca": "alta"}],
        "novos": [],
    })))
    # o guardrail não deixa uma menção sem o entregável fechar item de julgamento
    aplicadas = [g for g in stub_db if g.get("new_status") == "concluido"
                 and g.get("confianca") == "alta"]
    assert aplicadas == [], f"item de julgamento fechado por menção lateral: {out}"


def test_item_desta_reuniao_entra_no_contexto_mas_nunca_em_nao_tratados(stub_db, monkeypatch):
    """Regressão do dry-run contra a Vallen de 28/07.

    A primeira versão escondia do modelo os itens já cadastrados a partir da
    própria reunião — e ele re-propôs CINCO deles como "novos" (mapeamento de
    processos, modelagem financeira, PJ × CLT, recepção, vídeos de médicos).
    Reproduzia dentro da mesma reunião o empilhamento que a peça existe pra
    matar. Agora eles entram no contexto; e, como não são passivo antigo, não
    podem sair como "não tratados nesta reunião".
    """
    item_desta = "eeeeeeee-1111-2222-3333-444444444444"
    itens = list(ITENS) + [
        {"id": item_desta, "acao": "Mapeamento de Processos", "area": "Operacional",
         "status": "pendente", "prazo": date(2026, 8, 22), "responsavel_r": "Lara",
         "reuniao_data": date(2026, 7, 28), "desta_reuniao": True},
    ]
    monkeypatch.setattr(rec, "open_items", lambda e, r: itens)

    capturado = {}

    async def _call(prompt):
        capturado["prompt"] = prompt
        return json.dumps({"atualizacoes": [], "novos": []})

    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_call))

    # o item desta reunião FOI mostrado ao modelo (senão ele o duplicaria)
    assert item_desta in capturado["prompt"]
    assert "ESTA reunião" in capturado["prompt"]
    # ...mas não conta como passivo não tratado
    assert item_desta not in {n["item_id"] for n in out["nao_tratados"]}
    assert len(out["nao_tratados"]) == 3
    assert out["abertos_no_contexto"] == 4
    assert out["abertos_anteriores"] == 3


def test_item_concluido_nao_pode_ser_reaberto_pela_ata():
    """2º achado do dry-run. Item já entregue entra no contexto só pra não ser
    recriado; se o modelo tentar mexer no status dele, a proposta é podada aqui —
    o apply humano passa allow_downgrade=True e não seguraria a reabertura.
    """
    concluido = ITEM_A
    raw = json.dumps({"atualizacoes": [
        {"item_id": concluido, "action": "update_status", "new_status": "pendente",
         "notes": "voltou a ser discutido", "evidencia": "o conselho retomou o tema",
         "confianca": "alta"},
    ], "novos": []})
    out = rec.parse_llm_output(raw, VALID_IDS, concluidos={concluido})
    assert len(out["atualizacoes"]) == 1
    p = out["atualizacoes"][0]
    assert p["new_status"] is None and p["new_prazo"] is None
    assert p["action"] == "add_note"


def test_item_concluido_sem_nota_e_descartado():
    raw = json.dumps({"atualizacoes": [
        {"item_id": ITEM_A, "action": "complete", "new_status": "concluido",
         "notes": "", "evidencia": "assunto citado", "confianca": "alta"},
    ], "novos": []})
    assert rec.parse_llm_output(raw, VALID_IDS, concluidos={ITEM_A})["atualizacoes"] == []


def test_prompt_marca_concluido(stub_db, monkeypatch):
    itens = list(ITENS) + [
        {"id": "ffffffff-1111-2222-3333-444444444444", "acao": "Mapa do phase-out ao grupo",
         "area": "Operacional", "status": "concluido", "prazo": date(2026, 8, 3),
         "responsavel_r": "Amadeo", "reuniao_data": date(2026, 7, 28), "desta_reuniao": True},
    ]
    p = rec.build_prompt("Vallen", "2026-07-28", "# Ata", itens)
    assert "✅ JÁ CONCLUÍDO" in p


def test_truncagem_de_contexto_e_reportada(stub_db, monkeypatch):
    """Teto invisível lê como 'cobri tudo'. Se cortar, tem que sair no resultado."""
    muitos = [dict(ITENS[0], id=f"{i:08d}-1111-2222-3333-444444444444") for i in range(100)]
    monkeypatch.setattr(rec, "open_items", lambda e, r: muitos)
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({"atualizacoes": [], "novos": []})))
    assert out["abertos_anteriores"] == 100
    assert out["itens_truncados"] == 100 - rec.MAX_ITENS_CONTEXTO


# ── rede determinística contra empilhamento ──────────────────────────────────
def test_duplicata_pega_reformulacao_do_mesmo_compromisso():
    """O caso real do dry-run: o modelo oscilava entre atualizar 'Contratar
    recepção nova' e criar 'Contratar recepção nova para a clínica'."""
    dup = rec.duplicate_of_context("Contratar recepção nova para a clínica", ITENS)
    assert dup is not None and dup["id"] == ITEM_A


def test_duplicata_nao_confunde_acoes_diferentes():
    """Compromissos distintos que compartilham verbo não podem colidir."""
    assert rec.duplicate_of_context("Enviar aditivo contratual do phase-out", ITENS) is None
    assert rec.duplicate_of_context("Contratar consultoria de marketing digital", ITENS) is None


def test_duplicata_ignora_acao_vazia():
    assert rec.duplicate_of_context("", ITENS) is None
    assert rec.duplicate_of_context("de da os as", ITENS) is None


def test_item_novo_duplicado_e_descartado_e_reportado(stub_db):
    out = asyncio.run(rec.reconcile_ata("r-1", call_llm=_fake_llm({
        "atualizacoes": [],
        "novos": [
            {"area": "RH", "acao": "Contratar recepção nova para a clínica",
             "responsavel_r": "Jéssica", "evidencia": "combinado na reunião", "confianca": "alta"},
            {"area": "Jurídico", "acao": "Enviar aditivo contratual do phase-out",
             "responsavel_r": "Amadeo", "evidencia": "Amadeo manda a proposta", "confianca": "alta"},
        ],
    })))
    assert out["novos"] == 1
    assert len(out["novos_descartados_por_duplicata"]) == 1
    assert out["novos_descartados_por_duplicata"][0]["item_id"] == ITEM_A
    gravadas = [g for g in stub_db if g["action"] == "create_item"]
    assert len(gravadas) == 1
    assert "aditivo" in gravadas[0]["item_payload"]["acao"].lower()
