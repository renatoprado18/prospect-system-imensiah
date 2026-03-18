# Setup do Sistema de Prospects ImensIAH

## Repositório
https://github.com/renatoprado18/prospect-system-imensiah

## Deploy na Vercel

### 1. Conectar Repositório
1. Acesse https://vercel.com/new
2. Importe o repositório `renatoprado18/prospect-system-imensiah`
3. Framework Preset: `Other`
4. Root Directory: `.` (raiz)

### 2. Configurar Variáveis de Ambiente
No painel da Vercel, vá em **Settings > Environment Variables** e adicione:

```
GOOGLE_CLIENT_ID=seu_client_id_do_google
GOOGLE_CLIENT_SECRET=seu_client_secret_do_google
GOOGLE_CALENDAR_ID=primary
FATHOM_API_KEY=sua_api_key_do_fathom
```

### 3. Configurar Domínio Customizado
1. Vá em **Settings > Domains**
2. Adicione: `prospects.almeida-prado.com`
3. Configure o DNS no seu provedor:
   - Tipo: CNAME
   - Nome: `prospects`
   - Valor: `cname.vercel-dns.com`

## Integrações

### Google Workspace
1. Acesse https://console.cloud.google.com
2. Crie um projeto ou use existente
3. Ative a API do Google Calendar
4. Crie credenciais OAuth 2.0:
   - Tipo: Aplicativo Web
   - URLs autorizados: `https://prospects.almeida-prado.com`
   - Redirect URIs: `https://prospects.almeida-prado.com/auth/google/callback`
5. Copie Client ID e Client Secret para as variáveis de ambiente

### Fathom
1. Acesse https://fathom.video/settings/api
2. Gere uma API Key
3. Configure o webhook para: `https://prospects.almeida-prado.com/api/webhooks/fathom`

## Uso do Sistema

### Para Andressa Santos:

1. **Dashboard** - Visão geral de prospects e métricas
2. **Prospects** - Lista completa com filtros por Tier e Status
3. **Agendar Reunião** - Clique no botão para agendar via Google Calendar
4. **Feedback** - Após reunião, registre o resultado para alimentar o sistema

### Fluxo de Trabalho:
1. Filtre por Tier A e B (prospects prioritários)
2. Selecione prospect e clique em "Agendar"
3. Escolha data/hora e tipo de reunião
4. Sistema cria evento no Google Calendar com link do Meet
5. Após reunião, registre feedback com objeções e features de interesse
6. Sistema aprende e melhora o scoring automaticamente

## Critérios de Scoring

### Tier A (50+ pts) - Contato Imediato
- CEO/Fundador de PME
- Setor de consultoria/governança/finanças
- Dados completos (email + telefone)

### Tier B (35-49 pts) - Campanha Prioritária
- Diretores e C-Level
- Setor relevante
- Bom potencial de fit

### Tier C (25-34 pts) - Nurturing
- Gerentes seniores
- Requer qualificação adicional

## Manutenção

### Importar novos contatos:
1. Exporte CSV do Google Contacts
2. Execute: `python prospect_analyzer.py`
3. No sistema, vá em Configurações > Importar CSV

### Sincronizar Fathom:
- Automático via webhook
- Manual: clique em "Sincronizar Fathom" no topo

## Suporte
Desenvolvido para ImensIAH
Contato técnico: equipe de desenvolvimento
