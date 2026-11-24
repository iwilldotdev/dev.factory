import test from 'node:test';
import assert from 'node:assert/strict';
import { runHook, denied, bash, RUN, FH, FH_POSIX, WIN, TMP } from './helpers.mjs';

// The "mention + write verb" rule decides by the write's DESTINATION: a run/env path that is only a source or an
// argument is allowed; a protected destination, or one that cannot be determined, is denied.
const protect = (cmd, cwd = '/work', env = {}) => runHook('protect.js', [], bash(cmd, cwd), env);
const guard = (role, cmd, cwd = '/work') => runHook('worker-guard.js', [role], bash(cmd, cwd));
const STORAGE = `${FH_POSIX}/env/ws/React.storage.json`;
const SCRATCH = `${TMP.replace(/\\/g, '/')}/scratch`;

const ALLOW = [
  // observed false positives (runs 9593, 9531 and the scope batch)
  `head -c 300 ${RUN}/evidence/CHK-1.json`,
  `ls ${RUN}`,
  `python -c "import json,sys; d=json.load(open('${RUN}/remote/issue.json')); sys.stdout.write(d['key'])"`,
  `python -c "import json; print(json.load(open('${RUN}/remote/issue.json'))['key'])"`,
  `cp ${STORAGE} ${SCRATCH}/state.json`,
  `cp ${RUN}/run.json ${SCRATCH}/run.json`,
  `sed -i s/a/b/ ${SCRATCH}/f.txt && cat ${RUN}/run.json`,
  `sed -i s/a/b/ ${SCRATCH}/f.txt ${RUN}/remote/x.json`,
  `git checkout main && cat ${STORAGE}`,
  `git -C /other/repo checkout main -- src/a.ts`,
  `cd /other/repo && git checkout -b topic && cp ${RUN}/evidence/CHK-1.json ${SCRATCH}/c.json`,
  `factory run human acme-dev-1 "user: delete discover-task.spec.ts; rm -rf x then cp a ${RUN}/run.json"`,
  `factory run human acme-dev-1 "user: said \\"delete the spec\\" and move it" && cat ${RUN}/run.json`,
  `python -m factory run human acme-dev-1 "user: remove it" --note "rm a"`,
  `rm ${SCRATCH}/old.json && ls ${RUN}/evidence`,
  `cat ${RUN}/run.json | head -3; touch ${SCRATCH}/x`,
];

const DENY = [
  // writes through a redirection after a read
  `cat ${RUN}/run.json > ${RUN}/evidence/x.json`,
  `cat ${RUN}/run.json && echo hi > ${RUN}/run.json`,
  `echo ok; tee ${RUN}/run.json < ${SCRATCH}/a`,
  // a protected destination of a copy/move/delete
  `cp ${SCRATCH}/a.json ${RUN}/evidence/CHK-1.json`,
  `cp -t ${RUN}/evidence ${SCRATCH}/a.json`,
  `cp ${STORAGE} ${FH_POSIX}/env/ws/Other.storage.json`,
  `mv ${RUN}/run.json ${SCRATCH}/x`,
  `rm "${RUN}/evidence/a b.json"`,
  `rsync -a --remove-source-files ${RUN}/evidence/ ${SCRATCH}/x`,
  `install -d ${RUN}/evidence`,
  `robocopy ${SCRATCH}/a ${RUN}/evidence`,
  `robocopy ${RUN}/evidence ${SCRATCH}/b /MOVE`,
  // in-place editors, touch/mkdir/truncate/chmod/ln
  `sed -i s/a/b/ ${RUN}/run.json`,
  `sed -i -e s/a/b/ ${SCRATCH}/f ${RUN}/run.json`,
  `perl -pi -e s/a/b/ ${RUN}/run.json`,
  `awk -i inplace '{print}' ${RUN}/run.json`,
  `vim ${RUN}/run.json`,
  `touch ${RUN}/evidence/x`,
  `mkdir ${RUN}/evidence/sub`,
  `truncate -s 0 ${RUN}/run.json`,
  `chmod 000 ${RUN}/run.json`,
  `ln -sf ${SCRATCH}/a ${RUN}/run.json`,
  // git writing inside the protected tree
  `git -C ${RUN} checkout -- .`,
  `git clean -fd ${RUN}/evidence`,
  `cd ${RUN}/evidence && git checkout .`,
  `cd ${RUN} && git reset --hard`,
  `git checkout main -- ${STORAGE}`,
  // inline interpreter code writing a protected file
  `python -c "open('${RUN}/run.json','w').write('{}')"`,
  `python -c "import shutil; shutil.rmtree('${RUN}/evidence')"`,
  `node -e "require('fs').rmSync('${RUN}/evidence',{recursive:true})"`,
  // chained after the sanctioned writer
  `factory run human acme-dev-1 "x" && rm ${RUN}/evidence/CHK-1.json`,
  `factory run show acme-dev-1; cp ${SCRATCH}/a ${RUN}/run.json`,
  // cannot be determined: variables, substitutions, xargs, eval, nested shells, pipelines
  `cp ${RUN}/run.json $DEST`,
  `cp ${RUN}/run.json $(mktemp)`,
  `D=${RUN}/run.json; cp ${SCRATCH}/a $D`,
  `export D=${RUN}/run.json && rm $D`,
  `ls ${RUN}/evidence | xargs rm`,
  `ls ${RUN}/evidence | xargs -I {} cp {} ${SCRATCH}`,
  `eval "rm ${RUN}/run.json"`,
  `bash -c "cp ${SCRATCH}/a ${RUN}/run.json"`,
  `bash -c 'tar xf ${SCRATCH}/a.tar -C ${RUN}/evidence'`,
  `Get-ChildItem ${RUN}/evidence | Remove-Item`,
  // PowerShell variants
  `Copy-Item C:/tmp/a.json -Destination ${FH}\\runs\\acme-dev-1\\run.json`,
  `Copy-Item -Path C:/tmp/a.json -Destination $env:FACTORY_HOME\\runs\\acme-dev-1\\evidence\\x.json`,
  `Set-Content -Path $env:FACTORY_HOME\\runs\\r\\run.json -Value x`,
  `Remove-Item -Recurse $env:USERPROFILE\\.factory\\runs\\r\\evidence`,
  `Get-Content a | Out-File -FilePath ${FH}\\runs\\r\\metrics.json`,
  `$p='${FH}\\runs\\r\\run.json'; Set-Content $p x`,
  // mixed slashes and home forms of the protected path
  `cp ${SCRATCH}/a ${FH.replace(/\\/g, '/').replace(/\/\.factory/, '\\.factory')}/runs/acme-dev-1\\run.json`,
  `cp ${SCRATCH}/a ~/.factory/runs/r/run.json`,
  `cp ${SCRATCH}/a $HOME/.factory/runs/r/run.json`,
  `cp ${SCRATCH}/a %USERPROFILE%\\.factory\\runs\\r\\run.json`,
  `cp ${SCRATCH}/a \${FACTORY_HOME}/runs/r/run.json`,
];

test('protect: writes whose destination is outside the protected tree are allowed even when a run path is a source', () => {
  for (const cmd of ALLOW) assert.ok(!denied(protect(cmd)), `protect should allow: ${cmd}`);
});

test('protect: a protected, undeterminable or in-place destination stays denied', () => {
  for (const cmd of DENY) assert.ok(denied(protect(cmd)), `protect should deny: ${cmd}`);
});

test('worker guard: same decisions for every writing role', () => {
  for (const role of ['builder', 'author', 'auditor']) {
    // git checkout/switch and `factory run human` have their own worker-guard rules (worker-guard.test.mjs).
    for (const cmd of ALLOW.filter((c) => !c.includes('git ') && !c.includes('run human'))) assert.ok(!denied(guard(role, cmd)), `${role} should allow: ${cmd}`);
    for (const cmd of DENY) assert.ok(denied(guard(role, cmd)), `${role} should deny: ${cmd}`);
  }
});

test('a factory home with spaces: quoted protected paths are matched, sources still pass', { skip: !WIN && 'uses a Windows path' }, () => {
  const env = { FACTORY_HOME: 'C:\\fh test\\.factory' };
  const spaced = 'C:/fh test/.factory/runs/acme-dev-1';
  assert.ok(denied(protect(`rm "${spaced}/evidence/CHK-1.json"`, '/work', env)));
  assert.ok(denied(protect(`cp x.json "C:\\fh test\\.factory\\runs\\acme-dev-1\\run.json"`, '/work', env)));
  assert.ok(denied(protect(`sed -i s/a/b/ "${spaced}/run.json"`, '/work', env)));
  assert.ok(!denied(protect(`cp "${spaced}/run.json" ${SCRATCH}/x.json`, '/work', env)));
});
