import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { runHook, denied, HOOKS } from './helpers.mjs';

let n = 0;
const agent = () => `ff-test-${process.pid}-${Date.now()}-${n += 1}`;
const ff = (input) => runHook('failfast.js', [], input);
const pre = (id, command, tool = 'Bash') => ff({ hook_event_name: 'PreToolUse', agent_id: id, tool_name: tool, tool_input: { command } });
const post = (id, command, stdout, extra = {}, event = 'PostToolUse') => ff({
  hook_event_name: event, agent_id: id, tool_name: 'Bash', tool_input: { command },
  // PostToolUseFailure carries no tool_response (docs: tool_name, tool_input, error, is_interrupt, duration_ms).
  ...(event === 'PostToolUseFailure'
    ? { error: `Exit code ${extra.exit_code ?? 1}\n${stdout}` }
    : { tool_response: { stdout, stderr: '', ...extra } }),
});

const UNHEALTHY = 'ENV-FAIL repo=api kind=unhealthy detail=GET /health 503';
const PORT = 'ENV-FAIL repo=api kind=port detail=3000 held by pid 4';

test('first infra failure is silent, second injects the hand-back message, third attempt is denied', () => {
  const id = agent();
  assert.equal(post(id, 'factory env check acme api --run r', UNHEALTHY, { exit_code: 1 }), null);
  const second = post(id, 'factory env check acme api --run r', UNHEALTHY, { exit_code: 1 });
  assert.match(second.additionalContext, /status: blocked/);
  assert.match(second.additionalContext, /infra_signal: ENV-FAIL repo=api kind=unhealthy/);
  assert.match(second.additionalContext, /first_seen_at: \d{4}-/);
  const third = pre(id, 'factory env up acme api --run r');
  assert.ok(denied(third));
  assert.match(third.permissionDecisionReason, /status: blocked/);
  assert.ok(denied(pre(id, 'factory prove r --check CHK-1')));
  assert.ok(!denied(pre(id, 'python -m factory env check acme api')), 'the deny expires after 2 denies');
});

test('other commands stay allowed after the hand-back point (logs, down, git, reads)', () => {
  const id = agent();
  post(id, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  post(id, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  for (const cmd of ['factory env logs acme api', 'factory env down acme api', 'git status', 'factory run status r', 'cat notes.md']) {
    assert.ok(!denied(pre(id, cmd)), cmd);
  }
});

test('different kinds are counted separately', () => {
  const id = agent();
  post(id, 'factory env check acme api', UNHEALTHY, { exit_code: 1 });
  assert.equal(post(id, 'factory env up acme api', PORT, { exit_code: 4 }), null);
  assert.ok(!denied(pre(id, 'factory env check acme api')));
});

test('PROVE-FAIL kind=env|timeout counts, assert|setup does not', () => {
  const id = agent();
  for (let i = 0; i < 3; i += 1) {
    assert.equal(post(id, 'factory prove r --check CHK-1', 'PROVE-FAIL check=CHK-1 kind=assert detail=expected 1 got 2', { exit_code: 1 }), null);
    assert.equal(post(id, 'factory prove r --check CHK-1', 'PROVE-FAIL check=CHK-1 kind=setup detail=missing spec', { exit_code: 1 }), null);
  }
  assert.ok(!denied(pre(id, 'factory prove r --check CHK-1')));
  const t1 = post(id, 'factory prove r --check CHK-2', 'PROVE-FAIL check=CHK-2 kind=timeout detail=90s', { exit_code: 1 });
  assert.equal(t1, null);
  const t2 = post(id, 'factory prove r --check CHK-2', 'PROVE-FAIL check=CHK-2 kind=timeout detail=90s', { exit_code: 1 });
  assert.match(t2.additionalContext, /kind=timeout/);
  assert.ok(denied(pre(id, 'factory prove r --check CHK-2')));
});

test('prove exit 3 and env check non-zero exit count even without a signal line', () => {
  const a = agent();
  post(a, 'factory prove r', 'something broke', { exit_code: 3 });
  assert.ok(post(a, 'factory prove r', 'something broke', { exit_code: 3 }).additionalContext);
  const b = agent();
  post(b, 'factory env check acme api', 'NOT healthy', { exit_code: 1 });
  assert.ok(post(b, 'factory env check acme api', 'NOT healthy', { exit_code: 1 }).additionalContext);
  // prove exit 1 (checks failed) and env usage exit 2 are not infra failures
  const c = agent();
  for (let i = 0; i < 3; i += 1) {
    assert.equal(post(c, 'factory prove r', 'FAIL', { exit_code: 1 }), null);
    assert.equal(post(c, 'factory env check', 'usage: ...', { exit_code: 2 }), null);
  }
  assert.ok(!denied(pre(c, 'factory env check acme api')));
});

test('PostToolUseFailure events (non-zero exit reported by the harness) are counted', () => {
  const id = agent();
  post(id, 'factory env check acme api', UNHEALTHY, { exit_code: 1 }, 'PostToolUseFailure');
  const second = post(id, 'factory env check acme api', UNHEALTHY, { exit_code: 1 }, 'PostToolUseFailure');
  assert.match(second.additionalContext, /status: blocked/);
  assert.ok(denied(pre(id, 'factory env up acme api')));
});

test('a healthy env check or a passing prove resets the counters', () => {
  const id = agent();
  post(id, 'factory env check acme api', UNHEALTHY, { exit_code: 1 });
  assert.equal(post(id, 'factory env check acme api', 'api healthy', { exit_code: 0 }), null);
  assert.equal(post(id, 'factory env check acme api', UNHEALTHY, { exit_code: 1 }), null);
  const id2 = agent();
  post(id2, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  post(id2, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  assert.ok(denied(pre(id2, 'factory env check acme api')));
  post(id2, 'factory prove r', 'PASS CHK-1', { exit_code: 0 });
  assert.ok(!denied(pre(id2, 'factory env check acme api')));
});

test('state is per agent', () => {
  const a = agent();
  const b = agent();
  post(a, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  post(a, 'factory env up acme api', UNHEALTHY, { exit_code: 1 });
  assert.ok(denied(pre(a, 'factory env check acme api')));
  assert.ok(!denied(pre(b, 'factory env check acme api')));
  // no agent_id (main session): never counted, never denied (no session_id fallback)
  const s = `sess-${agent()}`;
  const call = (cmd) => ff({ hook_event_name: 'PostToolUse', session_id: s, tool_name: 'Bash', tool_input: { command: cmd }, tool_response: { stdout: UNHEALTHY, exit_code: 1 } });
  call('factory env up acme api');
  assert.equal(call('factory env up acme api'), null);
});

test('PowerShell and non-shell tools', () => {
  const id = agent();
  const ps = (cmd) => ff({ hook_event_name: 'PostToolUse', agent_id: id, tool_name: 'PowerShell', tool_input: { command: cmd }, tool_response: { stdout: PORT } });
  ps('factory env up acme api');
  assert.ok(ps('factory env up acme api').additionalContext);
  assert.ok(denied(pre(id, 'factory env up acme api', 'PowerShell')));
  const other = agent();
  assert.equal(ff({ hook_event_name: 'PostToolUse', agent_id: other, tool_name: 'Read', tool_input: {}, tool_response: PORT }), null);
});

test('non-factory commands that print ENV-FAIL text still count only as signals, never deny unrelated commands', () => {
  const id = agent();
  post(id, 'cat log.txt', UNHEALTHY);
  post(id, 'cat log.txt', UNHEALTHY);
  assert.ok(!denied(pre(id, 'git status')));
  assert.ok(!denied(pre(id, 'npm test')));
});

test('fails open on unreadable input and weird shapes', () => {
  for (const raw of ['nope', '', '[]', '{"tool_name":"Bash"}', '{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":null,"tool_response":null}']) {
    const r = spawnSync(process.execPath, [path.join(HOOKS, 'failfast.js')], { input: raw, encoding: 'utf8' });
    assert.equal(r.status, 0, raw);
    assert.equal(r.stdout.trim(), '', raw);
  }
});
