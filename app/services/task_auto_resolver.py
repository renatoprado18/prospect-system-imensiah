"""
Task Auto-Resolver Service

Automatically resolves pending tasks when matching actions are detected.
Called after actions: email_sent, whatsapp_sent, meeting_created, contact_updated.

Clear matches (>80% confidence): auto-complete + notify Renato
Ambiguous matches: ask Renato via WhatsApp for confirmation
"""
import re
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from database import get_db

logger = logging.getLogger(__name__)

# Matching rules per action type
MATCH_RULES = {
    "email_sent": {
        "keywords": ["enviar", "mandar", "email", "ata", "compartilhar"],
        "strong_keywords": ["enviar ata", "mandar ata", "compartilhar ata", "enviar email"],
    },
    "whatsapp_sent": {
        "keywords": ["mensagem", "follow-up", "followup", "cobrar", "responder", "whatsapp", "contatar", "falar"],
        "strong_keywords": ["mensagem para", "follow-up", "cobrar", "falar com", "contatar"],
    },
    "meeting_created": {
        "keywords": ["agendar", "reuniao", "meeting", "call", "encontro", "marcar"],
        "strong_keywords": ["agendar reuniao", "marcar reuniao", "agendar call", "marcar call"],
    },
    "contact_updated": {
        "keywords": ["email", "telefone", "atualizar", "adicionar", "cadastrar"],
        "strong_keywords": ["adicionar email", "cadastrar email", "atualizar email", "pegar email"],
    },
}


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, remove accents, extra spaces."""
    text = text.lower().strip()
    # Simple accent removal
    replacements = {
        'á': 'a', 'à': 'a', 'ã': 'a', 'â': 'a',
        'é': 'e', 'ê': 'e',
        'í': 'i',
        'ó': 'o', 'ô': 'o', 'õ': 'o',
        'ú': 'u', 'ü': 'u',
        'ç': 'c',
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def _calculate_match_score(task_title: str, action_type: str, context: Dict) -> float:
    """
    Calculate match score between a task and an action.
    Returns 0.0 to 1.0.
    """
    rules = MATCH_RULES.get(action_type, {})
    title_norm = _normalize(task_title)
    score = 0.0

    # Check strong keywords (0.5 each, max 0.5)
    for kw in rules.get("strong_keywords", []):
        if _normalize(kw) in title_norm:
            score += 0.5
            break

    # Check weak keywords (0.2 each, max 0.4)
    keyword_hits = 0
    for kw in rules.get("keywords", []):
        if _normalize(kw) in title_norm:
            keyword_hits += 1
    score += min(keyword_hits * 0.2, 0.4)

    # Check entity match (contact name, empresa)
    entity_match = False
    for entity_field in ["contact_name", "empresa_nome", "subject"]:
        entity = context.get(entity_field)
        if entity and _normalize(entity) in title_norm:
            score += 0.3
            entity_match = True
            break

    # If no keyword match at all, score is 0 regardless of entity
    if keyword_hits == 0 and score < 0.5:
        return 0.0

    return min(score, 1.0)


async def check_and_resolve_tasks(action_type: str, context: dict):
    """
    Called after actions are executed. Checks for matching pending tasks.

    action_type: "email_sent", "whatsapp_sent", "meeting_created", "contact_updated"
    context: {empresa_nome, contact_id, contact_name, subject, ...}

    Logic:
    1. Query pending tasks that might match the action
    2. For clear matches (>80% confidence): auto-complete + notify via bot
    3. For ambiguous matches (40-80%): ask via bot
    """
    if action_type not in MATCH_RULES:
        return

    try:
        # Build search conditions based on action type
        rules = MATCH_RULES[action_type]
        all_keywords = rules.get("keywords", []) + rules.get("strong_keywords", [])

        # Build ILIKE conditions for relevant keywords
        like_conditions = []
        like_params = []
        for kw in set(all_keywords):
            like_conditions.append("titulo ILIKE %s")
            like_params.append(f"%{kw}%")

        # Also match by contact_id if available
        contact_condition = ""
        if context.get("contact_id"):
            contact_condition = " OR contact_id = %s"
            like_params.append(context["contact_id"])

        if not like_conditions:
            return

        where_clause = " OR ".join(like_conditions) + contact_condition

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT id, titulo, descricao, contact_id, project_id, data_vencimento
                FROM tasks
                WHERE status = 'pending'
                  AND ({where_clause})
                ORDER BY data_vencimento ASC NULLS LAST
                LIMIT 10
            """, like_params)
            pending_tasks = [dict(r) for r in cursor.fetchall()]

        if not pending_tasks:
            return

        auto_completed = []
        ambiguous = []

        for task in pending_tasks:
            score = _calculate_match_score(task["titulo"], action_type, context)

            if score >= 0.8:
                auto_completed.append(task)
            elif score >= 0.4:
                ambiguous.append(task)

        # Auto-complete clear matches
        if auto_completed:
            with get_db() as conn:
                cursor = conn.cursor()
                task_ids = [t["id"] for t in auto_completed]
                for task_id in task_ids:
                    cursor.execute("""
                        UPDATE tasks SET status = 'completed', data_conclusao = NOW()
                        WHERE id = %s AND status = 'pending'
                    """, (task_id,))
                conn.commit()

            # Notify Renato
            from services.intel_bot import send_intel_notification
            action_desc = _action_description(action_type, context)
            task_list = "\n".join(f"  - {t['titulo']}" for t in auto_completed)
            msg = f"✅ {action_desc}\n\nTarefas concluidas automaticamente:\n{task_list}"
            await send_intel_notification(msg)
            logger.info(f"Auto-completed {len(auto_completed)} tasks for {action_type}: {[t['titulo'] for t in auto_completed]}")

        # Ask about ambiguous matches
        for task in ambiguous:
            from services.intel_bot import send_intel_notification
            action_desc = _action_description(action_type, context)
            msg = (
                f"Acabei de registrar: {action_desc}\n\n"
                f"Posso marcar como concluida: \"{task['titulo']}\"?\n"
                f"Responda: sim {task['id']} ou nao {task['id']}"
            )
            await send_intel_notification(msg)
            logger.info(f"Asked about ambiguous task #{task['id']}: {task['titulo']}")

    except Exception as e:
        logger.error(f"Error in check_and_resolve_tasks({action_type}): {e}")


def _action_description(action_type: str, context: dict) -> str:
    """Human-readable description of the action performed."""
    contact = context.get("contact_name", "")
    empresa = context.get("empresa_nome", "")
    subject = context.get("subject", "")

    if action_type == "email_sent":
        parts = ["Email enviado"]
        if subject:
            parts.append(f'"{subject}"')
        if contact:
            parts.append(f"para {contact}")
        elif empresa:
            parts.append(f"({empresa})")
        return " ".join(parts)

    elif action_type == "whatsapp_sent":
        if contact:
            return f"WhatsApp enviado para {contact}"
        return "WhatsApp enviado"

    elif action_type == "meeting_created":
        parts = ["Reuniao agendada"]
        if contact:
            parts.append(f"com {contact}")
        if subject:
            parts.append(f'"{subject}"')
        return " ".join(parts)

    elif action_type == "contact_updated":
        if contact:
            return f"Contato atualizado: {contact}"
        return "Contato atualizado"

    return action_type
