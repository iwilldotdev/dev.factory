// Shared path and shell analysis for dev.factory hooks (protect.js, worker-guard.js).
// A guardrail against mistakes, not a sandbox: it catches direct writes, shell
// redirection and common file commands aimed at CLI-owned run files.
// A shell guard can never be complete (encoded commands, indirection, scripts on disk).
// The AUTHORITATIVE defence is the factory CLI's evidence digests and plan-lock hashes,
// which detect tampering after the fact; this hook is defence in depth that makes the
// obvious and the cheap-to-catch bypasses fail loudly.
'use strict';
const os = require('os');

const PROTECTED_FILES = new Set(['run.json', 'metrics.json', 'ship-plan.json', 'review-report.json', 'review-report.md']);
const PROTECTED_DIRS = new Set(['evidence', 'reviews', 'baseline']);
// Written by the author worker during intake/plan (and the Figma pack by MCP save_to);
// denied to builder/auditor/scout and to shell writes. The CLI's plan-lock hashes catch later edits.
const OWNED_FILES = new Set(['acs.json', 'diagnosis.md']);
const OWNED_DIRS = new Set(['checks', 'figma']);
// runs/_inbox/<session>/...: scratch for scouts that have no run yet (not a run; nothing CLI-owned in it).
const INBOX = '_inbox';
const CONFIG = { homeRaw: '' };

// ---------- paths ----------

function argValue(argv, flag) {
  const i = argv.indexOf(flag);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : '';
}

/** FACTORY_HOME: env wins (it is what the CLI uses), then --factory-home, then ~/.factory. */
function factoryHome(argv = process.argv) {
  const raw = process.env.FACTORY_HOME || argValue(argv, '--factory-home') || '~/.factory';
  CONFIG.homeRaw = raw;
  return normPath(raw, os.homedir());
}

function homeDir() {
  return String(process.env.USERPROFILE || process.env.HOME || os.homedir());
}

function expandVars(s) {
  const fh = process.env.FACTORY_HOME || CONFIG.homeRaw || '~/.factory';
  return s
    .replace(/\$\{FACTORY_HOME\}|\$env:FACTORY_HOME|%FACTORY_HOME%|\$FACTORY_HOME\b/gi, () => fh)
    .replace(/\$\{HOME\}|\$env:USERPROFILE|%USERPROFILE%|\$HOME\b|\$env:HOME\b/gi, () => homeDir());
}

/**
 * Normalise a path to a comparable form: forward slashes, lower case,
 * drive letters as `c:/`, Git Bash `/c/` and WSL `/mnt/c/` mapped to `c:/`,
 * `~` and well-known variables expanded, relative paths resolved against cwd.
 */
function normPath(input, cwd) {
  let s = expandVars(String(input || '').trim().replace(/^['"]+|['"]+$/g, ''));
  s = s.replace(/\\/g, '/').replace(/^\/\/\?\//, '');
  if (s === '~' || s.startsWith('~/')) s = homeDir().replace(/\\/g, '/') + s.slice(1);
  const drive = s.match(/^\/mnt\/([a-zA-Z])(?=\/|$)(.*)$/) || s.match(/^\/([a-zA-Z])(?=\/|$)(.*)$/);
  if (drive) s = `${drive[1]}:${drive[2] || '/'}`;
  const absolute = /^[a-zA-Z]:\//.test(s) || /^[a-zA-Z]:$/.test(s) || s.startsWith('/');
  if (!absolute) {
    const base = cwd ? normPath(cwd) : normPath(process.cwd());
    s = `${base}/${s}`;
  }
  const lead = s.match(/^[a-zA-Z]:/) ? s.slice(0, 2) : '';
  const parts = [];
  for (const part of s.slice(lead.length).split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return `${lead}/${parts.join('/')}`.toLowerCase();
}

function relToRuns(p, home) {
  const prefix = `${home}/runs/`;
  return p.startsWith(prefix) ? p.slice(prefix.length) : null;
}

/** Intake/plan artefacts: acs.json, diagnosis.md, checks/**, figma/** (author-written). */
function isAuthorOwned(p, home) {
  const rel = relToRuns(p, home);
  if (rel === null) return false;
  const parts = rel.split('/');
  if (parts.length < 2 || parts[0] === INBOX) return false;
  if (parts.length === 2 && OWNED_FILES.has(parts[1])) return true;
  return OWNED_DIRS.has(parts[1]);
}

/** CLI-owned or author-owned: what shell commands and MCP save_to must not touch. */
function isProtected(p, home) {
  return isCliOwned(p, home) || isAuthorOwned(p, home);
}

/** A normalised path that only the factory CLI may write. */
/** env/<ws>/<alias>.pid and .storage.json: the CLI's worktree binding records (a forged pid defeats the binding). */
function isEnvOwned(p, home) {
  const prefix = `${home}/env/`;
  if (!p.startsWith(prefix)) return false;
  const parts = p.slice(prefix.length).split('/');
  return parts.length >= 2 && /\.pid$|\.storage\.json$/.test(parts[parts.length - 1]);
}

function isCliOwned(p, home) {
  if (isEnvOwned(p, home)) return true;
  const rel = relToRuns(p, home);
  if (rel === null) return false;
  const parts = rel.split('/');
  if (parts.length < 2 || parts[0] === INBOX) return false;
  if (parts.length === 2 && PROTECTED_FILES.has(parts[1])) return true;
  return PROTECTED_DIRS.has(parts[1]);
}

/** Protected, or a directory whose removal would remove protected files. */
function isProtectedOrAncestor(p, home) {
  if (isProtected(p, home)) return true;
  if (p === home || p === `${home}/runs`) return true;
  const rel = relToRuns(p, home);
  return rel !== null && !rel.includes('/');
}

function inRunSub(p, home, sub) {
  const rel = relToRuns(p, home);
  if (rel === null) return false;
  const parts = rel.split('/');
  return parts.length >= 3 && parts[1] === sub;
}

/** runs/_inbox/<anything>: free scratch area (scouts' notes before a run exists). */
function inInbox(p, home) {
  const rel = relToRuns(p, home);
  if (rel === null) return false;
  const parts = rel.split('/');
  return parts.length >= 2 && parts[0] === INBOX && !parts.includes('..');
}

function inRunDir(p, home) {
  const rel = relToRuns(p, home);
  return rel !== null && rel.split('/').length >= 2;
}

function under(p, dir) {
  return p === dir || p.startsWith(`${dir}/`);
}

function tempDirs() {
  const dirs = [os.tmpdir(), process.env.TEMP, process.env.TMP, process.env.TMPDIR, '/tmp']
    .filter(Boolean)
    .map((d) => normPath(d));
  return [...new Set(dirs)];
}

function inTemp(p) {
  return tempDirs().some((d) => under(p, d));
}

// ---------- shell ----------

const UNRESOLVED_VAR = /\$\{?\w+\}?|%\w+%|\$env:\w+/i;
const SUSPICIOUS_TAIL = /(?:^|[\\/])(?:run\.json|metrics\.json|ship-plan\.json|acs\.json|diagnosis\.md|review-report\.(?:json|md)|[^\/]+\.pid|[^\/]+\.storage\.json)$|(?:^|[\\/])(?:evidence|reviews|baseline|checks|figma)(?:[\\/]|$)/i;

/** Word that names a protected path. Unresolvable variables + suspicious tail count. */
function wordProtected(word, ctx, ancestor = false) {
  if (!word) return false;
  const p = normPath(word, ctx.cwd);
  if (ancestor ? isProtectedOrAncestor(p, ctx.home) : isProtected(p, ctx.home)) return true;
  const expanded = expandVars(word);
  return UNRESOLVED_VAR.test(expanded) && SUSPICIOUS_TAIL.test(expanded.replace(/['"]/g, ''));
}

/** Rough POSIX/PowerShell tokenizer: words, operators, redirections. */
function tokenize(cmd) {
  const tokens = [];
  let buf = '';
  let inWord = false;
  const push = () => {
    if (inWord) tokens.push({ t: 'word', v: buf });
    buf = '';
    inWord = false;
  };
  for (let i = 0; i < cmd.length; i += 1) {
    const c = cmd[i];
    if (c === "'" || c === '"') {
      const end = cmd.indexOf(c, i + 1);
      const stop = end < 0 ? cmd.length : end;
      buf += cmd.slice(i + 1, stop);
      inWord = true;
      i = stop;
    } else if (/\s/.test(c) && c !== '\n') {
      push();
    } else if (c === '\n' || c === ';') {
      push();
      tokens.push({ t: 'op', v: ';' });
    } else if (c === '&' || c === '|') {
      if (c === '&' && cmd[i + 1] === '>') {
        push();
        tokens.push({ t: 'redir', v: '>' });
        i += cmd[i + 2] === '>' ? 2 : 1;
        continue;
      }
      push();
      if (cmd[i + 1] === c) i += 1;
      tokens.push({ t: 'op', v: c });
    } else if (c === '>') {
      if (/^\d$/.test(buf) && inWord) { buf = ''; inWord = false; }
      push();
      if (cmd[i + 1] === '>' || cmd[i + 1] === '|') i += 1;
      if (cmd[i + 1] === '&') { i += 1; tokens.push({ t: 'dup' }); continue; }
      tokens.push({ t: 'redir', v: '>' });
    } else if (c === '<') {
      push();
      tokens.push({ t: 'in' });
    } else {
      buf += c;
      inWord = true;
    }
  }
  push();
  return tokens;
}

function segments(tokens) {
  const segs = [];
  let cur = { words: [], redirs: [] };
  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    if (tok.t === 'op') {
      segs.push(cur);
      cur = { words: [], redirs: [] };
    } else if (tok.t === 'redir' || tok.t === 'in' || tok.t === 'dup') {
      const next = tokens[i + 1];
      if (next && next.t === 'word') {
        if (tok.t === 'redir') cur.redirs.push(next.v);
        i += 1;
      }
    } else {
      cur.words.push(tok.v);
    }
  }
  segs.push(cur);
  return segs.filter((s) => s.words.length || s.redirs.length);
}

const WRAPPERS = new Set(['sudo', 'env', 'command', 'nice', 'nohup', 'time', 'exec', 'builtin', '&', '.']);
const DESTRUCTIVE = new Set(['rm', 'rmdir', 'del', 'erase', 'rd', 'unlink', 'shred', 'remove-item', 'ri',
  'mv', 'move', 'move-item', 'mi', 'ren', 'rename', 'rename-item', 'rni', 'truncate']);
const WRITERS = new Set(['tee', 'sponge', 'touch', 'set-content', 'sc', 'add-content', 'ac', 'out-file', 'new-item', 'ni',
  'clear-content', 'clc', 'ln', 'mklink', 'chmod', 'chown', 'export-csv', 'export-clixml']);
const COPIERS = new Set(['cp', 'copy', 'copy-item', 'cpi', 'xcopy', 'install', 'rsync', 'scp']);
const CHDIR = new Set(['cd', 'pushd', 'set-location', 'sl', 'chdir']);
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'cmd', 'powershell', 'pwsh']);
const INTERPRETERS = new Set(['python', 'python3', 'py', 'node', 'ruby', 'perl', 'deno', 'bun']);

function baseName(word) {
  return word.replace(/\\/g, '/').split('/').pop().toLowerCase().replace(/\.(?:exe|cmd|bat|ps1)$/, '');
}

function isFactory(words) {
  if (!words.length) return false;
  const cmd = baseName(words[0]);
  if (cmd === 'factory') return true;
  return INTERPRETERS.has(cmd) && words[1] === '-m' && words[2] === 'factory';
}

function stripPrefix(words) {
  let w = words.slice();
  while (w.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(w[0])) w = w.slice(1);
  while (w.length && WRAPPERS.has(baseName(w[0]))) {
    w = w.slice(1);
    while (w.length && (/^-/.test(w[0]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(w[0]))) w = w.slice(1);
  }
  return w;
}

const CANDIDATE = /[A-Za-z]:[\\/][^\s'"`;,)\]]+|[~$%][^\s'"`;,)\]]*|\/[^\s'"`;,)\]]+|[\w.-]+[\\/][^\s'"`;,)\]]+|\b(?:run|metrics|ship-plan)\.json\b/g;

function textMentionsProtected(text, ctx) {
  return (text.match(CANDIDATE) || []).some((c) => wordProtected(c, ctx, true));
}

// ---------- broad (cheap, coarse) guard ----------

const VERB_LIST = [
  'rm', 'rmdir', 'del', 'erase', 'rd', 'unlink', 'shred', 'mv', 'move', 'ren', 'rename', 'cp', 'copy', 'xcopy',
  'robocopy', 'rsync', 'scp', 'install', 'tee', 'sponge', 'touch', 'truncate', 'dd', 'ln', 'mklink', 'chmod', 'chown', 'tar',
  'unzip', '7z', 'expand-archive', 'certutil', 'wget', 'iwr', 'irm', 'invoke-webrequest', 'invoke-restmethod',
  'start-bitstransfer', 'bitsadmin', 'set-content', 'add-content', 'out-file', 'new-item', 'remove-item',
  'copy-item', 'move-item', 'rename-item', 'clear-content', 'export-csv', 'export-clixml', 'sc', 'ac', 'ni', 'ri',
  'mi', 'rni', 'cpi', 'clc', 'writealltext', 'writeallbytes', 'writealllines', 'appendalltext', 'appendallbytes',
  'writefile', 'write_text', 'write_bytes', 'writefilesync', 'appendfile', 'appendfilesync', 'rmsync', 'rmtree',
];
const WRITE_VERBS = new RegExp(
  `(?:^|[\\s;&|(\`'"=\\[.:])(?:${VERB_LIST.join('|')})(?=[\\s;&|)'"(\\[:]|$)`
  + '|\\bcurl\\b[^;&|]*\\s(?:-[a-z]*o|--output|--remote-name)\\b'
  + '|\\bsed\\b[^;&|]*\\s-[a-z]*i'
  + '|\\[io\\.file\\]::|(?:^|\\s)-(?:outfile|destination|literalpath|c)\\s',
  'i',
);
// `.factory` only as a path segment (e.g. ~/.factory, C:\Users\x\.factory), never inside a name like `dev.factory`.
const INTERP_REFS = /factory_home|(?:^|[^\w.-])\.factory(?=[\\/\s'"]|$)|process\.env|os\.environ|getenv|\$env:|%factory/i;
const SCRIPT_FILE = /\.(?:py|pyw|js|mjs|cjs|ts|rb|pl|ps1|php|lua)$/i;
const RUN_FILE_TAIL = /\/runs\/[\w.-]+\/(?:run\.json|metrics\.json|ship-plan\.json|review-report|evidence|reviews|baseline|acs\.json|checks|figma|diagnosis\.md)/;

function runsMention(text, home) {
  const t = expandVars(text).replace(/\\/g, '/').replace(/\/{2,}/g, '/').toLowerCase();
  const forms = [home, home.replace(/^([a-z]):/, '/$1'), home.replace(/^([a-z]):/, '/mnt/$1')];
  if (forms.some((f) => t.includes(`${f}/runs`))) return true;
  if (/(?:^|[\s'"=(])~\/\.factory\/runs/.test(t)) return true;
  if (/\/env\/[^/\s]+\/[^/\s]*(?:\.pid|\.storage\.json)/.test(t)) return true;
  return /(?:^|[^\w.-])\.factory\/runs|factory_home\}?%?\/runs/.test(t) || RUN_FILE_TAIL.test(t);
}

// Commands that only read. A segment starting with one of these is exempt from the "write verb" scan:
// `grep -rn remove runs/x/run.json` or `cat runs/x/evidence/CHK-1.json | jq .` are reads, not writes.
// Redirections (`> run.json`) are still caught per segment, and `tee`/`cp`/`sed -i` are not in this list.
const READONLY_CMDS = new Set(['cat', 'type', 'head', 'tail', 'less', 'more', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ls', 'dir',
  'wc', 'jq', 'stat', 'file', 'du', 'diff', 'cmp', 'basename', 'dirname', 'realpath', 'readlink', 'pwd', 'echo', 'printf',
  'get-content', 'gc', 'select-string', 'sls', 'get-childitem', 'gci', 'get-item', 'gi', 'get-location', 'test-path',
  'measure-object', 'format-table', 'ft', 'convertfrom-json', 'sort', 'cut', 'tr', 'nl', 'column', 'od',
  'sha256sum', 'md5sum', 'get-filehash', 'cd', 'pushd', 'popd', 'set-location', 'sl', 'chdir', 'test', '[', 'true', 'false']);
const FIND_WRITES = /^-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)$/;
const READ_UNSAFE_FLAGS = /^(?:--output(?:=.*)?|-[A-Za-z]*o.*|-i|--in-place.*)$/;

function readOnlySegment(words) {
  const w = stripPrefix(words);
  if (!w.length) return true;
  const cmd = baseName(w[0]);
  const args = w.slice(1);
  if (cmd === 'find') return !args.some((a) => FIND_WRITES.test(a));
  if (cmd === 'sort') return !args.some((a) => READ_UNSAFE_FLAGS.test(a));
  if (cmd === 'diff') return !args.some((a) => /^--output\b/.test(a));
  if (cmd === 'rg' || cmd === 'ag') return !args.some((a) => /^--(?:pre|hostname-bin)\b/.test(a));
  return READONLY_CMDS.has(cmd);
}

/** `python -c "..."` / `node -e "..."`: judged by what the code does (scriptWritesRun), not by flag-like words in it. */
function inlineInterpreter(words) {
  const w = stripPrefix(words);
  return w.length > 1 && INTERPRETERS.has(baseName(w[0])) && w.slice(1).some((a) => /^(?:-c|-e|--eval|-p|-pe|--print)$/.test(a));
}

// Any write call in a code blob: the "writes files" signature (combined with a protected name below).
const SCRIPT_WRITE = /\.write\w*\(|write_text|write_bytes|writeFile|appendFile|json\.dump\(|\bopen\w*\([^)]*,\s*(?:mode\s*=\s*)?['"](?:[wax][bt]?\+?|r\+)['"]|Set-Content|Out-File|Add-Content|\bunlink|rmtree|rmSync|os\.(?:remove|rename|replace|rmdir|truncate)\(|\.rename\(|\.replace\(\s*(?:Path|pathlib|os\.path|[\w.]*(?:path|dst|dest|target|run)\w*\s*\))|shutil\.(?:copy|move|rmtree)|fs\.(?:write|append|rm|unlink|rename|copy|truncate)|\.truncate\(|\bremove-item|\bmove-item|\bcopy-item|\brename-item|\[io\.file\]::|createWriteStream|subprocess\.\w+\([^)]*>/i;
const PROTECTED_NAME = /run\.json|metrics\.json|ship-plan\.json|review-report|acs\.json|diagnosis\.md|\.pid\b|\.storage\.json|[\/]runs[\/]|\b(?:evidence|reviews|baseline|checks|figma)\b/i;
// The factory home itself: lets code assemble a protected path piecewise ('ev'+'idence').
const HOME_NAME = /factory_home|(?:^|[^\w.-])\.factory(?![\w-])/i;

/**
 * True when the whole code blob has ANY write call AND names a protected file/folder or the factory home
 * anywhere (not necessarily on the same line: paths can be assembled across lines). Code with no write call
 * (read-only scripts) stays allowed.
 */
function scriptWritesRun(code) {
  const text = String(code);
  return SCRIPT_WRITE.test(text) && (PROTECTED_NAME.test(text) || HOME_NAME.test(text));
}

/** Content of a script file inside the temp dir (the session scratchpad is one), else null. */
/** True when `command` writes `file` from inline text (`> f`, `>> f`, `tee f`, Out-File/Set-Content f), so the body is in the command. */
function writtenInline(command, file) {
  const esc = String(file).replace(/^['"]|['"]$/g, '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\//g, '[\\\\/]');
  const re = new RegExp(`(?:>>?|\\btee\\s+(?:-a\\s+)?|\\b(?:Out-File|Set-Content|Add-Content)\\s+(?:-(?:Path|FilePath)\\s+)?)\\s*['"]?${esc}['"]?(?:\\s|$)`, 'i');
  return re.test(command);
}

function readScript(file, ctx) {
  const p = normPath(file, ctx.cwd);
  if (!inTemp(p)) return null;
  try {
    const fs = require('fs');
    const abs = /^[a-zA-Z]:|^[\\/]/.test(file) ? file : `${ctx.cwd}/${file}`;
    const st = fs.statSync(abs);
    return st.isFile() && st.size < 2000000 ? fs.readFileSync(abs, 'utf8') : null;
  } catch {
    return null;
  }
}

/**
 * Coarse checks over the whole command text, skipping `factory ...` segments and read-only commands:
 *  1. a non-read-only segment mentions a run path together with ANY write-capable verb or flag;
 *  2. runs a script interpreter on stdin / -c / -e / a script file while the command references
 *     FACTORY_HOME or .factory, and the code writes a run file (a line with a write call that names
 *     run.json/evidence/...). Scripts that only read are allowed. A script file is inspected only inside the
 *     temp dir (the session scratchpad is inside it); elsewhere it stays denied. The factory CLI is exempt.
 */
function broadReason(command, ctx) {
  const segs = segments(tokenize(command)).filter((seg) => {
    const w = stripPrefix(seg.words);
    return !(w.length && isFactory(w));
  });
  const text = segs.filter((s) => !readOnlySegment(s.words) && !inlineInterpreter(s.words)).map((s) => s.words.join(' ')).join('\n');
  if (runsMention(text, ctx.home) && WRITE_VERBS.test(text)) {
    return 'command mentions run files together with a write-capable verb';
  }
  const all = segs.map((s) => s.words.join(' ')).join('\n');
  const refs = INTERP_REFS.test(all);
  const piped = /<<|\|\s*(?:python3?|py|node|deno|bun|ruby|perl)\b/i.test(command);
  for (const seg of segs) {
    const words = stripPrefix(seg.words);
    if (!words.length) continue;
    const cmd = baseName(words[0]);
    if (!INTERPRETERS.has(cmd)) continue;
    const args = words.slice(1);
    if (args[0] === '-m') continue;
    const stdin = args.length === 0 || args.includes('-') || piped;
    const inline = args.some((a) => /^(?:-c|-e|--eval|-p|-pe|--print)$/.test(a));
    const files = args.filter((a) => !a.startsWith('-') && SCRIPT_FILE.test(a));
    if (!(stdin || inline || files.length)) continue;
    if (refs && scriptWritesRun(command)) return `${cmd} script/inline code writing run files`;
    for (const f of files) {
      const body = readScript(f, ctx);
      if (body !== null && scriptWritesRun(body)) return `${cmd} script ${f} writes run files`;
      // A temp script that does not exist yet is written by this same command (`cat > f.py <<EOF … ; python f.py`):
      // its body is in the command text, already judged above by scriptWritesRun(command).
      if (body === null && refs && !(inTemp(normPath(f, ctx.cwd)) && writtenInline(command, f))) {
        const where = inTemp(normPath(f, ctx.cwd)) ? 'not readable and not written inline by this command' : 'outside the temp dir';
        return `${cmd} script ${f} ${where} while the command references FACTORY_HOME or .factory`;
      }
    }
  }
  // Inline interpreter code nested in another shell (`bash -c "python -c \"...\""`) that the tokenizer cannot split.
  const nested = /\b(?:python3?|py|node|deno|bun|ruby|perl)(?:\.exe)?\s+(?:-[A-Za-z]+\s+)*(?:-c|-e|--eval|-pe?|--print)\b/i.test(command);
  if (nested && INTERP_REFS.test(command) && scriptWritesRun(command)) return 'interpreter inline code writing run files';
  return '';
}

/** A command that assigns FACTORY_ROLE (shell, PowerShell, cmd, interpreter env syntaxes). */
function setsFactoryRole(command) {
  return /FACTORY_ROLE/i.test(command)
    && /FACTORY_ROLE["']?\s*(?:=|,)|\b(?:set|export|setx|setenv|env)\b[^;&|\n]*FACTORY_ROLE|SetEnvironmentVariable|putenv|environ|process\.env/i.test(command);
}

/**
 * Bodies of `factory wait --cmd <body>` (quoted, unquoted, `--cmd=`): `factory wait` runs them with
 * shell=True, so they are judged exactly like a `bash -c` body.
 */
function waitCmdBodies(command) {
  const hasWait = segments(tokenize(command)).some((s) => {
    const w = stripPrefix(s.words);
    return w.length && isFactory(w) && w.includes('wait');
  });
  if (!hasWait) return [];
  const bodies = [];
  const re = /--cmd(?:=|\s+)("(?:\\[\s\S]|[^"\\])*"|'[^']*'|[^\s;&|]+)/g;
  let m;
  while ((m = re.exec(command))) {
    let v = m[1];
    if (v[0] === '"') v = v.slice(1, -1).replace(/\\(["\\$`])/g, '$1');
    else if (v[0] === "'") v = v.slice(1, -1);
    bodies.push(v);
  }
  return bodies;
}

/** Returns a reason string when the shell command writes a protected path, else ''. */
function bashWriteReason(command, ctxIn, depth = 0) {
  let ctx = ctxIn;
  if (depth > 3) return textMentionsProtected(command, ctx) ? 'nested command touching run files' : '';
  for (const body of waitCmdBodies(command)) {
    const r = bashWriteReason(body, ctx, depth + 1);
    if (r) return `factory wait --cmd: ${r}`;
  }
  // Command substitutions are analysed as commands of their own.
  const subs = [];
  const flat = command.replace(/\$\(([^()]*)\)|`([^`]*)`/g, (_, a, b) => { subs.push(a ?? b); return ' SUBST '; });
  for (const sub of subs) {
    const r = bashWriteReason(sub, ctx, depth + 1);
    if (r) return r;
  }
  if (depth === 0) {
    const broad = broadReason(command, ctx);
    if (broad) return broad;
  }
  const segs = segments(tokenize(flat));
  const mentions = textMentionsProtected(flat, ctx);
  for (const seg of segs) {
    for (const target of seg.redirs) {
      if (wordProtected(target, ctx)) return `shell redirection into ${target}`;
    }
    const words = stripPrefix(seg.words);
    if (!words.length || isFactory(words)) continue;
    const cmd = baseName(words[0]);
    const args = words.slice(1);
    if (CHDIR.has(cmd)) {
      const dir = args.find((a) => !a.startsWith('-'));
      if (dir) ctx = { ...ctx, cwd: normPath(dir, ctx.cwd) };
      continue;
    }
    if (cmd === 'xargs') {
      const inner = args.filter((a) => !a.startsWith('-'));
      if (inner.length && (DESTRUCTIVE.has(baseName(inner[0])) || WRITERS.has(baseName(inner[0]))
        || COPIERS.has(baseName(inner[0]))) && mentions) return `xargs ${inner[0]} over run files`;
      continue;
    }
    if (cmd === 'find' && args.some((a) => FIND_WRITES.test(a))) {
      if (args.some((a) => wordProtected(a, ctx, true))) return 'find write/-delete/-exec over run files';
      continue;
    }
    if (SHELLS.has(cmd)) {
      const i = args.findIndex((a) => /^(?:-[a-z]*c|\/c|\/k|-command)$/i.test(a));
      if (i >= 0) {
        const r = bashWriteReason(args.slice(i + 1).join(' '), ctx, depth + 1);
        if (r) return r;
      }
      continue;
    }
    if (INTERPRETERS.has(cmd)) {
      const i = args.findIndex((a) => a === '-c' || a === '-e' || a === '--eval' || a === '-p');
      if (i >= 0) {
        const code = args.slice(i + 1).join(' ');
        if (scriptWritesRun(code)) return `${cmd} inline code writing run files`;
      }
      continue;
    }
    if ((cmd === 'sed' || cmd === 'perl') && args.some((a) => /^-[a-z]*i/i.test(a) || a.startsWith('--in-place'))) {
      if (args.some((a) => !a.startsWith('-') && wordProtected(a, ctx))) return `${cmd} -i on run files`;
      continue;
    }
    if (cmd === 'sort') {
      const outs = [];
      args.forEach((a, i) => {
        let m;
        if (a === '--output') outs.push(args[i + 1]);
        else if (/^--output=/.test(a)) outs.push(a.slice(9));
        else if (!a.startsWith('--') && (m = a.match(/^-[A-Za-z]*?o(.*)$/))) outs.push(m[1] || args[i + 1]);
      });
      const hit = outs.find((a) => a && wordProtected(a, ctx));
      if (hit) return `sort -o into ${hit}`;
      continue;
    }
    if (cmd === 'diff') {
      const outs = args.filter((a) => /^--output=/.test(a)).map((a) => a.slice(9));
      args.forEach((a, i) => { if (a === '--output') outs.push(args[i + 1]); });
      const hit = outs.find((a) => a && wordProtected(a, ctx));
      if (hit) return `diff --output into ${hit}`;
      continue;
    }
    if (cmd === 'uniq' || cmd === 'xxd') {
      // the 2nd positional is the OUTPUT file
      const pos = args.filter((a) => !/^-/.test(a));
      const hit = pos.slice(1).find((a) => wordProtected(a, ctx));
      if (hit) return `${cmd} output into ${hit}`;
      continue;
    }
    if (cmd === 'dd') {
      const of = args.find((a) => a.startsWith('of='));
      if (of && wordProtected(of.slice(3), ctx)) return 'dd into run files';
      continue;
    }
    if (cmd === 'git' && args.some((a) => a === 'checkout' || a === 'restore' || a === 'rm' || a === 'mv')) {
      if (args.some((a) => !a.startsWith('-') && wordProtected(a, ctx, true))) return `git ${args[0]} on run files`;
      continue;
    }
    const positional = args.filter((a) => !/^-/.test(a) && !/^\/[a-z]$/i.test(a));
    if (DESTRUCTIVE.has(cmd)) {
      const hit = positional.find((a) => wordProtected(a, ctx, true));
      if (hit) return `${cmd} on ${hit}`;
    } else if (WRITERS.has(cmd)) {
      const hit = positional.find((a) => wordProtected(a, ctx));
      if (hit) return `${cmd} into ${hit}`;
    } else if (COPIERS.has(cmd)) {
      const targets = [];
      args.forEach((a, i) => {
        if (/^(?:-t|--target-directory|-destination)$/i.test(a)) targets.push(args[i + 1]);
        if (/^--target-directory=/.test(a)) targets.push(a.split('=')[1]);
      });
      if (positional.length > 1) targets.push(positional[positional.length - 1]);
      const hit = targets.find((a) => wordProtected(a, ctx));
      if (hit) return `${cmd} into ${hit}`;
    } else if (cmd === 'robocopy' && positional[1] && wordProtected(positional[1], ctx)) {
      return `robocopy into ${positional[1]}`;
    }
  }
  return '';
}

module.exports = {
  inInbox, scriptWritesRun, readOnlySegment, factoryHome, normPath, wordProtected, isProtected, isCliOwned, isAuthorOwned, setsFactoryRole, broadReason, isProtectedOrAncestor, inRunSub, inRunDir, inTemp, under,
  tokenize, segments, waitCmdBodies, bashWriteReason, isFactory, stripPrefix, baseName,
};
