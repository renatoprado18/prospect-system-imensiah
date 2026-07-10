/**
 * Claude Code Delegator — worker que executa tarefas complexas em nome da Tonia.
 *
 * Tonia detecta tarefa complexa (investigacao de codigo, analise de DB
 * multi-passo, pesquisa web, debug de log) e dispatcha pra esse worker via
 * /delegate. Worker spawna Claude Code headless com workspace = clone do repo
 * INTEL e retorna resultado em ate ~5min. Tonia repassa pro usuario no WA.
 *
 * Auth: CLAUDE_CODE_OAUTH_TOKEN (plano Max do user). Secret X-Delegator-Secret.
 *
 * ── FASE 2 (edit/diff) ──────────────────────────────────────────────────────
 * mode='edit' NAO edita mais o clone compartilhado. Em vez disso:
 *   1. cria uma WORKTREE ISOLADA numa branch efemera `tonia-dev/<id>` off
 *      origin/main (git worktree add -b ...);
 *   2. roda o Claude Code com cwd = worktree, permissionMode 'acceptEdits';
 *   3. commita local na branch e captura o `git diff` vs origin/main;
 *   4. DESTROI a worktree e a branch (nada e' pushado).
 *
 * Garantia de blast-radius: o clone e' read-only (sem credencial de push) e a
 * branch e' efemera. Logo edit NAO consegue tocar a main nem o remote — no
 * maximo edita arquivos numa worktree que ja' vai ser apagada. O diff volta na
 * resposta pro humano revisar (Opcao B). PR real = follow-up (precisa token).
 *
 * Defesa em profundidade: canUseTool nega Bash obviamente destrutivo
 * (git push, checkout main, rm -rf /, --dangerously, sudo).
 */

import express from "express";
import { query } from "@anthropic-ai/claude-agent-sdk";
import crypto from "crypto";
import { execFileSync } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const app = express();
app.use(express.json({ limit: "1mb" }));

const PORT = process.env.PORT || 3000;
const WORKER_SECRET = (process.env.WORKER_SECRET || "").trim();
const CLAUDE_CODE_OAUTH_TOKEN = (process.env.CLAUDE_CODE_OAUTH_TOKEN || "").replace(/\s+/g, "");
const REPO_PATH = process.env.REPO_PATH || "/app/repo";
const MAX_DURATION_MS = parseInt(process.env.MAX_DURATION_MS || "300000", 10); // 5min
const DAILY_CALL_CAP = parseInt(process.env.DAILY_CALL_CAP || "50", 10);
// Diff grande estoura payload/DB. Trunca com aviso. Revisor pede detalhe se precisar.
const MAX_DIFF_CHARS = parseInt(process.env.MAX_DIFF_CHARS || "60000", 10);

// Counter simples — reseta no restart. Backstop; o cap real fica na Tonia (DB).
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

function git(args, cwd = REPO_PATH) {
  return execFileSync("git", args, { cwd, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }).trim();
}

// ── Trava dura (defesa em profundidade sobre o isolamento por worktree) ──
const DANGEROUS_BASH = [
  /\bgit\s+push\b/,                          // sem push (a garantia principal)
  /\bgit\s+(checkout|switch)\b[^|;&]*\bmain\b/, // nao sai da branch efemera
  /\bgit\s+branch\s+-[dD]\b[^|;&]*\bmain\b/,
  /\bgit\s+reset\s+--hard\b[^|;&]*\borigin\/main\b/,
  /\brm\s+-rf\s+\/(?!tmp)/,                   // rm -rf / (menos /tmp)
  /--dangerously-skip-permissions/,
  /\bsudo\b/,
];
function bashIsDangerous(cmd) {
  return DANGEROUS_BASH.some((re) => re.test(cmd));
}

// slug curto e seguro pra nome de branch a partir do task
function slugify(s) {
  return (s || "task")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32) || "task";
}

// Cria worktree isolada numa branch efemera off origin/main. Retorna {wtPath, branch}.
function setupWorktree(delegationId, task) {
  git(["fetch", "--depth", "1", "origin", "main"]); // garante origin/main fresco
  const branch = `tonia-dev/${delegationId || "x"}-${slugify(task)}`;
  const wtPath = fs.mkdtempSync(path.join(os.tmpdir(), "tonia-wt-"));
  // Se a branch ja existir (retry), remove antes.
  try { git(["worktree", "remove", "--force", wtPath]); } catch { /* noop */ }
  try { git(["branch", "-D", branch]); } catch { /* noop */ }
  git(["worktree", "add", "-b", branch, wtPath, "origin/main"]);
  return { wtPath, branch };
}

function teardownWorktree(wtPath, branch) {
  try { git(["worktree", "remove", "--force", wtPath]); } catch { /* noop */ }
  try { git(["branch", "-D", branch]); } catch { /* noop */ }
  try { if (fs.existsSync(wtPath)) fs.rmSync(wtPath, { recursive: true, force: true }); } catch { /* noop */ }
}

// Depois do run: commita local e captura diff vs origin/main. Retorna
// {hasChanges, diff, stat, truncated}. NAO pusha.
function commitAndDiff(wtPath, task) {
  git(["add", "-A"], wtPath);
  const status = git(["status", "--porcelain"], wtPath);
  if (!status) return { hasChanges: false, diff: "", stat: "", truncated: false };
  git(["-c", "user.email=tonia@almeida-prado.com", "-c", "user.name=Tonia (delegate)",
       "commit", "-m", `tonia-dev: ${(task || "").slice(0, 72)}`], wtPath);
  const stat = git(["diff", "--stat", "origin/main..HEAD"], wtPath);
  let diff = git(["diff", "origin/main..HEAD"], wtPath);
  let truncated = false;
  if (diff.length > MAX_DIFF_CHARS) {
    diff = diff.slice(0, MAX_DIFF_CHARS) + "\n\n… [diff truncado — peca o restante por arquivo]";
    truncated = true;
  }
  return { hasChanges: true, diff, stat, truncated };
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
    fase2: "edit/diff (worktree isolada, sem push)",
  });
});

app.post("/delegate", async (req, res) => {
  const startedAt = Date.now();

  const secret = req.header("x-delegator-secret") || req.body?.secret || "";
  if (!WORKER_SECRET || !timingSafeEqual(secret, WORKER_SECRET)) {
    return res.status(401).json({ error: "unauthorized" });
  }
  if (!CLAUDE_CODE_OAUTH_TOKEN) {
    return res.status(503).json({ error: "CLAUDE_CODE_OAUTH_TOKEN nao configurado no worker" });
  }

  resetCounterIfNewDay();
  if (callsToday >= DAILY_CALL_CAP) {
    return res.status(429).json({ error: `cap diario atingido (${callsToday}/${DAILY_CALL_CAP})` });
  }
  callsToday += 1;

  const { task, context = "", mode = "investigate", requested_by = "tonia", delegation_id = null } =
    req.body || {};

  if (!task || typeof task !== "string" || task.trim().length < 5) {
    return res.status(400).json({ error: "task obrigatoria (min 5 chars)" });
  }
  const allowedModes = ["investigate", "edit", "full"];
  if (!allowedModes.includes(mode)) {
    return res.status(400).json({ error: `mode invalido (use: ${allowedModes.join(", ")})` });
  }

  // ── Worktree isolada pra edit (Fase 2) ──
  let workDir = REPO_PATH;
  let branch = null;
  let wtPath = null;
  if (mode === "edit") {
    try {
      const wt = setupWorktree(delegation_id, task);
      wtPath = wt.wtPath;
      branch = wt.branch;
      workDir = wtPath;
    } catch (e) {
      return res.status(500).json({ error: `falha criando worktree isolada: ${e.message}` });
    }
  }

  const fullPrompt = [
    "# Tarefa delegada pela Tonia (assistente do Renato Almeida Prado)",
    "",
    "## Contexto",
    "Voce opera no worker Railway com workspace do repo INTEL/prospect-system.",
    mode === "edit"
      ? `Voce esta numa WORKTREE ISOLADA na branch efemera '${branch}' (off origin/main). ` +
        "Edite a vontade — suas mudancas NAO tocam a main. NAO faca git push nem git commit " +
        "(o worker commita e captura o diff sozinho depois). NAO rode comando destrutivo."
      : mode === "investigate"
      ? "RESTRICAO: read-only. Nao edite arquivos, nao commite, nao rode comando destrutivo. Investigue, leia, sintetize."
      : "MODO FULL — permissoes amplas. Use com responsabilidade.",
    "",
    context ? `## Contexto adicional da Tonia\n\n${context}\n` : "",
    "## Tarefa",
    task,
    "",
    "## Output esperado",
    mode === "edit"
      ? "Sintese em portugues do que voce mudou e por que (o worker anexa o diff separado). Max ~400 palavras."
      : "Sintese clara em portugues que a Tonia relaye pro Renato. Max ~500 palavras. Cite evidencia (arquivos, queries, logs).",
    "Se nao deu pra concluir, explique o que faltou.",
  ].join("\n");

  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), MAX_DURATION_MS);

  let resultText = "";
  let usage = null, model = null, costUsd = null, errorMessage = null, turnCount = 0;
  const toolsUsed = [];

  try {
    process.env.CLAUDE_CODE_OAUTH_TOKEN = CLAUDE_CODE_OAUTH_TOKEN;
    const options = {
      cwd: workDir,
      permissionMode: mode === "full" ? "bypassPermissions" : mode === "edit" ? "acceptEdits" : "default",
      abortController,
      // Defesa em profundidade: nega Bash destrutivo. O isolamento por worktree
      // + ausencia de credencial de push ja' e' a garantia principal.
      canUseTool: async (toolName, input) => {
        if (toolName === "Bash" && bashIsDangerous(String(input?.command || ""))) {
          return { behavior: "deny", message: "Bloqueado pelo guardrail Fase 2 (comando destrutivo)" };
        }
        return { behavior: "allow", updatedInput: input };
      },
    };

    for await (const msg of query({ prompt: fullPrompt, options })) {
      if (msg.type === "assistant") {
        for (const block of msg.message?.content || []) {
          if (block.type === "text") resultText += block.text;
          else if (block.type === "tool_use") toolsUsed.push(block.name);
        }
      } else if (msg.type === "result") {
        usage = msg.usage || null;
        model = msg.model || null;
        if (msg.total_cost_usd !== undefined) costUsd = msg.total_cost_usd;
        if (msg.subtype === "success" && msg.result) resultText = msg.result;
        turnCount = msg.num_turns || 0;
      }
    }
  } catch (err) {
    errorMessage = err?.message || String(err);
    console.error("[delegate] error:", errorMessage);
  } finally {
    clearTimeout(timeoutId);
  }

  // ── Captura diff (edit) e limpa a worktree ──
  let editResult = null;
  if (mode === "edit" && wtPath) {
    if (!errorMessage) {
      try {
        editResult = commitAndDiff(wtPath, task);
      } catch (e) {
        errorMessage = errorMessage || `falha capturando diff: ${e.message}`;
      }
    }
    teardownWorktree(wtPath, branch);
  }

  const durationMs = Date.now() - startedAt;
  console.log(
    `[delegate] mode=${mode} duration=${durationMs}ms turns=${turnCount} ` +
      `tools=${toolsUsed.length} cost=${costUsd || "?"} ` +
      `changes=${editResult?.hasChanges ?? "-"} requested_by=${requested_by}`
  );

  if (errorMessage) {
    return res.status(500).json({ error: errorMessage, partial_result: resultText || null, duration_ms: durationMs });
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
    // Fase 2 — presente so em edit:
    branch: branch,
    has_changes: editResult ? editResult.hasChanges : null,
    diff: editResult ? editResult.diff : null,
    diff_stat: editResult ? editResult.stat : null,
    diff_truncated: editResult ? editResult.truncated : null,
  });
});

app.listen(PORT, () => {
  console.log(`claude-code-delegator listening on :${PORT}`);
  console.log(
    `  oauth: ${CLAUDE_CODE_OAUTH_TOKEN ? "configured" : "MISSING"} | ` +
      `secret: ${WORKER_SECRET ? "configured" : "MISSING"} | ` +
      `repo: ${REPO_PATH} | max_dur: ${MAX_DURATION_MS}ms | cap: ${DAILY_CALL_CAP}/day | fase2: edit/diff`
  );
});
