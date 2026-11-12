import test from 'node:test';
import assert from 'node:assert/strict';
import { runHook, denied, bash, write, FH, FH_POSIX, RUN, WIN } from './helpers.mjs';

const protect = (input, env) => runHook('protect.js', [], input, env);
const gitBashRun = WIN ? RUN.replace(/^([A-Za-z]):/, (_, d) => `/${d.toLowerCase()}`) : RUN;

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
    'figma/3544-5710.json', 'figma/meta.json', 'remote/issue-DEV-1.json', 'notes/scout.md']) {
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
    `cp ${RUN}/evidence/CHK-3/app.png /tmp/app.png`,
    `ls -la ${RUN}/evidence`,
    `grep -r status ${RUN}/evidence`,
    `python -c "import json; print(json.load(open('${RUN}/run.json'))['phase'])"`,
    `echo x > ${RUN}/notes/a.md`,
    `echo x > ${RUN}/acs.json`,
    `rm -rf ${RUN}/checks/tmp`,
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
  const mcp = (save_to, tool = 'mcp__dev_mcp__figma_get_design_context') => ({ tool_name: tool, tool_input: { fileKey: 'k', save_to } });
  for (const rel of ['run.json', 'metrics.json', 'ship-plan.json', 'review-report.json', 'review-report.md',
    'evidence/CHK-1.json', 'reviews/qa-1.json', 'baseline/React.json']) {
    assert.ok(denied(protect(mcp(`${RUN}/${rel}`))), rel);
  }
  assert.ok(denied(protect(mcp(`${RUN}/evidence/`, 'mcp__dev-mcp__figma_fetch_screenshot'))));
  assert.ok(denied(protect(mcp(`${RUN}/EVIDENCE/a.json`))));
  assert.ok(denied(protect(mcp('$FACTORY_HOME/runs/r/run.json'))));
});

test('allows MCP save_to into agent-owned run folders and calls without save_to', () => {
  const mcp = (save_to) => ({ tool_name: 'mcp__dev_mcp__jira_get_issue', tool_input: save_to ? { save_to } : {} });
  for (const rel of ['figma/3544-5710.json', 'figma/meta.json', 'figma/3544-5710.png', 'remote/issue-DEV-1.json',
    'remote/mr-app-main.json']) {
    assert.ok(!denied(protect(mcp(`${RUN}/${rel}`))), rel);
  }
  assert.ok(!denied(protect(mcp())));
  assert.ok(!denied(protect({ tool_name: 'mcp__x__y', tool_input: { save_to: 'C:/tmp/dev.mcp/a.json' } })));
});
