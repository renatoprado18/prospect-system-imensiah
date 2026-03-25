# Analise do ConselhoOS - Integracao com RAP

> **Autor**: ARCH (Coordenador/Arquiteto)
> **Data**: 2026-03-25
> **Repositorio**: /Users/rap/conselhoos

## Visao Geral

O ConselhoOS e um sistema separado para gestao de **participacao em conselhos**.
E uma aplicacao Next.js 14 com App Router, usando:

- **Frontend**: React 18, TailwindCSS, Radix UI, Framer Motion
- **Backend**: Next.js API Routes
- **Database**: Drizzle ORM + Neon PostgreSQL (serverless)
- **AI**: Anthropic Claude SDK
- **Auth**: NextAuth.js
- **Storage**: Google Drive API
- **Calendar**: Google Calendar API
- **Transcription**: Fathom API

## Estrutura do Banco de Dados

### Tabelas Principais

```
empresas              - Empresas onde Renato e conselheiro
├── reunioes          - Reunioes de conselho
│   ├── decisoes      - Decisoes tomadas
│   ├── raciItens     - Action items (RACI matrix)
│   └── temasReuniao  - Temas discutidos
├── documentos        - Documentos do Google Drive
├── chatMensagens     - Chat com AI sobre a empresa
├── pautasAnuais      - Agenda anual de temas
│   └── temasAnuais   - Temas por mes
└── onboardingEmpresas - Status de onboarding

checklistItems        - Templates de checklist (onboarding/recorrente)
preparacaoReunioes    - Checklist por reuniao
```

### Modelo de Empresa (empresas)

Campos principais:
- Dados cadastrais: nome, setor, cnpj, razaoSocial
- Endereco completo
- Contato: telefone, email, website, linkedin, instagram
- Dados oficiais: cnaes, capitalSocial, faturamentoAnual
- Socios e executivos (JSON)
- Estrutura organizacional: organogramaJson, pessoasChave
- **insightsJson**: Base de conhecimento inteligente extraida de documentos
- driveFolderId: Pasta no Google Drive

### Modelo de Reuniao (reunioes)

- Integracao com Google Calendar (calendarEventId, calendarLink)
- Integracao com Fathom (fathomRecordingId, fathomMeetingId)
- Transcricao automatica
- Geracao de pauta (pautaMd)
- Geracao de ata (ataMd, ataDocxDriveId)

### Modelo de Documento (documentos)

- Sincronizado do Google Drive (driveFileId, driveWebViewLink)
- Extracao de conteudo (conteudoExtraido)
- Analise por AI: resumo, tags, topicos, entidades, sentimento

## Funcionalidades Identificadas

1. **Gestao de Empresas/Conselhos**
   - Onboarding com checklist
   - Base de conhecimento extraida de documentos
   - Chat AI contextual por empresa

2. **Gestao de Reunioes**
   - Sincronizacao com Google Calendar
   - Preparacao com pauta estruturada
   - Integracao com Fathom para transcricao
   - Geracao automatica de ata

3. **Gestao de Documentos**
   - Sincronizacao com Google Drive
   - Extracao e analise de conteudo
   - Busca semantica

4. **RACI Matrix**
   - Action items com responsaveis
   - Status tracking
   - Vinculo com reunioes

5. **Pauta Anual**
   - Agenda de temas por ano/mes
   - Categorizacao e priorizacao

## Pontos de Integracao com RAP

### 1. Sincronizacao de Contatos (Circulos)

```
ConselhoOS.empresas.pessoasChave -> RAP.contacts
ConselhoOS.empresas.socios       -> RAP.contacts
ConselhoOS.empresas.executivos   -> RAP.contacts
```

**Proposta**:
- Criar link `contact_conselhoos_link` para relacionar contatos
- Contatos de empresas do conselho ganham tag automatica "conselho:{empresa}"
- Circulo padrao 2 (Proximo) para membros de conselho

### 2. Sincronizacao de Reunioes

```
ConselhoOS.reunioes -> RAP.calendar_events (futuro)
ConselhoOS.reunioes -> RAP.contact_memories (historico)
```

**Proposta**:
- Quando reuniao concluida, criar memoria para contatos presentes
- Usar insightsJson para enriquecer briefings pre-reuniao

### 3. Sincronizacao de Documentos

```
ConselhoOS.documentos.resumo     -> RAP.contact_facts (fatos relevantes)
ConselhoOS.documentos.entidades  -> RAP.contacts (mencoes)
```

**Proposta**:
- Extrair mencoes a pessoas de documentos
- Criar fatos automaticos de documentos importantes

### 4. Briefings Inteligentes

Quando Renato tiver reuniao com alguem de um conselho:
- Puxar dados da empresa do ConselhoOS
- Incluir decisoes recentes
- Incluir action items pendentes
- Incluir temas da pauta anual proximos

### 5. Alertas Cruzados

- Aniversario de membro de conselho -> Alerta no ConselhoOS
- Reuniao de conselho proxima -> Alerta no RAP com briefing
- Action item atrasado -> Lembrete no RAP

## Estrategia de Integracao

### Fase 1: Link Manual (Curto Prazo)

Adicionar no RAP:
```sql
ALTER TABLE contacts
ADD COLUMN conselhoos_empresa_id UUID,
ADD COLUMN conselhoos_pessoa_tipo TEXT; -- 'socio', 'executivo', 'board'
```

Endpoints:
```
POST /api/contacts/{id}/link-conselhoos
GET /api/conselhoos/sync-status
```

### Fase 2: Sincronizacao Automatica (Medio Prazo)

Cron job para sincronizar:
1. Novos contatos de pessoasChave/socios/executivos
2. Atualizacoes de dados
3. Novas reunioes como eventos no calendario

### Fase 3: Integracao Profunda (Longo Prazo)

1. SSO entre sistemas
2. API compartilhada de contatos
3. Briefings unificados
4. Dashboard consolidado

## Consideracoes Tecnicas

### Bancos de Dados Separados

- ConselhoOS: Neon PostgreSQL (serverless)
- RAP: Vercel PostgreSQL

**Opcoes**:
1. API calls entre sistemas
2. Database replication (complexo)
3. Migrar ConselhoOS para mesmo DB (invasivo)

**Recomendacao**: API calls via endpoints protegidos

### Autenticacao

- ConselhoOS: NextAuth.js
- RAP: Custom (ainda nao implementado)

**Proposta**: Compartilhar token Google entre sistemas

## Proximos Passos

1. [ ] Definir quais campos de contacts mapeiam para pessoasChave
2. [ ] Criar endpoint no ConselhoOS para listar pessoas por empresa
3. [ ] Criar endpoint no RAP para receber dados do ConselhoOS
4. [ ] Implementar sync inicial
5. [ ] Testar com empresa real (ex: Vallen Clinic)

---

## Empresas Conhecidas

Baseado na conversa anterior:
1. **Despertar** - (tipo a definir)
2. **Vallen Clinic** - Clinica medica
3. **Alba Consultoria** - Consultoria
4. **Federacao Assespro** - Associacao

Verificar se estas empresas ja estao cadastradas no ConselhoOS.
