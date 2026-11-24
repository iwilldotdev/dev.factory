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
const CHDIR = new Set(['cd', 'pushd', 'set-location', 'sl', 'chdir']);
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

// ---------- broad (cheap, coarse) guard ----------

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

// Commands whose write destination bashWriteReason() reads from the arguments (its per-command rules).
// Mentioning a run path as a SOURCE or other argument of one of these is not a write; only the destination counts.
const TARGET_AWARE = new Set([...DESTRUCTIVE, ...WRITERS, ...COPIERS, ...SHELLS, ...EDITORS, ...COMPRESSORS,
  'git', 'find', 'xargs', 'robocopy', 'dd', 'sort', 'diff', 'uniq', 'xxd']);
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

// ---------- target-aware certainty ----------
//
// The target-aware path may ALLOW a segment that mentions a protected path together with a write verb only when it is
// positively established that (1) every write destination is fully resolved, literal and outside the protected tree and
// (2) the protected path appears only as a plain literal read source of a modelled command. Anything else falls back to
// the coarse rule (mention + write verb = deny).

/** Glob, brace, expansion and odd-spelling markers in ONE raw word (quotes still present). */
function oddWord(raw) {
  const r = String(raw);
  const bare = r.replace(/^['"]+|['"]+$/g, '');
  const e = expandVars(bare);
  if (/[*?{()]|\[[^\]]*\]/.test(e)) return true;
  if (/\$\(|`|\$['"]|::|~\d|[<>]\(/.test(r) || /\$\{/.test(e)) return true;
  if (UNRESOLVED_VAR.test(e)) return true;
  if (/^[\\/]{2}/.test(bare)) return true;
  if (/^~[^\\/]/.test(bare)) return true;
  if (/\\/.test(bare) && /\//.test(bare)) return true;
  const body = e.replace(/^-+[A-Za-z][\w-]*[:=]/, '').replace(/^[A-Za-z]:(?=[\\/]|$)/, '');
  if (/:/.test(body)) return true;
  return body.split(/[\\/]/).some((c) => c && c !== '.' && c !== '..' && /[. ]$/.test(c));
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

const SH_KEYWORDS = new Set(['for', 'while', 'until', 'select', 'foreach', 'foreach-object', '%', 'read', 'xargs', 'mapfile', 'readarray']);
const REMOVER_LIKE = new Set([...DESTRUCTIVE, ...WRITERS, ...EDITORS]);
const POSIX_COPIERS = new Set(['cp', 'install', 'rsync', 'scp']);
const WIN_COPIERS = new Set(['copy', 'xcopy', 'robocopy', 'copy-item', 'cpi']);
const CP_SHORT = 'adfinpruvxPLHTbRZ';
const RSYNC_SHORT = 'avrzhucnPiltpogDxqWdLkKSyHAXm';
const SCP_SHORT = 'rpqCv';
const CP_LONG = new Set(['recursive', 'force', 'verbose', 'archive', 'no-clobber', 'update', 'interactive', 'dereference', 'no-dereference',
  'remove-destination', 'one-file-system', 'parents', 'no-target-directory', 'strip-trailing-slashes', 'attributes-only', 'debug',
  'copy-contents', 'backup', 'preserve', 'no-preserve', 'reflink', 'sparse', 'context', 'target-directory', 'link', 'symbolic-link',
  'directory', 'compare', 'mode', 'owner', 'group', 'strip', 'preserve-timestamps', 'suffix']);
const RSYNC_LONG = new Set(['archive', 'verbose', 'recursive', 'compress', 'dry-run', 'progress', 'human-readable', 'update', 'checksum',
  'times', 'perms', 'links', 'delete', 'delete-before', 'delete-after', 'delete-during', 'delete-excluded', 'remove-source-files',
  'link-dest', 'copy-dest', 'exclude', 'include', 'partial', 'inplace', 'stats', 'itemize-changes', 'size-only', 'ignore-existing']);
const VALUE_LONG = new Set(['mode', 'owner', 'group', 'suffix', 'exclude', 'include', 'compare', 'strip', 'link-dest', 'copy-dest']);

function windowsSwitch(w) { return /^\/[A-Za-z?-]+(?::.*)?$/.test(w); }

/**
 * For a plain copier: `{ src, writeAny }` where `src` holds the indexes (into args) that are plain literal read SOURCES and
 * `writeAny` says the command can alias or remove its sources (links, moves); null when the command or one of its flags
 * cannot be classified.
 */
function copierRoles(cmd, args) {
  const pos = [];
  let explicitDest = false;
  let destAll = false;
  let writeAny = false;
  if (POSIX_COPIERS.has(cmd)) {
    const shorts = cmd === 'cp' || cmd === 'install' ? CP_SHORT : cmd === 'rsync' ? RSYNC_SHORT : SCP_SHORT;
    const longs = cmd === 'rsync' ? RSYNC_LONG : cmd === 'scp' ? new Set() : CP_LONG;
    for (let i = 0; i < args.length; i += 1) {
      const a = args[i];
      if (a === '--') { for (let j = i + 1; j < args.length; j += 1) pos.push(j); break; }
      if (/^--./.test(a)) {
        const name = a.slice(2).split('=')[0].toLowerCase();
        if (!longs.has(name)) return null;
        const attached = a.includes('=');
        if (name === 'target-directory') { explicitDest = true; if (!attached) i += 1; }
        else if (name === 'link' || name === 'symbolic-link' || name === 'link-dest' || name === 'copy-dest' || name === 'remove-source-files') {
          writeAny = true;
          if (!attached && VALUE_LONG.has(name)) i += 1;
        } else if (name === 'directory') destAll = true;
        else if (!attached && VALUE_LONG.has(name)) i += 1;
        continue;
      }
      if (/^-./.test(a)) {
        for (let k = 1; k < a.length; k += 1) {
          const ch = a[k];
          if (ch === 't' && (cmd === 'cp' || cmd === 'install')) {
            explicitDest = true;
            if (k === a.length - 1) i += 1;
            break;
          }
          if ((ch === 'l' || ch === 's') && cmd === 'cp') { writeAny = true; continue; }
          if (ch === 'd' && cmd === 'install') { destAll = true; continue; }
          if (!shorts.includes(ch)) return null;
        }
        continue;
      }
      pos.push(i);
    }
    const src = new Set();
    if (explicitDest) pos.forEach((i) => src.add(i));
    else if (!destAll) {
      if (pos.length < 2) return null;
      pos.slice(0, -1).forEach((i) => src.add(i));
    }
    return { src, writeAny };
  }
  // copy, xcopy, robocopy, Copy-Item: cmd switches and PowerShell flags
  const ps = cmd === 'copy-item' || cmd === 'cpi';
  const src = new Set();
  let pathFlag = false;
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    if (windowsSwitch(a)) {
      if (/^\/mov(?:e)?$/i.test(a)) writeAny = true;
      continue;
    }
    if (/^-[A-Za-z]/.test(a)) {
      const colon = a.indexOf(':');
      const name = (colon > 0 ? a.slice(1, colon) : a.slice(1)).toLowerCase();
      if (colon > 0) { if (/^d/.test(name)) explicitDest = true; continue; }
      if (/^(?:p|pa|pat|path|l|li|lit|lite|liter|litera|literal|literalp|literalpa|literalpat|literalpath)$/.test(name)) {
        pathFlag = true;
        src.add(i + 1);
        i += 1;
        continue;
      }
      if (/^d/.test(name)) { explicitDest = true; i += 1; continue; }
      if (/^(?:recurse|force|container|passthru|whatif|confirm|verbose|debug|usetransaction|r|f|v)$/.test(name)) continue;
      return null;
    }
    pos.push(i);
  }
  if (cmd === 'robocopy') {
    if (pos.length < 2) return null;
    src.add(pos[0]);
  } else if (ps) {
    if (!pathFlag && pos.length) src.add(pos[0]);
  } else if (explicitDest) pos.forEach((i) => src.add(i));
  else {
    if (pos.length < 2) return null;
    pos.slice(0, -1).forEach((i) => src.add(i));
  }
  return { src, writeAny };
}

const EDITOR_CMD_FLAGS = /^(?:-c|-s|-S|--cmd|-e|\+.*)$/;

/**
 * True only when the segment is a modelled command whose every write destination is certain to lie outside the
 * protected tree and which names protected paths only as plain read sources. False means: use the coarse rule.
 */
function certainSegment(seg, ctx) {
  if (seg.raws.some(oddWord) || seg.redirs.some(oddWord)) return false;
  const w = stripPrefix(seg.words);
  if (!w.length) return true;
  const cmd = baseName(w[0]);
  const args = w.slice(1);
  if (SHELLS.has(cmd)) return true; // the body is analysed as a command of its own
  if (args.some((a) => unparsedMention(a, ctx))) return false;
  const touches = args.some((a) => candProtected(a, ctx));
  if (POSIX_COPIERS.has(cmd) || WIN_COPIERS.has(cmd)) {
    if (!touches) return true;
    const roles = copierRoles(cmd, args);
    if (!roles || roles.writeAny) return false;
    return args.every((a, i) => !candProtected(a, ctx) || (roles.src.has(i) && !/^-/.test(a)));
  }
  if (COMPRESSORS.has(cmd) && args.some((a) => /^(?:-[A-Za-z]*c[A-Za-z]*|--stdout|--to-stdout)$/.test(a))) return true;
  if (EDITORS.has(cmd) && (cmd === 'ex' || cmd === 'ed' || args.some((a) => EDITOR_CMD_FLAGS.test(a)))) return false;
  return !touches;
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

/**
 * Coarse net: a segment that mentions a run path AND carries a write-capable verb is denied. Skipped: factory CLI
 * segments, read-only commands, inline interpreter code (judged by scriptWritesRun) and modelled commands that
 * certainSegment() proves harmless (their destination is judged in bashWriteReason). A mention that is NOT proven
 * harmless (glob, expansion, odd spelling, loop) also makes a write verb in ANY other segment a deny, as before the
 * per-segment rule: `for f in R/*; do rm $f; done`.
 */
function verbMentionReason(command, ctx) {
  const tainted = taintedByAssignment(command, ctx);
  const COARSE = 'command mentions run files together with a write-capable verb';
  let uncertain = false;
  let verbElsewhere = false;
  for (const seg of segments(tokenize(maskSubstitutions(command)))) {
    const w = stripPrefix(seg.words);
    if (!w.length || skippableFactory(seg) || readOnlySegment(seg.words) || inlineInterpreter(seg.words)) continue;
    const cmd = baseName(w[0]);
    const text = seg.words.join(' ');
    if (!(runsMention(text, ctx.home) || (tainted && /[$%]/.test(text)))) {
      if (WRITE_VERBS.test(text)) verbElsewhere = true;
      continue;
    }
    if (EVAL_CMDS.has(cmd)) return 'eval of command text mentioning run files';
    const modelled = TARGET_AWARE.has(cmd) || inPlaceEditor(cmd, w.slice(1));
    if (modelled && certainSegment(seg, ctx)) continue;
    if (WRITE_VERBS.test(text)) return COARSE;
    if (seg.raws.some(oddWord) || SH_KEYWORDS.has(cmd)) uncertain = true;
  }
  return uncertain && verbElsewhere ? COARSE : '';
}

/**
 * Coarse checks over the whole command text, skipping `factory ...` segments and read-only commands:
 *  1. a segment mentions a run path together with a write-capable verb, unless its command is one whose
 *     destination is analysed precisely (verbMentionReason);
 *  2. runs a script interpreter on stdin / -c / -e / a script file while the command references
 *     FACTORY_HOME or .factory, and the code writes a run file (a line with a write call that names
 *     run.json/evidence/...). Scripts that only read are allowed. A script file is inspected only inside the
 *     temp dir (the session scratchpad is inside it); elsewhere it stays denied. The factory CLI is exempt.
 */
function broadReason(command, ctx) {
  const segs = segments(tokenize(command)).filter((seg) => !skippableFactory(seg));
  const verb = verbMentionReason(command, ctx);
  if (verb) return verb;
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

// ---------- heredoc bodies ----------

// Commands that only consume a heredoc as text (message, notes). A heredoc fed to a shell, an interpreter, eval, xargs
// or any other command keeps being analysed as commands: its body may be executed.
const HEREDOC_READERS = new Set(['cat', 'tee', 'wc', 'grep', 'head', 'tail', 'sort', 'uniq', 'jq', 'gh', 'git', 'factory', 'less', 'more', 'base64']);
const HEREDOC_EXEC = /\b(?:bash|sh|zsh|dash|ksh|cmd|powershell|pwsh|python3?|py|node|deno|bun|ruby|perl|php|eval|source|xargs|iex|invoke-expression|ssh|env|sudo)\b/i;
const DATA_FILE = /\.(?:md|txt|json|ya?ml|log|csv|html?|xml|diff|patch)$/i;

function heredocIsData(line, command, ctx) {
  const at = line.indexOf('<<');
  const before = line.slice(0, at);
  if (/\$\(|`|[<>]\(/.test(before)) return false; // the text flows into another command's arguments
  if (HEREDOC_EXEC.test(line)) return false;
  const seg = segments(tokenize(line)).find((sg) => sg.words.length && stripPrefix(sg.words).length);
  const segs = segments(tokenize(line));
  if (!seg) return false;
  for (const sg of segs) {
    const w = stripPrefix(sg.words);
    if (!w.length) continue;
    const cmd = baseName(w[0]);
    if (!(HEREDOC_READERS.has(cmd) || isFactory(w))) return false;
    if (cmd === 'git' && !w.some((a) => a === 'commit' || a === 'tag')) return false;
    const files = [...sg.redirs, ...(cmd === 'tee' ? w.slice(1).filter((a) => !a.startsWith('-')) : [])];
    for (const f of files) {
      if (f === '/dev/null' || f === 'NUL') continue;
      if (!DATA_FILE.test(f) || oddWord(f) || candProtected(f, ctx) || command.split(f).length !== 2) return false;
    }
  }
  return true;
}

/** Drops the body of heredocs whose reader only treats it as data, so prose in a message is not parsed as commands. */
function stripHeredocs(command, ctx) {
  if (!command.includes('<<')) return command;
  const lines = command.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    out.push(line);
    const m = !/<<</.test(line) && line.match(/<<(-?)\s*(['"]?)([A-Za-z_][\w-]*)\2/);
    if (!m) continue;
    let end = -1;
    for (let j = i + 1; j < lines.length; j += 1) {
      const t = m[1] ? lines[j].replace(/^\t+/, '') : lines[j];
      if (t.trimEnd() === m[3]) { end = j; break; }
    }
    if (end > i && heredocIsData(line, command, ctx)) i = end;
  }
  return out.join('\n');
}

const WRITING_MODULES = /^(?:zipfile|tarfile|shutil|py_compile|compileall|venv|ensurepip|pip|fileinput|gzip|bz2|lzma|json\.tool|cProfile|profile|trace)$/;

/** A shell/interpreter that reads its script from stdin (no -c, no script file): the text piped or heredoc'd into it runs. */
function readsStdinScript(cmd, args) {
  if (EVAL_CMDS.has(cmd)) return true;
  if (args.some((a) => /^(?:-[a-z]*c|\/c|\/k|-command|-c|-e|--eval|-p|-pe|--print|-m)$/i.test(a))) return false;
  return !args.some((a) => !a.startsWith('-') || a === '-') || args.includes('-') || args.includes('-s');
}

/** Returns a reason string when the shell command writes a protected path, else ''. */
function bashWriteReason(command, ctxIn, depth = 0) {
  let ctx = ctxIn;
  if (depth === 0) command = stripHeredocs(command, ctx);
  if (depth > 3) return textMentionsProtected(command, ctx) ? 'nested command touching run files' : '';
  for (const body of waitCmdBodies(command)) {
    const r = bashWriteReason(body, ctx, depth + 1);
    if (r) return `factory wait --cmd: ${r}`;
  }
  // Command substitutions are analysed as commands of their own.
  const subs = [];
  const flat = command.replace(/[$<>]\(([^()]*)\)|`([^`]*)`/g, (_, a, b) => { subs.push(a ?? b); return ' SUBST '; });
  for (const sub of subs) {
    const r = bashWriteReason(sub, ctx, depth + 1);
    if (r) return r;
  }
  if (depth === 0) {
    const broad = broadReason(command, ctx);
    if (broad) return broad;
  } else {
    const verb = verbMentionReason(command, ctx);
    if (verb) return verb;
  }
  const segs = segments(tokenize(flat));
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
        const r = bashWriteReason(args.slice(i + 1).join(' '), ctx, depth + 1);
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

module.exports = {
  inInbox, scriptWritesRun, readOnlySegment, factoryHome, normPath, wordProtected, isProtected, isCliOwned, isAuthorOwned, setsFactoryRole, broadReason, isProtectedOrAncestor, inRunSub, inRunDir, inTemp, under,
  tokenize, segments, waitCmdBodies, bashWriteReason, isFactory, stripPrefix, baseName,
};
