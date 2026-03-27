# Fila de Tarefas 2INTEL - WhatsApp Completo

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

## CONTEXTO

Ja existe:
- `app/integrations/whatsapp.py` - Evolution API integration
- `app/services/whatsapp_sync.py` - Sync service
- Webhook endpoint em main.py
- Templates de mensagem

---

## TAREFA 1: Parser WhatsApp .txt

**Status**: CONCLUIDO
**Prioridade**: CRITICA

### Criar `app/services/whatsapp_import.py`

Parser para arquivos .txt exportados do WhatsApp (formato iOS e Android).

```python
"""
WhatsApp Chat Import Service
Importa historico de conversas de arquivos .txt exportados do WhatsApp.

Formatos suportados:
- iOS: [DD/MM/YYYY, HH:MM:SS] Nome: Mensagem
- Android: DD/MM/YYYY HH:MM - Nome: Mensagem
- Android alt: DD/MM/YY, HH:MM - Nome: Mensagem

Autor: INTEL
"""
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from database import get_db

logger = logging.getLogger(__name__)


class WhatsAppImportService:
    """Service para importar chats do WhatsApp de arquivos .txt"""

    # Regex patterns para diferentes formatos
    PATTERNS = [
        # iOS: [DD/MM/YYYY, HH:MM:SS] Nome: Mensagem
        re.compile(r'^\[(\d{2}/\d{2}/\d{4}), (\d{2}:\d{2}:\d{2})\] ([^:]+): (.+)$'),
        # Android: DD/MM/YYYY HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Android alt: DD/MM/YY, HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{2}), (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Outro formato comum
        re.compile(r'^(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[AP]M)? [-–] ([^:]+): (.+)$'),
    ]

    # Mensagens de sistema para ignorar
    SYSTEM_MESSAGES = [
        'criou este grupo',
        'adicionou',
        'removeu',
        'saiu',
        'mudou o assunto',
        'mudou a imagem',
        'as mensagens e ligacoes',
        'messages and calls are end-to-end encrypted',
        'criptografia de ponta a ponta',
        'alterou as configuracoes',
        'entrou usando o link',
        'agora e admin',
        'deixou de ser admin',
    ]

    def __init__(self):
        self._import_status = {
            "running": False,
            "filename": None,
            "total_lines": 0,
            "parsed_messages": 0,
            "linked_contacts": 0,
            "saved_messages": 0,
            "errors": [],
            "participants": []
        }

    def _parse_date(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse date and time strings to datetime object."""
        formats = [
            ('%d/%m/%Y', '%H:%M:%S'),
            ('%d/%m/%Y', '%H:%M'),
            ('%d/%m/%y', '%H:%M'),
            ('%m/%d/%Y', '%H:%M:%S'),
            ('%m/%d/%Y', '%H:%M'),
            ('%m/%d/%y', '%H:%M'),
        ]

        for date_fmt, time_fmt in formats:
            try:
                dt_str = f"{date_str} {time_str}"
                return datetime.strptime(dt_str, f"{date_fmt} {time_fmt}")
            except ValueError:
                continue

        return None

    def _is_system_message(self, content: str) -> bool:
        """Check if message is a system message."""
        content_lower = content.lower()
        return any(sys_msg in content_lower for sys_msg in self.SYSTEM_MESSAGES)

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number to digits only."""
        return re.sub(r'\D', '', phone)

    def _extract_phone_from_name(self, name: str) -> Optional[str]:
        """Try to extract phone number from participant name."""
        # WhatsApp sometimes shows phone as name: +55 11 98765-4321
        digits = self._normalize_phone(name)
        if len(digits) >= 10:
            return digits
        return None

    def parse_file(self, content: str, filename: str = "chat.txt") -> Dict[str, Any]:
        """
        Parse WhatsApp export file content.

        Args:
            content: File content as string
            filename: Original filename

        Returns:
            Dict with parsed messages and metadata
        """
        self._import_status = {
            "running": True,
            "filename": filename,
            "total_lines": 0,
            "parsed_messages": 0,
            "linked_contacts": 0,
            "saved_messages": 0,
            "errors": [],
            "participants": []
        }

        lines = content.split('\n')
        self._import_status["total_lines"] = len(lines)

        messages = []
        participants = set()
        current_message = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to match with patterns
            matched = False
            for pattern in self.PATTERNS:
                match = pattern.match(line)
                if match:
                    # Save previous message if exists
                    if current_message:
                        messages.append(current_message)

                    date_str, time_str, sender, content = match.groups()

                    # Skip system messages
                    if self._is_system_message(content):
                        matched = True
                        current_message = None
                        break

                    # Parse datetime
                    timestamp = self._parse_date(date_str, time_str)
                    if not timestamp:
                        continue

                    # Extract phone if sender is a phone number
                    phone = self._extract_phone_from_name(sender)

                    current_message = {
                        "sender": sender.strip(),
                        "phone": phone,
                        "content": content.strip(),
                        "timestamp": timestamp,
                        "is_media": self._is_media_message(content)
                    }

                    participants.add(sender.strip())
                    matched = True
                    break

            # If no pattern matched, it's a continuation of previous message
            if not matched and current_message:
                current_message["content"] += "\n" + line

        # Don't forget last message
        if current_message:
            messages.append(current_message)

        self._import_status["parsed_messages"] = len(messages)
        self._import_status["participants"] = list(participants)
        self._import_status["running"] = False

        return {
            "filename": filename,
            "total_messages": len(messages),
            "participants": list(participants),
            "messages": messages,
            "date_range": {
                "start": messages[0]["timestamp"].isoformat() if messages else None,
                "end": messages[-1]["timestamp"].isoformat() if messages else None
            }
        }

    def _is_media_message(self, content: str) -> bool:
        """Check if message is a media message."""
        media_indicators = [
            '<midia oculta>',
            '<media omitted>',
            'imagem anexada',
            'video anexado',
            'audio anexado',
            'documento anexado',
            'figurinha omitida',
            'sticker omitted',
            '.jpg',
            '.mp4',
            '.opus',
            '.pdf',
        ]
        content_lower = content.lower()
        return any(indicator in content_lower for indicator in media_indicators)

    def find_contact_by_name_or_phone(self, sender: str, phone: Optional[str] = None) -> Optional[Dict]:
        """
        Find contact by name or phone number.

        Args:
            sender: Sender name from WhatsApp
            phone: Phone number if available

        Returns:
            Contact dict or None
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Try phone first
            if phone and len(phone) >= 8:
                cursor.execute("""
                    SELECT id, nome, telefones
                    FROM contacts
                    WHERE telefones IS NOT NULL AND telefones::text != '[]'
                """)
                contacts = cursor.fetchall()

                for contact in contacts:
                    telefones = contact["telefones"]
                    if isinstance(telefones, str):
                        try:
                            telefones = json.loads(telefones)
                        except:
                            telefones = []

                    for tel in telefones:
                        tel_number = tel.get("number", "") if isinstance(tel, dict) else str(tel)
                        tel_digits = self._normalize_phone(tel_number)

                        if tel_digits and len(tel_digits) >= 8:
                            if tel_digits[-9:] == phone[-9:] or tel_digits[-8:] == phone[-8:]:
                                return dict(contact)

            # Try name match
            cursor.execute("""
                SELECT id, nome FROM contacts
                WHERE LOWER(nome) = LOWER(%s)
                   OR LOWER(nome) LIKE LOWER(%s)
                LIMIT 1
            """, (sender, f"%{sender}%"))

            result = cursor.fetchone()
            if result:
                return dict(result)

            return None

    def import_to_contact(
        self,
        messages: List[Dict],
        contact_id: int,
        my_name: str = "Renato"
    ) -> Dict[str, Any]:
        """
        Import messages to a specific contact.

        Args:
            messages: List of parsed messages
            contact_id: Target contact ID
            my_name: Your name in the chat (to determine direction)

        Returns:
            Import statistics
        """
        result = {
            "contact_id": contact_id,
            "imported": 0,
            "skipped": 0,
            "errors": 0
        }

        with get_db() as conn:
            cursor = conn.cursor()

            # Ensure whatsapp_messages table exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS whatsapp_messages (
                    id SERIAL PRIMARY KEY,
                    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                    phone VARCHAR(50),
                    message_id VARCHAR(100) UNIQUE,
                    direction VARCHAR(20) NOT NULL,
                    content TEXT,
                    message_type VARCHAR(50) DEFAULT 'text',
                    message_date TIMESTAMP,
                    imported_from VARCHAR(255),
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Get contact info
            cursor.execute("SELECT nome FROM contacts WHERE id = %s", (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact_name = contact["nome"]

            # Import messages
            latest_date = None
            for msg in messages:
                try:
                    sender = msg["sender"]
                    content = msg["content"]
                    timestamp = msg["timestamp"]
                    is_media = msg.get("is_media", False)

                    # Determine direction
                    sender_lower = sender.lower()
                    my_name_lower = my_name.lower()

                    if my_name_lower in sender_lower or sender_lower in my_name_lower:
                        direction = "outbound"
                    else:
                        direction = "inbound"

                    # Generate unique message ID
                    msg_id = f"import_{contact_id}_{timestamp.timestamp()}_{hash(content) % 100000}"

                    # Insert message
                    cursor.execute("""
                        INSERT INTO whatsapp_messages
                        (contact_id, direction, content, message_type, message_date, message_id, imported_from)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_id) DO NOTHING
                        RETURNING id
                    """, (
                        contact_id,
                        direction,
                        content,
                        "media" if is_media else "text",
                        timestamp,
                        msg_id,
                        "whatsapp_export"
                    ))

                    if cursor.fetchone():
                        result["imported"] += 1
                        if latest_date is None or timestamp > latest_date:
                            latest_date = timestamp
                    else:
                        result["skipped"] += 1

                except Exception as e:
                    logger.error(f"Erro ao importar mensagem: {e}")
                    result["errors"] += 1

            # Update contact interaction
            if latest_date:
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = GREATEST(COALESCE(ultimo_contato, %s), %s),
                        total_interacoes = COALESCE(total_interacoes, 0) + %s
                    WHERE id = %s
                """, (latest_date, latest_date, result["imported"], contact_id))

            conn.commit()

        return result

    def get_import_status(self) -> Dict[str, Any]:
        """Return current import status."""
        return self._import_status.copy()


# Singleton
_whatsapp_import_service = None


def get_whatsapp_import_service() -> WhatsAppImportService:
    """Get singleton instance."""
    global _whatsapp_import_service
    if _whatsapp_import_service is None:
        _whatsapp_import_service = WhatsAppImportService()
    return _whatsapp_import_service
```

**Commit**: `git commit -m "feat(whatsapp): Add WhatsApp chat import service for .txt files"`

---

## TAREFA 2: Endpoints de Import

**Status**: CONCLUIDO
**Prioridade**: ALTA

### Adicionar endpoints em `app/main.py`

```python
from services.whatsapp_import import get_whatsapp_import_service

# ============== WhatsApp Import ==============

@app.post("/api/whatsapp/import/parse")
async def parse_whatsapp_file(request: Request, file: UploadFile = File(...)):
    """
    Parse WhatsApp export file and return preview.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        content = await file.read()
        content_str = content.decode('utf-8', errors='ignore')

        service = get_whatsapp_import_service()
        result = service.parse_file(content_str, file.filename)

        return result

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/whatsapp/import/confirm")
async def confirm_whatsapp_import(request: Request):
    """
    Confirm import of parsed messages to a contact.

    Body:
    {
        "messages": [...],
        "contact_id": 123,
        "my_name": "Renato"
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        data = await request.json()
        messages = data.get("messages", [])
        contact_id = data.get("contact_id")
        my_name = data.get("my_name", "Renato")

        if not contact_id:
            raise HTTPException(status_code=400, detail="contact_id obrigatorio")

        # Convert timestamp strings back to datetime
        for msg in messages:
            if isinstance(msg.get("timestamp"), str):
                msg["timestamp"] = datetime.fromisoformat(msg["timestamp"])

        service = get_whatsapp_import_service()
        result = service.import_to_contact(messages, contact_id, my_name)

        return result

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/whatsapp/import/status")
async def get_import_status(request: Request):
    """Get current import status."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_whatsapp_import_service()
    return service.get_import_status()


@app.get("/api/whatsapp/messages/{contact_id}")
async def get_whatsapp_messages(request: Request, contact_id: int, limit: int = 100):
    """Get WhatsApp messages for a contact."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, direction, content, message_type, message_date, imported_from
            FROM whatsapp_messages
            WHERE contact_id = %s
            ORDER BY message_date DESC
            LIMIT %s
        """, (contact_id, limit))

        messages = cursor.fetchall()
        return {"messages": [dict(m) for m in messages]}
```

**Commit**: `git commit -m "feat(whatsapp): Add import endpoints for WhatsApp chat history"`

---

## TAREFA 3: Pagina de Configuracoes WhatsApp

**Status**: CONCLUIDO
**Prioridade**: ALTA

### Criar `app/templates/rap_whatsapp.html`

```html
{% extends "rap_base.html" %}

{% block title %}WhatsApp - INTEL{% endblock %}

{% block extra_head %}
<style>
    .whatsapp-container {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 24px;
    }

    .status-card {
        background: var(--card-bg);
        border-radius: 16px;
        padding: 24px;
    }

    .status-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 20px;
    }

    .status-icon {
        width: 48px;
        height: 48px;
        border-radius: 12px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
    }

    .status-icon.connected {
        background: rgba(34, 197, 94, 0.1);
        color: #22c55e;
    }

    .status-icon.disconnected {
        background: rgba(239, 68, 68, 0.1);
        color: #ef4444;
    }

    .status-icon.connecting {
        background: rgba(234, 179, 8, 0.1);
        color: #eab308;
    }

    .status-details h2 {
        font-size: 1.25rem;
        margin-bottom: 4px;
    }

    .status-text {
        font-size: 0.875rem;
        color: var(--text-secondary);
    }

    .qr-section {
        text-align: center;
        padding: 24px;
        background: var(--bg-secondary);
        border-radius: 12px;
        margin-top: 20px;
    }

    .qr-code {
        width: 200px;
        height: 200px;
        margin: 0 auto 16px;
        background: white;
        border-radius: 8px;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .qr-code img {
        max-width: 180px;
        max-height: 180px;
    }

    .import-section {
        background: var(--card-bg);
        border-radius: 16px;
        padding: 24px;
    }

    .import-section h3 {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 16px;
    }

    .upload-area {
        border: 2px dashed var(--border-color);
        border-radius: 12px;
        padding: 40px;
        text-align: center;
        cursor: pointer;
        transition: all 0.2s;
    }

    .upload-area:hover {
        border-color: var(--primary);
        background: rgba(37, 211, 102, 0.05);
    }

    .upload-area.dragover {
        border-color: var(--primary);
        background: rgba(37, 211, 102, 0.1);
    }

    .upload-icon {
        font-size: 3rem;
        color: #25d366;
        margin-bottom: 12px;
    }

    .upload-text {
        font-size: 1rem;
        margin-bottom: 8px;
    }

    .upload-hint {
        font-size: 0.875rem;
        color: var(--text-secondary);
    }

    .preview-section {
        margin-top: 20px;
        display: none;
    }

    .preview-section.active {
        display: block;
    }

    .preview-stats {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 12px;
        margin-bottom: 16px;
    }

    .preview-stat {
        background: var(--bg-secondary);
        padding: 12px;
        border-radius: 8px;
        text-align: center;
    }

    .preview-stat-value {
        font-size: 1.5rem;
        font-weight: 600;
        color: var(--primary);
    }

    .preview-stat-label {
        font-size: 0.75rem;
        color: var(--text-secondary);
    }

    .participants-list {
        margin: 16px 0;
    }

    .participant-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px;
        background: var(--bg-secondary);
        border-radius: 8px;
        margin-bottom: 8px;
    }

    .participant-name {
        font-weight: 500;
    }

    .contact-select {
        padding: 6px 12px;
        border-radius: 6px;
        border: 1px solid var(--border-color);
        background: var(--card-bg);
        color: var(--text-primary);
        font-size: 0.875rem;
        min-width: 200px;
    }

    .templates-section {
        background: var(--card-bg);
        border-radius: 16px;
        padding: 24px;
    }

    .template-list {
        display: grid;
        gap: 12px;
    }

    .template-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px;
        background: var(--bg-secondary);
        border-radius: 12px;
        cursor: pointer;
        transition: all 0.2s;
    }

    .template-item:hover {
        transform: translateX(4px);
    }

    .template-info h4 {
        font-weight: 500;
        margin-bottom: 4px;
    }

    .template-info p {
        font-size: 0.875rem;
        color: var(--text-secondary);
    }

    .template-category {
        font-size: 0.75rem;
        padding: 4px 8px;
        border-radius: 12px;
        background: rgba(37, 211, 102, 0.1);
        color: #25d366;
    }

    @media (max-width: 1024px) {
        .whatsapp-container {
            grid-template-columns: 1fr;
        }
    }
</style>
{% endblock %}

{% block content %}
<div class="page-header">
    <div>
        <h1><i data-lucide="message-circle" style="color: #25d366;"></i> WhatsApp</h1>
        <p class="subtitle">Configuracoes e importacao de historico</p>
    </div>
</div>

<div class="whatsapp-container">
    <!-- Status e QR Code -->
    <div class="status-card">
        <div class="status-header">
            <div class="status-icon" id="statusIcon">
                <i data-lucide="wifi-off"></i>
            </div>
            <div class="status-details">
                <h2 id="statusTitle">Verificando conexao...</h2>
                <p class="status-text" id="statusText">Aguarde...</p>
            </div>
        </div>

        <div class="qr-section" id="qrSection" style="display: none;">
            <div class="qr-code" id="qrCode">
                <i data-lucide="loader" class="animate-spin"></i>
            </div>
            <p>Escaneie o QR Code com seu WhatsApp</p>
            <button class="btn btn-secondary mt-3" onclick="refreshQR()">
                <i data-lucide="refresh-cw"></i> Atualizar QR
            </button>
        </div>

        <div id="connectedInfo" style="display: none;">
            <div class="mt-4 p-4 bg-success-light rounded-lg">
                <p><strong>Instancia:</strong> <span id="instanceName">-</span></p>
                <p><strong>Numero:</strong> <span id="connectedNumber">-</span></p>
            </div>
            <button class="btn btn-danger mt-3" onclick="disconnect()">
                <i data-lucide="log-out"></i> Desconectar
            </button>
        </div>

        <div class="mt-4">
            <button class="btn btn-primary" onclick="syncChats()">
                <i data-lucide="refresh-cw"></i> Sincronizar Chats
            </button>
        </div>
    </div>

    <!-- Import Section -->
    <div class="import-section">
        <h3><i data-lucide="upload"></i> Importar Historico</h3>
        <p class="text-secondary mb-4">Exporte uma conversa do WhatsApp como .txt e importe aqui</p>

        <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
            <div class="upload-icon">
                <i data-lucide="file-text"></i>
            </div>
            <p class="upload-text">Clique ou arraste o arquivo .txt</p>
            <p class="upload-hint">Exportado do WhatsApp (iOS ou Android)</p>
        </div>
        <input type="file" id="fileInput" accept=".txt" style="display: none;" onchange="handleFileSelect(event)">

        <div class="preview-section" id="previewSection">
            <h4 class="mb-3">Preview da Importacao</h4>

            <div class="preview-stats">
                <div class="preview-stat">
                    <div class="preview-stat-value" id="previewMessages">0</div>
                    <div class="preview-stat-label">Mensagens</div>
                </div>
                <div class="preview-stat">
                    <div class="preview-stat-value" id="previewParticipants">0</div>
                    <div class="preview-stat-label">Participantes</div>
                </div>
                <div class="preview-stat">
                    <div class="preview-stat-value" id="previewDays">0</div>
                    <div class="preview-stat-label">Dias</div>
                </div>
            </div>

            <div class="participants-list" id="participantsList">
                <!-- Filled by JS -->
            </div>

            <div class="form-group">
                <label>Seu nome no chat (para identificar direcao)</label>
                <input type="text" id="myNameInput" class="form-control" value="Renato" placeholder="Seu nome">
            </div>

            <button class="btn btn-primary w-100 mt-3" onclick="confirmImport()">
                <i data-lucide="check"></i> Importar Mensagens
            </button>
        </div>
    </div>
</div>

<!-- Templates Section -->
<div class="templates-section mt-4">
    <h3><i data-lucide="file-text"></i> Templates de Mensagem</h3>
    <div class="template-list" id="templateList">
        <!-- Filled by JS -->
    </div>
</div>

<script>
let parsedData = null;
let contacts = [];

document.addEventListener('DOMContentLoaded', function() {
    lucide.createIcons();
    checkConnectionStatus();
    loadTemplates();
    loadContacts();
    setupDragDrop();
});

async function checkConnectionStatus() {
    const statusIcon = document.getElementById('statusIcon');
    const statusTitle = document.getElementById('statusTitle');
    const statusText = document.getElementById('statusText');
    const qrSection = document.getElementById('qrSection');
    const connectedInfo = document.getElementById('connectedInfo');

    try {
        const response = await fetch('/api/whatsapp/status');
        const data = await response.json();

        if (data.state === 'open' || data.state === 'connected') {
            statusIcon.className = 'status-icon connected';
            statusIcon.innerHTML = '<i data-lucide="wifi"></i>';
            statusTitle.textContent = 'Conectado';
            statusText.textContent = 'WhatsApp sincronizado';
            qrSection.style.display = 'none';
            connectedInfo.style.display = 'block';
            document.getElementById('instanceName').textContent = data.instance || '-';
            document.getElementById('connectedNumber').textContent = data.number || '-';
        } else if (data.state === 'connecting') {
            statusIcon.className = 'status-icon connecting';
            statusIcon.innerHTML = '<i data-lucide="loader" class="animate-spin"></i>';
            statusTitle.textContent = 'Conectando...';
            statusText.textContent = 'Escaneie o QR Code';
            qrSection.style.display = 'block';
            connectedInfo.style.display = 'none';
            loadQRCode();
        } else {
            statusIcon.className = 'status-icon disconnected';
            statusIcon.innerHTML = '<i data-lucide="wifi-off"></i>';
            statusTitle.textContent = 'Desconectado';
            statusText.textContent = 'Escaneie o QR Code para conectar';
            qrSection.style.display = 'block';
            connectedInfo.style.display = 'none';
            loadQRCode();
        }
    } catch (e) {
        statusIcon.className = 'status-icon disconnected';
        statusTitle.textContent = 'Erro de conexao';
        statusText.textContent = 'Nao foi possivel verificar status';
    }

    lucide.createIcons();
}

async function loadQRCode() {
    const qrCode = document.getElementById('qrCode');
    try {
        const response = await fetch('/api/whatsapp/qr');
        const data = await response.json();
        if (data.qrcode) {
            qrCode.innerHTML = `<img src="${data.qrcode}" alt="QR Code">`;
        } else {
            qrCode.innerHTML = '<p class="text-secondary">QR nao disponivel</p>';
        }
    } catch (e) {
        qrCode.innerHTML = '<p class="text-secondary">Erro ao carregar QR</p>';
    }
}

function refreshQR() {
    loadQRCode();
}

async function syncChats() {
    try {
        showToast('Sincronizando chats...');
        const response = await fetch('/api/whatsapp/sync', { method: 'POST' });
        const data = await response.json();
        showToast(`Sincronizado: ${data.linked || 0} contatos vinculados`);
    } catch (e) {
        showToast('Erro ao sincronizar', 'error');
    }
}

async function loadContacts() {
    try {
        const response = await fetch('/api/contacts?limit=500');
        const data = await response.json();
        contacts = data.contacts || [];
    } catch (e) {
        console.error('Error loading contacts:', e);
    }
}

function setupDragDrop() {
    const uploadArea = document.getElementById('uploadArea');

    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });

    uploadArea.addEventListener('dragleave', () => {
        uploadArea.classList.remove('dragover');
    });

    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        const file = e.dataTransfer.files[0];
        if (file) processFile(file);
    });
}

function handleFileSelect(event) {
    const file = event.target.files[0];
    if (file) processFile(file);
}

async function processFile(file) {
    if (!file.name.endsWith('.txt')) {
        showToast('Por favor selecione um arquivo .txt', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    try {
        showToast('Analisando arquivo...');

        const response = await fetch('/api/whatsapp/import/parse', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) throw new Error('Erro ao processar arquivo');

        parsedData = await response.json();
        showPreview(parsedData);

    } catch (e) {
        showToast('Erro ao processar arquivo', 'error');
        console.error(e);
    }
}

function showPreview(data) {
    document.getElementById('previewSection').classList.add('active');
    document.getElementById('previewMessages').textContent = data.total_messages;
    document.getElementById('previewParticipants').textContent = data.participants.length;

    // Calculate days
    if (data.date_range.start && data.date_range.end) {
        const start = new Date(data.date_range.start);
        const end = new Date(data.date_range.end);
        const days = Math.ceil((end - start) / (1000 * 60 * 60 * 24));
        document.getElementById('previewDays').textContent = days;
    }

    // Show participants with contact selector
    const participantsList = document.getElementById('participantsList');
    participantsList.innerHTML = data.participants.map(p => `
        <div class="participant-item">
            <span class="participant-name">${p}</span>
            <select class="contact-select" data-participant="${p}">
                <option value="">Selecionar contato...</option>
                ${contacts.map(c => `<option value="${c.id}">${c.nome}</option>`).join('')}
            </select>
        </div>
    `).join('');
}

async function confirmImport() {
    if (!parsedData) return;

    // Get selected contact
    const selects = document.querySelectorAll('.contact-select');
    let selectedContactId = null;

    selects.forEach(select => {
        if (select.value) {
            selectedContactId = parseInt(select.value);
        }
    });

    if (!selectedContactId) {
        showToast('Selecione um contato para importar', 'error');
        return;
    }

    const myName = document.getElementById('myNameInput').value || 'Renato';

    try {
        showToast('Importando mensagens...');

        const response = await fetch('/api/whatsapp/import/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: parsedData.messages,
                contact_id: selectedContactId,
                my_name: myName
            })
        });

        const result = await response.json();

        if (result.error) {
            showToast(result.error, 'error');
        } else {
            showToast(`Importado: ${result.imported} mensagens`);
            document.getElementById('previewSection').classList.remove('active');
            parsedData = null;
        }

    } catch (e) {
        showToast('Erro ao importar', 'error');
    }
}

async function loadTemplates() {
    try {
        const response = await fetch('/api/whatsapp/templates');
        const data = await response.json();

        const templateList = document.getElementById('templateList');
        templateList.innerHTML = (data.templates || []).map(t => `
            <div class="template-item" onclick="useTemplate('${t.id}')">
                <div class="template-info">
                    <h4>${t.nome}</h4>
                    <p>${t.descricao}</p>
                </div>
                <span class="template-category">${t.categoria}</span>
            </div>
        `).join('');

    } catch (e) {
        console.error('Error loading templates:', e);
    }
}

function useTemplate(templateId) {
    // Could open a modal to use the template
    showToast('Template selecionado: ' + templateId);
}

function showToast(message, type = 'success') {
    if (window.showToast) {
        window.showToast(message, type);
    } else {
        alert(message);
    }
}
</script>
{% endblock %}
```

### Adicionar rota em main.py

```python
@app.get("/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request):
    """Pagina de configuracoes WhatsApp"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("rap_whatsapp.html", {"request": request, "user": user})
```

### Adicionar link no sidebar (rap_base.html)

```html
<a href="/whatsapp" class="nav-item {% if request.url.path == '/whatsapp' %}active{% endif %}">
    <i data-lucide="message-circle"></i>
    <span>WhatsApp</span>
</a>
```

**Commit**: `git commit -m "feat(whatsapp): Add WhatsApp settings and import page"`

---

## TAREFA 4: Endpoints de Status e QR Code

**Status**: CONCLUIDO (ja existia)
**Prioridade**: ALTA

### Adicionar em main.py

```python
from integrations.whatsapp import WhatsAppIntegration, get_all_templates

@app.get("/api/whatsapp/status")
async def get_whatsapp_status(request: Request):
    """Get WhatsApp connection status."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    wa = WhatsAppIntegration()
    status = await wa.get_connection_status()
    return status


@app.get("/api/whatsapp/qr")
async def get_whatsapp_qr(request: Request):
    """Get QR Code for WhatsApp connection."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    wa = WhatsAppIntegration()

    # Evolution API endpoint for QR
    import httpx
    base_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
    api_key = os.getenv("EVOLUTION_API_KEY", "")
    instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

    if not base_url or not api_key:
        return {"error": "Evolution API not configured"}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{base_url}/instance/connect/{instance}",
                headers={"apikey": api_key},
                timeout=10.0
            )
            data = response.json()
            return {"qrcode": data.get("base64") or data.get("qrcode")}
        except Exception as e:
            return {"error": str(e)}


@app.post("/api/whatsapp/sync")
async def sync_whatsapp_chats(request: Request):
    """Sync all WhatsApp chats with contacts."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.whatsapp_sync import get_whatsapp_sync_service
    service = get_whatsapp_sync_service()
    result = await service.sync_all_chats()
    return result


@app.get("/api/whatsapp/templates")
async def get_whatsapp_templates(request: Request):
    """Get all message templates."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    templates = get_all_templates()
    return {"templates": templates}
```

**Commit**: `git commit -m "feat(whatsapp): Add status, QR code, and sync endpoints"`

---

## TAREFA 5: Modal de Envio no Contato

**Status**: CONCLUIDO
**Prioridade**: MEDIA

### Modificar `app/templates/rap_contact_detail.html`

Adicionar botao e modal para enviar WhatsApp.

#### Adicionar botao no header do contato:

```html
<button class="btn btn-success" onclick="openWhatsAppModal()" title="Enviar WhatsApp">
    <i data-lucide="message-circle"></i>
    WhatsApp
</button>
```

#### Adicionar modal:

```html
<!-- WhatsApp Modal -->
<div class="modal" id="whatsappModal">
    <div class="modal-content" style="max-width: 500px;">
        <div class="modal-header">
            <h3><i data-lucide="message-circle" style="color: #25d366;"></i> Enviar WhatsApp</h3>
            <button class="btn-close" onclick="closeWhatsAppModal()">&times;</button>
        </div>
        <div class="modal-body">
            <div class="form-group">
                <label>Telefone</label>
                <select id="waPhoneSelect" class="form-control">
                    <!-- Populated by JS -->
                </select>
            </div>

            <div class="form-group">
                <label>Template (opcional)</label>
                <select id="waTemplateSelect" class="form-control" onchange="applyTemplate()">
                    <option value="">Mensagem livre</option>
                </select>
            </div>

            <div class="form-group">
                <label>Mensagem</label>
                <textarea id="waMessage" class="form-control" rows="5" placeholder="Digite sua mensagem..."></textarea>
            </div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="closeWhatsAppModal()">Cancelar</button>
            <button class="btn btn-success" onclick="sendWhatsApp()">
                <i data-lucide="send"></i> Enviar
            </button>
        </div>
    </div>
</div>
```

#### Adicionar JavaScript:

```javascript
let waTemplates = [];

async function openWhatsAppModal() {
    // Populate phones
    const phoneSelect = document.getElementById('waPhoneSelect');
    phoneSelect.innerHTML = '';

    const telefones = contactData.telefones || [];
    if (telefones.length === 0) {
        showToast('Contato sem telefone cadastrado', 'error');
        return;
    }

    telefones.forEach((tel, i) => {
        const number = tel.number || tel.phone || tel;
        const label = tel.label || 'Telefone';
        phoneSelect.innerHTML += `<option value="${number}">${label}: ${number}</option>`;
    });

    // Load templates
    if (waTemplates.length === 0) {
        try {
            const response = await fetch('/api/whatsapp/templates');
            const data = await response.json();
            waTemplates = data.templates || [];
        } catch (e) {}
    }

    const templateSelect = document.getElementById('waTemplateSelect');
    templateSelect.innerHTML = '<option value="">Mensagem livre</option>';
    waTemplates.forEach(t => {
        templateSelect.innerHTML += `<option value="${t.id}">${t.nome}</option>`;
    });

    document.getElementById('waMessage').value = '';
    document.getElementById('whatsappModal').classList.add('active');
}

function closeWhatsAppModal() {
    document.getElementById('whatsappModal').classList.remove('active');
}

function applyTemplate() {
    const templateId = document.getElementById('waTemplateSelect').value;
    if (!templateId) {
        document.getElementById('waMessage').value = '';
        return;
    }

    const template = waTemplates.find(t => t.id === templateId);
    if (template) {
        let msg = template.mensagem;
        // Replace variables
        msg = msg.replace('{nome}', contactData.nome.split(' ')[0]);
        msg = msg.replace('{empresa}', contactData.empresa || '');
        document.getElementById('waMessage').value = msg;
    }
}

async function sendWhatsApp() {
    const phone = document.getElementById('waPhoneSelect').value;
    const message = document.getElementById('waMessage').value;

    if (!phone || !message) {
        showToast('Preencha telefone e mensagem', 'error');
        return;
    }

    try {
        const response = await fetch('/api/whatsapp/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                phone: phone,
                message: message,
                contact_id: contactId
            })
        });

        const result = await response.json();

        if (result.error) {
            showToast('Erro: ' + result.error, 'error');
        } else {
            showToast('Mensagem enviada!');
            closeWhatsAppModal();
        }
    } catch (e) {
        showToast('Erro ao enviar', 'error');
    }
}
```

### Adicionar endpoint de envio em main.py:

```python
@app.post("/api/whatsapp/send")
async def send_whatsapp_message(request: Request):
    """Send WhatsApp message."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    phone = data.get("phone")
    message = data.get("message")
    contact_id = data.get("contact_id")

    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone e message obrigatorios")

    wa = WhatsAppIntegration()
    result = await wa.send_text(phone, message)

    # Update contact interaction if successful
    if "error" not in result and contact_id:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = NOW(),
                        total_interacoes = COALESCE(total_interacoes, 0) + 1
                    WHERE id = %s
                """, (contact_id,))
                conn.commit()
        except:
            pass

    return result
```

**Commit**: `git commit -m "feat(whatsapp): Add send message modal to contact detail page"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-27 | Parser WhatsApp .txt | CONCLUIDO |
| 2026-03-27 | Endpoints de Import | CONCLUIDO |
| 2026-03-27 | Pagina de Configuracoes | CONCLUIDO |
| 2026-03-27 | Endpoints Status/QR | CONCLUIDO |
| 2026-03-27 | Modal de Envio | CONCLUIDO |
