# Fila de Tarefas 2INTEL - AI Avançado

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: Tabela ai_suggestions + Endpoints

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: CRITICA

### 1.1 Criar Tabela (executar no Neon ou criar script SQL)

```sql
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    tipo VARCHAR(50) NOT NULL,
    titulo VARCHAR(255) NOT NULL,
    descricao TEXT,
    acao_sugerida JSONB DEFAULT '{}',
    contexto JSONB DEFAULT '{}',
    prioridade INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_suggestions_contact ON ai_suggestions(contact_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON ai_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_tipo ON ai_suggestions(tipo);
```

### 1.2 Adicionar Endpoints em main.py

```python
# ========== AI SUGGESTIONS ==========

@app.get("/api/ai/suggestions")
async def get_ai_suggestions(
    request: Request,
    status: str = "pending",
    limit: int = 20
):
    """Lista sugestoes AI ativas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.*, c.nome as contact_name, c.foto_url as contact_foto
            FROM ai_suggestions s
            LEFT JOIN contacts c ON c.id = s.contact_id
            WHERE s.status = %s
            AND (s.expires_at IS NULL OR s.expires_at > NOW())
            ORDER BY s.prioridade DESC, s.created_at DESC
            LIMIT %s
        """, (status, limit))
        suggestions = [dict(row) for row in cursor.fetchall()]
        return {"suggestions": suggestions}


@app.post("/api/ai/suggestions/{suggestion_id}/accept")
async def accept_suggestion(request: Request, suggestion_id: int):
    """Aceita uma sugestao AI"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ai_suggestions SET status = 'accepted' WHERE id = %s RETURNING *
        """, (suggestion_id,))
        result = cursor.fetchone()
        conn.commit()
        if not result:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada")
        return {"status": "accepted", "suggestion": dict(result)}


@app.post("/api/ai/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(request: Request, suggestion_id: int):
    """Descarta uma sugestao AI"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ai_suggestions SET status = 'dismissed' WHERE id = %s RETURNING id
        """, (suggestion_id,))
        result = cursor.fetchone()
        conn.commit()
        if not result:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada")
        return {"status": "dismissed", "id": suggestion_id}


@app.get("/api/ai/suggestions/contact/{contact_id}")
async def get_contact_suggestions(request: Request, contact_id: int):
    """Sugestoes para um contato especifico"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM ai_suggestions
            WHERE contact_id = %s AND status = 'pending'
            ORDER BY prioridade DESC LIMIT 5
        """, (contact_id,))
        return {"suggestions": [dict(row) for row in cursor.fetchall()]}
```

**Commit**: `git commit -m "Add ai_suggestions table and CRUD endpoints"`

---

## TAREFA 2: Service ai_agent.py

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: CRITICA

### Criar arquivo: `app/services/ai_agent.py`

```python
"""
AI Agent Service - Orquestrador de sugestoes proativas
"""
import os
import json
from typing import List, Dict
from datetime import datetime
from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


class AIAgent:
    """Orquestrador principal de AI"""

    def generate_reconnect_suggestions(self, limit: int = 10) -> List[Dict]:
        """Gera sugestoes de reconexao para contatos com health baixo"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, circulo, health_score, ultimo_contato, empresa, contexto
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < 40
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'reconnect' AND status = 'pending'
                    AND created_at > NOW() - INTERVAL '7 days'
                )
                ORDER BY circulo ASC, health_score ASC
                LIMIT %s
            """, (limit,))

            contacts = cursor.fetchall()
            suggestions = []

            for contact in contacts:
                dias = 999
                if contact["ultimo_contato"]:
                    dias = (datetime.now() - contact["ultimo_contato"]).days

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "reconnect",
                    "titulo": f"Reconectar com {contact['nome']}",
                    "descricao": f"Health {contact['health_score']}% - {dias} dias sem contato",
                    "acao_sugerida": {"action": "send_message", "context": contact.get("contexto", "professional")},
                    "contexto": {"health": contact["health_score"], "dias_sem_contato": dias, "circulo": contact["circulo"]},
                    "prioridade": 10 - contact["circulo"] + (1 if contact["health_score"] < 20 else 0)
                }
                suggestions.append(suggestion)

                cursor.execute("""
                    INSERT INTO ai_suggestions (contact_id, tipo, titulo, descricao, acao_sugerida, contexto, prioridade, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '7 days')
                """, (suggestion["contact_id"], suggestion["tipo"], suggestion["titulo"], suggestion["descricao"],
                      json.dumps(suggestion["acao_sugerida"]), json.dumps(suggestion["contexto"]), suggestion["prioridade"]))

            conn.commit()
            return suggestions

    def generate_birthday_suggestions(self) -> List[Dict]:
        """Gera sugestoes para aniversarios proximos"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                WITH aniv AS (
                    SELECT id, nome, aniversario, circulo,
                           CASE
                               WHEN EXTRACT(DOY FROM aniversario) >= EXTRACT(DOY FROM CURRENT_DATE)
                               THEN EXTRACT(DOY FROM aniversario) - EXTRACT(DOY FROM CURRENT_DATE)
                               ELSE 365 + EXTRACT(DOY FROM aniversario) - EXTRACT(DOY FROM CURRENT_DATE)
                           END as dias_ate
                    FROM contacts WHERE aniversario IS NOT NULL AND COALESCE(circulo, 5) <= 4
                )
                SELECT * FROM aniv WHERE dias_ate <= 3
                AND id NOT IN (SELECT contact_id FROM ai_suggestions WHERE tipo = 'birthday' AND status = 'pending')
                ORDER BY dias_ate
            """)

            contacts = cursor.fetchall()
            suggestions = []

            for contact in contacts:
                dias = int(contact["dias_ate"])
                titulo = f"Aniversario de {contact['nome']}" + (" HOJE!" if dias == 0 else f" em {dias} dia{'s' if dias > 1 else ''}")

                cursor.execute("""
                    INSERT INTO ai_suggestions (contact_id, tipo, titulo, descricao, acao_sugerida, contexto, prioridade, expires_at)
                    VALUES (%s, 'birthday', %s, 'Envie uma mensagem de parabens', '{"action": "send_birthday_message"}', %s, %s, NOW() + INTERVAL '4 days')
                """, (contact["id"], titulo, json.dumps({"dias_ate": dias}), 9 if dias == 0 else 7))
                suggestions.append({"contact_id": contact["id"], "titulo": titulo})

            conn.commit()
            return suggestions

    def generate_followup_suggestions(self) -> List[Dict]:
        """Gera sugestoes de follow-up para conversas pendentes"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id as conversation_id, c.contact_id, c.assunto, ct.nome, c.ultimo_mensagem
                FROM conversations c
                JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.requer_resposta = TRUE
                AND c.ultimo_mensagem < NOW() - INTERVAL '3 days'
                AND c.contact_id NOT IN (SELECT contact_id FROM ai_suggestions WHERE tipo = 'followup' AND status = 'pending')
                ORDER BY c.ultimo_mensagem ASC LIMIT 10
            """)

            conversations = cursor.fetchall()
            suggestions = []

            for conv in conversations:
                dias = (datetime.now() - conv["ultimo_mensagem"]).days if conv["ultimo_mensagem"] else 0
                cursor.execute("""
                    INSERT INTO ai_suggestions (contact_id, tipo, titulo, descricao, acao_sugerida, contexto, prioridade, expires_at)
                    VALUES (%s, 'followup', %s, %s, %s, %s, %s, NOW() + INTERVAL '5 days')
                """, (
                    conv["contact_id"],
                    f"Follow-up com {conv['nome']}",
                    f"Conversa sobre '{conv['assunto'] or 'sem assunto'}' aguarda ha {dias} dias",
                    json.dumps({"action": "open_conversation", "conversation_id": conv["conversation_id"]}),
                    json.dumps({"conversation_id": conv["conversation_id"], "dias_aguardando": dias}),
                    min(8, 5 + dias // 2)
                ))
                suggestions.append({"contact_id": conv["contact_id"], "nome": conv["nome"]})

            conn.commit()
            return suggestions

    def run_daily_generation(self) -> Dict:
        """Executa geracao diaria de todas as sugestoes"""
        results = {
            "reconnect": len(self.generate_reconnect_suggestions()),
            "birthday": len(self.generate_birthday_suggestions()),
            "followup": len(self.generate_followup_suggestions())
        }
        results["total"] = sum(results.values())
        return results

    def cleanup_expired(self) -> int:
        """Remove sugestoes expiradas"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM ai_suggestions
                WHERE expires_at < NOW()
                OR (status != 'pending' AND created_at < NOW() - INTERVAL '30 days')
            """)
            deleted = cursor.rowcount
            conn.commit()
            return deleted


_ai_agent = None

def get_ai_agent() -> AIAgent:
    global _ai_agent
    if _ai_agent is None:
        _ai_agent = AIAgent()
    return _ai_agent
```

### Adicionar endpoint de geracao em main.py

```python
@app.post("/api/ai/generate-suggestions")
async def generate_suggestions(request: Request, background_tasks: BackgroundTasks):
    """Dispara geracao de sugestoes em background"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.ai_agent import get_ai_agent
    background_tasks.add_task(get_ai_agent().run_daily_generation)
    return {"status": "started", "message": "Geracao de sugestoes iniciada"}
```

**Commit**: `git commit -m "Add AIAgent service for proactive suggestions"`

---

## TAREFA 3: Tabela ai_automations + Smart Triggers

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: ALTA

### 3.1 Criar tabela

```sql
CREATE TABLE IF NOT EXISTS ai_automations (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(100) NOT NULL,
    descricao TEXT,
    trigger_type VARCHAR(50) NOT NULL,
    trigger_config JSONB DEFAULT '{}',
    action_type VARCHAR(50) NOT NULL,
    action_config JSONB DEFAULT '{}',
    enabled BOOLEAN DEFAULT TRUE,
    last_run TIMESTAMP,
    run_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO ai_automations (nome, descricao, trigger_type, trigger_config, action_type, action_config) VALUES
('Health Critico', 'Gera sugestao quando health < 30', 'health_drop', '{"threshold": 30, "circulos": [1, 2]}', 'generate_suggestion', '{"tipo": "reconnect"}'),
('Aniversario Proximo', 'Sugestao 3 dias antes do aniversario', 'birthday_upcoming', '{"days_before": 3}', 'generate_suggestion', '{"tipo": "birthday"}'),
('Sem Contato 30 dias', 'Alerta para contatos inativos', 'no_contact', '{"days": 30, "circulos": [1, 2, 3]}', 'create_notification', '{"priority": "high"}');
```

### 3.2 Criar arquivo: `app/services/smart_triggers.py`

```python
"""
Smart Triggers Service - Motor de automacoes
"""
import json
from typing import List, Dict
from datetime import datetime
from database import get_db


class SmartTriggers:
    def get_enabled_automations(self) -> List[Dict]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_automations WHERE enabled = TRUE")
            return [dict(row) for row in cursor.fetchall()]

    def evaluate_health_trigger(self, automation: Dict) -> List[Dict]:
        config = automation.get("trigger_config", {})
        threshold = config.get("threshold", 30)
        circulos = config.get("circulos", [1, 2, 3])

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, health_score, circulo FROM contacts
                WHERE COALESCE(health_score, 50) < %s AND COALESCE(circulo, 5) = ANY(%s)
                AND id NOT IN (SELECT contact_id FROM ai_suggestions WHERE tipo = 'reconnect' AND status = 'pending' AND created_at > NOW() - INTERVAL '7 days')
            """, (threshold, circulos))
            return [dict(row) for row in cursor.fetchall()]

    def execute_action(self, automation: Dict, contacts: List[Dict]) -> int:
        action_type = automation.get("action_type")
        executed = 0

        with get_db() as conn:
            cursor = conn.cursor()
            if action_type == "generate_suggestion":
                for contact in contacts:
                    cursor.execute("""
                        INSERT INTO ai_suggestions (contact_id, tipo, titulo, descricao, prioridade, expires_at)
                        VALUES (%s, 'reconnect', %s, %s, 7, NOW() + INTERVAL '7 days') ON CONFLICT DO NOTHING
                    """, (contact["id"], f"Automacao: {automation['nome']}", f"Sugestao para {contact['nome']}"))
                    executed += 1

            cursor.execute("UPDATE ai_automations SET last_run = NOW(), run_count = run_count + 1 WHERE id = %s", (automation["id"],))
            conn.commit()
        return executed

    def run_all_triggers(self) -> Dict:
        automations = self.get_enabled_automations()
        results = {"total_executed": 0, "automations": []}

        for auto in automations:
            contacts = []
            if auto["trigger_type"] == "health_drop":
                contacts = self.evaluate_health_trigger(auto)

            if contacts:
                executed = self.execute_action(auto, contacts)
                results["automations"].append({"name": auto["nome"], "executed": executed})
                results["total_executed"] += executed

        return results


_smart_triggers = None

def get_smart_triggers() -> SmartTriggers:
    global _smart_triggers
    if _smart_triggers is None:
        _smart_triggers = SmartTriggers()
    return _smart_triggers
```

### 3.3 Endpoints em main.py

```python
@app.get("/api/ai/automations")
async def list_automations(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ai_automations ORDER BY created_at")
        return {"automations": [dict(row) for row in cursor.fetchall()]}

@app.post("/api/ai/automations/{automation_id}/toggle")
async def toggle_automation(request: Request, automation_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE ai_automations SET enabled = NOT enabled WHERE id = %s RETURNING id, nome, enabled", (automation_id,))
        result = cursor.fetchone()
        conn.commit()
        if not result:
            raise HTTPException(status_code=404)
        return dict(result)

@app.post("/api/ai/triggers/run")
async def run_triggers(request: Request, background_tasks: BackgroundTasks):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    from services.smart_triggers import get_smart_triggers
    background_tasks.add_task(get_smart_triggers().run_all_triggers)
    return {"status": "started"}
```

**Commit**: `git commit -m "Add ai_automations table and SmartTriggers service"`

---

## TAREFA 4: Health Predictions

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: ALTA

### 4.1 Criar tabela

```sql
CREATE TABLE IF NOT EXISTS health_predictions (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE UNIQUE,
    health_atual INTEGER,
    health_previsto_7d INTEGER,
    health_previsto_30d INTEGER,
    risco_churn DECIMAL(3,2),
    fatores JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_predictions_risco ON health_predictions(risco_churn DESC);
```

### 4.2 Adicionar metodo em ai_agent.py

```python
def generate_health_predictions(self, limit: int = 100) -> List[Dict]:
    """Gera previsoes de health para contatos"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, circulo, health_score, ultimo_contato, frequencia_ideal_dias, total_interacoes
            FROM contacts WHERE COALESCE(circulo, 5) <= 4
            ORDER BY circulo ASC LIMIT %s
        """, (limit,))

        contacts = cursor.fetchall()
        predictions = []

        for contact in contacts:
            health_atual = contact["health_score"] or 50
            freq = contact["frequencia_ideal_dias"] or 30
            dias_sem = (datetime.now() - contact["ultimo_contato"]).days if contact["ultimo_contato"] else 999

            decay_rate = 50 / freq
            health_7d = max(0, health_atual - (7 * decay_rate))
            health_30d = max(0, health_atual - (30 * decay_rate))
            risco = min(1.0, max(0.0, (100 - health_atual) / 100 + (dias_sem / (freq * 2)) * 0.3))

            cursor.execute("""
                INSERT INTO health_predictions (contact_id, health_atual, health_previsto_7d, health_previsto_30d, risco_churn, fatores)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (contact_id) DO UPDATE SET
                    health_atual = EXCLUDED.health_atual, health_previsto_7d = EXCLUDED.health_previsto_7d,
                    health_previsto_30d = EXCLUDED.health_previsto_30d, risco_churn = EXCLUDED.risco_churn,
                    fatores = EXCLUDED.fatores, created_at = NOW()
            """, (contact["id"], health_atual, int(health_7d), int(health_30d), round(risco, 2),
                  json.dumps({"dias_sem_contato": dias_sem, "frequencia_ideal": freq})))

            predictions.append({"contact_id": contact["id"], "risco_churn": round(risco, 2)})

        conn.commit()
        return predictions
```

### 4.3 Endpoints

```python
@app.get("/api/ai/predictions/{contact_id}")
async def get_contact_predictions(request: Request, contact_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT p.*, c.nome FROM health_predictions p JOIN contacts c ON c.id = p.contact_id WHERE p.contact_id = %s", (contact_id,))
        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404)
        return dict(result)

@app.get("/api/ai/at-risk")
async def get_at_risk_contacts(request: Request, limit: int = 20):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, c.nome, c.empresa, c.circulo, c.foto_url
            FROM health_predictions p JOIN contacts c ON c.id = p.contact_id
            WHERE p.risco_churn > 0.5 ORDER BY p.risco_churn DESC LIMIT %s
        """, (limit,))
        return {"contacts": [dict(row) for row in cursor.fetchall()]}
```

**Commit**: `git commit -m "Add health_predictions table and at-risk endpoint"`

---

## TAREFA 5: Message Templates + Service

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: MEDIA

### 5.1 Criar tabela

```sql
CREATE TABLE IF NOT EXISTS message_templates (
    id SERIAL PRIMARY KEY,
    categoria VARCHAR(50) NOT NULL,
    contexto VARCHAR(50) DEFAULT 'professional',
    template TEXT NOT NULL,
    variaveis JSONB DEFAULT '[]',
    uso_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO message_templates (categoria, contexto, template, variaveis) VALUES
('reconnect', 'professional', 'Oi {nome}! Faz tempo que a gente nao se fala. Como andam as coisas na {empresa}?', '["nome", "empresa"]'),
('reconnect', 'casual', 'E ai {nome}! Sumiu! Como voce ta?', '["nome"]'),
('birthday', 'professional', 'Parabens {nome}! Desejo um excelente aniversario e muito sucesso!', '["nome"]'),
('birthday', 'casual', 'Feliz aniversario {nome}! Tudo de bom!', '["nome"]'),
('followup', 'professional', 'Oi {nome}, passando pra dar um follow-up sobre {assunto}. Tem novidades?', '["nome", "assunto"]');
```

### 5.2 Criar arquivo: `app/services/message_suggestions.py`

```python
"""Message Suggestions Service"""
from typing import Optional, Dict
from database import get_db


class MessageSuggestions:
    def get_template(self, categoria: str, contexto: str = "professional") -> Optional[Dict]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM message_templates WHERE categoria = %s AND contexto = %s ORDER BY uso_count DESC LIMIT 1", (categoria, contexto))
            row = cursor.fetchone()
            return dict(row) if row else None

    def personalize_template(self, template: str, contact: Dict) -> str:
        result = template
        replacements = {
            "{nome}": contact.get("apelido") or contact.get("nome", "").split()[0],
            "{empresa}": contact.get("empresa", "sua empresa"),
            "{assunto}": contact.get("assunto", "nosso ultimo assunto")
        }
        for key, value in replacements.items():
            result = result.replace(key, str(value))
        return result

    def suggest_reconnect(self, contact_id: int) -> Dict:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, nome, apelido, empresa, contexto FROM contacts WHERE id = %s", (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact = dict(contact)
            template = self.get_template("reconnect", contact.get("contexto", "professional"))
            if template:
                message = self.personalize_template(template["template"], contact)
                cursor.execute("UPDATE message_templates SET uso_count = uso_count + 1 WHERE id = %s", (template["id"],))
                conn.commit()
                return {"message": message, "template_id": template["id"]}
            return {"message": f"Oi {contact['nome'].split()[0]}! Como voce ta?"}

    def suggest_birthday(self, contact_id: int) -> Dict:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, nome, apelido, contexto FROM contacts WHERE id = %s", (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}
            contact = dict(contact)
            template = self.get_template("birthday", contact.get("contexto", "professional"))
            if template:
                return {"message": self.personalize_template(template["template"], contact)}
            return {"message": f"Feliz aniversario {contact['nome'].split()[0]}!"}


_message_suggestions = None

def get_message_suggestions() -> MessageSuggestions:
    global _message_suggestions
    if _message_suggestions is None:
        _message_suggestions = MessageSuggestions()
    return _message_suggestions
```

### 5.3 Endpoints

```python
@app.get("/api/ai/message-suggest/{contact_id}")
async def suggest_message(request: Request, contact_id: int, tipo: str = "reconnect"):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    from services.message_suggestions import get_message_suggestions
    svc = get_message_suggestions()
    return svc.suggest_birthday(contact_id) if tipo == "birthday" else svc.suggest_reconnect(contact_id)
```

**Commit**: `git commit -m "Add message_templates table and MessageSuggestions service"`

---

## TAREFA 6: Digests (ai_digests + service)

**Status**: ✅ CONCLUIDO (2026-03-27)
**Prioridade**: MEDIA

### 6.1 Criar tabela

```sql
CREATE TABLE IF NOT EXISTS ai_digests (
    id SERIAL PRIMARY KEY,
    tipo VARCHAR(20) NOT NULL,
    conteudo JSONB NOT NULL,
    highlights JSONB DEFAULT '[]',
    periodo_inicio TIMESTAMP,
    periodo_fim TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 6.2 Criar arquivo: `app/services/digest_generator.py`

```python
"""Digest Generator Service"""
from typing import Dict
from datetime import datetime, timedelta
from database import get_db


class DigestGenerator:
    def generate_daily_digest(self) -> Dict:
        with get_db() as conn:
            cursor = conn.cursor()
            hoje = datetime.now().date()

            cursor.execute("SELECT COUNT(*) FILTER (WHERE DATE(criado_em) = %s) as novos FROM contacts", (hoje,))
            novos_contatos = cursor.fetchone()["novos"]

            cursor.execute("SELECT COUNT(*) as total FROM messages WHERE DATE(enviado_em) = %s", (hoje,))
            mensagens = cursor.fetchone()["total"]

            cursor.execute("""
                SELECT nome, empresa FROM contacts
                WHERE EXTRACT(MONTH FROM aniversario) = %s AND EXTRACT(DAY FROM aniversario) = %s
                AND COALESCE(circulo, 5) <= 4
            """, (hoje.month, hoje.day))
            aniversarios = [dict(r) for r in cursor.fetchall()]

            cursor.execute("""
                SELECT nome, health_score FROM contacts
                WHERE COALESCE(circulo, 5) <= 2 AND COALESCE(health_score, 50) < 30
                ORDER BY health_score ASC LIMIT 5
            """)
            atencao = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT COUNT(*) as total FROM ai_suggestions WHERE status = 'pending'")
            sugestoes = cursor.fetchone()["total"]

            conteudo = {
                "data": hoje.isoformat(),
                "metricas": {"novos_contatos": novos_contatos, "mensagens": mensagens, "sugestoes_pendentes": sugestoes},
                "aniversarios": aniversarios,
                "precisam_atencao": atencao
            }

            highlights = []
            if aniversarios:
                highlights.append(f"{len(aniversarios)} aniversario(s) hoje")
            if atencao:
                highlights.append(f"{len(atencao)} contato(s) precisam atencao")

            cursor.execute("""
                INSERT INTO ai_digests (tipo, conteudo, highlights, periodo_inicio, periodo_fim)
                VALUES ('daily', %s, %s, %s, %s) RETURNING id
            """, (conteudo, highlights, datetime.combine(hoje, datetime.min.time()), datetime.combine(hoje, datetime.max.time())))
            digest_id = cursor.fetchone()["id"]
            conn.commit()

            return {"id": digest_id, "tipo": "daily", "conteudo": conteudo, "highlights": highlights}

    def get_latest_digest(self, tipo: str = "daily") -> Dict:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_digests WHERE tipo = %s ORDER BY created_at DESC LIMIT 1", (tipo,))
            row = cursor.fetchone()
            return dict(row) if row else None


_digest_generator = None

def get_digest_generator() -> DigestGenerator:
    global _digest_generator
    if _digest_generator is None:
        _digest_generator = DigestGenerator()
    return _digest_generator
```

### 6.3 Endpoints

```python
@app.get("/api/ai/digest/daily")
async def get_daily_digest(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    from services.digest_generator import get_digest_generator
    digest = get_digest_generator().get_latest_digest("daily")
    if not digest:
        digest = get_digest_generator().generate_daily_digest()
    return digest

@app.post("/api/ai/digest/generate")
async def generate_digest(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    from services.digest_generator import get_digest_generator
    return get_digest_generator().generate_daily_digest()
```

**Commit**: `git commit -m "Add ai_digests table and DigestGenerator service"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao - Fila Anterior

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-27 | Enriquecimento LinkedIn Batch | CONCLUIDO |
| 2026-03-27 | Geracao Insights AI Batch | CONCLUIDO |
| 2026-03-27 | API Busca Avancada | CONCLUIDO |
| 2026-03-27 | API Exportacao | CONCLUIDO |
| 2026-03-27 | API Batch Operations | CONCLUIDO |
| 2026-03-27 | Cron Manutencao | CONCLUIDO |
| 2026-03-27 | SSE Notifications | CONCLUIDO |

## Registro de Conclusao - Fila AI Avancado

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-27 | Tabela ai_suggestions + CRUD | CONCLUIDO |
| 2026-03-27 | Service ai_agent.py | CONCLUIDO |
| 2026-03-27 | ai_automations + smart_triggers.py | CONCLUIDO |
| 2026-03-27 | Health Predictions | CONCLUIDO |
| 2026-03-27 | Message Templates + service | CONCLUIDO |
| 2026-03-27 | Digests (ai_digests + service) | CONCLUIDO |

---

## FILA ATUAL: VAZIA

**Todas as tarefas foram concluídas.**

Aguardando nova fila do ARCH.
