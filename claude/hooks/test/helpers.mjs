import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import os from 'node:os';

const here = path.dirname(fileURLToPath(import.meta.url));
export const HOOKS = path.resolve(here, '..');

// A FACTORY_HOME outside the temp dir, so "temp scratch" rules do not mask run rules.
export const WIN = process.platform === 'win32';
export const FH = WIN ? 'C:\\fh-test\\.factory' : '/opt/fh-test/.factory';
export const FH_POSIX = FH.replace(/\\/g, '/');
export const RUN = `${FH_POSIX}/runs/acme-dev-1`;
export const TMP = os.tmpdir();

export function runHook(script, args, input, env = {}) {
  const raw = typeof input === 'string' ? input : JSON.stringify(input);
  const r = spawnSync(process.execPath, [path.join(HOOKS, script), ...args], {
    input: raw,
    env: { ...process.env, FACTORY_HOME: FH, ...env },
    encoding: 'utf8',
  });
  if (r.status !== 0) throw new Error(`hook exited ${r.status}: ${r.stderr}`);
  const out = r.stdout.trim();
  return out ? JSON.parse(out).hookSpecificOutput : null;
}

export const denied = (res) => Boolean(res && res.permissionDecision === 'deny');
export const bash = (command, cwd) => ({ tool_name: 'Bash', tool_input: { command }, cwd });
export const write = (file_path, cwd, tool = 'Write') => ({ tool_name: tool, tool_input: { file_path }, cwd });
