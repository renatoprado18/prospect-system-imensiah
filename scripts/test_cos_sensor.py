"""
Test cases pro CoS Sensor Agent (Stage 2).

Roda 3 cenarios com contexto mockado e imprime decisoes do agente:
1. Veridiana (10/06 15:33) — Thalita avisou cirurgia segunda.
2. Orioli (11/06 06:48) — email + cobranca pos-promessa de Meet.
3. Cadencia Assespro (11/06 11h) — cancelamento via grupo "sem pauta".

Uso:
    cd /Users/rap/prospect-system
    source .venv/bin/activate  # ou equivalente
    PYTHONPATH=app python scripts/test_cos_sensor.py

Pre-requisito: ANTHROPIC_API_KEY no env. Banco local (./dev.sh sync) NAO e
necessario — usamos mock_context, mas as tools de write (proposal/notes/draft)
vao bater no DB se acionadas. Pra dry-run, set DRY_RUN=1 (skip execute_tool).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Path setup
HERE = os.path.dirname(os.path.abspath(__file__))
APP_PATH = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_PATH)

from services.cos_sensor import CoSSensorAgent, load_autonomy_policy  # noqa: E402

DRY_RUN = bool(os.getenv("DRY_RUN", "").strip())

BRT = timezone(timedelta(hours=-3))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def case_veridiana() -> dict:
    """Contexto: Thalita Mendes (cliente Vallen) avisou via WA 10/06 15:33
    que Veridiana fara cirurgia segunda. Sensor deveria propor call sobre
    cobertura recepcao."""
    msg_ts = datetime(2026, 6, 10, 15, 33, tzinfo=BRT)
    now = datetime(2026, 6, 10, 16, 0, tzinfo=BRT)
    return {
        "now_brt": _iso(now),
        "window_min": 60,
        "msgs_recent": [
            {
                "id": 99001,
                "contact_id": 5715,
                "contact_name": "Dra. Thalita Mendes (Vallen Clinic)",
                "direcao": "incoming",
                "conteudo": (
                    "Veridiana me avisou agora que ira fazer uma cirurgia segunda "
                    "(ela disse que nao conseguiu escolher a data e acabaram de avisar). "
                    "Vamos ficar sem recepcao na segunda — como fazemos?"
                ),
                "enviado_em": _iso(msg_ts),
            }
        ],
        "group_msgs_recent": [],
        "events_upcoming": [
            {
                "id": 1234,
                "titulo": "Reuniao mensal Vallen",
                "inicio": _iso(now + timedelta(days=3)),
                "fim": _iso(now + timedelta(days=3, hours=1)),
                "local": "",
            }
        ],
        "proposals_open": [],
        "scheduled_open": [],
        "raci_critical": [],
    }


def case_orioli() -> dict:
    """Contexto: Felipe Orioli mandou email + 'ola bom dia' 11/06 06:48
    fechando ciclo de Renato prometeu Meet pra sex 12/06 10h BRT.
    Sensor deveria add_calendar_event com Meet (auto_cond confirmed_via_wa=True).
    """
    msg_ts = datetime(2026, 6, 11, 6, 48, tzinfo=BRT)
    promise_ts = datetime(2026, 6, 10, 18, 30, tzinfo=BRT)
    now = datetime(2026, 6, 11, 7, 0, tzinfo=BRT)
    meeting_start = datetime(2026, 6, 12, 10, 0, tzinfo=BRT)
    return {
        "now_brt": _iso(now),
        "window_min": 60,
        "msgs_recent": [
            # Promessa anterior do Renato (fora da janela mas relevante p/ contexto)
            {
                "id": 99100,
                "contact_id": 5800,
                "contact_name": "Felipe Orioli",
                "direcao": "outgoing",
                "conteudo": "Beleza Felipe, mando o Meet pra sexta 10h.",
                "enviado_em": _iso(promise_ts),
            },
            {
                "id": 99101,
                "contact_id": 5800,
                "contact_name": "Felipe Orioli",
                "direcao": "incoming",
                "conteudo": "Ola bom dia",
                "enviado_em": _iso(msg_ts),
            },
            {
                "id": 99102,
                "contact_id": 5800,
                "contact_name": "Felipe Orioli",
                "direcao": "incoming",
                "conteudo": "Felipe.orioli@gmail.com",
                "enviado_em": _iso(msg_ts + timedelta(minutes=1)),
            },
        ],
        "group_msgs_recent": [],
        "events_upcoming": [],  # ainda sem evento
        "proposals_open": [],
        "scheduled_open": [],
        "raci_critical": [],
        "_hint_para_o_modelo": (
            "Renato prometeu Meet pra sex 12/06 10h BRT. Felipe pediu o link essa manha. "
            f"start_iso esperado: {_iso(meeting_start)}"
        ),
    }


def case_assespro() -> dict:
    """Contexto: msg do grupo CC Federacao Assespro SP 11/06 ~10:30 BRT
    cancelando reuniao das 11h por 'sem pauta'. Sensor deveria detectar e
    propor cancelamento / record_observation."""
    msg_ts = datetime(2026, 6, 11, 10, 30, tzinfo=BRT)
    now = datetime(2026, 6, 11, 10, 45, tzinfo=BRT)
    meeting_start = datetime(2026, 6, 11, 11, 0, tzinfo=BRT)
    return {
        "now_brt": _iso(now),
        "window_min": 60,
        "msgs_recent": [],
        "group_msgs_recent": [
            {
                "id": 99200,
                "group_jid": "120363@grupo.assespro.test",
                "sender_name": "Coord Federacao Assespro SP",
                "content": "Pessoal, reuniao das 11h fica pra prox semana — estamos sem pauta hoje. Abracos.",
                "timestamp": _iso(msg_ts),
            }
        ],
        "events_upcoming": [
            {
                "id": 5678,
                "titulo": "Cadencia Federacao Assespro SP",
                "inicio": _iso(meeting_start),
                "fim": _iso(meeting_start + timedelta(hours=1)),
                "local": "Google Meet",
            }
        ],
        "proposals_open": [],
        "scheduled_open": [],
        "raci_critical": [],
    }


def run_case(name: str, ctx: dict) -> None:
    print(f"\n{'='*70}")
    print(f"CASE: {name}")
    print(f"{'='*70}")
    agent = CoSSensorAgent(mock_context=ctx)
    if DRY_RUN:
        # Monkey-patch execute_sensor_tool to no-op
        import services.cos_sensor as cs

        def _noop(tool_name, tool_input, policy):
            return {"success": True, "result": {"dry_run": True, "tool": tool_name, "input": tool_input}, "audit_log_id": None}

        cs.execute_sensor_tool = _noop
        print("[DRY_RUN=1] tools wont actually execute")

    result = agent.tick()
    print(f"\nStatus: {result.get('status')}")
    print(f"Iterations: {result.get('iterations')}")
    print(f"Tool count: {result.get('tool_count')}")
    print(f"Cost: ${result.get('cost_usd', 0):.4f}")
    print(f"Duration: {result.get('duration_ms')}ms")
    print(f"Final text: {result.get('final_text')}")
    print(f"\nTool calls:")
    for tc in result.get("tool_calls") or []:
        print(f"  - {tc['tool']}({json.dumps(tc['input'], default=str, ensure_ascii=False)[:250]})")
        print(f"    -> {json.dumps(tc['result'], default=str, ensure_ascii=False)[:250]}")
    if result.get("error_message"):
        print(f"\nERROR: {result.get('error_message')}")


def main():
    print(f"Autonomy policy carregada:")
    for k, v in load_autonomy_policy().items():
        print(f"  {k}: {v}")

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\nERRO: ANTHROPIC_API_KEY nao setada. Abortando.")
        sys.exit(1)

    run_case("Veridiana — Thalita avisou cirurgia segunda", case_veridiana())
    run_case("Orioli — promessa Meet + cobranca pelo email", case_orioli())
    run_case("Assespro — reuniao cancelada via grupo (sem pauta)", case_assespro())


if __name__ == "__main__":
    main()
