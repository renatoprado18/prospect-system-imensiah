# Evolution API self-hosted no Hetzner

Migra Evolution API do Railway pra VPS Hetzner — economia esperada **~$18/mes** ($22 Railway -> $6.30 Hetzner + $0 INTEL worker que fica no Railway).

## Stack

- **VPS:** Hetzner Cloud CX22 (2 vCPU / 4GB RAM / 40GB SSD / 20TB tx) — ~$6.30/mes em Ashburn
- **OS:** Ubuntu 24.04 LTS
- **Containers:** Evolution v2.3.7 + Postgres 16 + Redis 7 + Caddy 2 (SSL automatico)
- **Dominio:** `wa.almeida-prado.com` (HTTPS via Lets Encrypt)

## Custos comparados

| Item | Railway hoje | Hetzner |
|------|--------------|---------|
| Base/usage Evolution | ~$22/mes | $0 (incluido no VPS) |
| Postgres dedicado | incluido | $0 (mesmo VPS) |
| Redis | incluido | $0 (mesmo VPS) |
| **Total** | **~$22** | **~$6.30** |

INTEL worker continua no Railway ($1-2/mes — nao vale migrar).

## Pre-requisitos

1. Conta Hetzner Cloud (https://console.hetzner.cloud) com cartao
2. SSH key local (`~/.ssh/id_ed25519.pub`) registrada no Hetzner
3. Acesso DNS de `almeida-prado.com` (criar subdomain `wa`)
4. pg_dump local (`brew install postgresql@16`)
5. Celular pra escanear QR code de re-pareamento WhatsApp

## Runbook (45-60 min total)

### 1. Provisionar VPS (5 min)

No console Hetzner Cloud:
- New Project: "intel-prod"
- Add Server:
  - Location: **Ashburn, VA** (menor latencia BR)
  - Image: **Ubuntu 24.04**
  - Type: **CX22** (€5.83/mes ~ $6.30)
  - SSH key: selecione a sua
  - Name: `evolution-prod`
- Create & Buy

Anote o **IP publico** (ex: `5.161.xx.xx`).

### 2. Bootstrap do VPS (10 min)

```bash
ssh root@<IP_VPS>

# Cole o conteudo de setup.sh ou faca scp:
# (do laptop) scp deploy/hetzner-evolution/setup.sh root@<IP>:/root/
chmod +x setup.sh
./setup.sh
```

Espera o script terminar (instala Docker, ufw, fail2ban, swap).

### 3. Subir docker-compose (5 min)

```bash
# Do laptop, copia compose + Caddyfile pro VPS:
scp deploy/hetzner-evolution/{docker-compose.yml,Caddyfile,.env.example} \
    root@<IP>:/opt/evolution/

# No VPS:
ssh root@<IP>
cd /opt/evolution
cp .env.example .env

# Edite .env (use vim/nano). MUDE pelo menos:
# - POSTGRES_PASSWORD: gera nova com `openssl rand -hex 24`
# - DOMAIN: confirme wa.almeida-prado.com
nano .env
```

### 4. DNS (5 min + tempo de propagacao)

No provedor DNS de `almeida-prado.com`:
- Adicionar registro A: `wa.almeida-prado.com` -> `<IP_VPS>`
- TTL: 300

Aguarda propagar:
```bash
dig +short wa.almeida-prado.com
# deve retornar o IP do VPS
```

### 5. Subir stack (3 min)

```bash
# No VPS:
cd /opt/evolution
docker compose up -d

# Acompanhar logs:
docker compose logs -f evolution

# Testar (apos ~30s pra Caddy emitir cert):
curl https://wa.almeida-prado.com
# {"status":200,"message":"Welcome to the Evolution API..."}
```

### 6. Migrar dados Postgres (10 min)

```bash
# Do laptop:
VPS_IP=<IP_VPS> ./deploy/hetzner-evolution/migrate-data.sh
```

Restaura historico de mensagens, contatos, chats. **Nao** restaura a sessao WhatsApp (sera re-pareada no passo 7).

### 7. Re-parear WhatsApp (5 min)

Abra `https://wa.almeida-prado.com/manager` no browser. Use `EVOLUTION_API_KEY` como senha global.

1. Listar instancias -> ja deve aparecer `rap-whatsapp` (criada no restore)
2. Se nao: criar nova com mesmo nome `rap-whatsapp`
3. Connect/QR Code -> escanear no WhatsApp do celular (Configuracoes > Aparelhos conectados)
4. Verifica status: deve aparecer `open` apos parear

### 8. Apontar INTEL pra novo endpoint (5 min)

```bash
# Atualiza EVOLUTION_API_URL no Vercel:
vercel env rm EVOLUTION_API_URL production
echo "https://wa.almeida-prado.com" | vercel env add EVOLUTION_API_URL production

# Redeploy pra pegar nova URL:
vercel --prod
```

Confirma webhook configurado:
```bash
curl -H "apikey: $EVOLUTION_API_KEY" \
  https://wa.almeida-prado.com/webhook/find/rap-whatsapp
```

Se vazio, reconfigura:
```bash
curl -X POST https://wa.almeida-prado.com/webhook/set/rap-whatsapp \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "webhook": {
      "url": "https://intel.almeida-prado.com/api/webhooks/whatsapp",
      "events": ["MESSAGES_UPSERT","MESSAGES_UPDATE","CONNECTION_UPDATE","SEND_MESSAGE"],
      "webhookByEvents": true,
      "webhookBase64": false
    }
  }'
```

### 9. Smoke test (5 min)

- Mandar msg pelo celular pra um contato qualquer -> ver no log `docker compose logs -f evolution` o evento sair
- Mandar msg DO contato pro Renato -> ver chegar no INTEL (dashboard ou `/whatsapp`)
- Disparar uma proposta de acao pelo bot -> ver chegar no celular

### 10. Desligar Railway Evolution (apos 24h estavel)

```bash
# No Railway dashboard:
# - Project whatsapp-evolution -> Settings -> Delete project
```

Mantenha o Postgres dump por +30 dias antes de excluir.

## Operacao continua

### Backup Postgres (rodar via cron no VPS)

```bash
# Adicionar em /etc/cron.daily/evolution-backup
#!/bin/bash
docker exec evolution-postgres pg_dump -U evolution evolution \
  | gzip > /var/backups/evolution_$(date +\%Y\%m\%d).sql.gz
# Mantem 7 dias
find /var/backups/evolution_*.sql.gz -mtime +7 -delete
```

### Updates

```bash
# Subir versao Evolution (testar em homolog antes):
cd /opt/evolution
# editar docker-compose.yml -> trocar tag
docker compose pull evolution
docker compose up -d evolution

# Updates de seguranca do OS (automatico via unattended-upgrades)
```

### Monitoramento

INTEL ja tem `platform_costs` que tracka Railway. Pra Hetzner:
- Conta fixa $6.30/mes nao precisa tracking dinamico
- Alertas Hetzner: configurar em console (CPU/disk/tx)
- Uptime: usar `https://uptimerobot.com` (free 50 monitors) apontando pra `https://wa.almeida-prado.com`

## Rollback (se algo der errado)

```bash
# 1. Reverter EVOLUTION_API_URL pra Railway:
vercel env rm EVOLUTION_API_URL production
echo "https://evolution-api-production-ecee.up.railway.app" \
  | vercel env add EVOLUTION_API_URL production
vercel --prod

# 2. Re-parear no Railway (manager Railway URL + QR)
# 3. Investigar issue no Hetzner com calma
```

## Notas / gotchas

- **Re-pareamento e obrigatorio**: a sessao Baileys do WhatsApp e local-stateful. Nao da pra "transferir" — so re-escanear QR. Historico fica preservado via Postgres dump.
- **Numero do whatsapp continua o mesmo** — re-pareamento e do device, nao da conta.
- **Webhook precisa de HTTPS valido** — Caddy resolve isso automatico no primeiro request.
- **Latencia Ashburn-Brasil** ~110-130ms. Aceitavel pra webhook (nao e UI).
- **Sem auto-scale**: se conversa explodir, VPS pode saturar. CX22 (4GB) tem ~6x folga sobre uso atual (0.9GB). Se precisar: upgrade pra CX32 ($11/mes) sem downtime via console Hetzner.
