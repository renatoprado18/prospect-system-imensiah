FROM node:20

WORKDIR /app

# Workers files via ADD com URL raw do GitHub — sempre revalida ETag.
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/package.json ./package.json
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/server.js ./server.js
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/Procfile ./Procfile

RUN npm install --omit=dev

# INTEL repo pro Claude Code workspace.
RUN git clone --depth 1 https://github.com/renatoprado18/prospect-system-imensiah.git /app/repo

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Atualiza o repo a cada start pra pegar ultima main. Usa fetch+reset --hard
# (nao pull) pra evitar config de merge strategy que o container nao tem.
CMD ["sh", "-c", "cd /app/repo && git fetch --depth 1 origin main && git reset --hard origin/main && cd /app && node server.js"]
