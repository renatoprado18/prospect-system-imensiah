FROM node:20-slim

# Tools que o Claude Code pode precisar dentro do container (Bash, git, curl, etc).
# Postgres client pra deixar a possibilidade de Claude rodar psql via Bash se precisar.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    postgresql-client \
    ripgrep \
    jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala deps do worker (server.js + SDK Claude Code)
COPY package.json ./
RUN npm install --omit=dev

# Clona o INTEL repo no /app/repo — esse e o workspace que Claude Code vai usar
# pra ler/grep/editar codigo. Shallow clone pra rapido (depth=1).
# Branch main fixa por agora — futuro pode tornar config via env.
ARG REPO_URL=https://github.com/renatoprado18/prospect-system-imensiah.git
RUN git clone --depth 1 ${REPO_URL} /app/repo

# Copia o codigo do server
COPY server.js Procfile ./

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Pull do repo a cada inicio do container pra ficar atualizado (se Railway
# restart, pega ultima versao do main). Custo: ~1-3s no boot.
CMD ["sh", "-c", "cd /app/repo && git fetch --depth 1 origin main && git reset --hard origin/main && cd /app && node server.js"]
