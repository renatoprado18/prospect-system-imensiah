// Bootstrap: sanitiza env vars ANTES de importar server.js / SDK.
// Em ESM, imports rodam antes do top-level code, entao mutacoes no
// process.env dentro do server.js nao afetam o SDK ja importado/inicializado.
// Esse loader resolve isso: sanitiza e SO ENTAO dynamic-importa o server.

if (process.env.CLAUDE_CODE_OAUTH_TOKEN) {
  const before = process.env.CLAUDE_CODE_OAUTH_TOKEN.length;
  process.env.CLAUDE_CODE_OAUTH_TOKEN = process.env.CLAUDE_CODE_OAUTH_TOKEN.replace(/\s+/g, "");
  const after = process.env.CLAUDE_CODE_OAUTH_TOKEN.length;
  if (before !== after) {
    console.log(`[bootstrap] sanitized CLAUDE_CODE_OAUTH_TOKEN: ${before} -> ${after} chars`);
  }
}

if (process.env.WORKER_SECRET) {
  process.env.WORKER_SECRET = process.env.WORKER_SECRET.replace(/\s+/g, "");
}

await import("./server.js");
