"""
Smoke test — P6 Diligente, Fase 2.

Cobertura:
1. Migration 008 aplicada (coluna escalated_at + index)
2. tick_one: intent open + Claude mock retorna tool_use -> in_progress + step
3. tick_one: intent blocked -> noop (escalation cuida)
4. tick_one skip: intent atualizado < 10min -> pula
5. escalate_blocked_intents: blocked + velho + nao escalado -> escala + marca
6. escalate_blocked_intents skip: ja escalado -> nao escala de novo
7. manage_intent (via _tool_execute_action): cada uma das 4 actions

Mocks:
- send_intel_notification (evita WhatsApp real)
- httpx.AsyncClient.post (evita Claude real)

Rodar:
    USE_LOCAL_DB=1 python -m pytest tests/test_diligente_fase_2.py -v
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# Bootstrap path pro mesmo padrao dos outros testes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# Force banco local — ver test_diligente_fase_1.py pra contexto
os.environ["USE_LOCAL_DB"] = "1"
os.environ.pop("VERCEL", None)
os.environ.pop("VERCEL_ENV", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-mocks")

from app.services.agent_intents import (  # noqa: E402
    open_intent,
    update_intent,
    cancel_intent,
    get_intent,
    get_blocked_intents_to_escalate,
    mark_escalated,
    escalate_blocked_intents,
)
from app.database import get_db  # noqa: E402


# ==================== MIGRATION CHECK ====================


def test_migration_008_aplicada():
    """A coluna escalated_at + index existem."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'agent_intents' AND column_name = 'escalated_at'
            """
        )
        col = cursor.fetchone()
        assert col, "coluna escalated_at nao existe"

        cursor.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'agent_intents' AND indexname = 'idx_agent_intents_escalated'
            """
        )
        idx = cursor.fetchone()
        assert idx, "index idx_agent_intents_escalated nao existe"


# ==================== HELPERS ====================


@pytest.fixture
def cleanup_intents():
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


def _force_updated_at(intent_id: int, when: datetime):
    """Backdoor pra simular intent velho — modifica updated_at direto no banco."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agent_intents SET updated_at = %s WHERE id = %s",
            (when, intent_id),
        )
        conn.commit()


# ==================== ESCALATE_BLOCKED_INTENTS ====================


def test_get_blocked_to_escalate_filtra_corretamente(cleanup_intents):
    """SELECT respeita os 3 filtros: blocked + escalated_at NULL + velho."""
    # Caso 1: blocked + velho + nao escalado -> deve aparecer
    a = open_intent(intent_text="A — blocked velho", status="blocked", blocker="motivo A")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=90))

    # Caso 2: blocked + recente -> NAO deve aparecer (ainda dentro do threshold)
    b = open_intent(intent_text="B — blocked recente", status="blocked", blocker="motivo B")
    cleanup_intents.append(b["id"])
    # default updated_at = NOW() — esta recente

    # Caso 3: blocked + velho + JA escalado -> NAO deve aparecer
    c = open_intent(intent_text="C — blocked ja escalado", status="blocked", blocker="motivo C")
    cleanup_intents.append(c["id"])
    _force_updated_at(c["id"], datetime.utcnow() - timedelta(minutes=90))
    mark_escalated(c["id"])

    # Caso 4: open velho -> NAO deve aparecer (nao e blocked)
    d = open_intent(intent_text="D — open velho", status="open")
    cleanup_intents.append(d["id"])
    _force_updated_at(d["id"], datetime.utcnow() - timedelta(minutes=90))

    rows = get_blocked_intents_to_escalate(age_minutes=60)
    ids = {r["id"] for r in rows}
    assert a["id"] in ids, "blocked velho nao escalado deveria estar"
    assert b["id"] not in ids, "blocked recente nao deveria estar"
    assert c["id"] not in ids, "ja escalado nao deveria estar"
    assert d["id"] not in ids, "open nao deveria estar"


@pytest.mark.asyncio
async def test_escalate_blocked_envia_e_marca(cleanup_intents):
    a = open_intent(intent_text="travado faz tempo", status="blocked", blocker="motivo X")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=90))

    fake_send = AsyncMock(return_value=True)
    with patch("services.intel_bot.send_intel_notification", fake_send):
        escalated = await escalate_blocked_intents()

    assert any(e["id"] == a["id"] for e in escalated), "deveria ter escalado o intent A"
    fake_send.assert_called()
    # Mensagem contem o id e blocker
    sent_text = fake_send.call_args[0][0]
    assert f"#{a['id']}" in sent_text
    assert "motivo X" in sent_text

    # escalated_at agora preenchido
    refreshed = get_intent(a["id"])
    assert refreshed["escalated_at"] is not None


@pytest.mark.asyncio
async def test_escalate_skip_se_ja_escalado(cleanup_intents):
    a = open_intent(intent_text="ja escalei isso", status="blocked", blocker="motivo Y")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=90))
    mark_escalated(a["id"])

    fake_send = AsyncMock(return_value=True)
    with patch("services.intel_bot.send_intel_notification", fake_send):
        escalated = await escalate_blocked_intents()

    assert all(e["id"] != a["id"] for e in escalated), "nao deveria re-escalar"
    fake_send.assert_not_called()


def test_escalated_at_reseta_quando_status_destravado(cleanup_intents):
    """Quando intent destrava (status sai de blocked), escalated_at vira NULL.
    Garantia que ao re-travar com novo blocker, possa ser escalado de novo."""
    a = open_intent(intent_text="destrava esse", status="blocked", blocker="motivo Z")
    cleanup_intents.append(a["id"])
    mark_escalated(a["id"])

    pre = get_intent(a["id"])
    assert pre["escalated_at"] is not None

    # Destrava (passa pra in_progress)
    update_intent(a["id"], status="in_progress")
    pos = get_intent(a["id"])
    assert pos["escalated_at"] is None, "escalated_at deveria ter sido limpo"


# ==================== TICK_ONE ====================


def _make_anthropic_response(content_blocks, stop_reason="end_turn", status_code=200):
    """Helper pra construir mock de response.json() do Claude."""
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.json = MagicMock(return_value={
        "content": content_blocks,
        "stop_reason": stop_reason,
    })
    fake_resp.text = json.dumps({"content": content_blocks})
    return fake_resp


@pytest.mark.asyncio
async def test_tick_one_skip_recently_updated(cleanup_intents):
    from app.services.agent_intents_tick import tick_one
    a = open_intent(intent_text="acabou de ser atualizado")
    cleanup_intents.append(a["id"])
    # default updated_at e NOW() — esta dentro de 10min

    result = await tick_one(get_intent(a["id"]))
    assert result["action_taken"] == "skip"
    assert result["reason"] == "updated_recently"


@pytest.mark.asyncio
async def test_tick_one_blocked_status_e_filtrado_em_tick_all(cleanup_intents):
    """tick_all so pega get_open_intents (open|in_progress); blocked nao entra.
    Se chamar tick_one direto com blocked, ele tambem trata como noop."""
    from app.services.agent_intents_tick import tick_one, tick_all
    a = open_intent(intent_text="travado", status="blocked", blocker="motivo")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=30))

    # Direct tick_one com blocked -> noop
    result = await tick_one(get_intent(a["id"]))
    assert result["action_taken"] == "noop"


@pytest.mark.asyncio
async def test_tick_one_claude_chama_manage_intent_mark_step(cleanup_intents):
    """Claude retorna tool_use manage_intent action=mark_step + details ->
    tick aplica append_step + status=in_progress."""
    from app.services.agent_intents_tick import tick_one

    a = open_intent(intent_text="vincular tarefas LinkedIn ao editorial")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=30))

    # Claude responde com 1 tool_use de manage_intent + texto vazio depois (stop)
    tool_block = {
        "type": "tool_use",
        "id": "toolu_test123",
        "name": "manage_intent",
        "input": {"action": "mark_step", "details": "Vinculei 3 das 5 tarefas, faltam 2."},
    }
    response_after_tool = _make_anthropic_response(
        [{"type": "text", "text": "ok progredi"}],
        stop_reason="end_turn",
    )
    response_with_tool = _make_anthropic_response([tool_block], stop_reason="tool_use")

    fake_post = AsyncMock(side_effect=[response_with_tool, response_after_tool])
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = fake_post

    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await tick_one(get_intent(a["id"]))

    assert result["action_taken"] == "mark_step", f"esperava mark_step, veio {result}"
    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "in_progress"
    steps = refreshed["steps_done"]
    if isinstance(steps, str):
        steps = json.loads(steps)
    assert len(steps) >= 1
    # Step contem o details que veio do mock
    found = any(
        (isinstance(s, dict) and "Vinculei" in str(s.get("details", "")))
        for s in steps
    )
    assert found, f"step nao registrou detalhes esperados: {steps}"


@pytest.mark.asyncio
async def test_tick_one_claude_responde_so_texto_marca_blocked(cleanup_intents):
    """Quando Claude nao chama tool nenhuma e so responde texto, tick marca blocked."""
    from app.services.agent_intents_tick import tick_one

    a = open_intent(intent_text="fazer algo dificil")
    cleanup_intents.append(a["id"])
    _force_updated_at(a["id"], datetime.utcnow() - timedelta(minutes=30))

    response_no_tool = _make_anthropic_response(
        [{"type": "text", "text": "Nao consigo avancar sem mais info do user."}],
        stop_reason="end_turn",
    )
    fake_post = AsyncMock(return_value=response_no_tool)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = fake_post

    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await tick_one(get_intent(a["id"]))

    assert result["action_taken"] == "auto_blocked", f"esperava auto_blocked, veio {result}"
    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "blocked"
    assert "Nao consigo avancar" in (refreshed["blocker"] or "")


# ==================== MANAGE_INTENT TOOL ====================


@pytest.mark.asyncio
async def test_manage_intent_mark_step(cleanup_intents):
    from app.services.intel_bot import _tool_execute_action
    a = open_intent(intent_text="testando manage_intent")
    cleanup_intents.append(a["id"])

    out = await _tool_execute_action("manage_intent", {
        "intent_id": a["id"],
        "action": "mark_step",
        "details": "fiz passo X",
    })
    parsed = json.loads(out)
    assert parsed.get("sucesso") is True
    assert parsed.get("status") == "in_progress"

    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "in_progress"


@pytest.mark.asyncio
async def test_manage_intent_mark_blocked(cleanup_intents):
    from app.services.intel_bot import _tool_execute_action
    a = open_intent(intent_text="vai travar")
    cleanup_intents.append(a["id"])

    out = await _tool_execute_action("manage_intent", {
        "intent_id": a["id"],
        "action": "mark_blocked",
        "details": "falta integracao Y",
    })
    parsed = json.loads(out)
    assert parsed.get("sucesso") is True
    assert parsed.get("status") == "blocked"

    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "blocked"
    assert "falta integracao Y" in refreshed["blocker"]


@pytest.mark.asyncio
async def test_manage_intent_mark_completed(cleanup_intents):
    from app.services.intel_bot import _tool_execute_action
    a = open_intent(intent_text="vai completar")
    cleanup_intents.append(a["id"])

    out = await _tool_execute_action("manage_intent", {
        "intent_id": a["id"],
        "action": "mark_completed",
    })
    parsed = json.loads(out)
    assert parsed.get("sucesso") is True
    assert parsed.get("status") == "completed"

    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "completed"
    assert refreshed["completed_at"] is not None


@pytest.mark.asyncio
async def test_manage_intent_cancel(cleanup_intents):
    from app.services.intel_bot import _tool_execute_action
    a = open_intent(intent_text="vai cancelar")
    cleanup_intents.append(a["id"])

    out = await _tool_execute_action("manage_intent", {
        "intent_id": a["id"],
        "action": "cancel",
    })
    parsed = json.loads(out)
    assert parsed.get("sucesso") is True
    assert parsed.get("status") == "cancelled"

    refreshed = get_intent(a["id"])
    assert refreshed["status"] == "cancelled"


@pytest.mark.asyncio
async def test_manage_intent_action_invalida():
    from app.services.intel_bot import _tool_execute_action
    out = await _tool_execute_action("manage_intent", {
        "intent_id": 999999999,
        "action": "garbage_action",
    })
    parsed = json.loads(out)
    assert "erro" in parsed
    assert "garbage_action" in parsed["erro"]


@pytest.mark.asyncio
async def test_manage_intent_sem_intent_id():
    from app.services.intel_bot import _tool_execute_action
    out = await _tool_execute_action("manage_intent", {
        "action": "mark_step",
        "details": "sem id",
    })
    parsed = json.loads(out)
    assert "erro" in parsed
    assert "intent_id" in parsed["erro"]


@pytest.mark.asyncio
async def test_manage_intent_mark_step_sem_details():
    from app.services.intel_bot import _tool_execute_action
    a = open_intent(intent_text="deve falhar")
    try:
        out = await _tool_execute_action("manage_intent", {
            "intent_id": a["id"],
            "action": "mark_step",
        })
        parsed = json.loads(out)
        assert "erro" in parsed
        assert "details" in parsed["erro"]
    finally:
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM agent_intents WHERE id = %s", (a["id"],))
            conn.commit()
