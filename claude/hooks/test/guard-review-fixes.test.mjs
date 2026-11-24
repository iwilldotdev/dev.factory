import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { runHook, denied, bash, write, FH_POSIX, RUN, approveProfile } from './helpers.mjs';

const require = createRequire(import.meta.url);
const g = require('../guard-lib.js');
const guard = (role, input, env) => runHook('worker-guard.js', [role], input, env);
const mcp = (tool_name, tool_input = {}) => ({ tool_name, tool_input });
const ROLES = ['author', 'builder', 'auditor'];

// ---------- F1: reserved factory verbs ----------

test('factory migrate and profile approve are main-session only (also via python -m and nested shells)', () => {
  const deny = ['factory migrate', 'factory migrate --apply', 'python -m factory migrate plan', 'factory profile approve acme',
    'factory --json profile approve acme', 'bash -c "factory migrate"', 'env X=1 factory profile approve acme', 'factory profile new acme'];
  for (const role of ROLES) {
    for (const cmd of deny) assert.ok(denied(guard(role, bash(cmd, '/work'))), `${role}: ${cmd}`);
    for (const cmd of ['factory profile new acme --draft', 'factory profile test acme', 'factory profile check acme']) {
      assert.ok(!denied(guard(role, bash(cmd, '/work'))), `${role}: ${cmd}`);
    }
  }
});

// ---------- F3: profiles/ and workspaces/ are main-only; profile-drafts/ is open to author and builder ----------

test('every write route into profiles/ and workspaces/ is denied to workers', () => {
  const targets = [`${FH_POSIX}/profiles/acme/profile.json`, `${FH_POSIX}/profiles/acme/approved.json`, `${FH_POSIX}/workspaces/w1/workspace.json`];
  for (const role of [...ROLES, 'scout']) {
    for (const t of targets) {
      for (const tool of ['Write', 'Edit', 'MultiEdit']) assert.ok(denied(guard(role, write(t, '/work', tool))), `${role} ${tool} ${t}`);
    }
  }
  const cmds = (t) => [`echo x > ${t}`, `echo x >> ${t}`, `cp /work/p.json ${t}`, `mv /work/p.json ${t}`, `cat /work/p.json | tee ${t}`,
    `sed -i s/a/b/ ${t}`, `python -c "open('${t}','w').write('x')"`, `rm ${t}`, `printf x | tee -a ${t}`];
  for (const role of ROLES) {
    for (const t of targets) for (const c of cmds(t)) assert.ok(denied(guard(role, bash(c, '/work'))), `${role}: ${c}`);
    const ps = (c) => ({ tool_name: 'PowerShell', tool_input: { command: c }, cwd: '/work' });
    assert.ok(denied(guard(role, ps(`Set-Content -Path ${targets[0]} -Value x`))), `${role} Set-Content`);
    assert.ok(denied(guard(role, ps(`'x' | Out-File ${targets[2]}`))), `${role} Out-File`);
    assert.ok(denied(guard(role, bash('echo x > $FACTORY_HOME/profiles/acme/profile.json', '/work'))), `${role} var`);
    assert.ok(denied(guard(role, mcp('mcp__acme__get_ticket', { save_to: targets[0] }))), `${role} mcp`);
    // reading is fine
    assert.ok(!denied(guard(role, bash(`cat ${targets[0]}`, '/work'))), `${role} read`);
  }
});

test('profile-drafts/<name>/** is writable by the author and the builder, not by the auditor or scout', () => {
  const d = `${FH_POSIX}/profile-drafts/acme/profile.json`;
  for (const role of ['author', 'builder']) {
    assert.ok(!denied(guard(role, write(d, '/work'))), role);
    assert.ok(!denied(guard(role, bash(`echo x > ${d}`, '/work'))), `${role} redirect`);
  }
  for (const role of ['auditor', 'scout']) assert.ok(denied(guard(role, write(d, '/work'))), role);
  assert.ok(denied(guard('author', write(`${FH_POSIX}/profile-drafts`, '/work'))));
  // a draft path cannot smuggle into profiles/
  assert.ok(denied(guard('author', write(`${FH_POSIX}/profile-drafts/../profiles/acme/profile.json`, '/work'))));
});

// ---------- F6: normalisation ----------

function withHome(profile, approved, fn) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'fh-fix-'));
  try {
    fs.mkdirSync(path.join(home, 'workspaces', 'w1'), { recursive: true });
    fs.writeFileSync(path.join(home, 'workspaces', 'w1', 'workspace.json'), JSON.stringify({ providers: { tickets: 'acme' } }));
    fs.mkdirSync(path.join(home, 'profiles', 'acme'), { recursive: true });
    fs.writeFileSync(path.join(home, 'profiles', 'acme', 'profile.json'), JSON.stringify(profile));
    if (approved) approveProfile(home, 'acme');
    return fn(home);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}
const ACME = { cli_write_patterns: ['^acme-cli\\s+ticket\\s+create\\b'], mcp_read_allow: ['get_or_create_*'], save_args: ['target_file'] };

test('profile patterns see through env, bash -c, subshells, assignments, xargs, paths and call operators', () => {
  withHome(ACME, true, (home) => {
    const env = { FACTORY_HOME: home };
    const deny = ['env acme-cli ticket create x', "bash -c 'acme-cli ticket create x'", '(acme-cli ticket create x)', 'X=1 acme-cli ticket create x',
      'echo $(acme-cli ticket create x)', 'echo `acme-cli ticket create x`', 'command acme-cli ticket create x', 'exec acme-cli ticket create x',
      'time acme-cli ticket create x', 'nice acme-cli ticket create x', 'echo a | xargs acme-cli ticket create', "sh -c 'cd x && acme-cli ticket create y'",
      'pwsh -Command "acme-cli ticket create x"', 'env -i A=b acme-cli ticket create x', '/usr/local/bin/acme-cli ticket create x',
      '  acme-cli ticket create x', 'bash -c "env X=1 acme-cli ticket create x"', 'A=1 B=2 env C=3 acme-cli ticket create x'];
    for (const role of ROLES) {
      for (const cmd of deny) assert.ok(denied(guard(role, bash(cmd, '/work'), env)), `${role}: ${cmd}`);
      for (const cmd of ['env acme-cli ticket get x', "bash -c 'acme-cli ticket get x'", '(acme-cli ticket list)', 'X=1 acme-cli ticket get x']) {
        assert.ok(!denied(guard(role, bash(cmd, '/work'), env)), `${role}: ${cmd}`);
      }
    }
  });
});

test('generic patterns: bundled curl flags, data flags, wget, httpie and PowerShell web cmdlets', () => {
  const deny = ['curl -sXPOST https://x.example/a', 'curl -XPOST https://x.example/a', 'curl -sSX DELETE https://x.example/a', 'curl --request=POST https://x.example/a',
    'curl -sd @f https://x.example/a', 'curl --data-raw x https://x.example/a', 'curl --data-urlencode a=b https://x.example/a', 'curl -sSLT f https://x.example/a',
    'curl --upload-file f https://x.example/a', 'wget --method=PUT https://x.example/a', 'wget --post-data=a https://x.example/a', 'wget --post-file=f https://x.example/a',
    'http POST https://x.example/a', 'https PUT https://x.example/a', 'http https://x.example/a name=v', 'env X=1 http POST https://x.example/a',
    'Invoke-RestMethod -Method Post -Uri https://x.example/a', 'irm https://x.example/a -Method Delete', 'Invoke-WebRequest -Uri https://x.example/a -Method Put',
    'iwr https://x.example/a -Body x', 'irm https://x.example/a -Method:Patch', 'pwsh -Command "Invoke-RestMethod -Method Post https://x.example/a"',
    "bash -c 'curl -sXPOST https://x.example/a'", '(curl -XPOST https://x.example/a)', 'env curl -sXPOST https://x.example/a'];
  const allow = ['curl -s https://x.example/a', 'curl -sS -H "A: b" https://x.example/a', 'curl -sSL https://x.example/a', 'http GET https://x.example/a',
    'http https://x.example/a q==1', 'Invoke-RestMethod -Uri https://x.example/a', 'irm https://x.example/a -Method Get', 'curl -s -X GET https://x.example/a'];
  for (const role of ROLES) {
    for (const cmd of deny) assert.ok(denied(guard(role, bash(cmd, '/work'))), `${role}: ${cmd}`);
    for (const cmd of allow) assert.ok(!denied(guard(role, bash(cmd, '/work'))), `${role}: ${cmd}`);
  }
});

test('commandUnits strips prefixes and unwraps shell bodies', () => {
  const u = g.commandUnits("env X=1 bash -c 'A=2 acme-cli x; (b-cli y)' && xargs -n1 c-cli z");
  for (const want of ['acme-cli x', 'b-cli y', 'c-cli z']) assert.ok(u.includes(want), `${want} in ${JSON.stringify(u)}`);
});

// ---------- profile approval (guard reads approved profiles only) ----------

test('unapproved, drifted or draft profiles are ignored: generic rules only', () => {
  const cmd = bash('acme-cli ticket create x', '/work');
  withHome(ACME, false, (home) => {
    const env = { FACTORY_HOME: home };
    assert.ok(!denied(guard('builder', cmd, env)), 'no approved.json');
    assert.ok(denied(guard('builder', mcp('mcp__acme__get_or_create_cache'), env)), 'no mcp_read_allow without approval');
    assert.ok(denied(guard('builder', bash('curl -X POST https://x.example/a', '/work'), env)));
  });
  withHome(ACME, true, (home) => {
    const env = { FACTORY_HOME: home };
    assert.ok(denied(guard('builder', cmd, env)), 'approved');
    // tamper after approval: sha no longer matches
    fs.writeFileSync(path.join(home, 'profiles', 'acme', 'profile.json'), JSON.stringify({ ...ACME, cli_write_patterns: [] }));
    assert.ok(!denied(guard('builder', cmd, env)), 'sha mismatch');
    assert.ok(denied(guard('builder', mcp('mcp__acme__get_or_create_cache'), env)));
    // a draft is never read
    fs.mkdirSync(path.join(home, 'profile-drafts', 'acme'), { recursive: true });
    fs.writeFileSync(path.join(home, 'profile-drafts', 'acme', 'profile.json'), JSON.stringify(ACME));
    assert.ok(!denied(guard('builder', cmd, env)), 'drafts ignored');
  });
});

test('approvedProfile pins the file sha', () => {
  withHome(ACME, true, (home) => {
    assert.ok(g.approvedProfile(home, 'acme'));
    fs.writeFileSync(path.join(home, 'profiles', 'acme', 'approved.json'), JSON.stringify({ file_sha256: 'abc' }));
    assert.equal(g.approvedProfile(home, 'acme'), null);
  });
});

// ---------- F8: write verbs ----------

test('added write verbs classify as write, including inside compound names', () => {
  const names = ['drop_table', 'destroy_item', 'purge_cache', 'upsert_row', 'insert_row', 'patch_item', 'put_item', 'import_data', 'comment_on_issue',
    'reply_to_thread', 'dismiss_alert', 'mark_read', 'deploy_app', 'execute_query', 'invoke_function', 'sync_repo', 'lock_issue', 'unlock_issue',
    'toggle_flag', 'rerun_job', 'enable_hook', 'disable_hook', 'clone_repo', 'list_and_drop_tables', 'get_or_upsert', 'fetch_and_import',
    'read_then_comment', 'getAndPatch', 'search_then_lock'];
  for (const n of names) assert.equal(g.classifyMcpTool(`mcp__acme__${n}`), 'write', n);
  for (const n of ['get_comments', 'list_locks', 'get_ticket', 'list_imports']) assert.equal(g.classifyMcpTool(`mcp__acme__${n}`), 'read', n);
});

// ---------- F10: save destinations ----------

test('save destinations are found by name pattern at any depth', () => {
  assert.deepEqual(g.saveDestinations({ savePath: '/a' }), ['/a']);
  assert.deepEqual(g.saveDestinations({ opts: { save_to: '/b' } }), ['/b']);
  assert.deepEqual(g.saveDestinations({ a: [{ b: { outputDir: '/c' } }] }), ['/c']);
  assert.deepEqual(g.saveDestinations({ target_path: '/d', filePath: '/e', destination: '/f' }).sort(), ['/f']); // source-like names are not destinations
  assert.deepEqual(g.saveDestinations({ file_path: '/e', timeout: '/t', layout: '/l', about: '/o' }), []);
  assert.deepEqual(g.saveDestinations({ output_dir: '/h', destinationPath: '/i' }).sort(), ['/h', '/i']);
  assert.deepEqual(g.saveDestinations({ q: 'x', title: 'y', nested: { id: 'z' } }), []);
  assert.deepEqual(g.saveDestinations({ deep: { target_file: '/g' } }, ['target_file']), ['/g']);
});

test('worker guard: nested and oddly named save arguments aimed at run state are denied; raw/ is allowed', () => {
  for (const role of ['scout', ...ROLES]) {
    for (const args of [{ savePath: `${RUN}/run.json` }, { opts: { save_to: `${RUN}/run.json` } }, { a: [{ outputPath: `${RUN}/evidence/x.json` }] },
      { opts: { save_to: `${FH_POSIX}/profiles/acme/profile.json` } }, { destinationPath: `${RUN}/acs.json` }]) {
      assert.ok(denied(guard(role, mcp('mcp__acme__get_ticket', args))), `${role} ${JSON.stringify(args)}`);
    }
    assert.ok(!denied(guard(role, mcp('mcp__acme__get_ticket', { savePath: `${RUN}/raw/t.json` }))), role);
    assert.ok(!denied(guard(role, mcp('mcp__acme__get_ticket', { opts: { save_to: `${RUN}/raw/t.json` } }))), role);
  }
});

test('protect.js also denies nested save destinations into run state', () => {
  assert.ok(denied(runHook('protect.js', [], mcp('mcp__acme__get_ticket', { opts: { saveTo: `${RUN}/run.json` } }))));
  assert.ok(!denied(runHook('protect.js', [], mcp('mcp__acme__get_ticket', { opts: { saveTo: `${RUN}/raw/t.json` } }))));
});
