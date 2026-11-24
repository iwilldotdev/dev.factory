import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { runHook, denied, bash, write, FH, FH_POSIX, RUN, WIN, approveProfile } from './helpers.mjs';

const protect = (input, env) => runHook('protect.js', [], input, env);
const gitBashRun = WIN ? RUN.replace(/^([A-Za-z]):/, (_, d) => `/${d.toLowerCase()}`) : RUN;

test('a repo or folder named like dev.factory is not FACTORY_HOME', () => {
  assert.ok(!denied(protect(bash('cd C:/Users/x/Code/dev.factory && python install.py --profile ~/.claude-work --dry-run'))));
  assert.ok(!denied(protect(bash('python C:/Users/x/Code/dev.factory/install.py --help'))));
  assert.ok(denied(protect(bash("python -c \"open('x','w')\" ~/.factory/runs/r/run.json"))));
  assert.ok(denied(protect(bash('python script.py ~/.factory'))));
});

test('fails closed on unreadable input', () => {
  assert.ok(denied(protect('not json')));
  assert.ok(denied(protect('')));
});

test('denies Write/Edit/NotebookEdit into protected run files', () => {
  for (const rel of ['run.json', 'metrics.json', 'ship-plan.json', 'evidence/CHK-1.json',
    'evidence/CHK-3/app.png', 'reviews/code-1.json', 'baseline/React.json']) {
    assert.ok(denied(protect(write(`${RUN}/${rel}`))), rel);
  }
  assert.ok(denied(protect(write(`${RUN}/evidence/x.json`, undefined, 'Edit'))));
  assert.ok(denied(protect({ tool_name: 'NotebookEdit', tool_input: { notebook_path: `${RUN}/reviews/a.ipynb` } })));
});

test('normalises case, separators, Git Bash and relative paths', () => {
  assert.ok(denied(protect(write(`${RUN}/RUN.JSON`.toUpperCase()))));
  assert.ok(denied(protect(write(`${RUN}/evidence/a.json`.replace(/\//g, '\\')))));
  assert.ok(denied(protect(write(`${gitBashRun}/evidence/a.json`))));
  assert.ok(denied(protect(write('evidence/CHK-1.json', RUN))));
  assert.ok(denied(protect(write('../run.json', `${RUN}/checks`))));
  assert.ok(denied(protect(write('~/x/../.factory/runs/r/run.json'), { FACTORY_HOME: '~/.factory' })));
});

test('allows author-owned and other files', () => {
  for (const rel of ['acs.json', 'checks/browser/ac-1.spec.ts', 'checks/style/drawer.json',
    'design/frame-1.json', 'raw/ticket/ACME-1.json', 'raw/design_image/f-1.png', 'notes/scout.md']) {
    assert.ok(!denied(protect(write(`${RUN}/${rel}`))), rel);
  }
  assert.ok(!denied(protect(write(`${FH_POSIX}/workspaces/acme/workspace.json`))));
  assert.ok(!denied(protect(write('C:/code/app/src/run.json'))));
  assert.ok(!denied(protect(write('/repo/evidence/notes.md'))));
});

test('denies shell writes into protected files', () => {
  const cmds = [
    `echo '{}' > ${RUN}/run.json`,
    `echo x >> "${RUN}/evidence/CHK-1.json"`,
    `cat a.json 1> ${RUN}/reviews/qa-1.json`,
    `cat a.json &> ${RUN}/baseline/React.json`,
    `echo x >${RUN}/metrics.json`,
    `echo x | tee ${RUN}/ship-plan.json`,
    `cp /tmp/fake.json ${RUN}/evidence/CHK-1.json`,
    `cp -r /tmp/fake ${RUN}/evidence/`,
    `mv /tmp/fake.json ${RUN}/reviews/code-1.json`,
    `mv ${RUN}/run.json /tmp/x`,
    `rm -rf ${RUN}/evidence`,
    `rm -rf ${RUN}`,
    `rm -rf ${FH_POSIX}/runs`,
    `sed -i 's/fail/pass/' ${RUN}/evidence/CHK-1.json`,
    `touch ${RUN}/evidence/CHK-9.json`,
    `cd ${RUN} && echo x > run.json`,
    `echo x > $FACTORY_HOME/runs/r/run.json`,
    `echo x > "$RUNDIR/evidence/CHK-1.json"`,
    `bash -c "echo x > ${RUN}/run.json"`,
    `python -c "import json; json.dump({}, open('${RUN}/evidence/CHK-1.json','w'))"`,
    `node -e "require('fs').writeFileSync('${RUN}/reviews/code-1.json','{}')"`,
    `ls ${RUN}/evidence | xargs rm`,
    `find ${RUN} -name '*.json' -delete`,
    `echo $(cp a ${RUN}/run.json)`,
    `factory prove r > ${RUN}/evidence/CHK-1.json`,
    `dd if=/dev/zero of=${RUN}/run.json`,
  ];
  for (const cmd of cmds) assert.ok(denied(protect(bash(cmd, '/work'))), cmd);
  assert.ok(denied(protect(bash('echo x > run.json', RUN))));
});

test('denies PowerShell writes into protected files', () => {
  const ps = (command) => ({ tool_name: 'PowerShell', tool_input: { command } });
  assert.ok(denied(protect(ps(`Set-Content -Path "${RUN}/run.json" -Value "{}"`))));
  assert.ok(denied(protect(ps(`"{}" | Out-File ${RUN}/evidence/CHK-1.json`))));
  assert.ok(denied(protect(ps(`Remove-Item -Recurse ${RUN}/reviews`))));
  assert.ok(denied(protect(ps(`Copy-Item a.json -Destination ${RUN}/baseline/x.json`))));
  assert.ok(denied(protect(ps(`Set-Content $env:FACTORY_HOME/runs/r/run.json x`))));
  assert.ok(!denied(protect(ps(`Get-Content ${RUN}/run.json`))));
});

test('allows factory CLI and read-only access', () => {
  const ok = [
    'factory prove acme-dev-1',
    'factory gate acme-dev-1 prove',
    `python -m factory review record acme-dev-1 --kind qa --file ${RUN}/notes/qa.json`,
    `PYTHONPATH=C:/code/dev.factory python -m factory metrics acme-dev-1`,
    `C:/Users/me/.claude/bin/factory.cmd ship plan acme-dev-1`,
    `cat ${RUN}/run.json`,
    `cat ${RUN}/evidence/CHK-1.json | head -5`,
    `ls -la ${RUN}/evidence`,
    `grep -r status ${RUN}/evidence`,
    `echo x > ${RUN}/notes/a.md`,
    'git status 2>&1',
    'npm test > /tmp/out.txt 2>&1',
    `echo x > ${FH}/workspaces/acme/scratch.txt`.replace(/\\/g, '/'),
  ];
  for (const cmd of ok) assert.ok(!denied(protect(bash(cmd, '/work'))), cmd);
});

test('ignores other tools', () => {
  assert.ok(!denied(protect({ tool_name: 'Read', tool_input: { file_path: `${RUN}/run.json` } })));
});

test('denies review-report files for file tools and shell', () => {
  for (const rel of ['review-report.json', 'review-report.md']) {
    assert.ok(denied(protect(write(`${RUN}/${rel}`))), rel);
    assert.ok(denied(protect(bash(`echo x > ${RUN}/${rel}`, '/work'))), rel);
  }
});

test('denies MCP tools whose save_to targets protected run files', () => {
  const mcp = (save_to, tool = 'mcp__acme__get_node') => ({ tool_name: tool, tool_input: { ref: 'k', save_to } });
  for (const rel of ['run.json', 'metrics.json', 'ship-plan.json', 'review-report.json', 'review-report.md',
    'evidence/CHK-1.json', 'reviews/qa-1.json', 'baseline/React.json', 'contracts/ticket/ACME-1.json',
    'contracts/design_frame/f-1.json']) {
    assert.ok(denied(protect(mcp(`${RUN}/${rel}`))), rel);
  }
  assert.ok(denied(protect(mcp(`${RUN}/evidence/`, 'mcp__acme-tickets__fetch_node'))));
  assert.ok(denied(protect(mcp(`${RUN}/EVIDENCE/a.json`))));
  assert.ok(denied(protect(mcp('$FACTORY_HOME/runs/r/run.json'))));
});

test('MCP destination args: every default name is checked, raw/ is the free area', () => {
  const mcp = (args) => ({ tool_name: 'mcp__acme__get_ticket', tool_input: args });
  for (const arg of ['save_to', 'output_path', 'output_file', 'out', 'dest']) {
    assert.ok(denied(protect(mcp({ [arg]: `${RUN}/contracts/ticket/ACME-1.json` }))), `${arg} contracts`);
    assert.ok(denied(protect(mcp({ [arg]: `${RUN}/run.json` }))), `${arg} run.json`);
    assert.ok(denied(protect(mcp({ [arg]: `${RUN}/design/f-1.json` }))), `${arg} design`);
    assert.ok(denied(protect(mcp({ [arg]: `${RUN}/raw/../contracts/x.json` }))), `${arg} traversal`);
    assert.ok(!denied(protect(mcp({ [arg]: `${RUN}/raw/ticket/ACME-1.json` }))), `${arg} raw`);
    assert.ok(!denied(protect(mcp({ [arg]: `${RUN}/raw/design_image/f-1.png` }))), `${arg} raw image`);
  }
  assert.ok(!denied(protect(mcp({}))));
  assert.ok(!denied(protect({ tool_name: 'mcp__x__y', tool_input: { save_to: 'C:/tmp/scratch/a.json' } })));
  // an argument name that is not a destination is not judged
  assert.ok(!denied(protect(mcp({ target: `${RUN}/run.json` }))));
});

test('raw/ is free for shell redirects and file tools; contracts/ is CLI-only', () => {
  assert.ok(!denied(protect(write(`${RUN}/raw/ticket/ACME-1.json`))));
  assert.ok(!denied(protect(bash(`acme-cli ticket get ACME-1 > ${RUN}/raw/ticket/ACME-1.json`, '/work'))));
  assert.ok(!denied(protect(bash(`curl -sS https://api.example/t/ACME-1 > ${RUN}/raw/ticket/ACME-1.json`, '/work'))));
  assert.ok(denied(protect(write(`${RUN}/contracts/ticket/ACME-1.json`))));
  assert.ok(denied(protect(write(`${RUN}/contracts/ticket/ACME-1.json`, undefined, 'Edit'))));
  assert.ok(denied(protect(bash(`echo '{}' > ${RUN}/contracts/ticket/ACME-1.json`, '/work'))));
  assert.ok(denied(protect(bash(`cp /tmp/c.json ${RUN}/contracts/change/api-1.json`, '/work'))));
  assert.ok(denied(protect(bash(`rm -rf ${RUN}/contracts`, '/work'))));
  assert.ok(denied(protect(bash(`echo x > ${RUN}/design/f-1.json`, '/work'))));
  assert.ok(!denied(protect(bash(`factory ingest acme-dev-1 ticket ${RUN}/raw/ticket/ACME-1.json`, '/work'))));
});

test('profile save_args extend the destination names (read from FACTORY_HOME/profiles)', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'fh-save-'));
  try {
    fs.mkdirSync(path.join(home, 'workspaces', 'w1'), { recursive: true });
    fs.writeFileSync(path.join(home, 'workspaces', 'w1', 'workspace.json'), JSON.stringify({ providers: { tickets: 'acme' } }));
    fs.mkdirSync(path.join(home, 'profiles', 'acme'), { recursive: true });
    fs.writeFileSync(path.join(home, 'profiles', 'acme', 'profile.json'), JSON.stringify({ save_args: ['target_file'] }));
    approveProfile(home, 'acme');
    const run = `${home.replace(/\\/g, '/')}/runs/w1-1`;
    const call = { tool_name: 'mcp__acme__get_ticket', tool_input: { target_file: `${run}/run.json` } };
    assert.ok(denied(protect(call, { FACTORY_HOME: home })));
    assert.ok(!denied(protect({ ...call, tool_input: { target_file: `${run}/raw/t.json` } }, { FACTORY_HOME: home })));
    // without the profile the argument name is unknown
    assert.ok(!denied(protect(call, { FACTORY_HOME: path.join(home, 'nowhere') })));
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

const ps = (command) => ({ tool_name: 'PowerShell', tool_input: { command }, cwd: '/work' });

test('denies the audited shell bypasses', () => {
  const cmds = [
    `[IO.File]::WriteAllText("${RUN}/run.json", "{}")`,
    `$p="${RUN}/run.json"; Set-Content $p x`,
    `python - <<EOF
open('${RUN}/run.json','w').write('{}')
EOF`,
    `python fix.py ${FH_POSIX}/runs/r/run.json`,
    `node -e "require('fs').writeFileSync(process.env.FACTORY_HOME+'/runs/r/run.json','{}')"`,
    `curl -o ${RUN}/evidence/CHK-1.json http://x/y`,
    `Invoke-WebRequest http://x -OutFile ${RUN}/evidence/CHK-1.json`,
    `tar -xf a.tar -C ${RUN}/evidence`,
    `Expand-Archive a.zip ${RUN}/evidence`,
    `certutil -decode a.b64 ${RUN}/evidence/x.json`,
    String.raw`Set-Content $env:FACTORY_HOME\runs\r\run.json x`,
    String.raw`cp a.json %FACTORY_HOME%\runs\r\evidence\x.json`,
    `cp a.json $FACTORY_HOME/runs/r/evidence/x.json`,
    `cp a.json ~/.factory/runs/r/evidence/x.json`,
    `node script.js --out ${FH_POSIX}/x`,
    `echo x > ${RUN}/acs.json`,
    `rm -rf ${RUN}/checks/tmp`,
    `cp a ${RUN}/diagnosis.md`,
  ];
  for (const cmd of cmds) assert.ok(denied(protect(bash(cmd, '/work'))), cmd);
  assert.ok(denied(protect(ps(`Set-Content ${RUN.replace(/\//g, '\\')}\\run.json x`))));
  assert.ok(!denied(protect(bash(`python -m factory ship plan x --out ${RUN}/ship-plan.json`, '/work'))));
  assert.ok(!denied(protect(bash(`factory gate x plan # ${RUN}`, '/work'))));
});

test('protect does not block the auditor FACTORY_ROLE command (hooks also run inside subagents)', () => {
  for (const cmd of ['FACTORY_ROLE=auditor factory review record x --kind code --file /tmp/c.json',
    "$env:FACTORY_ROLE='auditor'; factory review record x --kind qa --file C:/tmp/q.json"]) {
    assert.ok(!denied(protect(bash(cmd, '/work'))), cmd);
    assert.ok(!denied(protect(ps(cmd))), cmd);
  }
  assert.ok(!denied(protect(bash('echo hello', '/work'))));
});

test('author-owned run files: file tools allowed, MCP saves only into raw/', () => {
  const mcp = (save_to) => ({ tool_name: 'mcp__acme__get_node', tool_input: { save_to } });
  for (const rel of ['acs.json', 'diagnosis.md', 'checks/browser/a.ts', 'design/1-2.json']) {
    assert.ok(!denied(protect(write(`${RUN}/${rel}`))), rel);
    assert.ok(denied(protect(mcp(`${RUN}/${rel}`))), rel);
  }
  assert.ok(!denied(protect(mcp(`${RUN}/raw/design_frame/1-2.json`))));
  assert.ok(denied(protect(mcp(`${RUN}/raw/../run.json`))));
});

test('env pid and storage records are CLI-owned (file tools, shell, MCP save_to)', () => {
  const pid = `${FH_POSIX}/env/acme/web.pid`;
  const storage = `${FH_POSIX}/env/acme/web.storage.json`;
  for (const f of [pid, storage]) {
    assert.ok(denied(protect(write(f))), f);
    assert.ok(denied(protect(bash(`echo 1 > ${f}`, '/work'))), f);
    assert.ok(denied(protect(bash(`Set-Content ${f} x`, '/work'))), f);
    assert.ok(denied(protect({ tool_name: 'mcp__acme__fetch_x', tool_input: { save_to: f } })), f);
  }
  assert.ok(!denied(protect(write(`${FH_POSIX}/env/acme/notes.txt`))));
  assert.ok(!denied(protect(bash(`cat ${pid}`, '/work'))));
});
