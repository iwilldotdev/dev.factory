import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { runHook, denied, bash, write, RUN, approveProfile } from './helpers.mjs';

const require = createRequire(import.meta.url);
const g = require('../guard-lib.js');

const guard = (role, input, env) => runHook('worker-guard.js', [role], input, env);
const mcp = (tool_name, tool_input = {}, extra = {}) => ({ tool_name, tool_input, ...extra });

// [tool name, expected class]. Synthetic names only: the classifier knows verbs, never servers.
const TABLE = [
  // plain read verbs
  ['mcp__acme__get_ticket', 'read'], ['mcp__acme__list_tickets', 'read'], ['mcp__acme__search_issues', 'read'],
  ['mcp__acme__read_page', 'read'], ['mcp__acme__fetch_node', 'read'], ['mcp__acme__find_user', 'read'],
  ['mcp__acme__lookup_account', 'read'], ['mcp__acme__query_records', 'read'], ['mcp__acme__describe_schema', 'read'],
  ['mcp__acme__show_change', 'read'], ['mcp__acme__view_diff', 'read'], ['mcp__acme__download_attachment', 'read'],
  ['mcp__acme__count_items', 'read'], ['mcp__acme__whoami', 'read'], ['mcp__acme__me', 'read'],
  ['mcp__acme__export_report', 'read'], ['mcp__acme__resolve_ref', 'read'], ['mcp__acme__get_me', 'read'],
  // kebab-case and camelCase reads
  ['mcp__acme__get-ticket', 'read'], ['mcp__acme__list-open-tickets', 'read'], ['mcp__acme__getTicket', 'read'],
  ['mcp__acme__listProjects', 'read'], ['mcp__acme__searchIssuesUsingQuery', 'read'], ['mcp__acme__GetTicket', 'read'],
  ['mcp__acme__fetchDesignNode', 'read'], ['mcp__acme__get_issue_comments', 'read'], ['mcp__acme__list_board_sprints', 'read'],
  // server names with underscores, hyphens, digits and doubled separators
  ['mcp__my_server__get_item', 'read'], ['mcp__my-server__get_item', 'read'], ['mcp__my_long_server_name__list_items', 'read'],
  ['mcp__srv2__search_items', 'read'], ['mcp__plugin_acme_tickets__get_ticket', 'read'], ['mcp__a-b_c__fetch_x', 'read'],
  ['mcp__my_server__create_item', 'write'], ['mcp__my-server__delete_item', 'write'], ['mcp__get_server__delete_item', 'write'],
  ['mcp__list_server__do_thing', 'write'], ['mcp__read-only-server__push_commit', 'write'],
  // plain write verbs
  ['mcp__acme__create_ticket', 'write'], ['mcp__acme__update_ticket', 'write'], ['mcp__acme__save_change', 'write'],
  ['mcp__acme__add_comment', 'write'], ['mcp__acme__edit_ticket', 'write'], ['mcp__acme__write_page', 'write'],
  ['mcp__acme__transition_ticket', 'write'], ['mcp__acme__merge_change', 'write'], ['mcp__acme__accept_change', 'write'],
  ['mcp__acme__delete_ticket', 'write'], ['mcp__acme__remove_label', 'write'], ['mcp__acme__post_note', 'write'],
  ['mcp__acme__send_message', 'write'], ['mcp__acme__approve_change', 'write'], ['mcp__acme__unapprove_change', 'write'],
  ['mcp__acme__publish_page', 'write'], ['mcp__acme__run_pipeline', 'write'], ['mcp__acme__trigger_build', 'write'],
  ['mcp__acme__retry_job', 'write'], ['mcp__acme__cancel_job', 'write'], ['mcp__acme__play_job', 'write'],
  ['mcp__acme__apply_patch', 'write'], ['mcp__acme__link_tickets', 'write'], ['mcp__acme__unlink_tickets', 'write'],
  ['mcp__acme__watch_ticket', 'write'], ['mcp__acme__upload_file', 'write'], ['mcp__acme__complete_task', 'write'],
  ['mcp__acme__reopen_ticket', 'write'], ['mcp__acme__close_ticket', 'write'], ['mcp__acme__label_ticket', 'write'],
  ['mcp__acme__assign_ticket', 'write'], ['mcp__acme__move_ticket', 'write'], ['mcp__acme__set_status', 'write'],
  ['mcp__acme__resolve_thread', 'write'], ['mcp__acme__rebase_branch', 'write'], ['mcp__acme__revert_commit', 'write'],
  ['mcp__acme__fork_repo', 'write'], ['mcp__acme__star_repo', 'write'], ['mcp__acme__push_files', 'write'],
  // ambiguous or unknown: fail closed
  ['mcp__acme__do_thing', 'write'], ['mcp__acme__sync', 'write'], ['mcp__acme__get_and_delete', 'write'],
  ['mcp__acme__getCreateMeta', 'write'], ['mcp__acme__list_and_merge', 'write'], ['mcp__acme__get_or_create_cache', 'write'],
  ['mcp__acme__fetch_then_update', 'write'], ['mcp__acme__search-and-replace', 'write'], ['mcp__acme__getAndSetValue', 'write'],
  ['mcp__acme__refresh', 'write'], ['mcp__acme__execute', 'write'], ['mcp__acme__refresh_all', 'write'],
  ['mcp__acme__export_and_delete', 'write'], ['mcp__acme__get_run', 'write'], ['mcp__acme__list_link', 'write'], ['mcp__acme__list_links', 'read'], ['mcp__acme__replace_text', 'write'], ['mcp__acme__rename_item', 'write'],
  ['mcp__acme__', 'write'], ['mcp__acme__x', 'write'], ['mcp__acme__42', 'write'],
  // namespace or vendor prefixes: the first known verb decides, any write token wins, no verb fails closed
  ['mcp__acme__prefix_get_x', 'read'], ['mcp__acme__vendor_list_items', 'read'], ['mcp__acme__acme_get_ticket', 'read'],
  ['mcp__acme__prefix_save_x', 'write'], ['mcp__acme__prefix_get_and_delete', 'write'], ['mcp__acme__vendor_create_item', 'write'],
  // `normalize` is in neither verb list: no verb at all, so WRITE (fail-closed)
  ['mcp__acme__prefix_normalize_x', 'write'], ['mcp__acme__normalize_issue', 'write'],
  ['mcp__acme__fetch_screenshot', 'read'], ['mcp__acme__figlike_fetch_screenshot', 'read'],
  ['mcp__acme__download_asset', 'read'], ['mcp__acme__download_issue_attachment', 'read'], ['mcp__acme__ticket_get', 'read'],
  // names that are not MCP names at all
  ['Bash', 'write'], ['mcp__only', 'write'], ['', 'write'],
];

test('classifier table: at least 80 synthetic names, each classified by verb tokens only', () => {
  assert.ok(TABLE.length >= 80, `table has ${TABLE.length} rows`);
  for (const [name, want] of TABLE) assert.equal(g.classifyMcpTool(name), want, name);
});

test('tokenizer splits snake, kebab and camel case', () => {
  assert.deepEqual(g.tokenizeToolName('get_issue-comments'), ['get', 'issue', 'comments']);
  assert.deepEqual(g.tokenizeToolName('getCreateMeta'), ['get', 'create', 'meta']);
  assert.deepEqual(g.tokenizeToolName('GetHTTPResponse'), ['get', 'http', 'response']);
  assert.deepEqual(g.tokenizeToolName('list.items'), ['list', 'items']);
});

test('server and tool segments are split at the first double underscore', () => {
  assert.deepEqual(g.parseMcpName('mcp__my_server__get_item'), { server: 'my_server', tool: 'get_item' });
  assert.deepEqual(g.parseMcpName('mcp__a-b__get_x'), { server: 'a-b', tool: 'get_x' });
  assert.equal(g.parseMcpName('Bash'), null);
});

test('profile mcp_read_allow globs certify a tool; the glob matches the tool segment', () => {
  assert.equal(g.classifyMcpTool('mcp__acme__get_or_create_cache'), 'write');
  assert.equal(g.classifyMcpTool('mcp__acme__get_or_create_cache', ['get_or_create_*']), 'read');
  assert.equal(g.classifyMcpTool('mcp__acme__get_or_create_cache', ['GET_OR_CREATE_CACHE']), 'read');
  assert.equal(g.classifyMcpTool('mcp__acme__do_thing', ['get_*']), 'write');
  assert.equal(g.classifyMcpTool('mcp__acme__do_thing', ['mcp__acme__do_*']), 'read');
  assert.equal(g.classifyMcpTool('mcp__other__do_thing', ['mcp__acme__do_*']), 'write');
  assert.equal(g.classifyMcpTool('mcp__acme__run_pipeline', ['get_*']), 'write');
});

test('worker guard denies a write tool for every role and allows a read tool', () => {
  for (const role of ['scout', 'author', 'builder', 'auditor']) {
    const res = guard(role, mcp('mcp__acme__create_ticket', { title: 'x' }));
    assert.ok(denied(res), role);
    assert.match(res.permissionDecisionReason, /remote writes belong to the main session/);
    assert.ok(denied(guard(role, mcp('mcp__acme__do_thing'))), `${role} ambiguous`);
    assert.ok(!denied(guard(role, mcp('mcp__acme__get_ticket', { key: 'ACME-1', save_to: `${RUN}/raw/ticket/ACME-1.json` }))), role);
  }
});

test('the guard never touches a caller whose agent_type is not a factory worker (main session safe)', () => {
  const write = mcp('mcp__acme__create_ticket', { title: 'x' });
  assert.ok(!denied(guard('builder', { ...write, agent_type: 'general-purpose' })));
  assert.ok(!denied(guard('builder', { ...write, agent_type: 'Explore' })));
  assert.ok(denied(guard('builder', { ...write, agent_type: 'factory-builder' })));
  assert.ok(denied(guard('builder', { ...write, agent_type: 'plugin:factory-builder' })));
  assert.ok(denied(guard('builder', write)), 'no agent_type: the role argument decides');
  assert.ok(!denied(guard('builder', { ...bash('git push'), agent_type: 'general-purpose' })));
  assert.ok(denied(guard('builder', { ...bash('git push'), agent_type: 'factory-builder' })));
});

// ---------- CLI write patterns ----------

const GENERIC_DENY = [
  'curl -X POST https://api.example/x', 'curl -XPUT https://api.example/x', 'curl --request PATCH https://api.example/x',
  'curl --request=DELETE https://api.example/x', 'curl -X DELETE https://api.example/x', 'curl -s https://api.example/x -d @body.json',
  'curl https://api.example/x --data "a=b"', 'curl https://api.example/x --data-binary @f', 'curl -F file=@a https://api.example/x',
  'curl --form k=v https://api.example/x', 'curl -T a.txt https://api.example/x', 'curl --json {} https://api.example/x',
  'echo hi && curl -X POST https://api.example/x', 'wget --method=POST https://api.example/x', 'wget --post-data=a=b https://api.example/x',
  'wget --post-file=f https://api.example/x', 'http POST https://api.example/x a=b', 'http -f PUT https://api.example/x',
  'cd /tmp; https DELETE https://api.example/x',
];
const GENERIC_ALLOW = [
  'curl -s https://api.example/x', 'curl -sS -H "Accept: application/json" https://api.example/x',
  'curl -s -X GET https://api.example/x', 'curl -sS https://api.example/x > raw.json', 'wget -qO- https://api.example/x',
  'http GET https://api.example/x', 'http https://api.example/x', 'acme-cli ticket get ACME-1', 'npm test', 'git status',
];

test('generic CLI write patterns: curl, wget and http clients that send a write', () => {
  for (const role of ['author', 'builder', 'auditor']) {
    for (const cmd of GENERIC_DENY) {
      const res = guard(role, bash(cmd));
      assert.ok(denied(res), `${role}: ${cmd}`);
      assert.match(res.permissionDecisionReason, /remote writes belong to the main session/);
    }
    for (const cmd of GENERIC_ALLOW) assert.ok(!denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  }
});

test('generic reason helper', () => {
  assert.ok(g.genericCliWriteReason('curl -X POST https://api.example/x'));
  assert.equal(g.genericCliWriteReason('curl https://api.example/x'), '');
});

// ---------- profiles ----------

function makeHome(profiles, workspaces, approve = true) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'fh-guard-'));
  for (const [name, prof] of Object.entries(profiles)) {
    fs.mkdirSync(path.join(home, 'profiles', name), { recursive: true });
    if (prof !== null) fs.writeFileSync(path.join(home, 'profiles', name, 'profile.json'), typeof prof === 'string' ? prof : JSON.stringify(prof));
    if (prof !== null && approve) approveProfile(home, name);
  }
  for (const [ws, conf] of Object.entries(workspaces)) {
    fs.mkdirSync(path.join(home, 'workspaces', ws), { recursive: true });
    fs.writeFileSync(path.join(home, 'workspaces', ws, 'workspace.json'), JSON.stringify(conf));
  }
  return home;
}
const withHome = (home, fn) => {
  try { return fn(); } finally { fs.rmSync(home, { recursive: true, force: true }); }
};

const ACME = { schema: 'profile.v1', name: 'acme', role: 'tickets',
  cli_write_patterns: ['^\\s*acme-cli\\s+(?:ticket|change|ci)\\s+(?:create|edit|comment|approve|merge|transition|retry)\\b'],
  mcp_read_allow: ['get_or_create_*'], save_args: ['target_file'] };

test('profile cli_write_patterns come from FACTORY_HOME/profiles named in workspace.json providers', () => {
  const home = makeHome({ acme: ACME, zeta: { cli_write_patterns: ['\\bzeta-cli\\s+push\\b'] } },
    { w1: { providers: { tickets: 'acme', scm: 'acme' } } });
  withHome(home, () => {
    const env = { FACTORY_HOME: home };
    for (const cmd of ['acme-cli ticket create --title x', 'acme-cli ci retry 12', 'npm test && acme-cli change merge 4',
      'cd x; acme-cli ticket comment ACME-1 hi']) {
      const res = guard('builder', bash(cmd), env);
      assert.ok(denied(res), cmd);
      assert.match(res.permissionDecisionReason, /remote writes belong to the main session/);
    }
    for (const cmd of ['acme-cli ticket get ACME-1', 'acme-cli change list', 'acme-cli ticket get ACME-1 > out.json']) {
      assert.ok(!denied(guard('builder', bash(cmd), env)), cmd);
    }
    // zeta is not named by any workspace: its patterns are not active
    assert.ok(!denied(guard('builder', bash('zeta-cli push now'), env)));
  });
});

test('profile patterns follow the workspace of the run named in the call', () => {
  const home = makeHome({ acme: ACME, zeta: { cli_write_patterns: ['\\bzeta-cli\\s+push\\b'] } },
    { w1: { providers: { tickets: 'acme' } }, w2: { providers: { tickets: 'zeta' } } });
  withHome(home, () => {
    fs.mkdirSync(path.join(home, 'runs', 'w2-1'), { recursive: true });
    fs.writeFileSync(path.join(home, 'runs', 'w2-1', 'run.json'), JSON.stringify({ workspace: 'w2' }));
    const runDir = `${home.replace(/\\/g, '/')}/runs/w2-1`;
    const env = { FACTORY_HOME: home };
    // inside the w2 run only its provider is active
    assert.ok(denied(guard('builder', bash(`zeta-cli push now # ${runDir}/notes`), env)));
    assert.ok(!denied(guard('builder', bash(`acme-cli ticket create x # ${runDir}/notes`), env)));
    // outside any run every workspace counts
    assert.ok(denied(guard('builder', bash('acme-cli ticket create x'), env)));
    assert.ok(denied(guard('builder', bash('zeta-cli push now'), env)));
  });
});

test('profile mcp_read_allow and save_args are honoured by the guard', () => {
  const home = makeHome({ acme: ACME }, { w1: { providers: { tickets: 'acme' } } });
  withHome(home, () => {
    const env = { FACTORY_HOME: home };
    const run = `${home.replace(/\\/g, '/')}/runs/w1-1`;
    assert.ok(!denied(guard('builder', mcp('mcp__acme__get_or_create_cache'), env)));
    assert.ok(denied(guard('builder', mcp('mcp__acme__get_or_create_cache'), { FACTORY_HOME: path.join(home, 'nowhere') })));
    assert.ok(denied(guard('builder', mcp('mcp__acme__create_ticket'), env)));
    assert.ok(denied(guard('builder', mcp('mcp__acme__get_ticket', { target_file: `${run}/contracts/ticket/ACME-1.json` }), env)));
    assert.ok(!denied(guard('builder', mcp('mcp__acme__get_ticket', { target_file: `${run}/raw/ticket/ACME-1.json` }), env)));
  });
});

test('unreadable workspace or profile: generic rules only, nothing inferred, nothing crashes', () => {
  const home = makeHome({ acme: '{not json', broken: null },
    { w1: { providers: { tickets: 'acme', scm: 'broken', design: 'missing', x: 7, y: '../escape' } } });
  withHome(home, () => {
    fs.writeFileSync(path.join(home, 'workspaces', 'w1', 'workspace.json'), '{oops');
    const env = { FACTORY_HOME: home };
    assert.ok(!denied(guard('builder', bash('acme-cli ticket create x'), env)));
    assert.ok(denied(guard('builder', bash('curl -X POST https://api.example/x'), env)));
    assert.ok(denied(guard('builder', mcp('mcp__acme__create_ticket'), env)));
    assert.ok(denied(guard('builder', bash('git push'), env)));
  });
});

test('invalid regexes in a profile are skipped, valid ones still apply', () => {
  const home = makeHome({ acme: { cli_write_patterns: ['([unclosed', 42, '\\bacme-cli\\s+nuke\\b'] } },
    { w1: { providers: { tickets: 'acme' } } });
  withHome(home, () => {
    const env = { FACTORY_HOME: home };
    assert.ok(denied(guard('builder', bash('acme-cli nuke all'), env)));
    assert.ok(!denied(guard('builder', bash('acme-cli list'), env)));
  });
});

test('scout keeps its git-only Bash allowlist; profiles do not widen it', () => {
  const home = makeHome({ acme: ACME }, { w1: { providers: { tickets: 'acme' } } });
  withHome(home, () => {
    assert.ok(denied(guard('scout', bash('acme-cli ticket get ACME-1'), { FACTORY_HOME: home })));
    assert.ok(!denied(guard('scout', bash('git status'), { FACTORY_HOME: home })));
  });
});
