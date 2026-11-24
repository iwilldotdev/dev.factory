#!/usr/bin/env node
// PreToolUse hook for ALL sessions (main session and workers).
// Denies hand-written changes to CLI-owned run files under FACTORY_HOME/runs/<id>/:
// run.json, metrics.json, ship-plan.json, review-report.json|md, evidence/**, reviews/**, baseline/**, contracts/**.
// MCP tools (mcp__*) whose input names a destination (save_to, output_path, output_file, out, dest, plus the
// active profiles' save_args) are checked the same way.
// Also author-owned (intake/plan): acs.json, diagnosis.md, checks/**, design/** - denied to shell writes and
// MCP saves; file tools may write them here, worker-guard restricts the role.
// runs/<id>/raw/** (untouched provider payloads) is free: MCP saves, shell redirects and file tools may write it.
// FACTORY_ROLE is NOT checked here: settings hooks also run inside subagents, so a block here would stop the
// auditor's documented `FACTORY_ROLE=auditor factory review record`. worker-guard limits it to the auditor;
// the main session is the trusted orchestrator (DESIGN "Threat model") and never records reviews.
// Defence in depth only: the factory CLI's evidence digests / plan-lock hashes are authoritative.
// `factory` / `python -m factory` commands are the legitimate writers and pass.
// Usage: protect.js [--factory-home <dir>]   (env FACTORY_HOME wins when set)
// Fails closed on unreadable input.
'use strict';
const lib = require('./protect-lib');
const guards = require('./guard-lib');

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
    // Destination args (save_to, output_path, output_file, out, dest + profile save_args): runs/<id>/raw/** is the
    // one run area an MCP save may target; contracts/ is CLI-owned and design/ author-owned, so both are denied.
    const profileGuards = guards.loadProfileGuards(input);
    for (const dest of guards.saveDestinations(args, profileGuards.saveArgs)) {
      const p = lib.normPath(dest, ctx.cwd);
      if (lib.isProtected(p, home) || lib.isProtectedOrAncestor(p, home) || lib.wordProtected(dest, ctx, true)) {
        deny(`${tool} save to ${dest}`);
      }
    }
  } else if (WRITE_TOOLS.includes(tool)) {
    const target = String(args.file_path || args.notebook_path || '');
    if (!target) deny('write without a target path');
    // File tools: only CLI-owned files are denied here; acs.json, checks/, design/, diagnosis.md are
    // author-written during intake/plan (worker-guard denies them for builder/auditor/scout).
    if (lib.isCliOwned(lib.normPath(target, ctx.cwd), home)) deny(`write to ${target}`);
  } else if (SHELL_TOOLS.includes(tool)) {
    const command = String(args.command || '');
    const reason = lib.bashWriteReason(command, ctx);
    if (reason) deny(reason);
  }
  process.exit(0);
});
