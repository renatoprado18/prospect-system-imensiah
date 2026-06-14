/**
 * Claude Code Delegator — worker que executa tarefas complexas em nome da Tonha.
 *
 * Tonha (Haiku) detecta tarefa complexa (investigacao de codigo, analise de
 * DB multi-passo, pesquisa web, debug de log), dispatcha pra esse worker via
 * /delegate. Worker spawna Claude Code headless (`claude -p`) com workspace =
 * clone do repo INTEL, retorna resultado em ate ~5min. Tonha repassa pro
 * usuario no WA.
 *
 * Por que Node.js: @anthropic-ai/claude-code so existe como pacote npm. Audio
 * worker (Python) fica separado. Servicos independentes no Railway.
 *
 * Auth: CLAUDE_CODE_OAUTH_TOKEN env var (token gerado via `claude /login` local
 * + copiado pro Railway). Subscriber-based — cai no plano Max do user.
 *
 * Seguranca:
 * - HMAC secret no header X-Delegator-Secret (WORKER_SECRET env)
 * - Cap de tempo por chamada (default 5min)
 * - Cap de chamadas/dia via counter in-memory (futuro: persistir em Redis/DB)
 * - Workspace isolado (worktree por chamada — futuro)
 * - Permissoes Claude Code: skipPermissions desabilitado por default;
 *   permission mode 'acceptEdits' nas chamadas operacionais (escreve, mas
 *   nao executa comando destrutivo)
 */

import express from "express";
import { query } from "@anthropic-ai/claude-code";
import crypto from "crypto";

const app = express();
app.use(express.json({ limit: "1mb" }));

const PORT = process.env.PORT || 3000;
const WORKER_SECRET = (process.env.WORKER_SECRET || "").trim();
const CLAUDE_CODE_OAUTH_TOKEN = (process.env.CLAUDE_CODE_OAUTH_TOKEN || "").trim();
const REPO_PATH = process.env.REPO_PATH || "/app/repo";
const MAX_DURATION_MS = parseInt(process.env.MAX_DURATION_MS || "300000", 10); // 5min
const DAILY_CALL_CAP = parseInt(process.env.DAILY_CALL_CAP || "50", 10);

// Counter simples — reseta no restart do container. Pra MVP basta.
let callsToday = 0;
let callsResetAt = new Date().setHours(0, 0, 0, 0) + 86400000;

function resetCounterIfNewDay() {
  const now = Date.now();
  if (now > callsResetAt) {
    callsToday = 0;
    callsResetAt = new Date().setHours(0, 0, 0, 0) + 86400000;
  }
}

function timingSafeEqual(a, b) {
  if (!a || !b) return false;
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

app.get("/health", (req, res) => {
  res.json({
    status: "ok",
    service: "claude-code-delegator",
    oauth_configured: !!CLAUDE_CODE_OAUTH_TOKEN,
    secret_configured: !!WORKER_SECRET,
    repo_path: REPO_PATH,
    calls_today: callsToday,
    daily_cap: DAILY_CALL_CAP,
  });
});

app.post("/delegate", async (req, res) => {
  const startedAt = Date.now();

  // Auth
  const secret = req.header("x-delegator-secret") || req.body?.secret || "";
  if (!WORKER_SECRET || !timingSafeEqual(secret, WORKER_SECRET)) {
    return res.status(401).json({ error: "unauthorized" });
  }

  if (!CLAUDE_CODE_OAUTH_TOKEN) {
    return res.status(503).json({
      error: "CLAUDE_CODE_OAUTH_TOKEN nao configurado no worker",
    });
  }

  resetCounterIfNewDay();
  if (callsToday >= DAILY_CALL_CAP) {
    return res.status(429).json({
      error: `cap diario atingido (${callsToday}/${DAILY_CALL_CAP})`,
    });
  }
  callsToday += 1;

  const { task, context = "", mode = "investigate", requested_by = "tonha" } = req.body || {};

  if (!task || typeof task !== "string" || task.trim().length < 5) {
    return res.status(400).json({ error: "task obrigatoria (min 5 chars)" });
  }

  // Mode -> permission strategy
  // 'investigate' (default): read-only, sem permissoes especiais
  // 'edit': pode editar arquivos, mas nao executa comando shell sem prompt
  // 'full': passa --dangerously-skip-permissions (USAR COM CUIDADO)
  const allowedModes = ["investigate", "edit", "full"];
  if (!allowedModes.includes(mode)) {
    return res.status(400).json({ error: `mode invalido (use: ${allowedModes.join(", ")})` });
  }

  // Compoe o prompt do task — instrui Claude Code sobre contexto INTEL +
  // restricoes operacionais.
  const fullPrompt = [
    "# Tarefa delegada pela Tonha (assistente do Renato Almeida Prado)",
    "",
    "## Contexto",
    "Voce esta operando no worker Railway com workspace clonado do repo INTEL/prospect-system.",
    "Tonha (CoS conversacional via WhatsApp) detectou que essa tarefa requer suas habilidades",
    "completas (Bash, Read, Edit, Agent, ToolSearch, DB access, log access).",
    "",
    `Modo de operacao: ${mode}`,
    mode === "investigate"
      ? "RESTRICAO: read-only. Nao edite arquivos. Nao faca commits. Nao execute comando destrutivo. Apenas investigue, leia, consulte, sintetize."
      : mode === "edit"
      ? "PERMISSAO: pode editar arquivos. Nao faca git push direto pra main sem confirmacao explicita."
      : "MODO FULL — voce tem permissoes amplas. Use com responsabilidade.",
    "",
    context ? `## Contexto adicional fornecido pela Tonha\n\n${context}\n` : "",
    "## Tarefa",
    task,
    "",
    "## Output esperado",
    "Sintese clara em portugues que a Tonha possa relayar pro Renato via WA.",
    "Maximo ~500 palavras. Cite evidencia concreta (arquivos, queries, logs).",
    "Se a tarefa nao pode ser concluida, explique o que faltou.",
  ].join("\n");

  // Timeout proteger contra hang
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), MAX_DURATION_MS);

  let resultText = "";
  let usage = null;
  let model = null;
  let costUsd = null;
  let errorMessage = null;
  let turnCount = 0;
  const toolsUsed = [];

  try {
    // Configura SDK
    process.env.CLAUDE_CODE_OAUTH_TOKEN = CLAUDE_CODE_OAUTH_TOKEN;

    const options = {
      cwd: REPO_PATH,
      permissionMode: mode === "full" ? "bypassPermissions" : mode === "edit" ? "acceptEdits" : "default",
      abortController,
      // Tools restritas pro modo investigate? Nao — Claude Code precisa
      // de tools pra trabalhar mesmo lendo. Deixar full toolset.
    };

    // SDK retorna AsyncGenerator de mensagens
    for await (const msg of query({ prompt: fullPrompt, options })) {
      if (msg.type === "assistant") {
        // Cada turn do assistant — acumula texto final
        const content = msg.message?.content || [];
        for (const block of content) {
          if (block.type === "text") {
            resultText += block.text;
          } else if (block.type === "tool_use") {
            toolsUsed.push(block.name);
          }
        }
      } else if (msg.type === "result") {
        // Mensagem de finalizacao com usage
        usage = msg.usage || null;
        model = msg.model || null;
        if (msg.total_cost_usd !== undefined) {
          costUsd = msg.total_cost_usd;
        }
        if (msg.subtype === "success" && msg.result) {
          // result vem como string com a resposta final completa
          resultText = msg.result;
        }
        turnCount = msg.num_turns || 0;
      }
    }
  } catch (err) {
    errorMessage = err?.message || String(err);
    console.error("[delegate] error:", errorMessage);
  } finally {
    clearTimeout(timeoutId);
  }

  const durationMs = Date.now() - startedAt;

  console.log(
    `[delegate] mode=${mode} duration=${durationMs}ms turns=${turnCount} ` +
      `tools=${toolsUsed.length} cost=${costUsd || "?"} requested_by=${requested_by}`
  );

  if (errorMessage) {
    return res.status(500).json({
      error: errorMessage,
      partial_result: resultText || null,
      duration_ms: durationMs,
    });
  }

  res.json({
    status: "success",
    result: resultText.trim(),
    model,
    cost_usd: costUsd,
    duration_ms: durationMs,
    turn_count: turnCount,
    tools_used: toolsUsed,
    usage,
    mode,
  });
});

app.listen(PORT, () => {
  console.log(`claude-code-delegator listening on :${PORT}`);
  console.log(
    `  oauth: ${CLAUDE_CODE_OAUTH_TOKEN ? "configured" : "MISSING"} | ` +
      `secret: ${WORKER_SECRET ? "configured" : "MISSING"} | ` +
      `repo: ${REPO_PATH} | max_dur: ${MAX_DURATION_MS}ms | cap: ${DAILY_CALL_CAP}/day`
  );
});
