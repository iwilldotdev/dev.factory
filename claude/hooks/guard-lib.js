// Provider-neutral guard helpers shared by protect.js and worker-guard.js.
// The factory knows no provider: everything provider-specific is DATA in
// FACTORY_HOME/profiles/<name>/profile.json (fields cli_write_patterns, mcp_read_allow, save_args).
// A guardrail against mistakes, not a sandbox.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const lib = require('./protect-lib');

// ---------- MCP tool classification ----------

const READ_VERBS = new Set([
  'get', 'list', 'search', 'read', 'fetch', 'find', 'lookup', 'query', 'describe', 'show', 'view', 'download',
  'count', 'whoami', 'me', 'export', 'resolve',
]);
const WRITE_VERBS = new Set([
  'create', 'update', 'save', 'add', 'edit', 'write', 'transition', 'merge', 'accept', 'delete', 'remove', 'post',
  'send', 'approve', 'unapprove', 'publish', 'run', 'trigger', 'retry', 'cancel', 'play', 'apply', 'link', 'unlink',
  'watch', 'upload', 'complete', 'reopen', 'close', 'label', 'assign', 'move', 'set', 'resolve_thread', 'rebase',
  'revert', 'fork', 'star', 'push',
  // beyond the spec list (fail-closed extensions)
  'replace', 'rename', 'archive', 'restore', 'grant', 'revoke', 'submit', 'invite', 'reset',
  'drop', 'destroy', 'purge', 'upsert', 'insert', 'patch', 'put', 'import', 'comment', 'reply', 'dismiss', 'mark',
  'deploy', 'execute', 'invoke', 'sync', 'lock', 'unlock', 'toggle', 'rerun', 'enable', 'disable', 'clone',
]);

/** `mcp__<server>__<tool>` -> {server, tool}; null when it is not an MCP tool name. */
function parseMcpName(name) {
  const m = /^mcp__(.+?)__(.+)$/s.exec(String(name || ''));
  return m ? { server: m[1], tool: m[2] } : null;
}

/** snake_case, kebab-case, camelCase, dotted and spaced names -> lower-case tokens. */
function tokenizeToolName(tool) {
  return String(tool)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((t) => t.toLowerCase());
}

function globToRegExp(glob) {
  const body = String(glob).split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${body}$`, 'i');
}

/**
 * 'read' or 'write' for a tool name. Tokens are scanned left to right (a namespace or vendor prefix such as
 * `acme_get_ticket` is skipped): the first token that is a known verb (read or write) decides, and any write token
 * (or adjacent pair) anywhere forces WRITE. A name with no known verb is WRITE (fail-closed). A tool segment that
 * matches a profile `mcp_read_allow` glob is READ.
 * `readAllow` globs match the tool segment (or the whole `mcp__<server>__<tool>` when the glob starts with `mcp__`).
 */
function classifyMcpTool(name, readAllow = []) {
  const parsed = parseMcpName(name);
  if (!parsed) return 'write';
  for (const g of readAllow) {
    if (typeof g !== 'string' || !g) continue;
    const target = g.startsWith('mcp__') ? String(name) : parsed.tool;
    if (globToRegExp(g).test(target)) return 'read';
  }
  const tokens = tokenizeToolName(parsed.tool);
  let decided = '';
  for (let i = 0; i < tokens.length; i += 1) {
    if (WRITE_VERBS.has(tokens[i])) return 'write';
    if (i + 1 < tokens.length && WRITE_VERBS.has(`${tokens[i]}_${tokens[i + 1]}`)) return 'write';
    if (!decided && READ_VERBS.has(tokens[i])) decided = 'read';
  }
  return decided || 'write';
}

// ---------- save arguments ----------

const SAVE_ARGS = ['save_to', 'output_path', 'output_file', 'out', 'dest'];

// Anchored: a name that merely CONTAINS "out" or "path" (timeout, layout, file_path of an upload) is not a destination.
const SAVE_NAME = /^(?:save_?(?:to|path|file|dir)|out(?:put)?(?:_?(?:path|file|dir))?|dest(?:ination)?(?:_?(?:path|dir))?)$/i;

/**
 * Destination paths carried by a tool call's arguments: string values whose argument name matches SAVE_NAME (or a
 * default / profile `save_args` name), at any depth of nested objects and arrays (a name applies to everything below it).
 */
function saveDestinations(args, extraNames = []) {
  if (!args || typeof args !== 'object') return [];
  const names = new Set([...SAVE_ARGS, ...extraNames.filter((n) => typeof n === 'string' && n)].map((n) => n.toLowerCase()));
  const out = [];
  const walk = (v, matched, depth) => {
    if (typeof v === 'string') {
      if (matched && v.trim()) out.push(v);
    } else if (v && typeof v === 'object' && depth < 8) {
      if (Array.isArray(v)) {
        for (const x of v) walk(x, matched, depth + 1);
      } else {
        for (const [k, x] of Object.entries(v)) walk(x, matched || names.has(k.toLowerCase()) || SAVE_NAME.test(k), depth + 1);
      }
    }
  };
  walk(args, false, 0);
  return out;
}

// ---------- generic CLI write patterns (no provider names) ----------

const WRITE_METHOD = String.raw`(?:POST|PUT|PATCH|DELETE)`;
const PS_WRITE_METHOD = String.raw`(?:Post|Put|Patch|Delete)`;
const GENERIC_CLI_WRITES = [
  // -X POST, -XPOST, bundled -sXPOST, --request[=| ]POST
  [new RegExp(String.raw`\bcurl\b[^;&|\n]*(?:\s-[A-Za-z]*X\s*|\s--request(?:[\s=]+))["']?${WRITE_METHOD}\b`, 'i'), 'curl with a write method'],
  // short body flags, alone or bundled after no-argument flags (-d, -sd, -sSLd, -F, -T)
  [new RegExp(String.raw`\bcurl\b[^;&|\n]*\s(?:-[sSLkvfiIgGN#]*[dFT]|--(?:data(?:-[\w-]+)?|form(?:-string)?|json|upload-file)(?![\w-]))`), 'curl sending a body'],
  [new RegExp(String.raw`\bwget\b[^;&|\n]*\s--(?:method[\s=]+["']?${WRITE_METHOD}\b|post-data|post-file|body-data|body-file)`, 'i'), 'wget sending a request body'],
  [new RegExp(String.raw`(?:^|[;&|(]\s*)(?:http|https|xh|xhs)(?:\.exe)?\s+(?:-\S+\s+)*["']?${WRITE_METHOD}\b`, 'i'), 'http client with a write method'],
  // httpie / xh: a data item (`field=value`, `field:=json`, `field=@file`) makes the request a POST
  [new RegExp(String.raw`(?:^|[;&|(]\s*)(?:http|https|xh|xhs)(?:\.exe)?\s+(?:-\S+\s+)*\S+\s+(?:\S+\s+)*[\w.-]+(?::=|=@|=(?!=))`, 'i'), 'http client sending a data item'],
  [new RegExp(String.raw`\b(?:Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b[^;&|\n]*\s-Method[\s:=]+["']?${PS_WRITE_METHOD}\b`, 'i'), 'PowerShell web request with a write method'],
  [new RegExp(String.raw`\b(?:Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b[^;&|\n]*\s-(?:Body|InFile)\b`, 'i'), 'PowerShell web request sending a body'],
];

/** Reason string when the command is a generic remote write, else ''. */
function genericCliWriteReason(command) {
  for (const [re, why] of GENERIC_CLI_WRITES) if (re.test(command)) return why;
  return '';
}

// ---------- profiles ----------

function rawHome(argv = process.argv) {
  const i = argv.indexOf('--factory-home');
  const flag = i >= 0 && argv[i + 1] ? argv[i + 1] : '';
  const raw = process.env.FACTORY_HOME || flag || '~/.factory';
  if (raw === '~' || raw.startsWith('~/') || raw.startsWith('~\\')) return path.join(os.homedir(), raw.slice(1));
  return path.resolve(raw);
}

function readJson(file) {
  try {
    const v = JSON.parse(fs.readFileSync(file, 'utf8'));
    return v && typeof v === 'object' ? v : null;
  } catch {
    return null;
  }
}

const SAFE_NAME = /^[A-Za-z0-9][\w.-]*$/;

function runIdsIn(input) {
  const texts = [];
  const walk = (v, depth) => {
    if (typeof v === 'string') texts.push(v);
    else if (v && typeof v === 'object' && depth < 3) for (const x of Object.values(v)) walk(x, depth + 1);
  };
  walk(input, 0);
  const ids = [];
  for (const t of texts) {
    for (const m of t.replace(/\\/g, '/').matchAll(/\/runs\/([\w.-]+)\//gi)) ids.push(m[1]);
  }
  return ids;
}

/** Names in `providers` of the workspace of the run named in the call, else of every workspace under FACTORY_HOME. */
function providerNames(home, input) {
  const wsNames = new Set();
  for (const id of runIdsIn(input)) {
    if (!SAFE_NAME.test(id)) continue;
    const run = readJson(path.join(home, 'runs', id, 'run.json'));
    if (run && typeof run.workspace === 'string' && SAFE_NAME.test(run.workspace)) wsNames.add(run.workspace);
  }
  if (!wsNames.size) {
    try {
      for (const e of fs.readdirSync(path.join(home, 'workspaces'), { withFileTypes: true })) {
        if (e.isDirectory() && SAFE_NAME.test(e.name)) wsNames.add(e.name);
      }
    } catch { /* no workspaces: generic rules only */ }
  }
  const names = new Set();
  for (const ws of wsNames) {
    const conf = readJson(path.join(home, 'workspaces', ws, 'workspace.json'));
    const providers = conf && conf.providers && typeof conf.providers === 'object' ? conf.providers : {};
    for (const v of Object.values(providers)) if (typeof v === 'string' && SAFE_NAME.test(v)) names.add(v);
  }
  return [...names];
}

/**
 * profile.json of an APPROVED profile, else null (fail closed: generic rules only). Approved means
 * FACTORY_HOME/profiles/<name>/approved.json exists and its `file_sha256` equals the sha256 of the profile.json bytes.
 * Drafts (FACTORY_HOME/profile-drafts/) are never read here.
 */
function approvedProfile(home, name) {
  const dir = path.join(home, 'profiles', name);
  let bytes;
  try { bytes = fs.readFileSync(path.join(dir, 'profile.json')); } catch { return null; }
  const approval = readJson(path.join(dir, 'approved.json'));
  const want = approval && typeof approval.file_sha256 === 'string' ? approval.file_sha256.trim().toLowerCase() : '';
  const have = crypto.createHash('sha256').update(bytes).digest('hex');
  if (!want || want !== have) {
    process.stderr.write(`dev.factory guard: profile "${name}" is not approved (approved.json missing or sha mismatch): generic rules only\n`);
    return null;
  }
  try {
    const prof = JSON.parse(bytes.toString('utf8'));
    return prof && typeof prof === 'object' ? prof : null;
  } catch {
    return null;
  }
}

/**
 * Active profile data, read on every call (no cache). An unreadable workspace or profile contributes nothing:
 * the generic rules still apply and nothing is inferred (documented limitation).
 */
function loadProfileGuards(input, argv = process.argv) {
  const home = rawHome(argv);
  const res = { cliWrite: [], mcpReadAllow: [], saveArgs: [] };
  for (const name of providerNames(home, input)) {
    const prof = approvedProfile(home, name);
    if (!prof) continue;
    for (const p of Array.isArray(prof.cli_write_patterns) ? prof.cli_write_patterns : []) {
      if (typeof p !== 'string' || !p) continue;
      try { res.cliWrite.push(new RegExp(p, 'i')); } catch { /* invalid regex: skipped */ }
    }
    for (const g of Array.isArray(prof.mcp_read_allow) ? prof.mcp_read_allow : []) if (typeof g === 'string') res.mcpReadAllow.push(g);
    for (const a of Array.isArray(prof.save_args) ? prof.save_args : []) if (typeof a === 'string') res.saveArgs.push(a);
  }
  return res;
}

/** Reason string when the shell command matches a generic or profile CLI write pattern, else ''. */
function cliWriteReason(command, guards) {
  const parts = [...new Set([command, ...command.split(/&&|\|\||[;|&\n]/), ...commandUnits(command)]
    .map((s) => s.trim()).filter(Boolean))];
  for (const s of parts) {
    const generic = genericCliWriteReason(s);
    if (generic) return generic;
  }
  for (const re of guards.cliWrite) {
    if (parts.some((s) => re.test(s))) return 'a CLI write pattern of the active provider profile';
  }
  return '';
}

// ---------- command normalisation ----------

const SHELL_NAMES = new Set(['bash', 'sh', 'zsh', 'dash', 'pwsh', 'powershell', 'cmd']);
// xargs / env flags that consume the next word
const XARGS_VALUE_FLAGS = /^-(?:[IneEdLPsSaE])$/;

/** Unquoted parentheses become statement separators: `(acme-cli x)` is the command `acme-cli x`. */
function ungroupParens(cmd) {
  let out = '';
  let quote = '';
  for (const c of cmd) {
    if (quote) quote = c === quote ? '' : quote;
    else if (c === "'" || c === '"') quote = c;
    out += !quote && (c === '(' || c === ')') ? ' ; ' : c;
  }
  return out;
}

// Wrapper commands and the flags that consume the NEXT word (a flag glued to its value, `-oL` / `--user=x`, is one word).
const NO_FLAGS = [];
const WRAPPER_TABLE = {
  env: { flags: ['-u', '--unset', '-C', '--chdir', '-S', '--split-string'], assign: true },
  sudo: { flags: ['-u', '--user', '-g', '--group', '-h', '--host', '-p', '--prompt', '-C', '--close-from', '-D', '--chdir', '-R', '--chroot', '-T', '--command-timeout', '-r', '--role', '-t', '--type', '-U', '--other-user'], assign: true },
  timeout: { flags: ['-s', '--signal', '-k', '--kill-after'], positional: 1 },
  stdbuf: { flags: ['-i', '--input', '-o', '--output', '-e', '--error'] },
  nice: { flags: ['-n', '--adjustment'] },
  ionice: { flags: ['-c', '--class', '-n', '--classdata', '-p', '--pid', '-P', '--pgid', '-u', '--uid'] },
  nohup: { flags: NO_FLAGS },
  time: { flags: ['-f', '--format', '-o', '--output'] },
  command: { flags: NO_FLAGS },
  exec: { flags: ['-a'] },
  builtin: { flags: NO_FLAGS },
  setsid: { flags: NO_FLAGS },
  '&': { flags: NO_FLAGS },
  '.': { flags: NO_FLAGS },
};
const ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*=/;

/** One wrapper at the start of `w` removed with its own flags (per-wrapper table), else `w` unchanged. */
function stripWrapper(w) {
  const spec = WRAPPER_TABLE[lib.baseName(w[0])];
  if (!spec) return w;
  let rest = w.slice(1);
  while (rest.length) {
    const a = rest[0];
    if (a === '--') { rest = rest.slice(1); break; }
    if (spec.assign && ASSIGN.test(a)) { rest = rest.slice(1); continue; }
    if (!/^-/.test(a)) break;
    rest = rest.slice(spec.flags.includes(a) ? 2 : 1);
  }
  for (let n = spec.positional || 0; n > 0 && rest.length; n -= 1) rest = rest.slice(1); // timeout DURATION
  return rest;
}

/** Words of a command segment after VAR=val, env/command/exec/nice/time/sudo/timeout/stdbuf... and xargs prefixes; `.exe/.cmd/.bat/.ps1` dropped from the command name. */
function normalizeWords(words) {
  let w = words;
  for (let i = 0; i < 8; i += 1) {
    const before = w.length;
    while (w.length && ASSIGN.test(w[0])) w = w.slice(1);
    if (w.length) w = stripWrapper(w); // the table covers every wrapper lib.stripPrefix knows, with the right flag arity
    if (w.length && lib.baseName(w[0]) === 'xargs') {
      w = w.slice(1);
      while (w.length && /^-/.test(w[0])) {
        const flag = w[0];
        w = w.slice(1);
        if (XARGS_VALUE_FLAGS.test(flag)) w = w.slice(1);
      }
    }
    if (w.length === before) break;
  }
  if (w.length) w = [w[0].replace(/\.(?:exe|cmd|bat|ps1)$/i, ''), ...w.slice(1)];
  return w;
}

/** Literal bodies fed to a shell: `bash <<< 'x'`, `sh <<< "x"`, `echo 'x' | bash|sh` (a literal only; nothing is evaluated). */
function shellFedBodies(command) {
  const lit = String.raw`("(?:[^"\\]|\\[\s\S])*"|'[^']*')`;
  const unq = (v) => (v[0] === '"' ? v.slice(1, -1).replace(/\\(["\\$`])/g, '$1') : v.slice(1, -1));
  const sh = String.raw`(?:[\w.:~/\\-]*[\\/])?(?:bash|sh|zsh|dash)(?:\.exe)?`;
  const out = [];
  for (const re of [
    new RegExp(String.raw`(?:^|[\s;&|(])${sh}(?:\s+-[\w-]+)*\s*<<<\s*${lit}`, 'g'),
    new RegExp(String.raw`(?:^|[\s;&|(])(?:echo|printf)(?:\s+-[\w-]+)*\s+${lit}\s*\|\s*${sh}(?![\w-])`, 'g'),
  ]) {
    let m;
    while ((m = re.exec(command))) out.push(unq(m[1]));
  }
  return out;
}

/** Body text of `bash -c <body>` / `pwsh -Command <body>` / `cmd /c <body>`, else null. */
function shellBody(words) {
  if (!words.length || !SHELL_NAMES.has(lib.baseName(words[0]))) return null;
  const args = words.slice(1);
  const i = args.findIndex((a) => /^-[a-z]*c$/.test(a) || /^-command$/i.test(a) || /^\/[ck]$/i.test(a));
  if (i < 0 || i + 1 >= args.length) return null;
  const powershellLike = /^(?:pwsh|powershell|cmd)$/.test(lib.baseName(words[0]));
  return powershellLike ? args.slice(i + 1).join(' ') : args[i + 1];
}

/**
 * Normalised command texts of a shell command line: every segment with its VAR=val / env / command / exec / nice /
 * time / sudo / xargs prefixes removed, plus the segments of `bash -c` / `sh -c` / `pwsh -Command` bodies,
 * `( ... )` groups, `$( ... )`, backtick and process substitutions. A path-qualified command also yields its bare name.
 * Heuristic and bounded (depth 5): not a shell parser.
 */
function commandUnits(command, depth = 0) {
  const out = [];
  if (depth > 5) return out;
  const subs = [];
  let flat = String(command);
  for (let pass = 0; pass < 5; pass += 1) {
    const next = flat.replace(/[$<>]\(([^()]*)\)|`([^`]*)`/g, (_, a, b) => { subs.push(a ?? b); return ' SUBST '; });
    if (next === flat) break;
    flat = next;
  }
  for (const sub of subs) out.push(...commandUnits(sub, depth + 1));
  for (const body of shellFedBodies(String(command))) out.push(...commandUnits(body, depth + 1));
  for (const seg of lib.segments(lib.tokenize(ungroupParens(flat)))) {
    const w = normalizeWords(seg.words);
    if (!w.length) continue;
    out.push(w.join(' '));
    if (/[\\/]/.test(w[0])) out.push([lib.baseName(w[0]), ...w.slice(1)].join(' '));
    const body = shellBody(w);
    if (body !== null) out.push(...commandUnits(body, depth + 1));
  }
  return out;
}

/** True when the hook input says the caller is not a factory worker (the main session or another agent). */
function notFactoryWorker(input) {
  const t = input && input.agent_type;
  return typeof t === 'string' && t.length > 0 && !/(?:^|[:/])factory-(?:scout|author|builder|auditor)$/.test(t);
}

module.exports = {
  READ_VERBS, WRITE_VERBS, SAVE_ARGS, parseMcpName, tokenizeToolName, classifyMcpTool, saveDestinations,
  genericCliWriteReason, loadProfileGuards, approvedProfile, cliWriteReason, commandUnits, notFactoryWorker, rawHome,
};
