import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import { createRequire } from 'node:module';
import { FH, FH_POSIX, TMP, WIN } from './helpers.mjs';

// Decision functions only: no command here is ever executed. Regression table for the write-target rule:
// a command that writes, deletes, moves, aliases or truncates a protected run file must be DENIED however the path is
// spelled and whichever verb or destination form is used; only a literal read source with a literal scratch
// destination (the ALLOW list) passes. The corpus is also compared with the frozen pre-target-rule library.
process.env.FACTORY_HOME = FH;
const require = createRequire(import.meta.url);
const lib = require('../protect-lib.js');
const baseline = require('./fixtures/protect-lib.baseline.js');

const HOME = (process.env.USERPROFILE || process.env.HOME || os.homedir()).replace(/\\/g, '/');
const HOME_WIN = HOME.replace(/\//g, '\\');
const hctx = (l) => ({ home: l.normPath(FH, os.homedir()), cwd: '/work' });
const decide = (l, cmd) => l.bashWriteReason(cmd, hctx(l));
const newDenies = (cmd) => Boolean(decide(lib, cmd));
const oldDenies = (cmd) => Boolean(decide(baseline, cmd));
const SCRATCH = `${TMP.replace(/\\/g, '/')}/scratch`;
const RUN = `${FH_POSIX}/runs/r1`;

// Spellings of the run directory R. Each is a function of the path separator used after it.
const SPELLINGS = [
  { name: 'factory-home forward', R: RUN },
  { name: 'factory-home backslash', R: RUN.replace(/\//g, '\\') },
  { name: 'factory-home mixed', R: RUN.replace('/runs', '\\runs') },
  { name: 'factory-home upper case', R: RUN.toUpperCase(), win: true },
  { name: 'tilde', R: '~/.factory/runs/r1' },
  { name: 'tilde quoted', R: '"~/.factory/runs/r1"' },
  { name: '$HOME', R: '$HOME/.factory/runs/r1' },
  { name: '${HOME}', R: '${HOME}/.factory/runs/r1' },
  { name: '%USERPROFILE%', R: '%USERPROFILE%\\.factory\\runs\\r1' },
  { name: '$env:USERPROFILE', R: '$env:USERPROFILE\\.factory\\runs\\r1' },
  { name: '$env:FACTORY_HOME', R: '$env:FACTORY_HOME\\runs\\r1' },
  { name: 'absolute home forward', R: `${HOME}/.factory/runs/r1` },
  { name: 'absolute home backslash', R: `${HOME_WIN}\\.factory\\runs\\r1` },
  { name: 'git bash drive', R: `${HOME.replace(/^([A-Za-z]):/, (_, d) => `/${d.toLowerCase()}`)}/.factory/runs/r1`, win: true },
  { name: 'home upper case', R: `${HOME}/.factory/runs/r1`.toUpperCase(), win: true },
].filter((s) => WIN || !s.win);

const subst = (tpl, R, sep = '/') => tpl.replace(/@F@/g, `${R}${sep}run.json`).replace(/@E@/g, `${R}${sep}evidence`).replace(/@R@/g, R);

// ---- REGRESSION table: every command denied under every spelling ----
const REGRESSION = [
  'rm @R@/*.json', 'rm -rf @R@/*', 'rm -rf @R@/ev*', 'rm @R@/{run,metrics}.json', 'Remove-Item @R@/* -Recurse',
  'del /s @R@\\*.json', 'mv @R@/*.json /tmp/', 'truncate -s0 @R@/*.json', 'sed -i s/a/b/ @R@/*.json', 'Set-Content -Path @R@/r*.json x',
  'cp /tmp/run.json @R@/', 'cp -r /tmp/evidence @R@', 'Copy-Item -Path /tmp/evidence -Destination @R@ -Recurse',
  'copy /y C:\\tmp\\run.json @R@', 'xcopy C:\\tmp\\x @R@', 'robocopy C:\\tmp @R@ run.json', 'rsync /tmp/run.json @R@/', 'scp /tmp/run.json @R@/',
  'ln /tmp/run.json @R@', 'ni @R@ -Name run.json -Force', 'cp -at @E@ a', 'cp -t@E@ a', 'mv --target-directory=@R@ f', 'install -t @R@ f',
  'ln -t @R@ f', 'Copy-Item a -Destination:@R@', 'Set-Content -Path:@F@ x', 'Out-File -FilePath:@F@', 'Clear-Content -Path:@F@',
  'rm $(echo @F@)', 'rm $(ls -d @E@)/*', 'Remove-Item (Join-Path $HOME .factory/runs/r1/run.json)',
  'ln -s @R@ /tmp/j && rm /tmp/j/run.json', 'mklink /J C:\\tmp\\j @R@', 'New-Item -ItemType Junction -Path C:\\tmp\\j -Target @R@',
  'cp -l @F@ /tmp/x', 'cp -al @R@ /tmp/x', 'cp -rs @R@ /tmp/x', 'chmod -R 000 @R@', "vim -c ':w! @F@' x",
  // odd spellings of the file, each with rm
  'rm @F@.', 'rm @F@ ', 'rm "@F@ "', 'rm @F@::$DATA', "rm $'@F@'", 'rm @R@/run.js$()on', 'rm @R@/run.js${x}on', 'rm @R@/run.json:stream',
  'Remove-Item @F@.', 'del @F@.', 'rm @R@/ru?.json', 'rm @R@/run.[j]son', 'rm `echo @F@`',
  // moves and links take a protected SOURCE
  'mv @F@ /tmp/x', 'Move-Item @F@ C:\\tmp\\x', 'robocopy @R@ C:\\tmp /MOVE', 'rsync --remove-source-files @E@/ /tmp/x',
  'ln @F@ /tmp/x', 'ln -s @F@ /tmp/x', 'mklink C:\\tmp\\x @F@', 'cp -s @F@ /tmp/x',
  // writers and permission changers
  'chmod 000 @F@', 'chown u @F@', 'icacls @F@ /deny x:F', 'attrib +r @F@', 'touch @F@', 'tee @F@', 'Add-Content -Path @F@ x', 'Clear-Content @F@',
  'truncate -s0 @F@', 'sed -i s/a/b/ @F@', 'sed -i s/a/b/ @R@/r*', 'perl -pi -e s/a/b/ @F@', 'vim @F@', 'ed @F@',
  // interpreters
  `python -c "import os,sys; sys.stdout=os.fdopen(os.open('@F@', os.O_WRONLY|os.O_CREAT)); sys.stdout.write('x')"`,
  `python -c "import sys; from pathlib import Path; sys.stdout=Path('@F@').open('w'); sys.stdout.write('x')"`,
  `node -e "const fs=require('fs');const {homedir}=require('os');process.stdout=fs.openSync(homedir()+'/.factory/runs/r1/run.json','w'); process.stdout.write('x')"`,
];
// Spellings that cannot be expressed through the R placeholder (their own home or odd forms of the file).
const FIXED_DENY = [
  'rm C:\\Users\\OWILL~1\\.factory\\runs\\r1\\run.json', 'rm C:\\fh-test\\FACTOR~1\\runs\\r1\\run.json',
  'rm \\\\?\\C:\\Users\\owill\\.factory\\runs\\r1\\run.json', 'rm \\\\localhost\\c$\\Users\\owill\\.factory\\runs\\r1\\run.json',
  'rm ~/.fac\\tory/runs/r1/run.json', "rm ~/.factory/runs/r1/run.json.", 'rm $HOME/.factory/runs/r1/run.json::$DATA',
  'Remove-Item "C:\\Users\\owill\\.factory\\runs\\r1\\run.json."', 'del C:\\Users\\owill\\.factory\\runs\\r1\\run.json ',
  'rm /c/Users/owill/.factory/runs/r1/run.js$()on',
];

// ---- previously fixed false positives that must stay allowed (same list as guard-targets.test.mjs) ----
const STORAGE = `${FH_POSIX}/env/ws/React.storage.json`;
const ALLOW = [
  `head -c 300 ${RUN}/evidence/CHK-1.json`, `ls ${RUN}`,
  `python -c "import json,sys; d=json.load(open('${RUN}/remote/issue.json')); sys.stdout.write(d['key'])"`,
  `python -c "import json; print(json.load(open('${RUN}/remote/issue.json'))['key'])"`,
  `cp ${STORAGE} ${SCRATCH}/state.json`, `cp ${RUN}/run.json ${SCRATCH}/run.json`,
  `sed -i s/a/b/ ${SCRATCH}/f.txt && cat ${RUN}/run.json`, `sed -i s/a/b/ ${SCRATCH}/f.txt ${RUN}/remote/x.json`,
  `git checkout main && cat ${STORAGE}`, `git -C /other/repo checkout main -- src/a.ts`,
  `cd /other/repo && git checkout -b topic && cp ${RUN}/evidence/CHK-1.json ${SCRATCH}/c.json`,
  `factory run human acme-dev-1 "user: delete discover-task.spec.ts; rm -rf x then cp a ${RUN}/run.json"`,
  `factory run human acme-dev-1 "user: said \\"delete the spec\\" and move it" && cat ${RUN}/run.json`,
  `python -m factory run human acme-dev-1 "user: remove it" --note "rm a"`,
  `rm ${SCRATCH}/old.json && ls ${RUN}/evidence`, `cat ${RUN}/run.json | head -3; touch ${SCRATCH}/x`,
  // a heredoc that only feeds text to a non-shell reader
  `cat <<'EOF'\nthen rm ${RUN}/run.json and cp a ${RUN}/evidence/x\nEOF`,
  `git commit -F - <<'EOF'\nfix: do not rm ${RUN}/run.json\nEOF`,
];
const sepOf = (R) => (/\\/.test(R) ? (/\//.test(R) ? null : '\\') : '/');
const ALLOW_SPELLED = SPELLINGS.flatMap(({ R }) => {
  const sep = sepOf(R); // a spelling that mixes both separators is itself an odd spelling: the coarse rule judges it
  if (!sep) return [];
  return [
    `cp ${R}${sep}run.json ${SCRATCH}/run.json`, `cp -r ${R}${sep}evidence ${SCRATCH}/ev`,
    `rsync -a ${R}${sep}evidence${sep} ${SCRATCH}/ev/`, `cat ${R}${sep}run.json > ${SCRATCH}/out.txt`, `ls ${R}${sep}evidence && cat ${R}${sep}run.json`,
  ];
});

test('regression: every write, delete, move, alias or permission change of a protected path is denied, in every spelling', () => {
  const misses = [];
  for (const tpl of REGRESSION) {
    for (const { name, R } of SPELLINGS) {
      for (const sep of ['/', '\\']) {
        const cmd = subst(tpl, R, sep);
        if (!newDenies(cmd)) misses.push(`${name}: ${cmd}`);
      }
    }
  }
  for (const cmd of FIXED_DENY) if (!newDenies(cmd)) misses.push(`fixed: ${cmd}`);
  assert.deepEqual([...new Set(misses)], []);
});

test('regression: literal read sources with a literal scratch destination stay allowed', () => {
  const wrongly = [...ALLOW, ...ALLOW_SPELLED].filter((c) => newDenies(c));
  assert.deepEqual(wrongly, []);
});

// ---- differential safety net against the frozen pre-target-rule library ----
// Every command the baseline DENIES and the current code ALLOWS must belong to a documented false-positive class below.
const VERBS_REMOVE = ['rm', 'rm -rf', 'rm -f', 'del', 'del /q', 'Remove-Item', 'ri', 'rmdir', 'rd /s /q', 'unlink', 'truncate -s0', 'shred',
  'mv', 'Move-Item', 'chmod 600', 'chown u', 'icacls', 'attrib +r', 'touch', 'mkdir', 'tee', 'Set-Content', 'Add-Content', 'Clear-Content',
  'Out-File', 'ni', 'New-Item', 'sed -i s/a/b/', 'perl -pi -e s/a/b/', 'vim', 'nano', 'ln -s'];
const VERBS_COPY = ['cp', 'cp -r', 'cp -a', 'install', 'rsync -a', 'scp', 'copy', 'xcopy', 'robocopy', 'Copy-Item', 'cpi', 'ln'];
const DEST_FORMS = [
  '@F@', '@E@', '@R@', '@R@/', '@R@/*.json', '@R@/ev*', '@R@/{run,metrics}.json', '@F@.', '@F@::$DATA', '@R@/run.js$()on', '@R@/run.js${x}on',
  '$(echo @F@)', '`echo @F@`', "$'@F@'", '@E@/CHK-1.json', '@R@/remote/x.json', '@R@/notes/n.md', '@R@/metrics.json', '@R@/reviews', '@R@/baseline/a',
];

// Destination forms that, used as a SOURCE, are plain literal paths (no glob, expansion or odd spelling).
const PLAIN_SOURCE_FORMS = ['@F@', '@E@', '@R@', '@R@/', '@E@/CHK-1.json', '@R@/remote/x.json', '@R@/notes/n.md', '@R@/metrics.json', '@R@/reviews', '@R@/baseline/a'];

// Literal run-folder paths that are NOT protected (remote/, notes/): the write-target rule allows operating on them.
const UNPROTECTED_FORMS = ['@R@/remote/x.json', '@R@/notes/n.md'];
const TARGET_FLAG_VERBS = ['cp', 'cp -r', 'cp -a', 'install', 'ln'];

function corpus() {
  const out = [];
  for (const cmd of [...ALLOW, ...ALLOW_SPELLED]) out.push({ cmd, cls: 'previously-fixed-false-positive' });
  for (const tpl of [...REGRESSION]) for (const { R } of SPELLINGS) for (const sep of ['/', '\\']) out.push({ cmd: subst(tpl, R, sep), cls: 'regression' });
  for (const cmd of FIXED_DENY) out.push({ cmd, cls: 'regression' });
  for (const { R } of SPELLINGS) {
    for (const d of DEST_FORMS) {
      const D = subst(d, R, '/');
      const cls = UNPROTECTED_FORMS.includes(d) ? 'unprotected-run-file' : 'generated';
      for (const v of VERBS_REMOVE) out.push({ cmd: `${v} ${D}`, cls });
      for (const v of VERBS_COPY) {
        out.push({ cmd: `${v} ${SCRATCH}/s ${D}`, cls });
        if (TARGET_FLAG_VERBS.includes(v)) out.push({ cmd: `${v} -t ${D} ${SCRATCH}/s`, cls });
        // protected path used only as the source, destination literal and outside the tree
        out.push({ cmd: `${v} ${D} ${SCRATCH}/d`, cls: cls === 'generated' ? 'generated-source-read' : cls, form: d, dest: `${SCRATCH}/d`, verb: v });
      }
      out.push({ cmd: `cat x > ${D}`, cls }, { cmd: `echo x >> ${D}`, cls });
    }
    out.push({ cmd: `ls ${R}/evidence | xargs rm`, cls: 'generated' }, { cmd: `for f in ${R}/*; do rm $f; done`, cls: 'generated' });
    out.push({ cmd: `cd ${R} && rm run.json`, cls: 'generated' }, { cmd: `D=${R}/run.json; rm $D`, cls: 'generated' });
    out.push({ cmd: `bash -c "rm ${R}/*.json"`, cls: 'generated' }, { cmd: `echo y | bash`, cls: 'generated' });
  }
  return out;
}

// Documented false-positive classes: the baseline denied, the current code allows, and it is correct to allow.
const DOCUMENTED_FALSE_POSITIVES = {
  // the previously fixed false positives (guard-targets.test.mjs) and the heredoc-as-text class
  'previously-fixed-false-positive': () => true,
  // a literal path inside the run folder that no rule protects (remote/, notes/): the target-aware rule allows it
  'unprotected-run-file': () => true,
  // a protected path that is only a plain literal READ source of a copier whose destination is a literal scratch path
  // (`ln` is not in the class: a link to a protected source aliases the tree.)
  'generated-source-read': (e) => /^(?:cp|cp -r|cp -a|install|rsync -a|scp|copy|xcopy|robocopy|Copy-Item|cpi)$/.test(e.verb)
    && e.dest.startsWith(SCRATCH) && PLAIN_SOURCE_FORMS.includes(e.form),
};

test('differential: nothing the frozen baseline denied is allowed now, except documented false-positive classes', () => {
  const entries = corpus();
  const gained = [];
  const byClass = {};
  let baselineDenied = 0;
  for (const e of entries) {
    if (!oldDenies(e.cmd)) continue;
    baselineDenied += 1;
    if (newDenies(e.cmd)) continue;
    const ok = DOCUMENTED_FALSE_POSITIVES[e.cls];
    if (!(ok && ok(e))) gained.push(e.cmd);
    else byClass[e.cls] = (byClass[e.cls] || 0) + 1;
  }
  assert.ok(entries.length > 3000, `corpus too small: ${entries.length}`);
  assert.deepEqual([...new Set(gained)], [], `${gained.length} of ${baselineDenied} baseline denials are now allowed`);
  process.stdout.write(`# differential corpus ${entries.length}, baseline denied ${baselineDenied}, allowed now (documented classes) ${JSON.stringify(byClass)}\n`);
});
