#!/usr/bin/env bash
# Bootstrap script para VPS Hetzner Ubuntu 24.04 LTS.
# Roda como root logo apos provisionar a maquina.
#
# Uso:
#   ssh root@<IP_VPS>
#   curl -fsSL https://raw.githubusercontent.com/<TROCAR>/hetzner-evolution/setup.sh -o setup.sh
#   # OU: scp setup.sh root@<IP>:/root/  (se repo privado)
#   chmod +x setup.sh && ./setup.sh

set -euo pipefail

echo "==> Atualizando sistema"
apt-get update -y
apt-get upgrade -y

echo "==> Pacotes basicos"
apt-get install -y curl ca-certificates gnupg lsb-release ufw fail2ban unattended-upgrades

echo "==> Firewall (SSH + HTTP + HTTPS)"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> fail2ban (proteje SSH de bruteforce)"
systemctl enable --now fail2ban

echo "==> Auto-security-updates"
dpkg-reconfigure --priority=low unattended-upgrades

echo "==> Instalando Docker oficial"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "==> Swap 2GB (CX22 tem so 4GB RAM — buffer pra picos)"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "==> Diretorio de deploy"
mkdir -p /opt/evolution
cd /opt/evolution

echo ""
echo "================================================================"
echo "PRONTO. Agora:"
echo "1. Copie docker-compose.yml, Caddyfile e .env (gerado a partir do .env.example) para /opt/evolution/"
echo "2. Aponte DNS A record: wa.almeida-prado.com -> $(curl -s ifconfig.me)"
echo "3. Aguarde DNS propagar (dig wa.almeida-prado.com)"
echo "4. Rode: cd /opt/evolution && docker compose up -d"
echo "5. Caddy emite cert SSL automatico (~30s pos primeiro request)"
echo "6. Confira: curl https://wa.almeida-prado.com"
echo "7. Acesse manager: https://wa.almeida-prado.com/manager e re-pareie instancia"
echo "================================================================"
