#!/usr/bin/env node
// PreToolUse guard for dev.factory workers.
// Usage: worker-guard.js <scout|author|builder|auditor> [--factory-home <dir>]
// A guardrail against mistakes, not a sandbox. Fails closed on bad input.
'use strict';
const lib = require('./protect-lib');

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
  [/(?:^|[;&|]\s*)(?:glab|gh)\s/, 'remote CLIs are forbidden; use dev.mcp read tools'],
];

// Git verbs that change the worktree or history; auditors and authors never run them.
const GIT_MUTATE = git(String.raw`(?:add|commit|checkout|switch|reset|merge|rebase|cherry-pick|revert|am|apply)\b`);

// factory subcommands reserved for the main session.
const FACTORY = String.raw`(?:^|[\s;&|(])(?:[\w.:~/\\-]*[\\/])?(?:factory(?:\.cmd|\.exe)?|python3?(?:\.exe)?\s+-m\s+factory|py\s+-m\s+factory)\s+(?:--json\s+)?`;
const factory = (rest) => new RegExp(FACTORY + rest, 'i');
const ORCHESTRATOR_ONLY = [
  [factory(String.raw`gate\b`), 'gates are run by the main session'],
  [factory(String.raw`ship\b`), 'ship belongs to the main session'],
  [factory(String.raw`feedback\b`), 'feedback ingest belongs to the main session'],
  [factory(String.raw`run\s+(?:start|branch|decide|block|unblock|human|replan|rewind)\b`), 'run state changes (incl. replan/rewind/decide/unblock) belong to the main session'],
  [factory(String.raw`workspace\s+(?:init|import)\b`), 'workspace changes belong to the main session'],
];
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

const DOC_FILE = /\.(?:md|mdx|markdown|txt|rst|adoc)$/i;

function checkBash(command, ctx) {
  if (role === 'scout') {
    if (!scoutBashAllowed(command.trim())) {
      deny('scout may run only read-only git commands (optionally `cd <path> &&`, `git -C <path>`, `&&` chains, and pipes into head/tail/wc/sort/uniq/grep/cut)');
    }
    return;
  }
  for (const [pattern, why] of FORBIDDEN) {
    if (pattern.test(command)) deny(why);
  }
  if ((role === 'auditor' || role === 'author') && GIT_MUTATE.test(command)) {
    deny(`${role}s do not change the worktree or commit`);
  }
  for (const [pattern, why] of ORCHESTRATOR_ONLY) {
    if (pattern.test(command)) deny(why);
  }
  if (role !== 'auditor' && REVIEW_RECORD.test(command)) deny('only the auditor records reviews');
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
  if (role !== 'author' && lib.isAuthorOwned(p, home)) {
    deny(`acs.json, diagnosis.md, checks/ and figma/ are written by the author during intake/plan only (${filePath})`);
  }
  const notes = lib.inRunSub(p, home, 'notes');
  if (role === 'scout') {
    if (!notes) deny('scout may write only its report under FACTORY_HOME/runs/<id>/notes/');
    return;
  }
  if (lib.under(p, `${home}/secrets`)) deny('secrets are provided by the user, never written by workers');
  if (role === 'builder') {
    if (lib.under(p, home) && !notes) {
      deny('builder may write in FACTORY_HOME only under runs/<id>/notes/');
    }
    return;
  }
  if (role === 'auditor') {
    if (notes || lib.inTemp(p)) return;
    deny(`auditor may write only scratch files (temp dir or runs/<id>/notes/), not ${filePath}`);
  }
  // author
  if (lib.inRunDir(p, home) || lib.inTemp(p) || DOC_FILE.test(p)) return;
  deny(`author may write run files (acs.json, checks/, figma/, remote/, notes/), docs, or temp scratch, not ${filePath}`);
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
  const { tool_name: tool, tool_input: args = {}, cwd } = input;
  const ctx = { home: lib.factoryHome(), cwd: cwd || process.cwd() };
  if (typeof tool === 'string' && tool.startsWith('mcp__') && role !== 'author') {
    const dest = args && typeof args.save_to === 'string' ? args.save_to : '';
    if (dest.trim() && lib.isAuthorOwned(lib.normPath(dest, ctx.cwd), ctx.home)) {
      deny(`${role} may not save into acs.json, diagnosis.md, checks/ or figma/ (${dest})`);
    }
  }
  if (tool === 'Bash' || tool === 'PowerShell') {
    checkBash(String(args.command || ''), ctx);
  } else if (['Write', 'Edit', 'MultiEdit', 'NotebookEdit'].includes(tool)) {
    checkWrite(String(args.file_path || args.notebook_path || ''), ctx);
  }
  process.exit(0);
});
