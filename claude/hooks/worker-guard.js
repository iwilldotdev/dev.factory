#!/usr/bin/env node
// PreToolUse guard for dev.factory workers.
// Usage: worker-guard.js <scout|author|builder|auditor> [--factory-home <dir>]
// A guardrail against mistakes, not a sandbox. Fails closed on bad input.
'use strict';
const lib = require('./protect-lib');
const guards = require('./guard-lib');

const role = process.argv[2];
const ROLES = ['scout', 'author', 'auditor', 'builder'];

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `dev.factory guard (${role}): ${reason}`,
    },
  }));
  process.exit(0);
}

const GIT = String.raw`\bgit(?:\s+-[cC]\s+\S+|\s+--\S+)*\s+`;
const git = (rest) => new RegExp(GIT + rest);

const FORBIDDEN = [
  [git(String.raw`push\b`), 'push belongs to the main session at ship'],
  [git(String.raw`reset\b[^;&|]*--hard`), 'hard reset is forbidden'],
  [git(String.raw`stash\b`), 'stash is forbidden; use a worktree'],
  [git(String.raw`checkout\b[^;&|]*(?:\s--(?:\s|$)|\s-f\b|\s--force\b)`), 'destructive checkout is forbidden'],
  [git(String.raw`restore\b`), 'destructive restore is forbidden'],
  [git(String.raw`clean\b[^;&|]*\s-\w*f`), 'git clean is forbidden'],
];

// Remote CLI writes: generic (curl/wget/http clients sending a write) plus every `cli_write_patterns` regex of the
// active profiles (guard-lib.loadProfileGuards). Reading with a provider CLI is allowed; redirect it under runs/<id>/raw/.
const REMOTE_WRITE = 'remote writes belong to the main session';

// Git verbs that change the worktree or history; auditors and authors never run them.
const GIT_MUTATE = git(String.raw`(?:add|commit|checkout|switch|reset|merge|rebase|cherry-pick|revert|am|apply)\b`);

// factory subcommands reserved for the main session.
const FACTORY = String.raw`(?:^|[\s;&|(])(?:[\w.:~/\\-]*[\\/])?(?:factory(?:\.cmd|\.exe)?|(?:python3?|py)(?:\.exe)?(?:\s+-[\w-]+)*\s+-m\s+factory(?:\.\w+)*)\s+(?:--json\s+)?`;
const factory = (rest) => new RegExp(FACTORY + rest, 'i');

const FACTORY_SUBMODULE = /(?:^|[\s;&|(])(?:python3?|py)(?:\.exe)?(?:\s+-[\w-]+)*\s+-m\s+factory\.(?!cli\b)\w/i;

// Inline interpreter code (python -c, python - <<, node -e, readable temp scripts) that reaches the factory package:
// `from factory import ...`, `import factory`, `factory.cli.main(...)`, require('.../factory/...'), spawning the CLI by
// name. Workers have no business calling the CLI's reserved commands (profile approve, migrate apply...) in-process.
const FACTORY_CODE = new RegExp([
  String.raw`\b(?:from|import)\s+factory\b`,
  String.raw`(?<![\w.\/\\-])factory\s*\.\s*(?:cli|core|profiles|commands|\w+)\b`,
  String.raw`(?:__import__|import_module|run_module|run_path|require|import)\s*\(\s*['"](?:[^'"]*[\/\\])?factory(?:[\/\\.][^'"]*)?['"]`,
  String.raw`['"](?:[^'"\s]*[\/\\])?factory(?:\.cmd|\.exe)?['"]`,
  String.raw`['"](?:[^'"\s]*[\/\\])?factory(?:\.cmd|\.exe)?\s+\S`,
  String.raw`-m['"]?,?\s*['"]?factory\b`,
].join('|'), 'i');
const ORCHESTRATOR_ONLY = [
  [factory(String.raw`gate\b`), 'gates are run by the main session'],
  [factory(String.raw`ship\b`), 'ship belongs to the main session'],
  [factory(String.raw`feedback\b`), 'feedback ingest belongs to the main session'],
  [factory(String.raw`run\s+(?:start|branch|decide|block|unblock|human|replan|rewind|delete|claim|session)\b`), 'run state changes (incl. replan/rewind/decide/unblock/delete/claim/session) belong to the main session'],
  [factory(String.raw`workspace\s+(?:init|import)\b`), 'workspace changes belong to the main session'],
  [factory(String.raw`run\s+(?:verify-human|retype|close)\b`), 'recording human checks, retyping and closing runs belong to the main session'],
  [factory(String.raw`run\s+(?:(?:target-ticket|env-only)\b|mirror(?![\w-]))`), 'run state changes (target-ticket, env-only, mirror) belong to the main session'],
  [factory(String.raw`env\s+lease\b[^;&|\n]*\b(?:release|acquire)\b`), 'env lease changes belong to the main session'],
  [factory(String.raw`env\s+down\b`), 'env lifecycle (down) belongs to the main session'],
  [factory(String.raw`env\s+up\b`), 'env lifecycle (up) belongs to the main session; run `factory env check` (no --auto-restart) and hand back blocked if the env is down'],
  [factory(String.raw`env\s+check\b[^;&|\n]*--auto-restart`), 'env check --auto-restart restarts an env: main session only'],
  [factory(String.raw`migrate\b`), '`factory migrate` rewrites FACTORY_HOME state: main session only'],
  [factory(String.raw`profile\s+approve\b`), 'approving a profile belongs to the main session (workers write drafts under profile-drafts/)'],
  [factory(String.raw`profile\s+new\b(?![^;&|\n]*--draft\b)`), '`factory profile new` without --draft writes an approved profile: main session only'],
];
// `factory wait --cmd` runs an arbitrary shell command: builders only (its body is also judged like `bash -c`).
const WAIT_CMD = factory(String.raw`wait\b[^\n]*--cmd\b`);
const REVIEW_RECORD = factory(String.raw`review\s+record\b`);

// Scout: read-only git, optionally `git -C <path>`, chained with `&&`, piped into
// text filters, and an optional leading `cd <path> &&`. No redirects or substitution.
const PATH_ARG = String.raw`(?:"[^"]+"|'[^']+'|\S+)`;
const SCOUT_GIT = new RegExp(
  String.raw`^git(?:\s+-C\s+${PATH_ARG})?\s+(?:` +
  String.raw`(?:status|log|show|diff|merge-base|rev-parse|blame|ls-files|ls-tree|shortlog|describe|name-rev|for-each-ref|cat-file|grep|rev-list|count-objects)\b` +
  String.raw`|branch(?:\s+(?:--show-current|-a|-r|-vv?|--list|--contains\s+\S+|--merged(?:\s+\S+)?|--no-merged(?:\s+\S+)?))*\s*$` +
  String.raw`|tag(?:\s+(?:-l|--list|--contains\s+\S+|--points-at\s+\S+))*\s*$` +
  String.raw`|worktree\s+list(?:\s+--porcelain)?\s*$` +
  String.raw`|config\s+(?:--get|--get-all|--list|-l)\b` +
  String.raw`|remote(?:\s+-v|\s+get-url\s+\S+|\s+show\s+-n\s+\S+)?\s*$)`,
);
// Flags that make read-only git run external programs or write files.
const SCOUT_GIT_UNSAFE = /(?:^|\s)(?:-c\s|--output\b|--ext-diff\b|-O\S*|--open-files-in-pager\b|--exec\b)/;
const SCOUT_FILTER = /^(?:head|tail|wc|sort|uniq|grep|cut)(?:\s|$)/;
const SCOUT_FILTER_UNSAFE = /(?:^|\s)(?:-o\b|--output\b|-f\b|--follow\b)/;
const SCOUT_CD = new RegExp(String.raw`^cd\s+${PATH_ARG}$`);

function scoutBashAllowed(command) {
  if (/[;`<>\n\r]|\$\(|\|\||&\s*$|(?:^|[^&])&(?!&)/.test(command)) return false;
  const segments = command.split('&&').map((s) => s.trim());
  if (segments.some((s) => !s)) return false;
  return segments.every((segment, i) => {
    if (i === 0 && segments.length > 1 && SCOUT_CD.test(segment)) return true;
    const [head, ...filters] = segment.split('|').map((s) => s.trim());
    if (!SCOUT_GIT.test(head) || SCOUT_GIT_UNSAFE.test(head)) return false;
    return filters.every((f) => SCOUT_FILTER.test(f) && !SCOUT_FILTER_UNSAFE.test(f));
  });
}

// `sleep` loops hide hung processes for minutes; `factory wait` is bounded and prints one line.
const SLEEP = /(?:^|[;&|(\n]\s*|\bdo\s+|\bthen\s+)(?:sleep|start-sleep)\b|\btimeout(?:\.exe)?\s+\/t\b/i;

function sleepsOutsideFactory(command) {
  if (lib.waitCmdBodies(command).some((b) => sleepsOutsideFactory(b))) return true;
  const text = lib.segments(lib.tokenize(command))
    .filter((s) => !lib.isFactory(lib.stripPrefix(s.words)))
    .map((s) => s.words.join(' '))
    .join('\n');
  return SLEEP.test(text);
}

const DOC_FILE = /\.(?:md|mdx|markdown|txt|rst|adoc)$/i;

function checkBash(command, ctx, profileGuards) {
  if (role === 'scout') {
    if (!scoutBashAllowed(command.trim())) {
      deny('scout may run only read-only git commands (optionally `cd <path> &&`, `git -C <path>`, `&&` chains, and pipes into head/tail/wc/sort/uniq/grep/cut)');
    }
    return;
  }
  // Each normalised command unit (prefixes stripped, bash -c bodies, subshells) is judged like the raw text.
  const texts = [command, ...guards.commandUnits(command)];
  for (const [pattern, why] of FORBIDDEN) {
    if (texts.some((t) => pattern.test(t))) deny(why);
  }
  const remote = guards.cliWriteReason(command, profileGuards);
  if (remote) deny(`${REMOTE_WRITE} (${remote})`);
  if (sleepsOutsideFactory(command)) {
    deny('sleep is blocked: use `factory wait --file <path> [--contains <text>] | --cmd "<cmd>" --until-exit 0 | --url <url> --status 2xx [--timeout S]` (bounded, one line), or hand back `status: blocked`');
  }
  if ((role === 'auditor' || role === 'author') && texts.some((t) => GIT_MUTATE.test(t))) {
    deny(`${role}s do not change the worktree or commit`);
  }
  for (const [pattern, why] of ORCHESTRATOR_ONLY) {
    if (texts.some((t) => pattern.test(t))) deny(why);
  }
  if (texts.some((t) => FACTORY_SUBMODULE.test(t))) {
    deny('`python -m factory.<module>` other than factory.cli reaches the factory package directly: use the `factory` CLI');
  }
  if ([command, ...guards.commandUnits(command)].some((t) => lib.interpreterCodeBlobs(t, ctx).some((b) => FACTORY_CODE.test(b)))) {
    deny('inline interpreter code (python -c, python - <<, node -e, a readable script) must not import or call the factory package: run the `factory` CLI, and leave reserved commands (profile approve, migrate apply, gate, ship...) to the main session');
  }
  if (role !== 'builder' && texts.some((t) => WAIT_CMD.test(t))) {
    deny('`factory wait --cmd` runs a shell command: only the builder may use it; use --file or --url');
  }
  if (role !== 'auditor' && texts.some((t) => REVIEW_RECORD.test(t))) deny('only the auditor records reviews');
  if (role !== 'auditor' && /FACTORY_ROLE/i.test(command)) {
    deny('only the auditor may mention FACTORY_ROLE (review record)');
  }
  if (/\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-e(?:c|nc\w*)?\b/i.test(command)) {
    deny('encoded PowerShell commands are forbidden for workers');
  }
  const reason = lib.bashWriteReason(command, ctx);
  if (reason) deny(`${reason}; run files are CLI-written only`);
}

function checkWrite(filePath, ctx) {
  if (!filePath) deny('write without a target path');
  const p = lib.normPath(filePath, ctx.cwd);
  const { home } = ctx;
  if (lib.isCliOwned(p, home)) deny(`run state, evidence, reviews and baseline are CLI-written only (${filePath})`);
  if (lib.isMainOwned(p, home)) deny(`profiles/, workspaces/ and migrations/ are written by the main session only; write a draft under profile-drafts/<name>/ or migration-drafts/ (${filePath})`);
  const draft = ['profile-drafts', 'migration-drafts'].some((d) => lib.under(p, `${home}/${d}`) && p !== `${home}/${d}`);
  if (role !== 'author' && lib.isAuthorOwned(p, home)) {
    deny(`acs.json, diagnosis.md, checks/ and design/ are written by the author during intake/plan only (${filePath})`);
  }
  const notes = lib.inRunSub(p, home, 'notes') || lib.inInbox(p, home);
  const rawDir = lib.inRunSub(p, home, 'raw'); // untouched provider payloads: free for every non-scout role
  if (role === 'scout') {
    if (!notes) deny('scout may write only its report under FACTORY_HOME/runs/<id>/notes/ (or runs/_inbox/<session>/ before a run exists)');
    return;
  }
  if (lib.under(p, `${home}/secrets`)) deny('secrets are provided by the user, never written by workers');
  if (role === 'builder') {
    if (lib.under(p, home) && !notes && !rawDir && !draft) {
      deny('builder may write in FACTORY_HOME only under runs/<id>/notes/, runs/<id>/raw/, profile-drafts/<name>/ or migration-drafts/');
    }
    return;
  }
  if (role === 'auditor') {
    if (notes || rawDir || lib.inTemp(p)) return;
    deny(`auditor may write only scratch files (temp dir, runs/<id>/notes/ or runs/<id>/raw/), not ${filePath}`);
  }
  // author
  if (lib.inRunDir(p, home) || lib.inTemp(p) || DOC_FILE.test(p) || draft) return;
  deny(`author may write run files (acs.json, checks/, design/, raw/, notes/), profile-drafts/<name>/, migration-drafts/, docs, or temp scratch, not ${filePath}`);
}

if (!ROLES.includes(role)) deny('unknown role');

let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  let input;
  try {
    input = JSON.parse(raw);
    if (!input || typeof input !== 'object') throw new Error('not an object');
  } catch {
    deny('unreadable hook input');
  }
  // A subagent that is not a factory worker (and the main session) is never judged by this guard.
  if (guards.notFactoryWorker(input)) process.exit(0);
  const { tool_name: tool, tool_input: args = {}, cwd } = input;
  const ctx = { home: lib.factoryHome(), cwd: cwd || process.cwd(), mainOwned: true };
  const profileGuards = guards.loadProfileGuards(input);
  if (typeof tool === 'string' && tool.startsWith('mcp__')) {
    if (guards.classifyMcpTool(tool, profileGuards.mcpReadAllow) !== 'read') {
      deny(`${REMOTE_WRITE} (${tool} is not a read tool; a profile can certify it with mcp_read_allow)`);
    }
    for (const dest of guards.saveDestinations(args, profileGuards.saveArgs)) {
      const p = lib.normPath(dest, ctx.cwd);
      if (lib.isProtected(p, ctx.home) || lib.isMainOwned(p, ctx.home) || lib.wordProtected(dest, ctx)) {
        deny(`${role} may save only into runs/<id>/raw/ (or notes/), not ${dest}: run state, contracts/, acs.json, diagnosis.md, checks/, design/, profiles/ and workspaces/ are protected`);
      }
    }
  }
  if (tool === 'Bash' || tool === 'PowerShell') {
    checkBash(String(args.command || ''), ctx, profileGuards);
  } else if (['Write', 'Edit', 'MultiEdit', 'NotebookEdit'].includes(tool)) {
    checkWrite(String(args.file_path || args.notebook_path || ''), ctx);
  }
  process.exit(0);
});
