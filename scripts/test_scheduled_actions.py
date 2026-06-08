"""
Smoke test pra scheduled_actions V0.

Roda contra DB local (USE_LOCAL_DB=1). NAO faz envio WA real — mocka
o Evolution client via monkey-patch pra evitar poluir WhatsApp do Renato.

Uso:
    USE_LOCAL_DB=1 python scripts/test_scheduled_actions.py

Exit code 0 se passou, 1 se falhou.
"""
import asyncio
import os
import sys
import time
from datetime import timedelta

# Setup path pra importar do app/
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.abspath(os.path.join(THIS_DIR, "..", "app"))
sys.path.insert(0, APP_DIR)

# Force local DB
os.environ["USE_LOCAL_DB"] = "1"

# Carrega .env se existir (pra ANTHROPIC etc nao serem necessarios, mas safe)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(THIS_DIR, "..", ".env"))
except ImportError:
    pass

DEDUP_KEY = "test_smoke_scheduled_actions_v0"


def run_migration():
    """Aplica migration 016 localmente. Idempotente (CREATE TABLE IF NOT EXISTS)."""
    from database import get_db
    mig_path = os.path.join(THIS_DIR, "migrations", "016_scheduled_actions.sql")
    with open(mig_path, "r") as f:
        sql = f.read()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        conn.commit()
    print("[OK] Migration 016 aplicada")


def cleanup():
    """Remove rows de teste."""
    from database import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_actions WHERE dedup_key = %s", (DEDUP_KEY,))
        conn.commit()


async def main():
    # ============== Setup ==============
    run_migration()
    cleanup()  # Garante estado limpo

    from services.scheduled_actions import (
        schedule_wa, process_due, list_pending, cancel, get_audit_log
    )
    from services.tz import now_utc
    from database import get_db
    import services.scheduled_actions as sa_module

    # ============== Mock Evolution e notification ==============
    sent_calls = []
    notify_calls = []

    async def fake_execute_wa_send(row):
        sent_calls.append(row["id"])
        return {"ok": True, "msg_id": "MOCKED_MSG_ID_123", "status": "PENDING"}

    async def fake_notify_renato(success, row, result):
        notify_calls.append((success, row["id"], result.get("msg_id")))

    sa_module._execute_wa_send = fake_execute_wa_send
    sa_module._notify_renato = fake_notify_renato

    # ============== Test 1: schedule_wa ==============
    when = now_utc() + timedelta(seconds=3)  # bem proximo (3s)
    id1 = schedule_wa(
        instance="rap-whatsapp",
        number="5511999999999",
        text="TEST — IGNORE smoke test scheduled_actions",
        scheduled_for=when,
        source="smoke test 08/06/26",
        dedup_key=DEDUP_KEY,
        created_by="test_script",
    )
    assert id1, f"schedule_wa retornou {id1}"
    print(f"[OK] schedule_wa criou id={id1}")

    # ============== Test 2: idempotency ==============
    id2 = schedule_wa(
        instance="rap-whatsapp",
        number="5511999999999",
        text="TEST — IGNORE (segunda tentativa)",
        scheduled_for=when,
        source="smoke test 08/06/26 (dup)",
        dedup_key=DEDUP_KEY,
        created_by="test_script",
    )
    assert id1 == id2, f"esperado mesmo id ({id1}), recebi {id2}"
    print(f"[OK] dedup_key respeitada: retornou mesmo id={id2}")

    # ============== Test 3: list_pending mostra a row ==============
    pendings = list_pending(limit=100)
    ids = [r["id"] for r in pendings]
    assert id1 in ids, f"id1={id1} nao apareceu em list_pending (vi {ids[:10]})"
    print(f"[OK] list_pending retornou {len(pendings)} rows, contendo id={id1}")

    # ============== Test 4: process_due antes do tempo ==============
    # Logo apos schedule, scheduled_for ainda no futuro -> nao deve processar
    result_early = await process_due()
    assert result_early["processed"] == 0, f"esperado 0 processed antes do tempo, recebi {result_early}"
    print(f"[OK] process_due cedo nao processou: {result_early}")

    # ============== Test 5: espera + process_due ==============
    print("[..] Esperando 5s pra scheduled_for ficar no passado...")
    time.sleep(5)
    result = await process_due()
    print(f"[..] process_due result: {result}")
    assert result["sent"] >= 1, f"esperado >=1 sent, recebi {result}"
    assert id1 in sent_calls, f"id1={id1} nao foi enviado (sent_calls={sent_calls})"
    print(f"[OK] process_due enviou id={id1}")

    # ============== Test 6: row virou status='sent' ==============
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, result_msg_id, executed_at, attempts FROM scheduled_actions WHERE id = %s",
            (id1,),
        )
        row = cursor.fetchone()
    assert row, f"row id={id1} sumiu do DB"
    assert row["status"] == "sent", f"esperado status=sent, recebi {row['status']}"
    assert row["result_msg_id"] == "MOCKED_MSG_ID_123", f"msg_id errado: {row['result_msg_id']}"
    assert row["executed_at"] is not None, "executed_at nao foi setado"
    print(f"[OK] row id={id1} status=sent, result_msg_id={row['result_msg_id']}")

    # ============== Test 7: notificacao foi disparada ==============
    assert any(success and rid == id1 for (success, rid, _) in notify_calls), (
        f"send_intel_notification nao foi chamada (notify_calls={notify_calls})"
    )
    print(f"[OK] notify_renato foi chamado com success=True")

    # ============== Test 8: process_due segunda vez nao reenvia ==============
    sent_calls.clear()
    result_again = await process_due()
    assert id1 not in sent_calls, f"reprocessou id={id1} (idempotency quebrada!)"
    print(f"[OK] segunda process_due nao reenvia: {result_again}")

    # ============== Test 9: cancel funciona em pending ==============
    # Cria outra row, cancela antes de processar
    id_cancel = schedule_wa(
        instance="rap-whatsapp",
        number="5511999999999",
        text="TEST — IGNORE cancel",
        scheduled_for=now_utc() + timedelta(hours=1),
        source="smoke test cancel",
        dedup_key=DEDUP_KEY + "_cancel",
        created_by="test_script",
    )
    ok = cancel(id_cancel)
    assert ok, "cancel retornou False pra row pending"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM scheduled_actions WHERE id = %s", (id_cancel,))
        st = cursor.fetchone()["status"]
    assert st == "cancelled", f"esperado cancelled, recebi {st}"
    # Cancel novamente deve falhar (nao mais pending)
    ok2 = cancel(id_cancel)
    assert not ok2, "cancel deveria retornar False pra row ja cancelada"
    print(f"[OK] cancel funciona em pending, idempotente pra cancelled")

    # ============== Test 10: audit log inclui tudo ==============
    audit = get_audit_log(limit=50, days=1)
    audit_ids = [r["id"] for r in audit]
    assert id1 in audit_ids, "audit log nao tem id1 (sent)"
    assert id_cancel in audit_ids, "audit log nao tem id_cancel"
    print(f"[OK] audit log com {len(audit)} rows, inclui sent + cancelled")

    # ============== Cleanup ==============
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM scheduled_actions WHERE dedup_key IN (%s, %s)",
            (DEDUP_KEY, DEDUP_KEY + "_cancel"),
        )
        conn.commit()
    print(f"[OK] Cleanup completo")

    print("\n========================================")
    print("SMOKE TEST PASSOU")
    print("========================================")


if __name__ == "__main__":
    try:
        asyncio.run(main())
        sys.exit(0)
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n[ERROR] {e}")
        sys.exit(1)
