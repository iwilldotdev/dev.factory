import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { runHook, denied, bash, RUN, FH_POSIX, TMP } from './helpers.mjs';

const protect = (cmd, cwd = '/work') => runHook('protect.js', [], bash(cmd, cwd));
const guard = (role, cmd, cwd = '/work') => runHook('worker-guard.js', [role], bash(cmd, cwd));
const ROLES = ['builder', 'author', 'auditor'];

// ---- F1: factory wait --cmd is judged like a `bash -c` body ----
test('F1: wait --cmd bodies that write run files or sleep are denied (quoted, unquoted, --cmd=)', () => {
  const bodies = [
    `factory wait --cmd "cp a.json ${RUN}/evidence/CHK-1.json" --timeout 2`,
    `factory wait --cmd "echo {} > ${RUN}/run.json" --timeout 2`,
    `factory wait --cmd='echo {} > ${RUN}/run.json'`,
    `factory wait --cmd=cp --timeout 2; cp a ${RUN}/run.json`,
    `factory wait --cmd "python -c \\"open('${RUN}/run.json','w').write('1')\\"" --timeout 2`,
    `python -m factory wait --cmd "tee ${RUN}/reviews/x.json" --until-exit 0`,
  ];
  for (const c of bodies) {
    assert.ok(denied(protect(c)), `protect: ${c}`);
    assert.ok(denied(guard('builder', c)), `builder: ${c}`);
  }
  for (const c of ['factory wait --cmd "sleep 300" --timeout 600', "factory wait --cmd 'sleep 5; true'", 'factory wait --cmd=sleep']) {
    assert.ok(denied(guard('builder', c)), c);
  }
  // a read-only body stays allowed for the builder
  for (const c of ['factory wait --cmd "curl -s localhost:3000/health" --until-exit 0 --timeout 60',
    `factory wait --cmd "cat ${RUN}/run.json" --timeout 5`]) {
    assert.ok(!denied(protect(c)), c);
    assert.ok(!denied(guard('builder', c)), c);
  }
});

test('F1: wait --cmd is builder-only; --file and --url stay allowed for every role', () => {
  const cmd = 'factory wait --cmd "curl -s localhost:3000" --until-exit 0 --timeout 60';
  assert.ok(!denied(guard('builder', cmd)));
  for (const role of ['author', 'auditor']) {
    assert.ok(denied(guard(role, cmd)), role);
    assert.ok(denied(guard(role, 'python -m factory wait --cmd=true')), role);
    assert.ok(!denied(guard(role, 'factory wait --file /tmp/x --timeout 30')), role);
    assert.ok(!denied(guard(role, 'factory wait --url http://localhost:3000 --status 2xx')), role);
  }
});

// ---- F2: lifecycle commands reserved for the main session ----
test('F2: verify-human, retype, close, env up/down/lease/--auto-restart are denied to every worker', () => {
  const reserved = ['factory run verify-human r CHK-9 --note ok --by user --user-said ok', 'factory run retype r --feature',
    'factory run close r --reason x', 'factory env lease release ws --run r', 'factory env lease acquire ws --run r --wait',
    'factory env down ws api', 'factory env up ws api --run r', 'factory env check ws api --run r --auto-restart',
    'python -m factory env up ws api'];
  for (const role of ROLES) {
    for (const c of reserved) assert.ok(denied(guard(role, c)), `${role}: ${c}`);
    for (const c of ['factory env check ws api --run r', 'factory env logs ws api', 'factory prove r --check CHK-1',
      'factory env lease status ws', 'factory run status r']) {
      assert.ok(!denied(guard(role, c)), `${role}: ${c}`);
    }
  }
});

// ---- F3: any write call + any protected/home name anywhere in the code blob ----
const SP = RUN;
test('F3: inline code with a write call and a protected name or the factory home is denied', () => {
  const cases = [
    `python -c "import os,pathlib; p=pathlib.Path(os.environ['FACTORY_HOME'])/'runs'/'r1'/'run.json'; p.write_text('1')"`,
    `node -e "require('fs').writeFileSync(process.env.FACTORY_HOME+'/runs/r1/run.json','{}')"`,
    `node -e "const fs=require('fs');const p=process.env.FACTORY_HOME+'/runs/r1/run.json';fs.writeFileSync(p,'{}')"`,
    `python - <<'EOF'\nimport os\np=os.environ['FACTORY_HOME']\np=p+'/runs/r1/run.json'\nopen(p,'w').write('1')\nEOF`,
    `cat a | python -c "import sys,os\np=os.environ['FACTORY_HOME']+'/runs/r1/run.json'\nopen(p,'w').write(sys.stdin.read())"`,
    `python -c "import os,shutil\nd=os.environ['FACTORY_HOME']+'/runs/r1'\nshutil.copy('a', d)"`,
    `python -c "import os\nh=os.environ['FACTORY_HOME']\nos.replace('a', h+'/x')"`,
    `python -c "import pathlib,os; pathlib.Path(os.environ['FACTORY_HOME']).joinpath('runs').rename('z')"`,
    `bash -c "python -c \\"import os; open(os.environ['FACTORY_HOME']+'/x','w')\\""`,
  ];
  for (const c of cases) {
    assert.ok(denied(protect(c)), `protect: ${c}`);
    assert.ok(denied(guard('builder', c)), `builder: ${c}`);
  }
});

test('F3: a temp script building a protected name piecewise while naming .factory is denied; read-only code stays allowed', () => {
  const dir = fs.mkdtempSync(path.join(TMP, 'fg-holes-'));
  try {
    const bad = path.join(dir, 'w.py').replace(/\\/g, '/');
    fs.writeFileSync(bad, "# target: ~/.factory\nimport os\nd=os.path.join('x','ev'+'idence')\nopen(d+'/CHK-1.json','w').write('{}')\n");
    assert.ok(denied(protect(`python ${bad}`, dir)), 'protect');
    assert.ok(denied(guard('builder', `python ${bad}`, dir)), 'builder');
    const good = path.join(dir, 'r.py').replace(/\\/g, '/');
    fs.writeFileSync(good, "import os,json\nprint(json.load(open(os.environ['FACTORY_HOME']+'/runs/r1/run.json')))\n");
    assert.ok(!denied(protect(`python ${good}`, dir)), 'read-only script');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  for (const c of [`python -c "import os,json;print(json.load(open(os.environ['FACTORY_HOME']+'/runs/r1/run.json')))"`,
    `node -e "console.log(require('fs').readFileSync(process.env.FACTORY_HOME+'/runs/r1/run.json','utf8'))"`]) {
    assert.ok(!denied(protect(c)), c);
  }
});

// ---- F10: read-only list holes ----
test('F10: sort/uniq/xxd/diff/find writing into run files are denied', () => {
  const cases = [
    `sort -uo ${SP}/run.json a.json`, `sort -o${SP}/run.json a`, `sort --output=${SP}/run.json a`, `sort -ro ${SP}/run.json a`,
    `uniq a ${SP}/run.json`, `uniq -c a ${SP}/run.json`, `find ${SP} -fprint ${SP}/evidence/x`,
    `find ${SP}/evidence -fprintf ${SP}/evidence/x '%p'`, `find ${SP} -fls ${SP}/run.json`, `find ${SP}/evidence -delete`,
    `xxd -r a ${SP}/run.json`, `diff --output=${SP}/run.json a b`, `diff --output ${SP}/run.json a b`,
    `cat a.json | sponge ${SP}/run.json`,
  ];
  for (const c of cases) {
    assert.ok(denied(protect(c)), `protect: ${c}`);
    assert.ok(denied(guard('builder', c)), `builder: ${c}`);
  }
  for (const c of [`sort ${SP}/run.json`, `uniq ${SP}/run.json`, `xxd ${SP}/run.json`, `diff ${SP}/run.json a`, `find ${SP} -name run.json`,
    'sort -u -o out.txt a.json', 'uniq a out.txt', `sort -k2 ${SP}/run.json | uniq`]) {
    assert.ok(!denied(protect(c)), c);
  }
});

// ---- F8: failfast ----
const STATE_DIR = path.join(os.tmpdir(), 'dev.factory-failfast');
const ff = (input) => runHook('failfast.js', [], input);
let n = 0;
const agent = () => `ffh-${process.pid}-${Date.now()}-${n += 1}`;
const pre = (id, command) => ff({ hook_event_name: 'PreToolUse', agent_id: id, tool_name: 'Bash', tool_input: { command } });
const post = (id, command, stdout, code = 1) => ff({ hook_event_name: 'PostToolUse', agent_id: id, tool_name: 'Bash', tool_input: { command }, tool_response: { stdout, exit_code: code } });
const FAIL = 'ENV-FAIL repo=api kind=unhealthy detail=x';
const stateFileOf = (id) => path.join(STATE_DIR, `${crypto.createHash('sha1').update(id).digest('hex').slice(0, 16)}.json`);
import crypto from 'node:crypto';

test('F8a: only factory env check|up and factory prove output counts', () => {
  const id = agent();
  for (const c of ['grep ENV-FAIL notes.md', 'cat log.txt', 'factory env logs ws api', 'echo ENV-FAIL']) {
    assert.equal(post(id, c, FAIL, 0), null);
    assert.equal(post(id, c, FAIL, 0), null);
  }
  assert.ok(!denied(pre(id, 'factory prove r1')));
  assert.ok(!denied(pre(id, 'factory env check ws api')));
});

test('F8b: without agent_id nothing is counted and nothing is denied (no session_id fallback)', () => {
  const sid = `sess-${agent()}`;
  const call = (o) => ff({ session_id: sid, tool_name: 'Bash', ...o });
  const failCall = () => call({ hook_event_name: 'PostToolUseFailure', tool_input: { command: 'factory env check ws api' }, error: `Exit code 1\n${FAIL}` });
  assert.equal(failCall(), null);
  assert.equal(failCall(), null);
  assert.equal(failCall(), null);
  assert.equal(call({ hook_event_name: 'PreToolUse', tool_input: { command: 'factory env check ws api' } }), null);
  // another worker (with an id) in the same session is untouched
  assert.ok(!denied(pre(agent(), 'factory env check ws api')));
});

test('F8c: the deny expires after 2 denies', () => {
  const id = agent();
  post(id, 'factory env check ws api', FAIL);
  assert.ok(post(id, 'factory env check ws api', FAIL).additionalContext);
  assert.ok(denied(pre(id, 'factory env up ws api')));
  assert.ok(denied(pre(id, 'factory prove r')));
  assert.ok(!denied(pre(id, 'factory env check ws api')), 'third attempt is allowed again');
  assert.equal(fs.existsSync(stateFileOf(id)), false);
});

test('F8c: the deny expires after 10 minutes', () => {
  const id = agent();
  post(id, 'factory env check ws api', FAIL);
  post(id, 'factory env check ws api', FAIL);
  const f = stateFileOf(id);
  const st = JSON.parse(fs.readFileSync(f, 'utf8'));
  assert.equal(st.blocked, true);
  st.blocked_at = Date.now() - 11 * 60 * 1000;
  fs.writeFileSync(f, JSON.stringify(st));
  assert.ok(!denied(pre(id, 'factory env check ws api')));
  assert.equal(fs.existsSync(f), false);
});

test('F8d: state files older than the max age are pruned on every run', () => {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  const stale = path.join(STATE_DIR, `stale-${process.pid}-${Date.now()}.json`);
  const fresh = path.join(STATE_DIR, `fresh-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(stale, '{}');
  fs.writeFileSync(fresh, '{}');
  const old = new Date(Date.now() - 4 * 60 * 60 * 1000);
  fs.utimesSync(stale, old, old);
  ff({ hook_event_name: 'PreToolUse', agent_id: agent(), tool_name: 'Bash', tool_input: { command: 'git status' } });
  assert.equal(fs.existsSync(stale), false);
  assert.equal(fs.existsSync(fresh), true);
  fs.unlinkSync(fresh);
});
