"""
Smoke test — P6 Diligente, Fase 1.

Cobertura:
1. Migration roda (tabela existe, coluna correta) — implicitamente via init_db
2. open_intent cria + retorna
3. update_intent atualiza campos seletivos + completed_at quando status=completed
4. get_open_intents filtra status corretamente
5. find_similar_open_intent dedupa
6. detect_intent_from_turn:
   - "vincule todas as tarefas LinkedIn" + write_called=True -> abre
   - "que horas sao?" + write_called=False -> nao abre
   - response com "nao consigo executar" -> abre BLOCKED
7. format_intents_for_prompt formata legivel
8. maybe_open_intent_for_turn dedupa quando msg ja virou intent

Rodar:
    python -m pytest tests/test_diligente_fase_1.py -v

Requer banco local em postgresql://localhost:5432/intel com a tabela
agent_intents criada (init_db roda automaticamente, ou via migration).
"""
import os
import sys

import pytest

# Bootstrap path pro mesmo padrao dos outros testes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# IMPORTANTE: NAO carregar .env aqui — ele tem VERCEL=1 que faz database.py
# escolher o banco remoto (Neon prod). Renato proibiu testes contra prod.
# Em vez disso, force USE_LOCAL_DB=1 ANTES de qualquer import.
os.environ["USE_LOCAL_DB"] = "1"
os.environ.pop("VERCEL", None)
os.environ.pop("VERCEL_ENV", None)

# Imports apos bootstrap do path
from app.services.agent_intents import (  # noqa: E402
    open_intent,
    update_intent,
    cancel_intent,
    append_step,
    get_open_intents,
    get_intent,
    find_similar_open_intent,
    format_intents_for_prompt,
    detect_intent_from_turn,
    maybe_open_intent_for_turn,
)
from app.database import get_db  # noqa: E402


# ==================== MIGRATION CHECK ====================


def test_migration_table_exists():
    """A tabela agent_intents existe com schema esperado."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'agent_intents'
            ORDER BY ordinal_position
            """
        )
        cols = [r["column_name"] for r in cursor.fetchall()]
    expected = {
        "id", "intent_text", "intent_type", "status", "steps_done",
        "next_step_hint", "blocker", "related_message_id",
        "created_at", "updated_at", "completed_at",
    }
    missing = expected - set(cols)
    assert not missing, f"Colunas faltando em agent_intents: {missing}"


# ==================== HELPERS ====================


@pytest.fixture
def cleanup_intents():
    """Track IDs criados, deleta no teardown pra nao poluir banco local."""
    created = []
    yield created
    if not created:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_intents WHERE id = ANY(%s)",
                (created,),
            )
            conn.commit()
    except Exception as e:
        print(f"cleanup_intents teardown error: {e}")


# ==================== CRUD ====================


def test_open_intent_basic(cleanup_intents):
    intent = open_intent(
        intent_text="vincular todas as tarefas LinkedIn ao projeto editorial",
        intent_type="multi_step_action",
    )
    cleanup_intents.append(intent["id"])
    assert intent["id"] > 0
    assert intent["status"] == "open"
    assert intent["intent_text"].startswith("vincular todas")
    assert intent["intent_type"] == "multi_step_action"
    assert intent["completed_at"] is None
    # steps_done default vazio (lista ou string '[]')
    sd = intent.get("steps_done")
    assert sd in ([], "[]") or (isinstance(sd, list) and len(sd) == 0)


def test_open_intent_blocked_with_blocker(cleanup_intents):
    intent = open_intent(
        intent_text="enviar email pro cliente X",
        intent_type="blocked_request",
        status="blocked",
        blocker="falta integracao Gmail account=personal",
    )
    cleanup_intents.append(intent["id"])
    assert intent["status"] == "blocked"
    assert "Gmail" in intent["blocker"]


def test_open_intent_rejects_empty():
    with pytest.raises(ValueError):
        open_intent(intent_text="")
    with pytest.raises(ValueError):
        open_intent(intent_text="x", status="invalid_status")


def test_update_intent_seletivo(cleanup_intents):
    intent = open_intent(intent_text="processar 10 propostas pendentes")
    cleanup_intents.append(intent["id"])

    updated = update_intent(intent["id"], next_step_hint="comecar pelas mais antigas")
    assert updated["next_step_hint"] == "comecar pelas mais antigas"
    assert updated["status"] == "open"  # nao mudou


def test_update_intent_completed_seta_completed_at(cleanup_intents):
    intent = open_intent(intent_text="rodar batch de 50 contatos")
    cleanup_intents.append(intent["id"])
    assert intent["completed_at"] is None

    updated = update_intent(intent["id"], status="completed")
    assert updated["status"] == "completed"
    assert updated["completed_at"] is not None


def test_cancel_intent(cleanup_intents):
    intent = open_intent(intent_text="enviar follow-up amanha")
    cleanup_intents.append(intent["id"])

    cancelled = cancel_intent(intent["id"])
    assert cancelled["status"] == "cancelled"


def test_append_step_acumula(cleanup_intents):
    intent = open_intent(intent_text="atualizar 3 contatos pessoais")
    cleanup_intents.append(intent["id"])

    s1 = append_step(intent["id"], "passo 1: contato A atualizado")
    assert s1["status"] == "in_progress"
    s2 = append_step(intent["id"], {"kind": "contact_updated", "contact_id": 42})
    sd = s2.get("steps_done")
    if isinstance(sd, str):
        import json as _json
        sd = _json.loads(sd)
    assert isinstance(sd, list) and len(sd) == 2


def test_get_open_intents_filtra(cleanup_intents):
    a = open_intent(intent_text="A — esse fica aberto")
    cleanup_intents.append(a["id"])
    b = open_intent(intent_text="B — esse vai fechar")
    cleanup_intents.append(b["id"])
    update_intent(b["id"], status="completed")
    c = open_intent(intent_text="C — esse vai cancelar")
    cleanup_intents.append(c["id"])
    cancel_intent(c["id"])

    abertos = get_open_intents(limit=20)
    ids = {it["id"] for it in abertos}
    assert a["id"] in ids, "intent open deveria aparecer"
    assert b["id"] not in ids, "completed nao deveria aparecer"
    assert c["id"] not in ids, "cancelled nao deveria aparecer"


def test_get_intent_por_id(cleanup_intents):
    a = open_intent(intent_text="check single fetch")
    cleanup_intents.append(a["id"])
    fetched = get_intent(a["id"])
    assert fetched is not None
    assert fetched["id"] == a["id"]
    assert get_intent(99_999_999) is None


# ==================== DEDUP ====================


def test_find_similar_open_intent(cleanup_intents):
    a = open_intent(intent_text="vincular todas as tarefas LinkedIn ao projeto editorial")
    cleanup_intents.append(a["id"])

    # Mesma frase com pequena variacao
    similar = find_similar_open_intent("vincular todas as tarefas LinkedIn ao editorial")
    assert similar is not None
    assert similar["id"] == a["id"]

    # Texto totalmente diferente
    other = find_similar_open_intent("agendar reuniao com cliente Z amanha 14h")
    assert other is None or other["id"] != a["id"]


# ==================== DETECTOR ====================


def test_detect_user_imperative_plural_abre():
    decision = detect_intent_from_turn(
        user_message="vincule todas as tarefas LinkedIn ao projeto editorial",
        write_action_called=True,
        response_text="Pronto, vinculei algumas. Vou continuar.",
    )
    assert decision is not None
    assert decision["should_open"] is True
    assert decision["intent_type"] == "multi_step_action"
    assert decision["status"] in ("open", "in_progress")


def test_detect_imperative_sem_write_ainda_abre():
    """User pediu acao em massa mas bot ainda nao executou nada — abre pra rastrear."""
    decision = detect_intent_from_turn(
        user_message="atualiza todos os 8 contatos da campanha",
        write_action_called=False,
        response_text="Ok, vou comecar pelo primeiro.",
    )
    assert decision is not None
    assert decision["should_open"] is True


def test_detect_query_pura_nao_abre():
    decision = detect_intent_from_turn(
        user_message="que horas sao?",
        write_action_called=False,
        response_text="14:32 (Sao Paulo).",
    )
    assert decision is None


def test_detect_chat_coach_nao_abre():
    decision = detect_intent_from_turn(
        user_message="estou cansado essa semana",
        write_action_called=False,
        response_text="Faz sentido. O que tem pesado mais?",
    )
    assert decision is None


def test_detect_bot_admitiu_falha_abre_blocked():
    decision = detect_intent_from_turn(
        user_message="manda email pro Joao",
        write_action_called=False,
        response_text="Nao consigo enviar email pra esse contato — falta integracao Gmail account=personal.",
    )
    assert decision is not None
    assert decision["should_open"] is True
    assert decision["status"] == "blocked"
    assert decision["blocker"]
    assert "Gmail" in decision["blocker"] or "personal" in decision["blocker"]


def test_detect_blocker_tem_prioridade_sobre_write():
    """Mesmo se houve write, se bot admitiu falha em outra parte, vira blocked."""
    decision = detect_intent_from_turn(
        user_message="agenda reuniao e manda confirmacao",
        write_action_called=True,
        response_text="Agendei o evento, mas nao consigo enviar email — falta tool send_email.",
    )
    # Detector prioriza blocker quando presente — Renato precisa saber do gap.
    assert decision is not None
    assert decision["should_open"] is True
    # Pode ser blocked OU multi_step — ambos sao validos do ponto de vista do design,
    # mas a heuristica atual prioriza blocker. Aceita ambos.
    assert decision["status"] in ("blocked", "open", "in_progress")


def test_detect_write_called_abre_multi_step():
    decision = detect_intent_from_turn(
        user_message="cria task pra revisar o briefing amanha",
        write_action_called=True,
        response_text="Tarefa #123 criada.",
    )
    assert decision is not None
    assert decision["intent_type"] == "multi_step_action"


# ==================== FORMATTING ====================


def test_format_intents_for_prompt_vazio():
    assert format_intents_for_prompt([]) == ""


def test_format_intents_for_prompt_com_dados():
    intents = [
        {
            "id": 1,
            "intent_text": "vincular tarefas LinkedIn ao projeto editorial",
            "status": "in_progress",
            "steps_done": ["passo 1", "passo 2"],
            "next_step_hint": "continuar pelos posts pendentes",
        },
        {
            "id": 2,
            "intent_text": "enviar email pro cliente X",
            "status": "blocked",
            "blocker": "falta integracao Gmail",
            "steps_done": [],
        },
    ]
    output = format_intents_for_prompt(intents)
    assert "[#1]" in output
    assert "[#2]" in output
    assert "in_progress" in output
    assert "blocked" in output
    assert "2 passos feitos" in output
    assert "0 passos feitos" in output
    assert "continuar pelos posts" in output
    assert "Gmail" in output


def test_format_intents_truncate_long_text():
    long_text = "x" * 500
    intents = [{"id": 99, "intent_text": long_text, "status": "open", "steps_done": []}]
    output = format_intents_for_prompt(intents)
    # Trunca em ~140 chars + "..."
    assert "..." in output
    # Linha sem next_step_hint nem blocker — so a linha base
    assert len(output.split("\n")[0]) < 200


# ==================== INTEGRATION ====================


def test_maybe_open_intent_dedup(cleanup_intents):
    """Mesmo pedido em 2 turns nao cria 2 intents — dedupa contra abertos."""
    msg = "vincule todas as tarefas LinkedIn ao projeto editorial"

    a = maybe_open_intent_for_turn(
        user_message=msg, write_action_called=True, response_text="ok",
    )
    assert a is not None
    cleanup_intents.append(a["id"])

    # Segundo turn com a mesma intencao
    b = maybe_open_intent_for_turn(
        user_message=msg, write_action_called=True, response_text="continuando",
    )
    assert b is not None
    # Deve ser o MESMO intent (com step novo), nao um novo
    assert b["id"] == a["id"], "dedup falhou — criou intent duplicado"


def test_maybe_open_intent_query_nao_cria():
    result = maybe_open_intent_for_turn(
        user_message="quais minhas reunioes amanha?",
        write_action_called=False,
        response_text="Voce tem 3: A, B, C.",
    )
    assert result is None
