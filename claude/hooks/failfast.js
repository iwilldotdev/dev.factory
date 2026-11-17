#!/usr/bin/env node
// Fail-fast for dev.factory workers (PreToolUse + PostToolUse + PostToolUseFailure on Bash/PowerShell).
// Why: after the first infra failure (env down, stale env, port/lease conflict, prove timeout) workers kept
// retrying `factory env up|check|prove` for 9-15 minutes. The contract is: one self-repair attempt, then hand
// back `status: blocked` with the signal. This hook enforces it:
//   - PostToolUse(Failure): parses the output for `ENV-FAIL ...`, `PROVE-FAIL ... kind=env|timeout` and
//     non-zero exits of `factory env check|up` / `factory prove` (exit 3), and counts same-kind infra failures
//     per agent (state under <tmp>/dev.factory-failfast/). On the 2nd it injects "hand back blocked now".
//   - PreToolUse: once that point is reached, a further `factory env up|check|prove` is denied.
//   - A healthy `factory env check` / a passing `factory prove` resets the counters.
// A guardrail against wasted time, not a sandbox. Fails OPEN on any parse or I/O error.
// Usage: failfast.js            (reads the hook JSON on stdin)
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const STATE_DIR = path.join(os.tmpdir(), 'dev.factory-failfast');
const MAX_AGE_MS = 3 * 60 * 60 * 1000;
const BLOCK_TTL_MS = 10 * 60 * 1000;
const MAX_DENIES = 2;
const SHELL_TOOLS = new Set(['Bash', 'PowerShell']);

const FACTORY = String.raw`(?:^|[\s;&|(])(?:[\w.:~/\\-]*[\\/])?(?:factory(?:\.cmd|\.exe)?|python3?(?:\.exe)?\s+-m\s+factory|py\s+-m\s+factory)\s+(?:--json\s+)?`;
const ENV_CMD = new RegExp(`${FACTORY}env\\s+(check|up)\\b`, 'i');
const PROVE_CMD = new RegExp(`${FACTORY}prove\\b`, 'i');

function out(obj) {
  process.stdout.write(JSON.stringify({ hookSpecificOutput: obj }));
}

function flatten(v, acc, depth = 0) {
  if (v == null || depth > 4) return;
  if (typeof v === 'string') acc.push(v);
  else if (Array.isArray(v)) v.forEach((x) => flatten(x, acc, depth + 1));
  else if (typeof v === 'object') Object.values(v).forEach((x) => flatten(x, acc, depth + 1));
}

function responseText(input) {
  const acc = [];
  flatten(input.tool_response, acc);
  flatten(input.error, acc);
  return acc.join('\n').slice(0, 400000);
}

function exitCode(input, text) {
  const r = input.tool_response;
  if (r && typeof r === 'object') {
    for (const k of ['exit_code', 'exitCode', 'returncode', 'returnCode', 'code']) {
      if (Number.isInteger(r[k])) return r[k];
    }
  }
  const m = text.match(/(?:^|\n)\s*(?:Exit code:?|exit status:?|Process exited with code)\s*(\d+)/i);
  if (m) return Number(m[1]);
  return null;
}

/** One state per worker (agent_id). No agent_id (main session): no key, so nothing is counted or denied. */
function stateFile(input) {
  if (!input.agent_id) return null;
  const key = String(input.agent_id);
  return path.join(STATE_DIR, `${crypto.createHash('sha1').update(key).digest('hex').slice(0, 16)}.json`);
}

function loadState(file) {
  try {
    const st = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (st && typeof st === 'object' && Date.now() - (st.updated || 0) < MAX_AGE_MS) return st;
  } catch { /* none yet */ }
  return { counts: {}, blocked: false, first_seen_at: '', signal: '', updated: Date.now() };
}

function saveState(file, st) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  st.updated = Date.now();
  fs.writeFileSync(file, JSON.stringify(st));
}

/** Remove state files not touched for MAX_AGE_MS (workers that never came back). */
function prune() {
  try {
    for (const name of fs.readdirSync(STATE_DIR)) {
      const f = path.join(STATE_DIR, name);
      try {
        if (Date.now() - fs.statSync(f).mtimeMs > MAX_AGE_MS) fs.unlinkSync(f);
      } catch { /* raced or gone */ }
    }
  } catch { /* no state dir */ }
}

function clearState(file) {
  try { fs.unlinkSync(file); } catch { /* nothing to clear */ }
}

/** Infra failure signals of one tool call: [{kind, line}] (kind is the counter key). */
function signals(command, text, code, failed) {
  // Only the output of `factory env check|up` and `factory prove` counts (not `grep ENV-FAIL notes.md`).
  if (!(ENV_CMD.test(command) || PROVE_CMD.test(command))) return [];
  const found = [];
  for (const line of text.split(/\r?\n/)) {
    let m = line.match(/\bENV-FAIL\b[^\n]*?\bkind=([\w-]+)/);
    if (m) found.push({ kind: `env-${m[1]}`, line: line.trim().slice(0, 300) });
    m = line.match(/\bPROVE-FAIL\b[^\n]*?\bkind=(env|timeout)\b/);
    if (m) found.push({ kind: `prove-${m[1]}`, line: line.trim().slice(0, 300) });
  }
  if (found.length) return found;
  const nonZero = code !== null ? code !== 0 : failed;
  if (nonZero) {
    const first = text.split(/\r?\n/).find((l) => l.trim()) || '';
    const env = command.match(ENV_CMD);
    if (env && code !== 2) return [{ kind: `exit-env-${env[1].toLowerCase()}`, line: `factory env ${env[1]} exit ${code === null ? 'non-zero' : code}: ${first.trim().slice(0, 240)}` }];
    if (PROVE_CMD.test(command) && code === 3) return [{ kind: 'exit-prove-env', line: `factory prove exit 3: ${first.trim().slice(0, 240)}` }];
  }
  return [];
}

const HANDBACK = (st) => `dev.factory failfast: this is the 2nd infra failure of the same kind (${st.signal}). Stop now and hand back in this turn with `
  + '`status: blocked`, '
  + `\`infra_signal: ${st.signal}\`, \`first_seen_at: ${st.first_seen_at}\`, \`tried: [...]\` (what you ran, in order). `
  + 'Do not retry `factory env up|check|prove`, do not wait on timeouts, do not restart an environment you did not start. The orchestrator decides.';

function handle(input) {
  const tool = input.tool_name;
  if (!SHELL_TOOLS.has(tool)) return;
  const command = String((input.tool_input && input.tool_input.command) || '');
  const event = input.hook_event_name || '';
  const file = stateFile(input);
  if (!file) return;

  if (event === 'PreToolUse') {
    if (!(ENV_CMD.test(command) || PROVE_CMD.test(command))) return;
    const st = loadState(file);
    if (!st.blocked) return;
    // The deny expires: after MAX_DENIES denies or BLOCK_TTL_MS, so a resumed worker is not stuck for good.
    if (Date.now() - (st.blocked_at || 0) > BLOCK_TTL_MS) { clearState(file); return; }
    st.denies = (st.denies || 0) + 1;
    if (st.denies >= MAX_DENIES) clearState(file);
    else saveState(file, st);
    out({
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: HANDBACK(st),
    });
    return;
  }

  const failed = event === 'PostToolUseFailure';
  const text = responseText(input);
  const code = exitCode(input, text);
  const found = signals(command, text, code, failed);
  if (!found.length) {
    const healthy = !failed && (code === null || code === 0);
    if (healthy && (ENV_CMD.test(command) || PROVE_CMD.test(command))) clearState(file);
    return;
  }
  const st = loadState(file);
  const seen = new Set();
  let repeated = null;
  for (const f of found) {
    if (seen.has(f.kind)) continue;
    seen.add(f.kind);
    st.counts[f.kind] = (st.counts[f.kind] || 0) + 1;
    if (!st.first_seen_at) st.first_seen_at = new Date().toISOString();
    if (st.counts[f.kind] >= 2 && !repeated) repeated = f;
    if (!repeated) st.signal = f.line;
  }
  if (repeated) {
    st.blocked = true;
    st.blocked_at = Date.now();
    st.denies = 0;
    st.signal = repeated.line;
  }
  saveState(file, st);
  if (repeated) out({ hookEventName: event || 'PostToolUse', additionalContext: HANDBACK(st) });
}

let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  try {
    prune();
    const input = JSON.parse(raw);
    if (input && typeof input === 'object') handle(input);
  } catch { /* fail open */ }
  process.exit(0);
});
