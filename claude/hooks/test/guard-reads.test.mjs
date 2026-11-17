import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { runHook, denied, bash, write, RUN, FH_POSIX, TMP } from './helpers.mjs';

const protect = (input) => runHook('protect.js', [], input);
const guard = (role, input) => runHook('worker-guard.js', [role], input);

const READS = [
  `cat ${RUN}/run.json`,
  `type ${RUN}/run.json`,
  `head -50 ${RUN}/evidence/CHK-1.json`,
  `tail -n 20 ${RUN}/evidence/CHK-1.json`,
  `grep -rn "remove" ${RUN}/evidence`,
  `grep -rn "rm -rf" ${RUN}/reviews`,
  `rg -n "copy|move|delete" ${RUN}/baseline`,
  `ls ${RUN}/evidence`,
  `dir ${RUN}`,
  `find ${RUN}/evidence -name "*.json" -newer ${RUN}/run.json`,
  `wc -l ${RUN}/run.json`,
  `jq '.phase' ${RUN}/run.json`,
  `jq '.checks[] | select(.id=="rm")' ${RUN}/evidence/CHK-1.json`,
  `cat ${RUN}/evidence/CHK-1.json | jq .status | head -3`,
  `Get-Content ${RUN}/run.json`,
  `Select-String -Path ${RUN}/evidence/*.json -Pattern remove`,
  `ls ${RUN}/evidence | grep tar`,
  `cd ${RUN} && cat run.json | head -5`,
  `cat ${RUN}/acs.json ${RUN}/diagnosis.md`,
  `grep -c copy ${RUN}/checks/CHK-1.spec.ts`,
];

const WRITES = [
  `echo x > ${RUN}/run.json`,
  `cat a.json > ${RUN}/evidence/CHK-1.json`,
  `cat a.json >> ${RUN}/run.json`,
  `cat ${RUN}/run.json | tee ${RUN}/run.json`,
  `grep x ${RUN}/run.json | tee ${RUN}/metrics.json`,
  `cp a.json ${RUN}/evidence/x.json`,
  `rm ${RUN}/evidence/CHK-1.json`,
  `sed -i s/a/b/ ${RUN}/run.json`,
  `find ${RUN}/evidence -delete`,
  `find ${RUN}/evidence -exec rm {} ;`,
  `ls ${RUN} && rm ${RUN}/ship-plan.json`,
  `cat ${RUN}/run.json; mv x ${RUN}/baseline/y`,
  `sort -o ${RUN}/run.json ${RUN}/run.json`,
  `echo '{}' | tee ${RUN}/review-report.json`,
  `python -c "open('${RUN}/run.json','w').write('{}')"`,
  `node -e "require('fs').writeFileSync('${RUN}/evidence/x.json','{}')"`,
  `Set-Content ${RUN}/run.json x`,
];

test('protect allows pure reads of run files (verbs inside patterns are not writes)', () => {
  for (const cmd of READS) assert.ok(!denied(protect(bash(cmd, '/work'))), `should allow: ${cmd}`);
});

test('protect still denies writes to run files', () => {
  for (const cmd of WRITES) assert.ok(denied(protect(bash(cmd, '/work'))), `should deny: ${cmd}`);
});

test('worker guard allows the same reads for every writing role', () => {
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of READS) assert.ok(!denied(guard(role, bash(cmd, '/work'))), `${role} should allow: ${cmd}`);
  }
});

test('worker guard still denies the same writes for every writing role', () => {
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of WRITES) assert.ok(denied(guard(role, bash(cmd, '/work'))), `${role} should deny: ${cmd}`);
  }
});

test('read-only interpreter code that names FACTORY_HOME is allowed; writing code is denied', () => {
  const ok = [
    `python -c "import json,os; print(json.load(open(os.environ['FACTORY_HOME']+'/runs/r/run.json'))['phase'])"`,
    `python -c "import json; d=json.load(open('${RUN}/run.json')); print(d['phase'])"`,
    `node -e "console.log(JSON.parse(require('fs').readFileSync(process.env.FACTORY_HOME+'/runs/r/run.json','utf8')).phase)"`,
    `python - <<EOF\nimport json\nprint(json.load(open('${RUN}/run.json')))\nEOF`,
    `python -c "print(1)" # $FACTORY_HOME`,
  ];
  for (const cmd of ok) {
    assert.ok(!denied(protect(bash(cmd, '/work'))), `protect should allow: ${cmd}`);
    assert.ok(!denied(guard('builder', bash(cmd, '/work'))), `guard should allow: ${cmd}`);
  }
  const bad = [
    `python -c "import os; open(os.environ['FACTORY_HOME']+'/runs/r/run.json','w').write('{}')"`,
    `python -c "from pathlib import Path; Path('${RUN}/evidence/x.json').write_text('{}')"`,
    `node -e "require('fs').writeFileSync(process.env.FACTORY_HOME+'/runs/r/run.json','{}')"`,
    `python - <<EOF\nopen('${RUN}/run.json','w').write('{}')\nEOF`,
    `node -e "require('fs').rmSync(process.env.FACTORY_HOME+'/runs/r/evidence',{recursive:true})"`,
  ];
  for (const cmd of bad) {
    assert.ok(denied(protect(bash(cmd, '/work'))), `protect should deny: ${cmd}`);
    assert.ok(denied(guard('builder', bash(cmd, '/work'))), `guard should deny: ${cmd}`);
  }
});

test('scratch scripts in the temp dir are inspected: reads allowed, run-file writes denied', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dfguard-'));
  try {
    const reader = path.join(dir, 'read.py');
    const writer = path.join(dir, 'write.py');
    const jsWriter = path.join(dir, 'write.js');
    fs.writeFileSync(reader, `import json, os\nd = json.load(open(os.environ['FACTORY_HOME'] + '/runs/r/run.json'))\nprint(d['phase'])\n`);
    fs.writeFileSync(writer, `import os\nopen(os.environ['FACTORY_HOME'] + '/runs/r/run.json', 'w').write('{}')\n`);
    fs.writeFileSync(jsWriter, `require('fs').writeFileSync(process.env.FACTORY_HOME + '/runs/r/evidence/x.json', '{}');\n`);
    assert.ok(!denied(protect(bash(`FACTORY_HOME=${FH_POSIX} python ${reader}`, '/work'))));
    assert.ok(!denied(protect(bash(`python ${reader}`, '/work'))));
    // Whole-blob rule: any write call plus a protected name anywhere is denied, even to an unrelated file.
    const mixed = path.join(dir, 'mixed.py');
    fs.writeFileSync(mixed, `import json, os\nd = json.load(open(os.environ['FACTORY_HOME'] + '/runs/r/run.json'))\nopen('out.txt', 'w').write(d['phase'])\n`);
    assert.ok(denied(protect(bash(`python ${mixed}`, '/work'))));
    assert.ok(!denied(guard('auditor', bash(`python "${reader}"`, '/work'))));
    assert.ok(denied(protect(bash(`python ${writer}`, '/work'))));
    assert.ok(denied(guard('builder', bash(`python ${writer}`, '/work'))));
    assert.ok(denied(protect(bash(`node ${jsWriter}`, '/work'))));
    // A script outside the temp dir is not inspected: still denied when the command names FACTORY_HOME.
    assert.ok(denied(protect(bash(`FACTORY_HOME=${FH_POSIX} python fix.py`, '/work'))));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI-owned and authoring paths keep their owners', () => {
  for (const f of ['run.json', 'evidence/CHK-1.json', 'reviews/code.json', 'baseline/b.json', 'metrics.json', 'ship-plan.json', 'review-report.json', 'review-report.md']) {
    for (const role of ['scout', 'builder', 'author', 'auditor']) {
      assert.ok(denied(guard(role, write(`${RUN}/${f}`))), `${role} write ${f}`);
    }
    assert.ok(denied(protect(write(`${RUN}/${f}`))));
  }
  for (const f of ['acs.json', 'diagnosis.md', 'checks/CHK-1.spec.ts', 'figma/n.json']) {
    assert.ok(!denied(guard('author', write(`${RUN}/${f}`))), `author write ${f}`);
    for (const role of ['scout', 'builder', 'auditor']) assert.ok(denied(guard(role, write(`${RUN}/${f}`))), `${role} write ${f}`);
  }
});

test('runs/_inbox/<anything> is writable by every worker, including scouts', () => {
  const inbox = `${FH_POSIX}/runs/_inbox/sess-1/notes.md`;
  for (const role of ['scout', 'builder', 'author', 'auditor']) {
    assert.ok(!denied(guard(role, write(inbox))), `${role} inbox`);
    assert.ok(!denied(guard(role, write(`${FH_POSIX}/runs/_inbox/sess-1/sub/dir/report.md`))), `${role} nested inbox`);
  }
  assert.ok(!denied(protect(write(inbox))));
  // Even a folder named like a protected one is just scratch there.
  assert.ok(!denied(guard('scout', write(`${FH_POSIX}/runs/_inbox/s/evidence/x.md`))));
  // But no escaping to a real run through the inbox, and the runs dir itself stays protected.
  assert.ok(denied(guard('scout', write(`${FH_POSIX}/runs/_inbox/../acme-dev-1/acs.json`))));
  assert.ok(denied(guard('scout', write(`${FH_POSIX}/runs/other/notes-x.md`))));
  assert.ok(denied(protect(bash(`rm -rf ${FH_POSIX}/runs/_inbox`, '/work'))));
  assert.ok(denied(guard('scout', write(path.join(TMP, 'x.md')))));
});

test('sleep is denied for workers and points to factory wait; factory wait is allowed', () => {
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of ['sleep 3', 'until curl -s localhost:3000; do sleep 3; done', 'echo a && sleep 1', 'Start-Sleep -Seconds 5',
      'timeout /t 5']) {
      const res = guard(role, bash(cmd, '/work'));
      assert.ok(denied(res), `${role}: ${cmd}`);
      assert.match(res.permissionDecisionReason, /factory wait/);
    }
    const waits = ['factory wait --file /tmp/x --timeout 30', 'factory wait --url http://localhost:3000/health --status 2xx',
      'python -m factory wait --file x', 'echo sleeping > /tmp/x'];
    if (role === 'builder') waits.push('factory wait --cmd "curl -s localhost:3000" --until-exit 0 --timeout 60');
    for (const cmd of waits) {
      assert.ok(!denied(guard(role, bash(cmd, '/work'))), `${role}: ${cmd}`);
    }
  }
  assert.ok(denied(guard('scout', bash('sleep 3', '/work'))));
});
