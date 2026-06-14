FROM node:20

WORKDIR /app

# Workers files via ADD com URL raw do GitHub — sempre revalida ETag,
# evita cache obsoleto que estava sabotando builds anteriores.
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/package.json ./package.json
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/server.js ./server.js
ADD https://raw.githubusercontent.com/renatoprado18/prospect-system-imensiah/main/workers/claude-code-delegator/Procfile ./Procfile

# Instala deps (Express + @anthropic-ai/claude-code SDK).
RUN npm install --omit=dev

# INTEL repo pro Claude Code workspace. Cache bust via ADD da timestamp
# do main HEAD — sempre revalida.
ADD https://api.github.com/repos/renatoprado18/prospect-system-imensiah/commits/main /tmp/main-head.json
RUN git clone --depth 1 https://github.com/renatoprado18/prospect-system-imensiah.git /app/repo

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Pull do repo a cada start pra atualizar pra ultima main.
CMD ["sh", "-c", "cd /app/repo && git pull --depth 1 origin main && cd /app && node server.js"]
