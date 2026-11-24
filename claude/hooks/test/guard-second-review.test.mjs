// Second-review fixes: factory package reach (N1), profiles/workspaces/migrations nets (N2), anchored save names (N4),
// wrapper flag table and shell-fed bodies (N5), migrations/ ownership (N6).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { runHook, denied, bash, write, RUN, FH_POSIX, TMP, HOOKS } from './helpers.mjs';

const require = createRequire(import.meta.url);
const guards = require(path.join(HOOKS, 'guard-lib.js'));
const guard = (role, input) => runHook('worker-guard.js', [role], input);
const protect = (input) => runHook('protect.js', [], input);
const ROLES = ['builder', 'author', 'auditor'];
const CUSTOM = '/data/fh-custom';

test('N1: python -m factory.<module> and in-process imports of the factory package are reserved for the main session', () => {
  const reserved = [
    'python -m factory.cli profile approve acme --digest x --user-said y',
    'python3 -m factory.cli gate r plan', 'py -3 -m factory.cli ship plan r', 'python -u -m factory.commands.migrate apply r m.json',
    `python -c "from factory import profiles; profiles.approve('acme', 'x', 'y')"`,
    `python -c "import factory.profiles as p; p.approve('a', 'b')"`,
    `python -c "import factory; factory.cli.main(['gate'])"`,
    `python - <<'EOF'\nfrom factory.commands import migrate\nEOF`,
    `node -e "require('child_process').execSync('factory profile approve acme')"`,
    `node -e "require('/opt/dev.factory/factory/cli.js')"`,
    `python -c "import subprocess; subprocess.run(['factory', 'migrate', 'apply'])"`,
    `bash -c "python -c 'from factory import core'"`,
  ];
  for (const role of ROLES) for (const cmd of reserved) assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  const script = path.join(TMP, `n1-${process.pid}.py`);
  fs.writeFileSync(script, 'from factory import profiles\nprofiles.approve("a", "b", "c")\n');
  try {
    for (const role of ROLES) assert.ok(denied(guard(role, bash(`python ${script}`))), `${role}: readable script`);
  } finally {
    fs.rmSync(script, { force: true });
  }
  for (const ok of ['python -c "print(1)"', 'python -m unittest tests.test_x', 'node -e "console.log(1)"',
    `python -c "import json; print(json.dumps({'dev.factory': 1}))"`, 'factory env check acme React --run r',
    'factory prove r --check CHK-1']) {
    assert.ok(!denied(guard('builder', bash(ok))), ok);
  }
});

test('N2: inline interpreter writes into profiles/, workspaces/ and migrations/ of any home are denied to workers', () => {
  const bad = [
    `python -c "open('${CUSTOM}/profiles/acme/profile.json','w').write('x')"`,
    `python3 -c "import pathlib; pathlib.Path('${CUSTOM}/workspaces/ws/workspace.json').write_text('{}')"`,
    `node -e "require('fs').writeFileSync('${CUSTOM}/migrations/m.json','[]')"`,
    `node -e "require('fs').writeFileSync('${FH_POSIX}/profiles/acme/approved.json','{}')"`,
    `python -c "open('C:\\\\fh\\\\profiles\\\\acme\\\\approved.json','w')"`,
    `python -c "import os; open(os.path.join(os.environ['FACTORY_HOME'],'profiles','x'),'w')"`,
    `python -c "open('$FACTORY_HOME/profiles/x/approved.json','w')"`,
    `python -c "open('~/.factory/workspaces/w/workspace.json','w')"`,
    `bash -c "python -c \\"open('${CUSTOM}/profiles/x','w')\\""`,
  ];
  for (const role of ROLES) for (const cmd of bad) assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  const script = path.join(TMP, `n2-${process.pid}.py`);
  fs.writeFileSync(script, `open('${CUSTOM}/profiles/acme/approved.json', 'w').write('{}')\n`);
  try {
    assert.ok(denied(guard('builder', bash(`python ${script}`))), 'readable script file');
  } finally {
    fs.rmSync(script, { force: true });
  }
  for (const ok of [`python -c "open('${TMP.replace(/\\/g, '/')}/x.txt','w').write('x')"`,
    `python -c "print(open('${CUSTOM}/profiles/acme/profile.json').read())"`]) {
    assert.ok(!denied(guard('builder', bash(ok))), ok);
  }
  // the main session is not judged by the main-owned net (protect.js does not set it)
  assert.ok(!denied(protect(bash(`python -c "open('${CUSTOM}/profiles/acme/approved.json','w')"`))));
});

test('N6: migrations/ is main-owned, migration-drafts/ is the worker area for maps', () => {
  for (const role of ROLES) assert.ok(denied(guard(role, write(`${FH_POSIX}/migrations/m.json`))), `${role} migrations`);
  assert.ok(!denied(guard('author', write(`${FH_POSIX}/migration-drafts/m.json`))));
  assert.ok(!denied(guard('builder', write(`${FH_POSIX}/migration-drafts/m.json`))));
  assert.ok(denied(guard('auditor', write(`${FH_POSIX}/migration-drafts/m.json`))));
  assert.ok(denied(guard('builder', bash(`echo '[]' > ${FH_POSIX}/migrations/m.json`))));
  assert.ok(denied(guard('builder', bash(`cp m.json ${FH_POSIX}/migrations/m.json`))));
});

test('N4: save-destination names are anchored; ordinary arguments are not judged', () => {
  const mcp = (args, tool = 'mcp__acme__upload_attachment') => ({ tool_name: tool, tool_input: args });
  assert.ok(!denied(protect(mcp({ file_path: `${RUN}/evidence/x.png` }))), 'file_path is the SOURCE of an upload');
  assert.ok(!denied(protect(mcp({ target_path: `${RUN}/evidence/x.png` }))));
  assert.ok(!denied(protect(mcp({ timeout: `${RUN}/run.json` }))));
  assert.ok(!denied(protect(mcp({ layout: `${RUN}/run.json` }))));
  assert.ok(!denied(protect(mcp({ about: `${RUN}/run.json` }))));
  assert.ok(denied(protect(mcp({ save_to: `${RUN}/run.json` }))));
  assert.ok(denied(protect(mcp({ saveTo: `${RUN}/run.json` }))));
  assert.ok(denied(protect(mcp({ output_dir: `${RUN}/evidence` }))));
  assert.ok(denied(protect(mcp({ destination_path: `${RUN}/run.json` }))));
  assert.ok(denied(protect(mcp({ opts: { save_to: `${RUN}/run.json` } }))), 'nested');
  assert.ok(denied(protect(mcp({ items: [{ opts: { output_file: `${RUN}/evidence/a.json` } }] }))), 'nested in an array');
});

test('N5: wrapper flags with values, .exe/.cmd names, here-strings and echo-to-shell bodies', () => {
  const cases = [
    ['env -u FOO factory gate r', 'factory gate r'],
    ['env -C /x factory gate r', 'factory gate r'],
    ['sudo -u bob factory gate r', 'factory gate r'],
    ['sudo -g grp -u bob factory gate r', 'factory gate r'],
    ['timeout 5 factory gate r', 'factory gate r'],
    ['timeout --signal KILL 5 factory gate r', 'factory gate r'],
    ['timeout -s KILL -k 2 5 factory gate r', 'factory gate r'],
    ['stdbuf -o L factory gate r', 'factory gate r'],
    ['stdbuf -oL factory gate r', 'factory gate r'],
    ['stdbuf -i0 -o0 -e0 factory gate r', 'factory gate r'],
    ['nice -n 5 factory gate r', 'factory gate r'],
    ['nice -5 factory gate r', 'factory gate r'],
    ['ionice -c 2 -n 7 factory gate r', 'factory gate r'],
    ['nohup factory gate r', 'factory gate r'],
    ['nohup env -u A timeout 9 factory gate r', 'factory gate r'],
    ['factory.exe gate r', 'factory gate r'],
    ['factory.cmd gate r', 'factory gate r'],
    ['acme-cli.exe issue create', 'acme-cli issue create'],
    ['acme-cli.ps1 issue create', 'acme-cli issue create'],
    ["bash <<< 'factory gate r'", 'factory gate r'],
    ['sh <<< "factory ship plan r"', 'factory ship plan r'],
    ["echo 'factory gate r' | bash", 'factory gate r'],
    ['echo "factory gate r" | sh', 'factory gate r'],
    ['printf "acme-cli issue create" | bash', 'acme-cli issue create'],
  ];
  for (const [cmd, unit] of cases) assert.ok(guards.commandUnits(cmd).includes(unit), `${cmd} -> ${JSON.stringify(guards.commandUnits(cmd))}`);
  const reserved = ['env -u FOO factory gate r', 'timeout --signal KILL 5 factory gate r', 'stdbuf -oL factory gate r',
    'stdbuf -o L factory ship plan r', 'nice -n 5 factory feedback ingest r', "bash <<< 'factory gate r'",
    'sh <<< "factory ship plan r"', "echo 'factory gate r' | bash", 'echo "factory gate r" | sh', 'sudo -u bob factory gate r',
    'env -C /x factory gate r', 'ionice -c 2 factory gate r'];
  for (const role of ROLES) for (const cmd of reserved) assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  assert.ok(!denied(guard('builder', bash('timeout 30 factory prove r --check CHK-1'))));
  assert.ok(!denied(guard('builder', bash("echo 'hello' | cat"))));
});
