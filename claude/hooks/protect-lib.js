// Shared path and shell analysis for dev.factory hooks (protect.js, worker-guard.js).
// A guardrail against mistakes, not a sandbox: it catches direct writes, shell
// redirection and common file commands aimed at CLI-owned run files.
// A shell guard can never be complete (encoded commands, indirection, scripts on disk).
// The AUTHORITATIVE defence is the factory CLI's evidence digests and plan-lock hashes,
// which detect tampering after the fact; this hook is defence in depth that makes the
// obvious and the cheap-to-catch bypasses fail loudly.
//
// Shell decision (bashWriteReason):
//   deny = extraReason(cmd) || ((BASELINE.bashWriteReason(cmd) || coarseReason(cmd)) && !provablySafe(cmd))
// extraReason: precise per-target rules that only ever ADD denials. BASELINE: the frozen pre-write-target logic.
// coarseReason: the same coarse net with the later verbs and indirections. provablySafe: a short POSITIVE grammar;
// a coarse denial is lifted only when the whole command parses into it.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

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
  BASELINE.factoryHome(argv);
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
    if (part === '..') { parts.pop(); continue; }
    // Windows ignores a trailing dot or space and an NTFS stream suffix (`run.json.`, `run.json::$DATA`, `run.json:s`).
    const name = part.replace(/:.*$/, '').replace(/[. ]+$/, '');
    if (name) parts.push(name);
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
  // The default home (~/.factory) stays protected even when FACTORY_HOME points elsewhere: a command can name either.
  const homes = [ctx.home, normPath('~/.factory')];
  if (homes.some((h) => (ancestor ? isProtectedOrAncestor(p, h) : isProtected(p, h)))) return true;
  const expanded = expandVars(word);
  return UNRESOLVED_VAR.test(expanded) && SUSPICIOUS_TAIL.test(expanded.replace(/['"]/g, ''));
}

/** Rough POSIX/PowerShell tokenizer: words, operators, redirections. */
function tokenize(cmd) {
  const tokens = [];
  let buf = '';
  let inWord = false;
  let start = 0;
  let at = 0;
  const push = () => {
    if (inWord) tokens.push({ t: 'word', v: buf, raw: cmd.slice(start, at) });
    buf = '';
    inWord = false;
  };
  for (let i = 0; i < cmd.length; i += 1) {
    const c = cmd[i];
    at = i;
    if ((c === "'" || c === '"') && !inWord) start = i;
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
      if (!inWord) start = i;
      buf += c;
      inWord = true;
    }
  }
  at = cmd.length;
  push();
  return tokens;
}

function segments(tokens) {
  const segs = [];
  let cur = { words: [], raws: [], redirs: [] };
  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    if (tok.t === 'op') {
      segs.push(cur);
      cur = { words: [], raws: [], redirs: [] };
    } else if (tok.t === 'redir' || tok.t === 'in' || tok.t === 'dup') {
      const next = tokens[i + 1];
      if (next && next.t === 'word') {
        if (tok.t === 'redir') cur.redirs.push(next.v);
        i += 1;
      }
    } else {
      cur.words.push(tok.v);
      cur.raws.push(tok.raw === undefined ? tok.v : tok.raw);
    }
  }
  segs.push(cur);
  return segs.filter((s) => s.words.length || s.redirs.length);
}

const WRAPPERS = new Set(['sudo', 'env', 'command', 'nice', 'nohup', 'time', 'exec', 'builtin', '&', '.']);
const DESTRUCTIVE = new Set(['rm', 'rmdir', 'del', 'erase', 'rd', 'unlink', 'shred', 'remove-item', 'ri',
  'mv', 'move', 'move-item', 'mi', 'ren', 'rename', 'rename-item', 'rni', 'truncate', 'icacls', 'cacls', 'attrib', 'takeown', 'chgrp']);
const WRITERS = new Set(['tee', 'sponge', 'touch', 'set-content', 'sc', 'add-content', 'ac', 'out-file', 'new-item', 'ni',
  'clear-content', 'clc', 'ln', 'mklink', 'chmod', 'chown', 'export-csv', 'export-clixml', 'mkdir', 'md', 'junction', 'tee-object', 'start-transcript']);
const COMPRESSORS = new Set(['gzip', 'gunzip', 'bzip2', 'bunzip2', 'xz', 'unxz', 'zstd', 'compress', 'uncompress']);
const EDITORS = new Set(['vi', 'vim', 'nvim', 'nano', 'emacs', 'notepad', 'notepad++', 'code', 'ed', 'ex', 'micro', 'subl', 'gedit', 'pico', 'patch']);
const GIT_WRITES = new Set(['checkout', 'restore', 'rm', 'mv', 'clean', 'reset', 'stash', 'apply', 'switch']);
const COPIERS = new Set(['cp', 'copy', 'copy-item', 'cpi', 'xcopy', 'install', 'rsync', 'scp']);
const CHDIR = new Set(['cd', 'pushd', 'set-location', 'sl', 'chdir', 'push-location']);
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'cmd', 'powershell', 'pwsh']);
const INTERPRETERS = new Set(['python', 'python3', 'py', 'node', 'ruby', 'perl', 'deno', 'bun']);

function baseName(word) {
  return word.replace(/\\/g, '/').split('/').pop().toLowerCase().replace(/\.(?:exe|cmd|bat|ps1)$/, '');
}

/** The factory CLI by its BARE command name (or `python -m factory`): never `./factory`, a path to some other `factory`, `factory.py`. */
function isFactory(words) {
  if (!words.length) return false;
  if (/^factory(?:\.cmd)?$/i.test(words[0])) return true;
  const cmd = baseName(words[0]);
  return INTERPRETERS.has(cmd) && words[1] === '-m' && words[2] === 'factory';
}

/** A factory CLI segment the coarse nets may skip: no command substitution or backticks in its arguments. */
function skippableFactory(seg) {
  const w = stripPrefix(seg.words);
  return w.length > 0 && isFactory(w) && !seg.raws.some((r) => /\$\(|`|[<>]\(/.test(r));
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

// ---------- coarse net ----------

const VERB_LIST = [
  'rm', 'rmdir', 'del', 'erase', 'rd', 'unlink', 'shred', 'mv', 'move', 'ren', 'rename', 'cp', 'copy', 'xcopy',
  'robocopy', 'rsync', 'scp', 'install', 'tee', 'sponge', 'touch', 'truncate', 'dd', 'ln', 'mklink', 'chmod', 'chown', 'tar',
  'unzip', '7z', 'expand-archive', 'certutil', 'wget', 'iwr', 'irm', 'invoke-webrequest', 'invoke-restmethod',
  'start-bitstransfer', 'bitsadmin', 'set-content', 'add-content', 'out-file', 'new-item', 'remove-item',
  'copy-item', 'move-item', 'rename-item', 'clear-content', 'export-csv', 'export-clixml', 'sc', 'ac', 'ni', 'ri',
  'mi', 'rni', 'cpi', 'clc', 'writealltext', 'writeallbytes', 'writealllines', 'appendalltext', 'appendallbytes',
  'writefile', 'write_text', 'write_bytes', 'writefilesync', 'appendfile', 'appendfilesync', 'rmsync', 'rmtree',
  'icacls', 'cacls', 'attrib', 'takeown', 'chgrp', 'junction', 'tee-object', 'start-transcript', 'patch', 'gzip', 'gunzip', 'bzip2', 'bunzip2',
  'xz', 'unxz', 'zstd', 'compress', 'uncompress',
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
const SCRIPT_WRITE = /\.write\w*\(|write_text|write_bytes|writeFile|appendFile|json\.dump\(|\bopen\w*\((?:[^()\n]|\([^()\n]*\))*,\s*(?:mode\s*=\s*)?['"](?:[wax][bt]?\+?|r\+)['"]|\.open\(\s*(?:mode\s*=\s*)?['"](?:[wax][bt]?\+?|r\+)['"]|\bos\.open\(|\bO_(?:WRONLY|RDWR|TRUNC|CREAT|APPEND|EXCL)\b|\bfdopen\(|\bopenSync\(|\bsys\.std(?:out|err)\s*=(?!=)|\bprocess\.std(?:out|err)\s*=(?!=)|Set-Content|Out-File|Add-Content|\bunlink|rmtree|rmSync|os\.(?:remove|rename|replace|rmdir|truncate)\(|\.rename\(|\.replace\(\s*(?:Path|pathlib|os\.path|[\w.]*(?:path|dst|dest|target|run)\w*\s*\))|shutil\.(?:copy|move|rmtree)|fs\.(?:write|append|rm|unlink|rename|copy|truncate)|\.truncate\(|\bremove-item|\bmove-item|\bcopy-item|\brename-item|\[io\.file\]::|createWriteStream|subprocess\.\w+\([^)]*>|\bos\.(?:system|popen|exec\w*|spawn\w*)\(|\bsubprocess\.\w+\(|\bchild_process\b|\b(?:exec|execSync|execFileSync|spawnSync)\(|\bFile\.(?:delete|write|binwrite|rename|chmod|truncate|unlink)\b|\bFileUtils\b|\bDeno\.(?:writeTextFile|writeFile|remove|rename|truncate)\b|\bBun\.write\b/i;
// Writing to the process's own stdout/stderr is output, not a file write (unless the stream was reassigned: SCRIPT_WRITE flags that).
const STD_STREAM_WRITE = /\b(?:sys|process)\.std(?:out|err)(?:\.buffer)?\.write\w*\(/gi;
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
  const reassigned = /\b(?:sys|process)\.std(?:out|err)\s*=(?!=)/i.test(text);
  const scan = reassigned ? text : text.replace(STD_STREAM_WRITE, '(');
  return SCRIPT_WRITE.test(scan) && (PROTECTED_NAME.test(text) || HOME_NAME.test(text));
}

/** True when `command` writes `file` from inline text (`> f`, `>> f`, `tee f`, Out-File/Set-Content f), so the body is in the command. */
function writtenInline(command, file) {
  const esc = String(file).replace(/^['"]|['"]$/g, '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\//g, '[\\\\/]');
  const re = new RegExp(`(?:>>?|\\btee\\s+(?:-a\\s+)?|\\b(?:Out-File|Set-Content|Add-Content)\\s+(?:-(?:Path|FilePath)\\s+)?)\\s*['"]?${esc}['"]?(?:\\s|$)`, 'i');
  return re.test(command);
}

/** Content of a script file inside the temp dir (the session scratchpad is one), else null. */
function readScript(file, ctx) {
  const p = normPath(file, ctx.cwd);
  if (!inTemp(p)) return null;
  try {
    const abs = /^[a-zA-Z]:|^[\\/]/.test(file) ? file : `${ctx.cwd}/${file}`;
    const st = fs.statSync(abs);
    return st.isFile() && st.size < 2000000 ? fs.readFileSync(abs, 'utf8') : null;
  } catch {
    return null;
  }
}

const ASSIGNMENT = /(?:^|[\s;&|(])(?:export\s+|set\s+|local\s+)?\$?[A-Za-z_]\w*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s;&|]+)/g;
const EVAL_CMDS = new Set(['eval', 'iex', 'invoke-expression']);

function inPlaceEditor(cmd, args) {
  return (cmd === 'sed' || cmd === 'perl' || cmd === 'awk' || cmd === 'gawk')
    && args.some((a) => /^-[a-z]*i/i.test(a) || a.startsWith('--in-place'));
}

/** True when a variable is assigned a run path: a later `$var` may then hide a protected destination. */
function taintedByAssignment(command, ctx) {
  return (command.match(ASSIGNMENT) || []).some((m) => runsMention(m, ctx.home) || textMentionsProtected(m, ctx));
}

/** Hides statement separators inside `$(...)`, `<(...)`, `>(...)` and backticks so a segment keeps its substitution body. */
function maskSubstitutions(cmd) {
  let out = '';
  let depth = 0;
  let tick = false;
  for (let i = 0; i < cmd.length; i += 1) {
    const c = cmd[i];
    if (c === '(' && (cmd[i - 1] === '$' || cmd[i - 1] === '<' || cmd[i - 1] === '>')) depth += 1;
    else if (depth > 0 && c === '(') depth += 1;
    else if (depth > 0 && c === ')') depth -= 1;
    else if (c === '`') tick = !tick;
    out += (depth > 0 || tick) && /[;&|\n]/.test(c) ? ' \u0001 ' : c;
  }
  return out;
}

const COARSE = 'command mentions run files together with a write-capable verb';

/**
 * The baseline's coarse net with what was added after it: the strict factory name, the longer verb list, a variable
 * assigned a run path, eval. The non-read-only, non-factory segments mention a run path and carry a write-capable verb.
 */
function coarseReason(command, ctx) {
  const segs = segments(tokenize(maskSubstitutions(command))).filter((seg) => {
    const w = stripPrefix(seg.words);
    return w.length && !skippableFactory(seg) && !readOnlySegment(seg.words) && !inlineInterpreter(seg.words);
  });
  const text = segs.map((s) => s.words.join(' ')).join('\n');
  if (!(runsMention(text, ctx.home) || (taintedByAssignment(command, ctx) && /[$%]/.test(text)))) return '';
  if (segs.some((s) => EVAL_CMDS.has(baseName(stripPrefix(s.words)[0])))) return 'eval of command text mentioning run files';
  return WRITE_VERBS.test(text) ? COARSE : '';
}

// ---------- precise rules (they only ever add denials) ----------

/**
 * Interpreters: runs a script interpreter on stdin / -c / -e / a script file while the command references
 * FACTORY_HOME or .factory, and the code writes a run file (a write call plus a protected name). Scripts that only
 * read are allowed. A script file is inspected only inside the temp dir (the session scratchpad is inside it);
 * elsewhere it stays denied. The factory CLI is exempt.
 */
function interpreterReason(command, ctx) {
  const segs = segments(tokenize(command)).filter((seg) => !skippableFactory(seg));
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

/** Path readings of a word: itself, a flag's attached value (`-Path:X`, `--target-directory=X`, `-tX`), without backslashes. */
function pathCands(v) {
  const s = String(v);
  const out = [s];
  if (/^-/.test(s)) {
    const eq = s.search(/[=:]/);
    if (eq > 0) out.push(s.slice(eq + 1));
    if (!/^--/.test(s)) for (let i = 2; i < Math.min(s.length, 60); i += 1) out.push(s.slice(i));
  }
  if (/\\/.test(s)) out.push(s.replace(/\\/g, ''));
  return out.filter(Boolean);
}

/** The word, read in any way, is a protected path, an ancestor of one or inside the protected tree. */
function candProtected(v, ctx) {
  return pathCands(v).some((c) => wordProtected(c, ctx, true));
}

/** The word names a protected path somewhere inside it without BEING one (script text, `a:b`, `-c:w! path`). */
function unparsedMention(v, ctx) {
  return textMentionsProtected(String(v), ctx) && !candProtected(v, ctx);
}

const WRITING_MODULES = /^(?:zipfile|tarfile|shutil|py_compile|compileall|venv|ensurepip|pip|fileinput|gzip|bz2|lzma|json\.tool|cProfile|profile|trace)$/;

/** A shell/interpreter that reads its script from stdin (no -c, no script file): the text piped or heredoc'd into it runs. */
function readsStdinScript(cmd, args) {
  if (EVAL_CMDS.has(cmd)) return true;
  if (args.some((a) => /^(?:-[a-z]*c|\/c|\/k|-command|-c|-e|--eval|-p|-pe|--print|-m)$/i.test(a))) return false;
  return !args.some((a) => !a.startsWith('-') || a === '-') || args.includes('-') || args.includes('-s');
}

/** Unquoted parens and lone braces as statement separators: `(cd R && echo x > run.json)` is a cd and a redirection. */
function ungroup(cmd) {
  let out = '';
  let quote = '';
  for (const c of cmd) {
    if (quote) quote = c === quote ? '' : quote;
    else if (c === "'" || c === '"') quote = c;
    out += !quote && (c === '(' || c === ')') ? ' ; ' : c;
  }
  return out.replace(/(^|[\s;])[{}](?=[\s;]|$)/g, '$1;');
}

/** Precise per-target rules: a reason when a destination is protected or cannot be determined, else ''. Nested bodies get the full decision. */
function extraReason(command, ctxIn, depth) {
  let ctx = ctxIn;
  if (depth > 3) return textMentionsProtected(command, ctx) ? 'nested command touching run files' : '';
  for (const body of waitCmdBodies(command)) {
    const r = decide(body, ctx, depth + 1);
    if (r) return `factory wait --cmd: ${r}`;
  }
  // Command and process substitutions are analysed as commands of their own.
  const subs = [];
  const flat = command.replace(/[$<>]\(([^()]*)\)|`([^`]*)`/g, (_, a, b) => { subs.push(a ?? b); return ' SUBST '; });
  for (const sub of subs) {
    const r = decide(sub, ctx, depth + 1);
    if (r) return r;
  }
  if (depth === 0) {
    const r = interpreterReason(command, ctx);
    if (r) return r;
  }
  const segs = segments(tokenize(ungroup(flat)));
  const mentions = textMentionsProtected(flat, ctx);
  const tainted = taintedByAssignment(command, ctx);
  for (const seg of segs) {
    for (const target of seg.redirs) {
      if (wordProtected(target, ctx)) return `shell redirection into ${target}`;
    }
    const words = stripPrefix(seg.words);
    if (!words.length || isFactory(words)) continue;
    const cmd = baseName(words[0]);
    const args = words.slice(1);
    // A destination that cannot be resolved (variable, command substitution) in a segment that touches run files.
    const segMention = tainted || textMentionsProtected(seg.words.join(' '), ctx);
    const undecidable = (a) => segMention && (a === 'SUBST' || UNRESOLVED_VAR.test(expandVars(a)));
    if (CHDIR.has(cmd)) {
      const dir = args.find((a) => !a.startsWith('-'));
      if (dir) ctx = { ...ctx, cwd: normPath(dir, ctx.cwd) };
      continue;
    }
    if (cmd === 'xargs') {
      const inner = args.filter((a) => !a.startsWith('-')).map(baseName);
      const risky = inner.find((c) => DESTRUCTIVE.has(c) || WRITERS.has(c) || COPIERS.has(c) || SHELLS.has(c)
        || INTERPRETERS.has(c) || EVAL_CMDS.has(c) || EDITORS.has(c));
      if (risky && mentions) return `xargs ${risky} over run files`;
      continue;
    }
    if (cmd === 'find' && args.some((a) => FIND_WRITES.test(a))) {
      if (args.some((a) => wordProtected(a, ctx, true))) return 'find write/-delete/-exec over run files';
      continue;
    }
    if ((SHELLS.has(cmd) || EVAL_CMDS.has(cmd)) && readsStdinScript(cmd, args)
      && (segs.length > 1 || /<<</.test(command)) && (mentions || runsMention(command, ctx.home))) {
      return `text piped into ${cmd} mentions run files`;
    }
    if (/^(?:sed|gsed|awk|gawk|mawk|nawk)$/.test(cmd) && args.some((a) => unparsedMention(a, ctx))) {
      return `${cmd} script names run files`;
    }
    if (SHELLS.has(cmd)) {
      const i = args.findIndex((a) => /^(?:-[a-z]*c|\/c|\/k|-command)$/i.test(a));
      if (i >= 0) {
        const r = decide(args.slice(i + 1).join(' '), ctx, depth + 1);
        if (r) return r;
      }
      continue;
    }
    if (inPlaceEditor(cmd, args)) {
      // sed -i / perl -i / awk -i inplace: the file operands are written in place.
      if (args.some((a) => !a.startsWith('-') && (wordProtected(a, ctx) || undecidable(a)))) return `${cmd} -i on run files`;
      continue;
    }
    if (cmd === 'patch' && ctx.cwd && wordProtected(ctx.cwd, ctx, true)) return 'patch inside run files';
    if (EDITORS.has(cmd)) {
      const hit = args.find((a) => !a.startsWith('-') && (wordProtected(a, ctx) || undecidable(a)));
      if (hit) return `${cmd} on ${hit}`;
      continue;
    }
    if (INTERPRETERS.has(cmd) && args[0] === '-m' && WRITING_MODULES.test(args[1] || '')) {
      // `python -m json.tool in.json` only reads its first operand; every other module here writes where it is pointed.
      const operands = args.slice(2);
      const firstRead = args[1] === 'json.tool' ? operands.findIndex((a) => !a.startsWith('-')) : -1;
      if (operands.some((a, i) => i !== firstRead && candProtected(a, ctx))) return `${cmd} -m ${args[1]} on run files`;
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
    if (cmd === 'git') {
      // The worktree git changes is the cwd, `-C <dir>` or a path operand: only one inside the protected tree counts.
      const verb = args.find((a) => GIT_WRITES.has(a));
      if (verb) {
        const inside = (ctx.cwd && wordProtected(ctx.cwd, ctx, true))
          || args.some((a) => candProtected(a, ctx) || undecidable(a))
          || seg.words.slice(0, seg.words.length - words.length).some((a) => /^GIT_(?:WORK_TREE|DIR|INDEX_FILE)=/.test(a) && candProtected(a.replace(/^[^=]*=/, ''), ctx));
        if (inside) return `git ${verb} on run files`;
      }
      continue;
    }
    // Operands: plain words plus the value attached to a flag (`-Path:X`, `-FilePath=X`).
    const attached = args.filter((a) => /^-[A-Za-z][\w-]*[:=]./.test(a)).map((a) => a.replace(/^-[^:=]*[:=]/, ''));
    const positional = args.filter((a) => !/^-/.test(a) && !/^\/[a-z]$/i.test(a)).concat(attached);
    const globbed = positional.some((a) => /[*?[{]/.test(a)) && ctx.cwd && wordProtected(ctx.cwd, ctx, true);
    if (DESTRUCTIVE.has(cmd) || COMPRESSORS.has(cmd)) {
      if (COMPRESSORS.has(cmd) && args.some((a) => /^(?:-[A-Za-z]*c[A-Za-z]*|--stdout|--to-stdout)$/.test(a))) continue;
      const hit = positional.find((a) => wordProtected(a, ctx, true) || undecidable(a)
        || (COMPRESSORS.has(cmd) && wordProtected(a.replace(/\.(?:gz|bz2|xz|zst|z|tgz)$/i, ''), ctx, true)));
      if (hit) return `${cmd} on ${hit}`;
      if (globbed) return `${cmd} with a glob inside run files`;
      // `Get-ChildItem <run folder> | Remove-Item`: the operand arrives on the pipeline, so it cannot be seen.
      if (!positional.length && mentions) return `${cmd} fed by a pipeline over run files`;
    } else if (WRITERS.has(cmd)) {
      const hit = positional.find((a) => wordProtected(a, ctx) || undecidable(a));
      if (hit) return `${cmd} into ${hit}`;
      if (globbed) return `${cmd} with a glob inside run files`;
    } else if (COPIERS.has(cmd)) {
      const targets = [];
      args.forEach((a, i) => {
        if (/^(?:-t|--target-directory|-d\w*)$/i.test(a) || /^-[A-Za-z]*t$/.test(a)) targets.push(args[i + 1]);
        else if (/^--target-directory=/.test(a)) targets.push(a.split('=')[1]);
        else if (/^-[A-Za-z]*t./.test(a) && /^(?:cp|install)$/.test(cmd)) targets.push(a.replace(/^-[A-Za-z]*?t/, ''));
        else if (/^-\w+[:=]./.test(a) && /^-d/i.test(a)) targets.push(a.replace(/^-[^:=]*[:=]/, ''));
      });
      const explicit = targets.length > 0;
      if (!explicit && positional.length > 1) targets.push(positional[positional.length - 1]);
      // `install -d dir` has a single operand that is the target; rsync may delete the source files it copies.
      if (cmd === 'install' && positional.length === 1) targets.push(positional[0]);
      if (cmd === 'rsync' && args.some((a) => /^--(?:remove-source-files|delete\w*)$/.test(a))) targets.push(...positional);
      // a destination that is a protected directory (or the run folder above it) is written into
      const hit = targets.find((a) => a && (wordProtected(a, ctx, true) || undecidable(a)));
      if (hit) return `${cmd} into ${hit}`;
    } else if (cmd === 'robocopy') {
      const moves = args.some((a) => /^\/(?:mov|move|mir|purge)$/i.test(a));
      const hit = moves ? positional.find((a) => wordProtected(a, ctx, true)) : positional[1] && (wordProtected(positional[1], ctx) || undecidable(positional[1])) ? positional[1] : '';
      if (hit) return `robocopy into ${hit}`;
    }
  }
  return '';
}

// ---------- provably safe: a positive grammar ----------
//
// A command the coarse nets deny is allowed only when the WHOLE command parses into this grammar. It does not look
// for dangers; it accepts a short list of shapes and everything else keeps the coarse verdict:
//   readers (cat, grep, jq, Get-Content...; sort/diff/gzip with listed flags), with output redirections;
//   cp [listed flags] SRC... DEST;  sed [-i|-n|-E|-r] s/a/b/[gI0-9] FILE...;  cd /absolute;  the factory CLI (data).
// A write target (redirection, cp destination, sed -i file) is /dev/null, NUL, or a strict literal whose real path is
// inside a safe root (the temp dir; the session cwd when it is outside every factory home) and outside the guarded
// tree, from every cwd the command may be in. Each rule holds in bash, PowerShell 5.1 and later (where cp, cat, ls,
// sort, diff, echo are cmdlet aliases and native arguments are re-quoted) and on Windows paths.

// Smart quotes and dashes are quotes and parameter dashes to PowerShell: only ASCII, letters and digits pass.
const LOOKALIKE = /[^\t\n\x20-\x7e\p{L}\p{N}]/u;
// The only variables a word may use, at its start, and only where the word is read: their values are known.
const KNOWN_VAR = /^(?:\$HOME|\$\{HOME\}|\$FACTORY_HOME|\$\{FACTORY_HOME\}|\$env:USERPROFILE|\$env:HOME|\$env:FACTORY_HOME|%USERPROFILE%|%FACTORY_HOME%)(?![\w:%}])/i;

/**
 * Strict lexer: per segment, words `{v, raw, quoted, whole, split, odd, qbad}` plus output (`outs`) and input (`ins`)
 * redirection words; null on anything outside the grammar (substitutions, globs, braces, parens, comments, `&`,
 * heredocs, process substitution, escapes other than `\x` in a word, unknown variables, `~user`).
 * `whole`: the word is one quoted string. `split`: PowerShell would split it (`"a"/b`). `odd`: a variable, `~` or `\`.
 * `qbad`: a quoted part is empty, holds whitespace or a quote character, or starts with `-`: PowerShell 5.1 re-quotes
 * such an argument for a native program without escaping, so the program may see other words or a flag.
 * `mode` is the shell whose reading of an unquoted `\` is used: 'bash' (escapes the next character) or 'ps' (plain).
 */
function lexStrict(cmd, mode = 'bash') {
  if (LOOKALIKE.test(cmd)) return null;
  const segs = [];
  let seg = { words: [], outs: [], ins: [], piped: false };
  let w = null;
  let redir = '';
  const endWord = () => {
    if (!w || w.raw === '') return;
    (redir === '>' ? seg.outs : redir === '<' ? seg.ins : seg.words).push(w);
    w = null;
    redir = '';
  };
  const endSeg = (op) => {
    endWord();
    if (redir) return false;
    // A newline after `|` or `&&` continues the pipeline or list: the next command keeps the piped mark.
    if (!(seg.words.length || seg.outs.length || seg.ins.length)) return op === ';';
    segs.push(seg);
    seg = { words: [], outs: [], ins: [], piped: op === '|' };
    return true;
  };
  for (let i = 0; i < cmd.length;) {
    const c = cmd[i];
    const next = cmd[i + 1] || '';
    if (c === ' ' || c === '\t') { endWord(); i += 1; continue; }
    if (c === '\n' || c === ';' || c === '|' || c === '&') {
      const op = c === '\n' ? ';' : (c === '&' || c === '|') && next === c ? c + c : c;
      if (op === '&' || (op === '|' && next === '&') || !endSeg(op)) return null;
      i += op.length;
      continue;
    }
    if (c === '>') {
      if (w && /^[0-9]$/.test(w.raw) && !redir) w = null; else endWord();
      if (redir) return null;
      let j = i + 1;
      if (cmd[j] === '>' || cmd[j] === '|') j += 1;
      if (cmd[j] === '&') { // `2>&1`, `>&2`: a descriptor, no file
        if (!/^[0-9]$/.test(cmd[j + 1] || '') || /[^ \t\n;|&]/.test(cmd[j + 2] || ' ')) return null;
        i = j + 2;
        continue;
      }
      if (cmd[j] === '(') return null;
      redir = '>';
      i = j;
      continue;
    }
    if (c === '<') {
      if (/[<(&>]/.test(next)) return null;
      endWord();
      if (redir) return null;
      redir = '<';
      i += 1;
      continue;
    }
    if (!w) w = { v: '', raw: '', quoted: false, whole: false, split: false, odd: false, qbad: false };
    const fresh = w.raw === '';
    if (c === "'" || c === '"') {
      const e = cmd.indexOf(c, i + 1);
      if (e < 0) return null;
      const body = cmd.slice(i + 1, e);
      if (c === '"' && /[$`\\]/.test(body)) return null;
      if (body === '' || /[\s'"]/.test(body) || body.startsWith('-')) w.qbad = true;
      w.v += body;
      w.raw += cmd.slice(i, e + 1);
      w.quoted = true;
      if (fresh) w.whole = true;
      i = e + 1;
      // PowerShell splits `"a"/b` into two arguments and reads `"a".b` or `"a"-b` as a member or a parameter.
      if (fresh && i < cmd.length && !/[ \t\n;|&<>]/.test(cmd[i])) {
        if (!/[\\/]/.test(cmd[i])) return null;
        w.split = true;
      }
      continue;
    }
    if (c === '\\') { // bash: escapes the next character (a newline continues the line); PowerShell: a plain character
      const take = mode === 'bash' ? cmd.slice(i, i + 2) : c;
      if (take !== '\\\n') {
        w.v += mode === 'bash' ? next : c;
        w.raw += take;
        w.odd = true;
      }
      i += take.length;
      continue;
    }
    if (c === '$' || c === '%') {
      const m = fresh && cmd.slice(i).match(KNOWN_VAR);
      if (!m) return null;
      w.v += m[0];
      w.raw += m[0];
      w.odd = true;
      i += m[0].length;
      continue;
    }
    if (c === '~') {
      if (!fresh || !/^(?:[\\/ \t\n;|&]|)$/.test(next)) return null;
      w.v += c;
      w.raw += c;
      w.odd = true;
      i += 1;
      continue;
    }
    if (/[`(){}*?[\]#!^@]/.test(c) || c < ' ') return null;
    w.v += c;
    w.raw += c;
    i += 1;
  }
  return endSeg(';') ? segs : null;
}

const ABS = /^(?:[A-Za-z]:\/|\/)/;
const STRICT_PATH = /^(?:[A-Za-z]:\/)?[A-Za-z0-9_.+\/-]+$/;

/** A literal ASCII path with one meaning in bash, PowerShell and Windows: no quoting tricks, variables, `\`, `..`, `//`, spaces, streams, short names, trailing dots. */
function strictPath(w) {
  if (!w || w.odd || w.split || w.qbad || (w.quoted && !w.whole)) return false;
  const v = w.v;
  if (!STRICT_PATH.test(v) || v.startsWith('-') || v.includes('//')) return false;
  return v.replace(/^[A-Za-z]:/, '').split('/').every((c) => c === '' || c === '.' || (c !== '..' && !c.endsWith('.')));
}

/** Real path of an fs path: the nearest existing ancestor resolved through links and junctions, plus the rest; null when unsure. */
function realOf(p) {
  const tail = [];
  let cur = path.resolve(p);
  for (let i = 0; i < 200; i += 1) {
    try {
      return path.join(fs.realpathSync.native(cur), ...tail);
    } catch (e) {
      if (e.code !== 'ENOENT') return null;
    }
    try {
      fs.lstatSync(cur);
      return null; // exists but does not resolve: a dangling link
    } catch (e) {
      if (e.code !== 'ENOENT') return null;
    }
    const up = path.dirname(cur);
    if (up === cur) return null;
    tail.unshift(path.basename(cur));
    cur = up;
  }
  return null;
}

/**
 * Filesystem paths a strict literal denotes from `cwd`, one per shell reading, or null when a reading is not certain.
 * On Windows a `/x` path is the root of the current drive (`drive`) in PowerShell and a Git Bash mount: only the drive
 * mounts (`/c/`) and `/tmp` are mapped; any other mount (`/proc/cygdrive`, `/mnt`, `/cygdrive`, `/dev`...) is unknown.
 */
function writeForms(v, cwd, drive) {
  if (!ABS.test(v)) {
    const bases = writeForms(String(cwd).replace(/\\/g, '/'), null, drive);
    return bases && bases.map((b) => `${b.replace(/\/$/, '')}/${v}`);
  }
  if (process.platform !== 'win32' || !v.startsWith('/')) return [v];
  const mount = /^\/[A-Za-z](?:\/|$)/.test(v) ? `${v[1]}:/${v.slice(3)}` : /^\/tmp(?:\/|$)/.test(v) ? os.tmpdir() + v.slice(4) : null;
  return mount && [`${drive}:${v}`, mount];
}

let homeCache = null;

/** The guarded homes, lexical and real: FACTORY_HOME (ctx and env) and ~/.factory. */
function guardedHomes(ctx) {
  const raws = [ctx.home, process.env.FACTORY_HOME, CONFIG.homeRaw, `${homeDir()}/.factory`].filter(Boolean);
  const key = raws.join('|');
  if (homeCache && homeCache.key === key) return homeCache.homes;
  const lexical = raws.map((r) => normPath(r, os.homedir()));
  const real = raws.map((r) => realOf(expandVars(r).replace(/^~(?=[\\/]|$)/, homeDir())))
    .filter(Boolean).map((r) => normPath(r));
  homeCache = { key, homes: [...new Set([...lexical, ...real])] };
  return homeCache.homes;
}

/** A factory home, inside one (any run file, remote/ and notes/ included), or an ancestor of one. */
function nearHome(p, homes) {
  return homes.some((h) => under(h, p) || under(p, h));
}

/** Where a write may land: the real temp dir, and the real session cwd when it is outside every guarded home. */
function safeRoots(ctx, homes) {
  const inHome = (p) => homes.some((h) => under(p, h));
  const cwd = ctx.cwd ? realOf(String(ctx.cwd)) : null;
  const tmp = realOf(os.tmpdir());
  return [tmp, cwd && !inHome(normPath(ctx.cwd)) && !inHome(normPath(cwd)) ? cwd : null].filter(Boolean).map((r) => normPath(r));
}

/**
 * A real path a write may land on: inside a safe root, neither inside nor above a factory home, not an existing folder (a cp
 * into it would write <dest>/<name>, perhaps an existing link) and not a file with a second name (a hard link).
 */
function landsSafe(real, homes, roots) {
  if (real === null) return false;
  const p = normPath(real);
  if (nearHome(p, homes) || !roots.some((r) => under(p, r))) return false;
  // Links node cannot see: MSYS/Cygwin follow a "magic cookie" file (`!<symlink>` + target) and a `<name>.lnk` shortcut as
  // symlinks. So no path component may have a `.lnk` twin, the nearest existing ancestor must be a real folder (a cookie
  // is a file), and an existing target must not be a cookie.
  let at = real;
  let st = null;
  for (;;) {
    if (fs.existsSync(`${at}.lnk`)) return false;
    try {
      st = fs.lstatSync(at);
      break;
    } catch (e) {
      if (e.code !== 'ENOENT') return false;
    }
    const up = path.dirname(at);
    if (up === at) return false;
    at = up;
  }
  if (at !== real) return st.isDirectory();
  if (!st.isFile() || st.nlink > 1) return false;
  try {
    const fd = fs.openSync(real, 'r');
    try {
      const head = Buffer.alloc(12);
      const n = fs.readSync(fd, head, 0, 12, 0);
      return !head.subarray(0, n).toString('latin1').includes('!<symlink>');
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return false;
  }
}

/**
 * The real paths a write target lands on, from every cwd the command may be in and in every shell reading, or null when
 * the target is not acceptable: not /dev/null or NUL, not a strict literal, naming a run path, or landing outside a safe root.
 */
function targetPaths(w, cwds, ctx) {
  if (w && !w.quoted && (w.v === '/dev/null' || (process.platform === 'win32' && /^nul$/i.test(w.v)))) return [];
  if (!strictPath(w) || mentions(w, ctx)) return null;
  const homes = guardedHomes(ctx);
  const roots = safeRoots(ctx, homes);
  const drive = (String(ctx.cwd || '').match(/^([A-Za-z]):/) || process.cwd().match(/^([A-Za-z]):/) || ['', 'C'])[1];
  const out = [];
  for (const cwd of ABS.test(w.v) ? [ctx.cwd || process.cwd()] : cwds) {
    if (nearHome(normPath(w.v, cwd), homes)) return null;
    const forms = writeForms(w.v, cwd, drive);
    if (!forms) return null;
    for (const f of forms) {
      const real = realOf(f);
      if (!landsSafe(real, homes, roots)) return null;
      out.push(real);
    }
  }
  return out;
}

/** The word names a run or env path in any reading (the baseline's mention test, per word and per flag-attached value). */
function mentions(w, ctx) {
  return [w.v, w.raw, w.v.replace(/\\/g, ''), ...pathCands(w.v)].some((t) => runsMention(t, ctx.home) || textMentionsProtected(t, ctx));
}

// Readers: every operand is read and no flag of theirs writes or runs a program.
const READ_ANY = new Set(['cat', 'type', 'head', 'tail', 'wc', 'ls', 'dir', 'stat', 'grep', 'egrep', 'fgrep', 'jq', 'echo', 'pwd', 'true',
  'get-content', 'gc', 'select-string', 'sls', 'get-childitem', 'gci', 'test-path', 'get-item', 'gi']);
// Readers with flags that write or run programs (sort -o/-T/--compress-program, diff --output, gzip without -c): flags from a list.
const READ_FLAGS = {
  sort: /^(?:-[bdfghiMnRrsuVz]+|-k\S+|-t.|--(?:numeric-sort|reverse|unique|ignore-case|version-sort|human-numeric-sort|stable|key=\S+))$/,
  diff: /^(?:-[abBdEiNqrstTuwy]+|-U\d+|--(?:brief|recursive|unified|strip-trailing-cr|ignore-all-space))$/,
  gzip: /^(?:-[cdfnqv1-9]+|--stdout|--to-stdout)$/,
  gunzip: /^(?:-[cdfnqv]+|--stdout|--to-stdout)$/,
  zcat: /^-[fqv]+$/,
};
// cp flags that mean the same harmless thing to GNU cp and, as Copy-Item parameters, are a switch or a binding error
// (no -t/-T/-l/-s/-b; no -L, which PowerShell reads as -LiteralPath and so swaps source and destination; no -p, -pv is -PipelineVariable).
const CP_FLAGS = /^(?:-[rRafvnu]+|--(?:recursive|archive|verbose|no-clobber|update|force))$/;
const SED_SCRIPT = /^s\/[^\/\\\n]*\/[^\/\\\n]*\/[gI0-9]*$/;
// cmd.exe reads these in the arguments of factory.cmd however they were quoted (PowerShell re-quotes only on whitespace).
const CMD_META = /["&|<>^%!]/;

/** A segment's kind (`read`, `cp`, `sed`, `factory`) and its words' roles (`read`, `data`, `plain`, `write`, `dest`), or null outside the grammar. */
function segmentRoles(seg) {
  const head = seg.words[0];
  if (!head || head.quoted || head.odd || !/^[A-Za-z][\w.-]*$/.test(head.v)) return null;
  const name = head.v.toLowerCase();
  const args = seg.words.slice(1);
  const as = (role, ws = args) => ws.map((w) => ({ w, role }));
  const isFlag = (w) => /^-./.test(w.v);
  if (/^factory(?:\.cmd)?$/.test(name) || (/^python3?$/.test(name) && args.length >= 2 && args[0].raw === '-m' && args[1].raw === 'factory')) {
    return args.some((w) => CMD_META.test(w.v)) ? null : { kind: 'factory', roles: as('data') };
  }
  if (seg.words.some((w) => w.qbad)) return null;
  if (READ_ANY.has(name)) return { kind: 'read', roles: as('read') };
  if (READ_FLAGS[name]) {
    const flags = args.filter(isFlag);
    if (!flags.every((w) => READ_FLAGS[name].test(w.v))) return null;
    if (/^g/.test(name) && !flags.some((w) => /^-[^-]*c|^--(?:to-)?stdout$/.test(w.v))) return null;
    return { kind: 'read', roles: args.map((w) => ({ w, role: isFlag(w) ? 'flag' : 'read' })) };
  }
  if (name === 'cp' && !seg.piped) { // PowerShell binds piped objects to Copy-Item's path parameters
    const pos = [];
    let opts = true;
    for (const w of args) {
      if (opts && w.v === '--') opts = false;
      else if (opts && isFlag(w)) { if (!CP_FLAGS.test(w.v)) return null; } else if (w.v.includes(',')) return null; // PowerShell: an array
      else pos.push(w);
    }
    if (pos.length < 2) return null;
    return { kind: 'cp', roles: [...as('read', pos.slice(0, -1)), { w: pos[pos.length - 1], role: 'dest' }] };
  }
  if (name === 'sed') {
    let script = null;
    let inPlace = false;
    const files = [];
    for (const w of args) {
      if (isFlag(w)) {
        if (w.v === '-i') inPlace = true;
        else if (!/^-[nEr]$/.test(w.v)) return null;
      } else if (!script) script = w;
      else files.push(w);
    }
    if (!script || !SED_SCRIPT.test(script.v)) return null;
    return { kind: 'sed', roles: [{ w: script, role: 'plain' }, ...as(inPlace ? 'write' : 'read', files)] };
  }
  return null;
}

/**
 * True only when the whole command parses into the grammar in both shells: known command words, every word classified,
 * run paths named only where they are read (reader operands, cp sources, factory CLI arguments), every write target
 * landing in a safe root, and no write under a path that an earlier cp of the same command created (a link it copied).
 */
function provablySafe(command, ctx) {
  return ['bash', 'ps'].every((mode) => safeIn(String(command), ctx, mode));
}

/** provablySafe() under one shell's reading of an unquoted backslash. */
function safeIn(command, ctx, mode) {
  const segs = lexStrict(command, mode);
  if (!segs || !segs.length) return false;
  const cwds = [ctx.cwd || process.cwd()];
  for (const seg of segs) {
    const name = seg.words.length ? seg.words[0].v.toLowerCase() : '';
    if (!CHDIR.has(name)) continue;
    const args = seg.words.slice(1);
    if (name !== 'cd' || args.length !== 1 || seg.piped || seg.outs.length || seg.ins.length) return false;
    if (!strictPath(args[0]) || !ABS.test(args[0].v) || mentions(args[0], ctx)) return false;
    cwds.push(args[0].v);
  }
  const writes = [];
  let named = false;
  for (const [i, seg] of segs.entries()) {
    if (CHDIR.has(seg.words.length ? seg.words[0].v.toLowerCase() : '')) continue;
    const s = segmentRoles(seg);
    if (!s || ((seg.outs.length || seg.ins.length) && s.kind !== 'read')) return false;
    for (const w of seg.outs) {
      const paths = targetPaths(w, cwds, ctx);
      if (!paths) return false;
      writes.push({ i, paths });
    }
    for (const { w, role } of [...s.roles, ...seg.ins.map((x) => ({ w: x, role: 'read' }))]) {
      const m = mentions(w, ctx);
      named = named || m;
      if (role === 'write' || role === 'dest') {
        const paths = targetPaths(w, cwds, ctx);
        if (!paths) return false;
        writes.push({ i, paths, cp: role === 'dest' });
      } else if (m && role !== 'read' && role !== 'data') return false;
    }
  }
  // cp -r/-a copies links as links: a later write at or under an earlier cp destination may go through one.
  const norm = (ps) => ps.map((p) => normPath(p));
  for (const d of writes.filter((x) => x.cp)) {
    const dp = norm(d.paths);
    if (writes.some((t) => t.i > d.i && norm(t.paths).some((p) => dp.some((q) => under(p, q))))) return false;
  }
  // A run path in the raw text that no single word accounts for was split or spelled in a way the lexer did not see.
  return named || !(runsMention(command, ctx.home) || textMentionsProtected(command, ctx));
}

// ---------- heredoc text ----------

/**
 * `<first line> <<DELIM` + body + `DELIM` as the WHOLE command, where the heredoc feeds a data consumer: `cat` with no
 * operand, `tee`, `git commit|tag ... -F -`, `gh ... --body-file -`, the factory CLI. The body is dropped and the
 * first line (with an optional trailing `> file`) is judged in full. An unquoted DELIM keeps any body with `$`, a
 * backtick or `\` (the shell expands those). Anything else is returned unchanged.
 */
function stripDataHeredoc(command) {
  const lines = command.split('\n');
  const m = lines[0].match(/^(.*?)<<(-?)(?:'([A-Za-z_][\w-]*)'|"([A-Za-z_][\w-]*)"|([A-Za-z_][\w-]*))((?:[ \t]*>>?[ \t]*[\w.\/:-]+)?)[ \t]*$/);
  if (!m || lines.length < 3) return command;
  const delim = m[3] || m[4] || m[5];
  let end = lines.length - 1;
  while (end > 0 && lines[end].trim() === '') end -= 1;
  const close = (l) => (m[2] ? l.replace(/^\t+/, '') : l) === delim;
  const first = lines.findIndex((l, i) => i > 0 && close(l));
  if (first !== end) return command;
  const body = lines.slice(1, end).join('\n');
  if (m[5] && /[$`\\]/.test(body)) return command;
  const segs = lexStrict(m[1]);
  const last = segs && segs[segs.length - 1];
  if (!last || last.outs.length || last.ins.length) return command;
  const w = last.words.map((x) => x.v);
  const name = w[0].toLowerCase();
  const dash = (flag) => w.some((x, i) => (x === flag && w[i + 1] === '-') || x === `${flag}=-`);
  const consumer = (name === 'cat' && w.length === 1) || name === 'tee' || isFactory(w)
    || (name === 'git' && (w.includes('commit') || w.includes('tag')) && (dash('-F') || dash('--file')))
    || (name === 'gh' && (dash('--body-file') || dash('-F')));
  return consumer ? `${m[1]}${m[6]}` : command;
}

// ---------- decision ----------

function decide(command, ctx, depth) {
  const extra = extraReason(command, ctx, depth);
  if (extra) return extra;
  const coarse = BASELINE.bashWriteReason(command, ctx) || coarseReason(command, ctx);
  return coarse && !provablySafe(command, ctx) ? coarse : '';
}

/** Returns a reason string when the shell command writes a protected path, else ''. */
function bashWriteReason(command, ctx) {
  return decide(stripDataHeredoc(String(command)), ctx, 0);
}

// ---------- baseline: the coarse logic as it was before the write-target rule ----------
// Byte-identical to test/fixtures/protect-lib.baseline.js from its 'use strict' line on (guard-provable.test.mjs
// checks it). Never edit it here: it is the deny side of the decision; provablySafe() is the only way out of it.
const BASELINE = (function frozenBaseline() {
  const module = { exports: {} };
// >>> frozen baseline
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
// <<< frozen baseline
  return module.exports;
}());

module.exports = {
  inInbox, scriptWritesRun, readOnlySegment, factoryHome, normPath, wordProtected, isProtected, isCliOwned, isAuthorOwned, setsFactoryRole, isProtectedOrAncestor, inRunSub, inRunDir, inTemp, under,
  tokenize, segments, waitCmdBodies, bashWriteReason, isFactory, stripPrefix, baseName, provablySafe, stripDataHeredoc, lexStrict,
};
