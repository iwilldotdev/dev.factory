#!/usr/bin/env node
// PreToolUse hook for ALL sessions (main session and workers).
// Denies hand-written changes to CLI-owned run files under FACTORY_HOME/runs/<id>/:
// run.json, metrics.json, ship-plan.json, review-report.json|md, evidence/**, reviews/**, baseline/**.
// MCP tools (mcp__*) whose input has `save_to` are checked the same way.
// Also author-owned (intake/plan): acs.json, diagnosis.md, checks/**, figma/** - denied to shell writes and
// MCP save_to (except into figma/); file tools may write them here, worker-guard restricts the role.
// Defence in depth only: the factory CLI's evidence digests / plan-lock hashes are authoritative.
// `factory` / `python -m factory` commands are the legitimate writers and pass.
// Usage: protect.js [--factory-home <dir>]   (env FACTORY_HOME wins when set)
// Fails closed on unreadable input.
'use strict';
const lib = require('./protect-lib');

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `dev.factory protect: ${reason}. Run files are written only by the factory CLI (collectors, gates, review record).`,
    },
  }));
  process.exit(0);
}

const WRITE_TOOLS = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit'];
const SHELL_TOOLS = ['Bash', 'PowerShell'];

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
  const home = lib.factoryHome();
  const { tool_name: tool, tool_input: args = {}, cwd } = input;
  const ctx = { home, cwd: cwd || process.cwd() };
  if (typeof tool === 'string' && tool.startsWith('mcp__')) {
    const dest = args.save_to;
    if (typeof dest === 'string' && dest.trim()) {
      const p = lib.normPath(dest, ctx.cwd);
      // The Figma pack (runs/<id>/figma/**) is the one author-owned area MCP may save into.
      const figma = lib.inRunSub(p, home, 'figma') && !p.includes('/../');
      if (!figma && (lib.isProtected(p, home) || lib.isProtectedOrAncestor(p, home) || lib.wordProtected(dest, ctx, true))) {
        deny(`${tool} save_to ${dest}`);
      }
    }
  } else if (WRITE_TOOLS.includes(tool)) {
    const target = String(args.file_path || args.notebook_path || '');
    if (!target) deny('write without a target path');
    // File tools: only CLI-owned files are denied here; acs.json, checks/, figma/, diagnosis.md are
    // author-written during intake/plan (worker-guard denies them for builder/auditor/scout).
    if (lib.isCliOwned(lib.normPath(target, ctx.cwd), home)) deny(`write to ${target}`);
  } else if (SHELL_TOOLS.includes(tool)) {
    const command = String(args.command || '');
    if (lib.setsFactoryRole(command)) deny('FACTORY_ROLE must not be set from a command');
    const reason = lib.bashWriteReason(command, ctx);
    if (reason) deny(reason);
  }
  process.exit(0);
});
