FROM node:20

# Estrategia: NAO depende de COPY do build context (Railway estava enviando
# context vazio). Pega tudo via git clone do repo inteiro. Robusto sem
# precisar configurar Root Directory.

WORKDIR /app

# Clona INTEL repo — workspace pro Claude Code E fonte do server worker.
ARG REPO_URL=https://github.com/renatoprado18/prospect-system-imensiah.git
RUN git clone --depth 1 ${REPO_URL} /app/repo

# Copia arquivos do worker DE DENTRO do clone (nao do build context).
RUN cp /app/repo/workers/claude-code-delegator/package.json ./ \
    && cp /app/repo/workers/claude-code-delegator/server.js ./ \
    && cp /app/repo/workers/claude-code-delegator/Procfile ./

# Instala deps (Express + @anthropic-ai/claude-code SDK)
RUN npm install --omit=dev

ENV REPO_PATH=/app/repo
ENV PORT=3000

EXPOSE 3000

# Pull do repo a cada start pra atualizar pra ultima main do INTEL.
# Tambem re-copia worker files caso server.js/package.json tenham sido
# atualizados (no caso de hot reload via git push).
CMD ["sh", "-c", "cd /app/repo && git fetch --depth 1 origin main && git reset --hard origin/main && cp workers/claude-code-delegator/server.js /app/ && cp workers/claude-code-delegator/package.json /app/package.json && cd /app && node server.js"]
