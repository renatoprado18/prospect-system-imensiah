"""
Vigilancia da invariante de tasks: status aberto NUNCA convive com
data_conclusao preenchida.

Historia (09/07/26): 20 tasks violavam isso em prod. Causa: push_all_pending
excluia `status != 'completed'`, entao conclusoes nunca subiam pro Google;
o pull seguinte reabria a task (status='pending') sem limpar data_conclusao.
Consumidores que filtravam so por status viam task-fantasma — tarefa concluida
reaparecendo como atrasada dias depois. A Tonia mostrou 15 atrasadas quando
eram 7.

O bug foi corrigido em tasks_sync.py e o backfill zerou as 20. Este modulo
existe pra provar que continua zerado. Se voltar a passar de zero, o fix do
tasks_sync regrediu ou um novo escritor apareceu.
"""
import logging
from typing import Any, Dict

from database import get_db

logger = logging.getLogger(__name__)

# Amostra pequena: o alerta e um gatilho pra investigar, nao um relatorio.
SAMPLE_SIZE = 5


def check_tasks_integrity() -> Dict[str, Any]:
    """
    Conta tasks que violam a invariante e devolve amostra pra diagnostico.

    Retorna {"violations": int, "sample": [{id, titulo, status, data_conclusao}]}.
    Read-only: nunca corrige sozinho. Fechar ou reabrir uma task e decisao do
    Renato — em 09/07 duas das varridas eram objetivos declarados do trimestre.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, status, data_conclusao
              FROM tasks
             WHERE status IN ('pending', 'in_progress')
               AND data_conclusao IS NOT NULL
             ORDER BY data_conclusao DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]

    sample = [
        {
            "id": r["id"],
            "titulo": (r["titulo"] or "")[:60],
            "status": r["status"],
            "data_conclusao": r["data_conclusao"].isoformat() if r["data_conclusao"] else None,
        }
        for r in rows[:SAMPLE_SIZE]
    ]
    return {"violations": len(rows), "sample": sample}


def build_alert_text(result: Dict[str, Any]) -> str:
    """Texto curto pro WhatsApp. So chamado quando violations > 0."""
    n = result["violations"]
    linhas = [
        f"Integridade de tasks: {n} com status aberto e data_conclusao preenchida.",
        "Isso reabre task-fantasma no briefing. Suspeito: regressao no tasks_sync.",
        "",
    ]
    linhas += [f"  #{s['id']} {s['titulo']}" for s in result["sample"]]
    if n > len(result["sample"]):
        linhas.append(f"  (+{n - len(result['sample'])} outras)")
    return "\n".join(linhas)
