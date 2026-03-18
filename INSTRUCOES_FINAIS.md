# Sistema de Prospects ImensIAH - Instruções Finais

## URLs do Sistema

- **Sistema Principal**: https://prospect-system.vercel.app
- **Painel Admin (Renato)**: https://prospect-system.vercel.app/admin
- **Dashboard (Andressa)**: https://prospect-system.vercel.app/

## Domínio Customizado

Para configurar `prospects.almeida-prado.com`:

1. Acesse seu provedor de DNS (Cloudflare, GoDaddy, etc.)
2. Adicione um registro CNAME:
   - **Nome**: `prospects`
   - **Valor**: `cname.vercel-dns.com`
3. Aguarde propagação (até 24h)

## Variáveis de Ambiente (Vercel)

As variáveis já foram criadas com placeholders. Para atualizar:

1. Acesse https://vercel.com/renato-prados-projects/prospect-system/settings/environment-variables
2. Edite cada variável com os valores reais:
   - `GOOGLE_CLIENT_ID` → Seu Client ID do Google Cloud
   - `GOOGLE_CLIENT_SECRET` → Seu Client Secret
   - `FATHOM_API_KEY` → Sua API Key do Fathom

### Obtendo credenciais Google:
1. https://console.cloud.google.com
2. Criar projeto → APIs e Serviços → Credenciais
3. Criar credencial OAuth 2.0
4. Adicionar URI de redirect: `https://prospect-system.vercel.app/auth/google/callback`

### Obtendo API Key Fathom:
1. https://fathom.video/settings/api
2. Gerar nova API Key
3. Configurar webhook: `https://prospect-system.vercel.app/api/webhooks/fathom`

---

## Fluxo de Trabalho

### Para Renato (Admin):

1. **Acesse**: https://prospect-system.vercel.app/admin
2. **Filtre por Tier**: A e B são prioritários
3. **Revise cada prospect**: veja score, cargo, empresa
4. **Aprove ou Rejeite**: um a um ou em massa
5. **Defina prioridade**: 1-10 para ordenar na lista da Andressa

### Para Andressa (Operadora):

1. **Acesse**: https://prospect-system.vercel.app/
2. **Primeiro acesso**: Tutorial automático de 5 passos
3. **Dashboard**: Veja top prospects aprovados por Renato
4. **Agendar**: Clique para criar reunião no Google Calendar
5. **Feedback**: Após reunião, registre resultado

---

## Importar Contatos

1. Acesse Configurações no dashboard
2. Clique em "Importar CSV"
3. Os prospects entram como "Pendente Aprovação"
4. Renato aprova no painel /admin

---

## Comandos Úteis

```bash
# Redeploy
vercel --prod

# Ver logs
vercel logs

# Atualizar variável de ambiente
vercel env rm FATHOM_API_KEY production
echo "nova_chave" | vercel env add FATHOM_API_KEY production
```

---

## Repositório GitHub
https://github.com/renatoprado18/prospect-system-imensiah

---

## Contato
Sistema desenvolvido para ImensIAH
