FROM node:20

# Node:20 (full) ja vem com git, curl, ca-certificates, bash bundled.
# Claude Code traz ripgrep internamente.

WORKDIR /app

# Build context = workers/claude-code-delegator/ (configurar Root Directory
# no Railway Settings -> Source apontando pra esse dir).
COPY package.json ./
RUN npm install --omit=dev

# Clona o INTEL repo no /app/repo — workspace que Claude Code vai usar.
ARG REPO_URL=https://github.com/renatoprado18/prospect-system-imensiah.git
RUN git clone --depth 1 ${REPO_URL} /app/repo

COPY server.js Procfile ./

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Pull do repo a cada start pra atualizar pra ultima main.
CMD ["sh", "-c", "cd /app/repo && git fetch --depth 1 origin main && git reset --hard origin/main && cd /app && node server.js"]
