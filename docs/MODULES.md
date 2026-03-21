# Modulos do Sistema - Referencia Detalhada

## Integracoes

### google_calendar.py
**Caminho**: `app/integrations/google_calendar.py`
**Funcao**: Criar reunioes no Google Calendar com Google Meet

**Funcoes principais**:
- `create_meeting()` - Cria evento com link Meet
- `get_available_slots()` - Retorna horarios disponiveis
- `cancel_meeting()` - Cancela evento

**Endpoints relacionados**:
- POST /api/meetings
- GET /api/meetings/slots

---

### google_contacts.py
**Caminho**: `app/integrations/google_contacts.py`
**Funcao**: Sincronizar contatos do Google

**Funcoes principais**:
- `sync_contacts()` - Sincroniza com Google People API
- `get_contact()` - Busca contato especifico
- `update_contact()` - Atualiza dados

**Endpoints relacionados**:
- GET /api/contacts
- POST /api/contacts/sync

---

### fathom.py
**Caminho**: `app/integrations/fathom.py`
**Funcao**: Integracao com Fathom para gravacao de reunioes

**Funcoes principais**:
- `sync_meetings()` - Sincroniza reunioes do Fathom
- `process_webhook()` - Processa eventos do webhook
- `extract_insights()` - Extrai resumo e action items

**Endpoints relacionados**:
- POST /api/webhooks/fathom
- GET /api/fathom/sync

---

### linkedin.py
**Caminho**: `app/integrations/linkedin.py`
**Funcao**: Enriquecimento de dados via LinkedIn

**Funcoes principais**:
- `enrich_prospect()` - Busca dados adicionais
- `import_connections()` - Importa conexoes

**Endpoints relacionados**:
- GET /rap/linkedin-import
- POST /api/linkedin/import

**STATUS**: Em desenvolvimento (INST-1)

---

### whatsapp.py
**Caminho**: `app/integrations/whatsapp.py`
**Funcao**: Mensagens via WhatsApp (Evolution API)

**Funcoes principais**:
- `send_message()` - Envia mensagem
- `get_qr_code()` - QR para conectar
- `sync_messages()` - Sincroniza conversas
- `normalize_phone()` - Normaliza formato BR

**Endpoints relacionados**:
- GET /api/whatsapp/status
- POST /api/whatsapp/send
- GET /api/whatsapp/qr
- POST /api/whatsapp/sync
- GET /api/whatsapp/chats

---

## Services

### scoring.py
**Caminho**: `app/scoring.py`
**Funcao**: Sistema de pontuacao dinamica com ML

**Fatores de score**:
| Fator | Pontos |
|-------|--------|
| CEO/Fundador | 30 |
| Diretor | 24 |
| Gerente | 15 |
| Consultoria (setor) | 20 |
| Tech (setor) | 15 |
| Email + Telefone | +10 |

**Tiers**:
- A: 50+ pontos
- B: 35-49 pontos
- C: 25-34 pontos
- D: 15-24 pontos
- E: <15 pontos

---

### contact_dedup.py
**Caminho**: `app/services/contact_dedup.py`
**Funcao**: Deteccao e merge de duplicatas

**Funcoes principais**:
- `find_duplicates()` - Encontra duplicatas por tel/email
- `merge_contacts()` - Combina dois contatos
- `normalize_name()` - Corrige caps lock, etc

---

### linkedin_import.py
**Caminho**: `app/services/linkedin_import.py`
**Funcao**: Importacao de contatos do LinkedIn

**STATUS**: Em desenvolvimento (INST-1)

---

## Templates Principais

| Template | Caminho | Usuario |
|----------|---------|---------|
| dashboard.html | app/templates/ | Andressa |
| admin.html | app/templates/ | Renato |
| prospect_detail.html | app/templates/ | Ambos |
| rap_*.html | app/templates/ | Renato |

---

## Endpoints Criticos

### Autenticacao
```
GET  /login
GET  /logout
GET  /auth/google/login
GET  /auth/google/callback
```

### Prospects
```
GET    /api/prospects
GET    /api/prospects/{id}
POST   /api/prospects
PATCH  /api/prospects/{id}
DELETE /api/prospects/{id}
POST   /api/prospects/{id}/convert
```

### Admin
```
GET  /api/admin/pending
POST /api/admin/approve/{id}
POST /api/admin/approve-bulk
GET  /api/admin/stats
```

### Interactions
```
GET    /api/prospects/{id}/interactions
POST   /api/prospects/{id}/interactions
PUT    /api/interactions/{id}
DELETE /api/interactions/{id}
```
