# Plano de Melhorias WhatsApp - INST-2

**Branch**: `feature/whatsapp-improvements`
**Status**: Em planejamento
**Responsavel**: INST-2 (COORD)

## Analise do Estado Atual

### Arquivos Envolvidos
- `app/integrations/whatsapp.py` (615 linhas) - Core da integracao
- `app/templates/rap_whatsapp.html` (822 linhas) - UI
- `app/main.py` - Endpoints (linhas ~1117-1900)

### Funcionalidades Existentes
1. Conexao via QR Code (Evolution API)
2. Envio de mensagens texto e midia
3. Verificar se numero tem WhatsApp
4. Buscar foto de perfil
5. Listar todos os chats
6. Sincronizar historico de mensagens
7. Suporte a grupos (mensagens onde usuario participou)
8. Re-vincular mensagens a contatos
9. Webhook para mensagens em tempo real

---

## Melhorias Propostas

### PRIORIDADE ALTA

#### 1. Templates de Mensagem
**Problema**: Nao ha templates pre-definidos para mensagens comuns
**Solucao**: Criar sistema de templates

```
- Saudacao inicial
- Follow-up apos reuniao
- Lembrete de reuniao
- Envio de proposta
- Agradecimento
```

**Arquivos a modificar**:
- whatsapp.py: Adicionar funcao `send_template()`
- main.py: Endpoint `/api/whatsapp/templates`
- rap_whatsapp.html: UI para selecionar template

#### 2. Agendamento de Mensagens
**Problema**: Nao e possivel agendar envios
**Solucao**: Sistema de mensagens agendadas

**Arquivos a modificar**:
- database.py: Nova tabela `scheduled_messages`
- whatsapp.py: Funcao para processar fila
- main.py: Endpoints CRUD para agendamentos
- Cron job para processar fila

#### 3. Indicador de Leitura/Entrega
**Problema**: Nao sabemos se mensagem foi entregue/lida
**Solucao**: Processar eventos de status do webhook

**Arquivos a modificar**:
- whatsapp.py: Processar evento `message.status`
- database.py: Campo status em messages
- UI: Mostrar icones de status

---

### PRIORIDADE MEDIA

#### 4. Busca em Conversas
**Problema**: Nao ha busca nas mensagens
**Solucao**: Full-text search nas mensagens

**Arquivos a modificar**:
- main.py: Endpoint `/api/whatsapp/search`
- rap_whatsapp.html: Campo de busca na UI

#### 5. Exportar Conversas
**Problema**: Nao e possivel exportar historico
**Solucao**: Export para CSV/PDF

**Arquivos a modificar**:
- main.py: Endpoint `/api/whatsapp/export/{contact_id}`
- UI: Botao de exportar

#### 6. Filtros Avancados
**Problema**: Listagem de chats sem filtros
**Solucao**: Filtrar por data, contato, tipo

**Arquivos a modificar**:
- main.py: Parametros de filtro em `/api/whatsapp/chats`
- rap_whatsapp.html: UI de filtros

---

### PRIORIDADE BAIXA

#### 7. Respostas Rapidas (Quick Replies)
Botoes de resposta pre-definidos nas mensagens

#### 8. Notificacoes Desktop
Alerta quando nova mensagem chega

#### 9. Estatisticas de Uso
Dashboard com metricas de mensagens

#### 10. Integracao com Prospects
Enviar mensagem direto da pagina do prospect

---

## Ordem de Implementacao Sugerida

| Fase | Feature | Complexidade | Arquivos Bloqueados |
|------|---------|--------------|---------------------|
| 1 | Templates de Mensagem | Baixa | Nenhum |
| 2 | Indicador Leitura | Media | Nenhum |
| 3 | Busca em Conversas | Baixa | Nenhum |
| 4 | Agendamento | Alta | database.py |
| 5 | Exportar Conversas | Baixa | Nenhum |

---

## Decisoes Pendentes

1. **Templates**: Armazenar em banco ou arquivo JSON?
2. **Agendamento**: Usar cron do Vercel ou fila externa?
3. **Busca**: PostgreSQL full-text ou busca simples?

---

## Proximos Passos

1. [ ] Escolher primeira feature a implementar
2. [ ] Detalhar design tecnico
3. [ ] Implementar
4. [ ] Testar
5. [ ] Atualizar COORDINATION.md
