FROM node:20

# Node:20 (full, nao slim) ja vem com git, curl, ca-certificates, bash.
# Claude Code traz ripgrep bundled. psql/jq podem ser instalados depois se
# preciso — pra MVP, evita complicacao de apt-get no build.

WORKDIR /app

# Instala deps do worker (server.js + SDK Claude Code)
COPY package.json ./
RUN npm install --omit=dev

# Clona o INTEL repo no /app/repo — workspace que Claude Code vai usar pra
# ler/grep/editar codigo. Shallow clone (depth=1) pra rapido.
ARG REPO_URL=https://github.com/renatoprado18/prospect-system-imensiah.git
RUN git clone --depth 1 ${REPO_URL} /app/repo

# Copia codigo do server
COPY server.js Procfile ./

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Pull do repo a cada start pra atualizar pra ultima main. ~1-3s no boot.
CMD ["sh", "-c", "cd /app/repo && git fetch --depth 1 origin main && git reset --hard origin/main && cd /app && node server.js"]
