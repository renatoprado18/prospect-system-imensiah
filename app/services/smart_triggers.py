"""
Smart Triggers Service - Automacoes inteligentes

Triggers disponiveis:
- health_drop: Quando health score cai abaixo de threshold
- no_contact: Quando tempo sem contato excede limite
- birthday: Proximo de aniversario
- circle_change: Quando circulo muda
- message_received: Quando mensagem recebida
"""
import json
from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta
from database import get_db


class SmartTriggersService:
    def __init__(self):
        self.trigger_handlers = {
            "health_drop": self._handle_health_drop,
            "no_contact": self._handle_no_contact,
            "birthday": self._handle_birthday,
            "circle_change": self._handle_circle_change,
            "message_received": self._handle_message_received
        }

        self.action_handlers = {
            "create_suggestion": self._action_create_suggestion,
            "send_notification": self._action_send_notification,
            "update_tag": self._action_update_tag,
            "create_task": self._action_create_task
        }

    def get_automations(self, active_only: bool = True) -> List[Dict]:
        """Lista todas as automacoes"""
        with get_db() as conn:
            cursor = conn.cursor()

            if active_only:
                cursor.execute("""
                    SELECT * FROM ai_automations
                    WHERE ativo = TRUE
                    ORDER BY nome
                """)
            else:
                cursor.execute("SELECT * FROM ai_automations ORDER BY nome")

            return [dict(row) for row in cursor.fetchall()]

    def create_automation(
        self,
        nome: str,
        descricao: str,
        trigger_type: str,
        trigger_config: Dict,
        action_type: str,
        action_config: Dict
    ) -> int:
        """Cria nova automacao"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO ai_automations
                (nome, descricao, trigger_type, trigger_config, action_type, action_config)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                nome,
                descricao,
                trigger_type,
                json.dumps(trigger_config),
                action_type,
                json.dumps(action_config)
            ))

            automation_id = cursor.fetchone()["id"]
            conn.commit()

            return automation_id

    def toggle_automation(self, automation_id: int, ativo: bool) -> bool:
        """Ativa/desativa uma automacao"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE ai_automations
                SET ativo = %s
                WHERE id = %s
                RETURNING id
            """, (ativo, automation_id))

            result = cursor.fetchone()
            conn.commit()

            return result is not None

    def delete_automation(self, automation_id: int) -> bool:
        """Remove uma automacao"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("DELETE FROM ai_automations WHERE id = %s", (automation_id,))
            deleted = cursor.rowcount > 0
            conn.commit()

            return deleted

    def run_automations(self) -> Dict:
        """Executa todas as automacoes ativas"""
        results = {
            "started_at": datetime.now().isoformat(),
            "automations_run": 0,
            "triggers_fired": 0,
            "actions_executed": 0,
            "errors": []
        }

        automations = self.get_automations(active_only=True)

        for automation in automations:
            try:
                triggered = self._run_automation(automation)
                results["automations_run"] += 1

                if triggered:
                    results["triggers_fired"] += triggered.get("triggers_fired", 0)
                    results["actions_executed"] += triggered.get("actions_executed", 0)

            except Exception as e:
                results["errors"].append({
                    "automation_id": automation["id"],
                    "nome": automation["nome"],
                    "error": str(e)
                })

        results["completed_at"] = datetime.now().isoformat()
        return results

    def _run_automation(self, automation: Dict) -> Optional[Dict]:
        """Executa uma automacao especifica"""
        trigger_type = automation["trigger_type"]
        trigger_config = automation["trigger_config"] or {}
        action_type = automation["action_type"]
        action_config = automation["action_config"] or {}

        # Obter handler do trigger
        handler = self.trigger_handlers.get(trigger_type)
        if not handler:
            return None

        # Executar trigger para obter contatos afetados
        contacts = handler(trigger_config)
        if not contacts:
            return {"triggers_fired": 0, "actions_executed": 0}

        # Executar acao para cada contato
        action_handler = self.action_handlers.get(action_type)
        if not action_handler:
            return {"triggers_fired": len(contacts), "actions_executed": 0}

        executed = 0
        for contact in contacts:
            try:
                action_handler(contact, action_config, automation)
                executed += 1
            except Exception as e:
                print(f"Action error for contact {contact['id']}: {e}")

        # Atualizar contador de execucoes
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ai_automations
                SET ultima_execucao = NOW(), total_execucoes = total_execucoes + %s
                WHERE id = %s
            """, (executed, automation["id"]))
            conn.commit()

        return {"triggers_fired": len(contacts), "actions_executed": executed}

    # =========================================================================
    # TRIGGER HANDLERS
    # =========================================================================

    def _handle_health_drop(self, config: Dict) -> List[Dict]:
        """Trigger: Health score caiu abaixo do threshold"""
        threshold = config.get("threshold", 40)
        circulo_max = config.get("circulo_max", 3)

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, empresa, circulo, health_score
                FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                AND COALESCE(health_score, 50) < %s
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'health_alert'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '7 days'
                )
            """, (circulo_max, threshold))

            return [dict(row) for row in cursor.fetchall()]

    def _handle_no_contact(self, config: Dict) -> List[Dict]:
        """Trigger: Sem contato por X dias"""
        dias = config.get("dias", 30)
        circulo_max = config.get("circulo_max", 3)

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, empresa, circulo, ultimo_contato,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                AND ultimo_contato < NOW() - INTERVAL '%s days'
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'reconnect'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '7 days'
                )
            """, (circulo_max, dias))

            return [dict(row) for row in cursor.fetchall()]

    def _handle_birthday(self, config: Dict) -> List[Dict]:
        """Trigger: Aniversario em X dias"""
        dias_antes = config.get("dias_antes", 3)
        circulo_max = config.get("circulo_max", 4)

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                WITH aniv_calc AS (
                    SELECT
                        id, nome, empresa, circulo, aniversario,
                        CASE
                            WHEN EXTRACT(DOY FROM aniversario::date) >= EXTRACT(DOY FROM CURRENT_DATE)
                            THEN EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                            ELSE 365 + EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                        END as dias_ate
                    FROM contacts
                    WHERE aniversario IS NOT NULL
                      AND COALESCE(circulo, 5) <= %s
                )
                SELECT * FROM aniv_calc
                WHERE dias_ate >= 0 AND dias_ate <= %s
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'birthday'
                    AND status = 'pending'
                )
            """, (circulo_max, dias_antes))

            return [dict(row) for row in cursor.fetchall()]

    def _handle_circle_change(self, config: Dict) -> List[Dict]:
        """Trigger: Circulo mudou (placeholder para implementacao futura)"""
        # Requer tracking de mudancas historicas
        return []

    def _handle_message_received(self, config: Dict) -> List[Dict]:
        """Trigger: Mensagem recebida (placeholder para webhook)"""
        # Ativado via webhook, nao por polling
        return []

    # =========================================================================
    # ACTION HANDLERS
    # =========================================================================

    def _action_create_suggestion(
        self,
        contact: Dict,
        config: Dict,
        automation: Dict
    ):
        """Acao: Criar sugestao"""
        tipo = config.get("tipo", "reconnect")
        titulo_template = config.get("titulo", "Acao para {nome}")
        descricao_template = config.get("descricao", "")
        prioridade = config.get("prioridade", 5)

        titulo = titulo_template.format(**contact)
        descricao = descricao_template.format(**contact) if descricao_template else None

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO ai_suggestions
                (contact_id, tipo, titulo, descricao, razao, prioridade, confianca)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                contact["id"],
                tipo,
                titulo,
                descricao,
                f"Gerado por automacao: {automation['nome']}",
                prioridade,
                0.85
            ))
            conn.commit()

    def _action_send_notification(
        self,
        contact: Dict,
        config: Dict,
        automation: Dict
    ):
        """Acao: Enviar notificacao (placeholder)"""
        # Integrar com sistema de notificacoes
        pass

    def _action_update_tag(
        self,
        contact: Dict,
        config: Dict,
        automation: Dict
    ):
        """Acao: Adicionar tag ao contato"""
        tag = config.get("tag")
        if not tag:
            return

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE contacts
                SET tags = CASE
                    WHEN tags IS NULL THEN %s::jsonb
                    WHEN NOT (tags ? %s) THEN tags || %s::jsonb
                    ELSE tags
                END,
                atualizado_em = NOW()
                WHERE id = %s
            """, (
                json.dumps([tag]),
                tag,
                json.dumps([tag]),
                contact["id"]
            ))
            conn.commit()

    def _action_create_task(
        self,
        contact: Dict,
        config: Dict,
        automation: Dict
    ):
        """Acao: Criar tarefa"""
        titulo_template = config.get("titulo", "Tarefa para {nome}")
        descricao = config.get("descricao", "")
        prioridade = config.get("prioridade", 5)
        dias_vencimento = config.get("dias_vencimento", 7)

        titulo = titulo_template.format(**contact)
        vencimento = datetime.now() + timedelta(days=dias_vencimento)

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO tasks
                (titulo, descricao, contact_id, origem, data_vencimento, prioridade, ai_generated)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """, (
                titulo,
                descricao,
                contact["id"],
                f"automation:{automation['id']}",
                vencimento,
                prioridade
            ))
            conn.commit()

    # =========================================================================
    # DEFAULT AUTOMATIONS
    # =========================================================================

    def setup_default_automations(self):
        """Configura automacoes padrao"""
        defaults = [
            {
                "nome": "Alerta Health Baixo",
                "descricao": "Cria sugestao quando health cai abaixo de 30%",
                "trigger_type": "health_drop",
                "trigger_config": {"threshold": 30, "circulo_max": 3},
                "action_type": "create_suggestion",
                "action_config": {
                    "tipo": "health_alert",
                    "titulo": "[URGENTE] Reconectar com {nome}",
                    "prioridade": 8
                }
            },
            {
                "nome": "Reconexao C1 30 dias",
                "descricao": "Alerta quando contato C1 fica 30 dias sem contato",
                "trigger_type": "no_contact",
                "trigger_config": {"dias": 30, "circulo_max": 1},
                "action_type": "create_suggestion",
                "action_config": {
                    "tipo": "reconnect",
                    "titulo": "Reconectar com {nome} (C1)",
                    "prioridade": 9
                }
            },
            {
                "nome": "Lembrete Aniversario",
                "descricao": "Lembra de aniversarios com 3 dias de antecedencia",
                "trigger_type": "birthday",
                "trigger_config": {"dias_antes": 3, "circulo_max": 4},
                "action_type": "create_suggestion",
                "action_config": {
                    "tipo": "birthday",
                    "titulo": "Aniversario de {nome} se aproxima!",
                    "prioridade": 7
                }
            }
        ]

        created = 0
        for d in defaults:
            try:
                # Check if already exists
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id FROM ai_automations WHERE nome = %s",
                        (d["nome"],)
                    )
                    if cursor.fetchone():
                        continue

                self.create_automation(**d)
                created += 1
            except Exception as e:
                print(f"Error creating default automation: {e}")

        return created


_smart_triggers = None


def get_smart_triggers() -> SmartTriggersService:
    global _smart_triggers
    if _smart_triggers is None:
        _smart_triggers = SmartTriggersService()
    return _smart_triggers
