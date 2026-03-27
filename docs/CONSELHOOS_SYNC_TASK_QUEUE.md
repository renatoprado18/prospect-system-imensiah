# Fila de Tarefas 2INTEL - ConselhoOS Data Sync

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar
**Estrategia**: Manter ConselhoOS separado, criar sync de dados

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## CONTEXTO

### ConselhoOS (App Separado)
- **Stack**: Next.js + TypeScript + Drizzle ORM
- **Database**: Neon PostgreSQL (mesmo provedor que INTEL)
- **URL**: conselhoos.vercel.app

### Entidades Principais ConselhoOS
- `empresas` - Empresas onde usuario e conselheiro
- `reunioes` - Reunioes do conselho
- `raciItens` - Tarefas RACI
- `decisoes` - Decisoes do conselho
- `documentos` - Atas, DMRs

### Objetivo da Integracao
1. Vincular contatos INTEL a empresas/membros do ConselhoOS
2. Exibir reunioes do conselho no calendario INTEL
3. Mostrar tarefas RACI pendentes no dashboard INTEL
4. Notificacoes unificadas

---

## TAREFA 1: Tabela de Vinculo

**Status**: CONCLUIDO
**Prioridade**: ALTA

### Adicionar em `app/database.py`

```python
# Tabela para vincular contatos INTEL com entidades ConselhoOS
"""
CREATE TABLE IF NOT EXISTS conselhoos_links (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    conselhoos_empresa_id UUID,
    conselhoos_empresa_nome VARCHAR(255),
    role VARCHAR(100),  -- 'membro_conselho', 'executivo', 'stakeholder'
    notes TEXT,
    synced_at TIMESTAMP,
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_conselhoos_links_contact ON conselhoos_links(contact_id);
CREATE INDEX idx_conselhoos_links_empresa ON conselhoos_links(conselhoos_empresa_id);
"""
```

Adicionar migracao na funcao `init_db()`.

**Commit**: `git commit -m "feat(conselhoos): Add conselhoos_links table for data sync"`

---

## TAREFA 2: ConselhoOS Sync Service

**Status**: CONCLUIDO
**Prioridade**: ALTA

### Criar `app/services/conselhoos_sync.py`

```python
"""
ConselhoOS Sync Service
Sincroniza dados entre INTEL e ConselhoOS (app separado).

ConselhoOS usa Neon PostgreSQL com Drizzle ORM.
Conexao direta ao banco do ConselhoOS para leitura.

Autor: INTEL
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from database import get_db

logger = logging.getLogger(__name__)


class ConselhoOSSyncService:
    """
    Service para sincronizar dados entre INTEL e ConselhoOS.

    ConselhoOS tem seu proprio banco Neon. Conectamos diretamente
    para leitura e exibimos dados no INTEL.
    """

    def __init__(self):
        # ConselhoOS database URL (separada do INTEL)
        self.conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")
        self._cached_empresas = None
        self._cache_time = None

    def _get_conselhoos_conn(self):
        """Get connection to ConselhoOS database."""
        if not self.conselhoos_url:
            raise ValueError("CONSELHOOS_DATABASE_URL not configured")

        return psycopg2.connect(
            self.conselhoos_url,
            cursor_factory=RealDictCursor
        )

    def get_empresas(self, force_refresh: bool = False) -> List[Dict]:
        """
        Busca empresas do ConselhoOS.

        Returns:
            List of empresas with basic info
        """
        # Cache for 5 minutes
        if not force_refresh and self._cached_empresas and self._cache_time:
            if datetime.now() - self._cache_time < timedelta(minutes=5):
                return self._cached_empresas

        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        id,
                        nome,
                        setor,
                        descricao,
                        cor_hex,
                        logo_url,
                        created_at
                    FROM empresas
                    WHERE user_id = %s
                    ORDER BY nome
                """, (os.getenv("CONSELHOOS_USER_ID", ""),))

                empresas = [dict(row) for row in cursor.fetchall()]

                self._cached_empresas = empresas
                self._cache_time = datetime.now()

                return empresas

        except Exception as e:
            logger.error(f"Erro ao buscar empresas ConselhoOS: {e}")
            return []

    def get_proximas_reunioes(self, limit: int = 10) -> List[Dict]:
        """
        Busca proximas reunioes de todas as empresas.

        Returns:
            List of upcoming meetings
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        r.id,
                        r.titulo,
                        r.data,
                        r.status,
                        r.calendar_link,
                        e.id as empresa_id,
                        e.nome as empresa_nome,
                        e.cor_hex
                    FROM reunioes r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.data >= NOW()
                    AND r.status != 'cancelada'
                    ORDER BY r.data ASC
                    LIMIT %s
                """, (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar reunioes ConselhoOS: {e}")
            return []

    def get_raci_pendentes(self, limit: int = 20) -> List[Dict]:
        """
        Busca tarefas RACI pendentes/atrasadas.

        Returns:
            List of pending RACI items
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        r.id,
                        r.area,
                        r.acao,
                        r.prazo,
                        r.status,
                        r.responsavel_r,
                        e.id as empresa_id,
                        e.nome as empresa_nome,
                        e.cor_hex,
                        CASE
                            WHEN r.prazo < CURRENT_DATE THEN 'atrasado'
                            WHEN r.prazo <= CURRENT_DATE + INTERVAL '3 days' THEN 'urgente'
                            ELSE 'normal'
                        END as urgencia
                    FROM raci_itens r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.status IN ('pendente', 'em_andamento')
                    ORDER BY r.prazo ASC
                    LIMIT %s
                """, (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar RACI ConselhoOS: {e}")
            return []

    def get_empresa_detail(self, empresa_id: str) -> Optional[Dict]:
        """
        Busca detalhes de uma empresa.
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT *
                    FROM empresas
                    WHERE id = %s
                """, (empresa_id,))

                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

        except Exception as e:
            logger.error(f"Erro ao buscar empresa {empresa_id}: {e}")
            return None

    def link_contact_to_empresa(
        self,
        contact_id: int,
        empresa_id: str,
        empresa_nome: str,
        role: str = "stakeholder"
    ) -> Dict:
        """
        Vincula um contato INTEL a uma empresa do ConselhoOS.

        Args:
            contact_id: ID do contato no INTEL
            empresa_id: UUID da empresa no ConselhoOS
            empresa_nome: Nome da empresa
            role: Papel (membro_conselho, executivo, stakeholder)

        Returns:
            Result dict
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Check if link exists
                cursor.execute("""
                    SELECT id FROM conselhoos_links
                    WHERE contact_id = %s AND conselhoos_empresa_id = %s
                """, (contact_id, empresa_id))

                if cursor.fetchone():
                    # Update
                    cursor.execute("""
                        UPDATE conselhoos_links
                        SET role = %s, synced_at = NOW()
                        WHERE contact_id = %s AND conselhoos_empresa_id = %s
                    """, (role, contact_id, empresa_id))
                else:
                    # Insert
                    cursor.execute("""
                        INSERT INTO conselhoos_links
                        (contact_id, conselhoos_empresa_id, conselhoos_empresa_nome, role, synced_at)
                        VALUES (%s, %s, %s, %s, NOW())
                    """, (contact_id, empresa_id, empresa_nome, role))

                conn.commit()
                return {"success": True}

        except Exception as e:
            logger.error(f"Erro ao vincular contato: {e}")
            return {"error": str(e)}

    def get_contact_empresas(self, contact_id: int) -> List[Dict]:
        """
        Busca empresas vinculadas a um contato.
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        conselhoos_empresa_id,
                        conselhoos_empresa_nome,
                        role,
                        synced_at
                    FROM conselhoos_links
                    WHERE contact_id = %s
                """, (contact_id,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar empresas do contato: {e}")
            return []

    def get_dashboard_summary(self) -> Dict:
        """
        Resumo para exibir no dashboard INTEL.

        Returns:
            Dict with counts and highlights
        """
        try:
            empresas = self.get_empresas()
            reunioes = self.get_proximas_reunioes(limit=5)
            raci_pendentes = self.get_raci_pendentes(limit=10)

            # Count urgencies
            raci_atrasados = len([r for r in raci_pendentes if r.get("urgencia") == "atrasado"])
            raci_urgentes = len([r for r in raci_pendentes if r.get("urgencia") == "urgente"])

            # Next meeting
            proxima_reuniao = reunioes[0] if reunioes else None

            return {
                "total_empresas": len(empresas),
                "proximas_reunioes": len(reunioes),
                "raci_pendentes": len(raci_pendentes),
                "raci_atrasados": raci_atrasados,
                "raci_urgentes": raci_urgentes,
                "proxima_reuniao": proxima_reuniao,
                "empresas": empresas[:5],
                "reunioes": reunioes[:3],
                "raci_destaque": raci_pendentes[:3]
            }

        except Exception as e:
            logger.error(f"Erro ao buscar resumo ConselhoOS: {e}")
            return {
                "error": str(e),
                "total_empresas": 0,
                "proximas_reunioes": 0,
                "raci_pendentes": 0
            }


# Singleton
_conselhoos_sync_service = None


def get_conselhoos_sync_service() -> ConselhoOSSyncService:
    """Get singleton instance."""
    global _conselhoos_sync_service
    if _conselhoos_sync_service is None:
        _conselhoos_sync_service = ConselhoOSSyncService()
    return _conselhoos_sync_service
```

**Commit**: `git commit -m "feat(conselhoos): Add ConselhoOS sync service"`

---

## TAREFA 3: Endpoints ConselhoOS

**Status**: CONCLUIDO
**Prioridade**: ALTA

### Adicionar em main.py

```python
from services.conselhoos_sync import get_conselhoos_sync_service

# ============== ConselhoOS Sync ==============

@app.get("/api/conselhoos/empresas")
async def get_conselhoos_empresas(request: Request):
    """Get empresas from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    empresas = service.get_empresas()
    return {"empresas": empresas}


@app.get("/api/conselhoos/reunioes")
async def get_conselhoos_reunioes(request: Request, limit: int = 10):
    """Get upcoming meetings from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    reunioes = service.get_proximas_reunioes(limit=limit)
    return {"reunioes": reunioes}


@app.get("/api/conselhoos/raci")
async def get_conselhoos_raci(request: Request, limit: int = 20):
    """Get pending RACI items from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    raci = service.get_raci_pendentes(limit=limit)
    return {"raci": raci}


@app.get("/api/conselhoos/dashboard")
async def get_conselhoos_dashboard(request: Request):
    """Get ConselhoOS summary for INTEL dashboard."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    summary = service.get_dashboard_summary()
    return summary


@app.post("/api/contacts/{contact_id}/conselhoos/link")
async def link_contact_to_conselhoos(request: Request, contact_id: int):
    """Link a contact to a ConselhoOS empresa."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    empresa_id = data.get("empresa_id")
    empresa_nome = data.get("empresa_nome")
    role = data.get("role", "stakeholder")

    if not empresa_id or not empresa_nome:
        raise HTTPException(status_code=400, detail="empresa_id e empresa_nome obrigatorios")

    service = get_conselhoos_sync_service()
    result = service.link_contact_to_empresa(contact_id, empresa_id, empresa_nome, role)
    return result


@app.get("/api/contacts/{contact_id}/conselhoos")
async def get_contact_conselhoos(request: Request, contact_id: int):
    """Get ConselhoOS empresas linked to a contact."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    empresas = service.get_contact_empresas(contact_id)
    return {"empresas": empresas}
```

**Commit**: `git commit -m "feat(conselhoos): Add API endpoints for ConselhoOS sync"`

---

## TAREFA 4: Widget ConselhoOS no Dashboard

**Status**: CONCLUIDO
**Prioridade**: MEDIA

### Modificar `app/templates/rap_dashboard.html`

Adicionar widget de ConselhoOS na coluna direita.

#### CSS:

```css
/* ConselhoOS Widget */
.conselhoos-widget {
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    border-radius: 16px;
    padding: 20px;
    color: white;
}

.conselhoos-widget h3 {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
    font-size: 1rem;
}

.conselhoos-widget h3 img {
    width: 24px;
    height: 24px;
}

.conselhoos-stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 16px;
}

.conselhoos-stat {
    text-align: center;
    padding: 12px;
    background: rgba(255,255,255,0.1);
    border-radius: 8px;
}

.conselhoos-stat-value {
    font-size: 1.5rem;
    font-weight: 600;
}

.conselhoos-stat-label {
    font-size: 0.75rem;
    opacity: 0.8;
}

.conselhoos-next-meeting {
    background: rgba(255,255,255,0.1);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 12px;
}

.conselhoos-next-meeting .date {
    font-size: 0.875rem;
    opacity: 0.8;
}

.conselhoos-next-meeting .title {
    font-weight: 500;
}

.conselhoos-next-meeting .empresa {
    font-size: 0.875rem;
    display: flex;
    align-items: center;
    gap: 6px;
}

.conselhoos-next-meeting .empresa-color {
    width: 8px;
    height: 8px;
    border-radius: 50%;
}

.conselhoos-raci-alert {
    background: rgba(239, 68, 68, 0.2);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 8px;
    padding: 12px;
    font-size: 0.875rem;
}

.conselhoos-link {
    display: block;
    text-align: center;
    padding: 12px;
    background: rgba(255,255,255,0.1);
    border-radius: 8px;
    color: white;
    text-decoration: none;
    margin-top: 12px;
    transition: background 0.2s;
}

.conselhoos-link:hover {
    background: rgba(255,255,255,0.2);
}
```

#### HTML Widget:

```html
<!-- ConselhoOS Widget -->
<div class="conselhoos-widget" id="conselhoosWidget" style="display: none;">
    <h3>
        <i data-lucide="briefcase"></i>
        ConselhoOS
    </h3>

    <div class="conselhoos-stats">
        <div class="conselhoos-stat">
            <div class="conselhoos-stat-value" id="cosEmpresas">0</div>
            <div class="conselhoos-stat-label">Empresas</div>
        </div>
        <div class="conselhoos-stat">
            <div class="conselhoos-stat-value" id="cosReunioes">0</div>
            <div class="conselhoos-stat-label">Reunioes</div>
        </div>
        <div class="conselhoos-stat">
            <div class="conselhoos-stat-value" id="cosRaci">0</div>
            <div class="conselhoos-stat-label">RACI</div>
        </div>
    </div>

    <div id="cosNextMeeting" style="display: none;">
        <div class="conselhoos-next-meeting">
            <div class="date" id="cosNextDate">-</div>
            <div class="title" id="cosNextTitle">-</div>
            <div class="empresa">
                <span class="empresa-color" id="cosNextColor"></span>
                <span id="cosNextEmpresa">-</span>
            </div>
        </div>
    </div>

    <div id="cosRaciAlert" class="conselhoos-raci-alert" style="display: none;">
        <i data-lucide="alert-triangle"></i>
        <span id="cosRaciAlertText">-</span>
    </div>

    <a href="https://conselhoos.vercel.app" target="_blank" class="conselhoos-link">
        Abrir ConselhoOS <i data-lucide="external-link" style="width:14px;height:14px;"></i>
    </a>
</div>
```

#### JavaScript:

```javascript
async function loadConselhoOSWidget() {
    const widget = document.getElementById('conselhoosWidget');

    try {
        const response = await fetch('/api/conselhoos/dashboard');
        if (!response.ok) {
            widget.style.display = 'none';
            return;
        }

        const data = await response.json();

        if (data.error || data.total_empresas === 0) {
            widget.style.display = 'none';
            return;
        }

        widget.style.display = 'block';

        // Stats
        document.getElementById('cosEmpresas').textContent = data.total_empresas;
        document.getElementById('cosReunioes').textContent = data.proximas_reunioes;
        document.getElementById('cosRaci').textContent = data.raci_pendentes;

        // Next meeting
        if (data.proxima_reuniao) {
            const nextMeeting = document.getElementById('cosNextMeeting');
            nextMeeting.style.display = 'block';

            const date = new Date(data.proxima_reuniao.data);
            document.getElementById('cosNextDate').textContent = date.toLocaleDateString('pt-BR', {
                weekday: 'short',
                day: 'numeric',
                month: 'short',
                hour: '2-digit',
                minute: '2-digit'
            });
            document.getElementById('cosNextTitle').textContent = data.proxima_reuniao.titulo || 'Reuniao do Conselho';
            document.getElementById('cosNextEmpresa').textContent = data.proxima_reuniao.empresa_nome;
            document.getElementById('cosNextColor').style.background = data.proxima_reuniao.cor_hex || '#B89C60';
        }

        // RACI alert
        if (data.raci_atrasados > 0) {
            const alert = document.getElementById('cosRaciAlert');
            alert.style.display = 'block';
            document.getElementById('cosRaciAlertText').textContent =
                `${data.raci_atrasados} tarefa${data.raci_atrasados > 1 ? 's' : ''} RACI atrasada${data.raci_atrasados > 1 ? 's' : ''}`;
        }

        lucide.createIcons();

    } catch (e) {
        console.error('Error loading ConselhoOS widget:', e);
        widget.style.display = 'none';
    }
}

// Chamar no init
loadConselhoOSWidget();
```

**Commit**: `git commit -m "feat(dashboard): Add ConselhoOS widget to dashboard"`

---

## TAREFA 5: Secao ConselhoOS no Contato

**Status**: CONCLUIDO
**Prioridade**: MEDIA

### Modificar `app/templates/rap_contact_detail.html`

Adicionar secao mostrando vinculos com ConselhoOS.

```html
<!-- ConselhoOS Section -->
<div class="section-card" id="conselhoosSection" style="display: none;">
    <div class="section-header">
        <h3><i data-lucide="briefcase"></i> ConselhoOS</h3>
        <button class="btn btn-sm btn-secondary" onclick="openLinkConselhoOSModal()">
            <i data-lucide="link"></i> Vincular
        </button>
    </div>
    <div class="section-body" id="conselhoosLinks">
        <p class="text-secondary">Nenhuma empresa vinculada</p>
    </div>
</div>
```

#### JavaScript:

```javascript
async function loadConselhoOSLinks() {
    const section = document.getElementById('conselhoosSection');
    const container = document.getElementById('conselhoosLinks');

    try {
        const response = await fetch(`/api/contacts/${contactId}/conselhoos`);
        if (!response.ok) return;

        const data = await response.json();
        const empresas = data.empresas || [];

        if (empresas.length > 0) {
            section.style.display = 'block';
            container.innerHTML = empresas.map(e => `
                <div class="d-flex justify-content-between align-items-center p-2 bg-secondary rounded mb-2">
                    <div>
                        <strong>${e.conselhoos_empresa_nome}</strong>
                        <div class="text-secondary small">${e.role}</div>
                    </div>
                    <a href="https://conselhoos.vercel.app/empresa/${e.conselhoos_empresa_id}" target="_blank" class="btn btn-sm">
                        <i data-lucide="external-link"></i>
                    </a>
                </div>
            `).join('');
            lucide.createIcons();
        }

    } catch (e) {
        console.error('Error loading ConselhoOS links:', e);
    }
}

loadConselhoOSLinks();
```

**Commit**: `git commit -m "feat(contact): Add ConselhoOS links section"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Adicionar `CONSELHOOS_DATABASE_URL` ao .env com a connection string do banco ConselhoOS.

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-27 | Tabela de Vinculo | CONCLUIDO |
| 2026-03-27 | Sync Service | CONCLUIDO |
| 2026-03-27 | Endpoints API | CONCLUIDO |
| 2026-03-27 | Widget Dashboard | CONCLUIDO |
| 2026-03-27 | Secao no Contato | CONCLUIDO |
