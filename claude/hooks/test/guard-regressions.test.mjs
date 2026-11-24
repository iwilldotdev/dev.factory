import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
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
// The auditor's corpus targets the default home (~/.factory) from a project folder, as its harness did.
const auditCtx = { home: lib.normPath('~/.factory', os.homedir()), cwd: `${HOME}/Code/proj` };
// Decisions are memoised: the corpora are judged by several tests.
const memo = new Map();
const decide = (l, cmd, c = hctx(l)) => {
  const key = `${l === lib ? 'n' : 'b'}|${c.home}|${c.cwd}|${cmd}`;
  if (!memo.has(key)) memo.set(key, l.bashWriteReason(cmd, c));
  return memo.get(key);
};
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
  // third audit, M1: the verb hidden behind a name defined in another segment (function, alias, positional parameters)
  'zap() { rm -rf "$1"; }; zap @E@', 'q() { rm "$@"; }; q @F@', 'function q { rm -f "$1"; }; q @F@',
  'function z() { Remove-Item -Recurse -Force $args[0] }; z @E@', 'Set-Alias wipe Clear-Content; wipe @F@', 'sal wipe Remove-Item; wipe @F@',
  'New-Alias wipe Remove-Item; wipe @F@', 'set -- rm -rf; "$@" @E@',
  // M2: xargs -I completes the protected name
  'echo run.json | xargs -I% cp /tmp/x @R@/%', 'echo run.json | xargs -iX rm @R@/X', 'cat /tmp/list | xargs -I@ rm @R@/@',
  'echo run.json | xargs --replace=X rm @R@/X',
  // M6: shells whose body the lexer cannot judge; M7: Push-Location; a subshell around a cd
  'cmd /c del @F@', 'cmd /c "del @F@"', 'Push-Location @R@; Remove-Item run.json', 'Push-Location @R@; rm run.json',
  'Push-Location -Path @R@; Clear-Content run.json',
  // fourth audit (round 3 of the provably-safe grammar). R2: PowerShell 5.1 re-quotes a native argument with spaces
  // without escaping its inner quotes (sed then sees -i), and strips the inner quotes of python -c code
  `sed s/orig/forged/ 'q "q -i" q' @F@`, `sed -n s/orig/forged/ 'q "q -i" q' @F@`, `sed s/orig/forged/ "q 'q -i' q" @F@`, `cp '-t' @E@ ${SCRATCH}/x`,
  `python -c 'print("exec(bytes([111]).decode())")'; cp @R@/remote/x ${SCRATCH}/y`,
  `python -c 'print("getattr(open(chr(120),chr(119)),chr(119))")'; cp @R@/remote/x ${SCRATCH}/y`,
  // R3: the real path is checked before an earlier segment moves or copies a folder holding a junction into the run
  `mv ${SCRATCH}/D ${SCRATCH}/E && cp ${SCRATCH}/forged.json ${SCRATCH}/E/J/run.json && cp @R@/remote/x ${SCRATCH}/y`,
  `mv ${SCRATCH}/D ${SCRATCH}/E; cp ${SCRATCH}/forged.json ${SCRATCH}/E/J/run.json; cp @R@/remote/x ${SCRATCH}/y`,
  `cp -r ${SCRATCH}/D ${SCRATCH}/E && cp ${SCRATCH}/forged.json ${SCRATCH}/E/J/run.json && cp @R@/remote/x ${SCRATCH}/y`,
  `cp -a ${SCRATCH}/D ${SCRATCH}/E && cp ${SCRATCH}/forged.json ${SCRATCH}/E/J/x.json && cp @F@ ${SCRATCH}/y`,
  // R4: a repository whose core.worktree is the run folder
  `git -C ${SCRATCH}/repo checkout -f HEAD -- run.json && cp @R@/remote/x ${SCRATCH}/y`, `git -C ${SCRATCH}/repo checkout -f HEAD -- run.json; cp @F@ ${SCRATCH}/y`,
  // minors: rmdir -p walks up, a bare cd (USERPROFILE vs $HOME), non-ASCII letters in a write target, PowerShell's cp -L
  // (-LiteralPath swaps source and destination) and -pv, cmd.exe metacharacters in factory.cmd data, the run folder as target
  'rmdir -p @R@/remote/a', 'cd && cp @R@/remote/x x.json && cat @F@', `cp @R@/remote/x ${SCRATCH}/evıdence && cat @F@`,
  `cp @F@ -L ${SCRATCH}/x`, `cp -pv @F@ ${SCRATCH}/x`, `factory run show r1 'x&echo>@F@'; cp @R@/remote/x ${SCRATCH}/y`,
  `factory.cmd run show r1 'a" & echo x > @F@ & "'; cp @R@/remote/x ${SCRATCH}/y`, 'cp /tmp/x @R@/remote/y && cat @F@',
  'sed -i s/a/b/ @R@/notes/n.md && cat @F@', 'cat @F@ > @R@/remote/copy.json && cp @F@ /tmp/x',
  // sixth audit: a scratch write target that MSYS resolves through a link node cannot see (C: a `ck.exe` twin cookie,
  // B: an explicit `ck.lnk` shortcut). Denied whatever is on disk: no file write target is provably safe any more.
  `cp @R@/remote/x ${SCRATCH}/ck`, `cp @R@/remote/x ${SCRATCH}/z && cat ${SCRATCH}/forged.json > ${SCRATCH}/ck`,
  `cp @R@/remote/x ${SCRATCH}/z && cat ${SCRATCH}/forged.json > ${SCRATCH}/ck.lnk`, `cp @F@ ${SCRATCH}/ck.lnk`,
];
// Third audit, spelled with this machine's home: junction aliases (M3), bash escapes in all-backslash paths (M4), odd
// spellings attached to a flag value (M5), cmd.exe carets and quote loss (M6). The junction rows need the junction.
const U = path.basename(HOME);
const UPPER = path.basename(HOME).toUpperCase();
const M_HOME = WIN ? [
  `rm C:\\\\Users\\\\${U}\\\\.factory\\\\runs\\\\r1\\\\run.jso\\n`, `rm -rf C:\\\\Users\\\\${U}\\\\.factory\\\\runs\\\\r1\\\\evidenc\\e`,
  `rm C:\\\\Users\\\\${U.slice(0, 3)}\\${U.slice(3)}\\\\.factory\\\\runs\\\\r1\\\\run.json`, `rm /c/Users/${U}/.factory/runs/r1/run.jso\\n`,
  `dd if=/dev/zero of=//localhost/c$/Users/${U}/.factory/runs/r1/run.json`, `dd of=//127.0.0.1/C$/Users/${U}/.factory/runs/r1/run.json if=/dev/null`,
  `cp -t//127.0.0.1/c$/Users/${U}/.factory/runs/r1/evidence /tmp/x`, `cp /tmp/x --target-directory=//localhost/c$/Users/${U}/.factory/runs/r1/evidence`,
  `Set-Content -Path://localhost/c$/Users/${U}/.factory/runs/r1/run.json -Value x`,
  `Copy-Item C:/tmp/x -Destination:\\\\localhost\\c$\\Users\\${U}\\.factory\\runs\\r1\\run.json`,
  `Out-File -FilePath://localhost/c$/Users/${U}/.factory/runs/r1/run.json`, `dd if=/dev/zero of=~${U}/.factory/runs/r1/run.json`,
  `cmd /c del C:\\Users\\${U.slice(0, 3)}^${U.slice(3)}\\.factory\\runs\\r1\\run.json`, `cmd /c del C:\\Users\\${U}\\.fac^tory\\runs\\r1\\run.json`,
  `cmd /c del "C:/Users/All Users/../${U}/.factory/runs/r1/run.json"`, `rm C:/Users/${UPPER.slice(0, 6)}~1/.factory/runs/r1/run.json`,
  `rm "C:/Users/All Users/../${U}/.factory/runs/r1/run.json"`, `cp /tmp/x "C:/Users/All Users/../${U}/.factory/runs/r1/run.json"`,
  // fourth audit R1: the Git Bash /proc/cygdrive mount is another name of the drive (as the write target of rm, cp, mv,
  // sed -i, touch, and through a cd). Editors and redirections there were allowed before the grammar too (not covered here).
  ...['c', 'C'].flatMap((d) => {
    const C = HOME.replace(/^([A-Za-z]):/, `/proc/cygdrive/${d}`);
    const F = `${C}/.factory/runs/r1`;
    return [
      `rm ${F}/run.json`, `cp /tmp/forged.json ${F}/run.json`, `mv /tmp/forged.json ${F}/run.json`, `sed -i s/orig/forged/ ${F}/run.json`,
      `touch ${F}/evidence/CHK-9.json`, `rm -rf ${F}/evidence`, `rm -r ${F}`, `cp -r /tmp/ev ${F}/evidence`,
      `cp /tmp/forged.json ${F}/run.json && cat ~/.factory/runs/r1/run.json`, `cd ${C} && rm .factory/runs/r1/run.json`,
      `cd ${C} && cp /tmp/x .factory/runs/r1/run.json && cat ~/.factory/runs/r1/run.json`,
    ];
  }),
  ...(fs.existsSync('C:/Documents and Settings') ? [
    `rm "C:/Documents and Settings/${U}/.factory/runs/r1/run.json"`, `rm -rf "C:/Documents and Settings/${U}/.factory/runs/r1/evidence"`,
    `cat C:/x && cp /tmp/x "C:/Documents and Settings/${U}/.factory/runs/r1/run.json"`,
    `sed -i s/a/b/ "C:/Documents and Settings/${U}/.factory/runs/r1/run.json"`,
  ] : []),
] : [];
// Spellings that cannot be expressed through the R placeholder (their own home or odd forms of the file).
const FIXED_DENY = [
  'rm C:\\Users\\OWILL~1\\.factory\\runs\\r1\\run.json', 'rm C:\\fh-test\\FACTOR~1\\runs\\r1\\run.json',
  'rm \\\\?\\C:\\Users\\owill\\.factory\\runs\\r1\\run.json', 'rm \\\\localhost\\c$\\Users\\owill\\.factory\\runs\\r1\\run.json',
  'rm ~/.fac\\tory/runs/r1/run.json', "rm ~/.factory/runs/r1/run.json.", 'rm $HOME/.factory/runs/r1/run.json::$DATA',
  'Remove-Item "C:\\Users\\owill\\.factory\\runs\\r1\\run.json."', 'del C:\\Users\\owill\\.factory\\runs\\r1\\run.json ',
  'rm /c/Users/owill/.factory/runs/r1/run.js$()on',
  // sixth audit, env storage as the source: C1 `.exe` twin cookie, B2 explicit `.lnk` shortcut, E1 Interix `IntxLNK` cookie
  ...[`${FH_POSIX}/env/ws/React.storage.json`, '~/.factory/env/ws/React.storage.json'].flatMap((src) => [
    `cp ${src} ${SCRATCH}/ck`, `cp ${src} ${SCRATCH}/ck.lnk`, `cp ${src} ${SCRATCH}/ik`,
  ]),
];

// ---- previously fixed false positives that must stay allowed (same list as guard-targets.test.mjs) ----
const STORAGE = `${FH_POSIX}/env/ws/React.storage.json`;
const ALLOW = [
  `head -c 300 ${RUN}/evidence/CHK-1.json`, `ls ${RUN}`,
  `python -c "import json; print(json.load(open('${RUN}/remote/issue.json'))['key'])"`,
  `cat ${STORAGE} > ${SCRATCH}/state.json`, `cat ${RUN}/remote/x > ${SCRATCH}/x`,
  `sed -i s/a/b/ ${SCRATCH}/f.txt && cat ${RUN}/run.json`, // the frozen baseline allows it (no grammar involved)
  `git checkout main && cat ${STORAGE}`, `git -C /other/repo checkout main -- src/a.ts`,
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
  return [`cat ${R}${sep}run.json > ${SCRATCH}/out.txt`, `ls ${R}${sep}evidence && cat ${R}${sep}run.json`];
});
// Documented false positives: once allowed on this branch, now outside the provably-safe grammar (python -c, git, rsync,
// cp, sed -i, any file write target), so the baseline denial stands. Use `cat <file> > <dest>` as its own command, the
// Read tool, or the Edit/Write tools.
const STILL_DENIED = [
  `cp ${STORAGE} ${SCRATCH}/state.json`, `cp ${RUN}/run.json ${SCRATCH}/run.json`,
  ...SPELLINGS.flatMap(({ R }) => (sepOf(R) ? [`cp ${R}${sepOf(R)}run.json ${SCRATCH}/run.json`, `cp -r ${R}${sepOf(R)}evidence ${SCRATCH}/ev`] : [])),
  `python -c "import json,sys; d=json.load(open('${RUN}/remote/issue.json')); sys.stdout.write(d['key'])"`,
  `sed -i s/a/b/ ${SCRATCH}/f.txt ${RUN}/remote/x.json`,
  `cd /other/repo && git checkout -b topic && cp ${RUN}/evidence/CHK-1.json ${SCRATCH}/c.json`,
  ...SPELLINGS.flatMap(({ R }) => (sepOf(R) ? [`rsync -a ${R}${sepOf(R)}evidence${sepOf(R)} ${SCRATCH}/ev/`] : [])),
];

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
  for (const cmd of M_HOME) if (!newDenies(cmd) || !decide(lib, cmd, auditCtx)) misses.push(`home: ${cmd}`);
  assert.deepEqual([...new Set(misses)], []);
});

test('regression: literal read sources with a literal scratch destination stay allowed', () => {
  const wrongly = [...ALLOW, ...ALLOW_SPELLED].filter((c) => newDenies(c));
  assert.deepEqual(wrongly, []);
});

test('regression: documented false positives stay denied (forms outside the grammar keep the baseline verdict)', () => {
  assert.deepEqual(STILL_DENIED.filter((c) => !newDenies(c) || !oldDenies(c)), []);
});

// ---- differential safety net against the frozen pre-target-rule library ----
// Every command the baseline DENIES and the current code ALLOWS must be provably safe (protect-lib provablySafe) AND fall
// in one of the classes below, whose checkers are written here independently of the library's grammar.
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
  for (const cmd of [...FIXED_DENY, ...M_HOME]) out.push({ cmd, cls: 'regression' });
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

// ---- classes of allowed baseline denials, checked without the library ----
// A gain (baseline denies, current code allows) is legitimate only when EVERY segment of the command is one of these
// kinds; the class of a command is the set of its segment kinds. The only gains left: read-only commands (output only
// to /dev/null), factory CLI data, and a heredoc that is data. No file write target is ever a gain.
const writable = (p) => p === '/dev/null' || (WIN && /^nul$/i.test(p));

/** Top-level segments split on unquoted ; && || | and newlines; null on substitutions, groups or heredocs. */
function splitTop(input) {
  const cmd = input.replace(/\$\{(HOME|FACTORY_HOME)\}/g, '$$$1');
  if (/\$\(|`|<<|[<>]\(/.test(cmd)) return null;
  const segs = [];
  let cur = '';
  let q = '';
  for (let i = 0; i < cmd.length; i += 1) {
    const c = cmd[i];
    if (q) { if (c === q) q = ''; cur += c; continue; }
    if (c === "'" || c === '"') { q = c; cur += c; continue; }
    if ('(){}'.includes(c)) return null;
    if (';\n|&'.includes(c) && !(c === '&' && cmd[i - 1] === '>') && !(c === '&' && cmd[i + 1] === '>')) {
      segs.push(cur);
      cur = '';
      if (cmd[i + 1] === c) i += 1;
      continue;
    }
    cur += c;
  }
  segs.push(cur);
  return segs.map((s) => s.trim()).filter(Boolean);
}

const unquote = (w) => w.replace(/^"([^"]*)"$|^'([^']*)'$/, '$1$2');
const SEGMENT_KINDS = {
  'read-only': (w) => /^(?:cat|head|tail|ls|grep|jq|wc|echo|stat|type|get-content|gc|test-path|select-string)$/i.test(w[0])
    || (/^(?:sort|diff)$/.test(w[0]) && !w.some((a) => /^-[^-]*[oT]|^--(?:output|temporary|compress)/.test(a)))
    || (/^(?:gzip|gunzip)$/.test(w[0]) && w.some((a) => /^-[a-z]*c[a-z]*$/.test(a)))
    || (w[0] === 'sed' && !w.some((a) => /^-[^-]*i|^--in-place/.test(a)) && /^s\/[^/]*\/[^/]*\/[gI0-9]*$/.test(unquote(w.find((a, i) => i > 0 && !/^-/.test(a)) || ''))),
  'factory-data': (w) => /^factory(?:\.cmd)?$/i.test(w[0]) || (/^python3?$/.test(w[0]) && w[1] === '-m' && w[2] === 'factory'),
};
// A file write the shell or the command makes: cp/Copy-Item, sed -i, or a redirection to anything but /dev/null or NUL.
// Quoted strings are data (a quoted redirection target is still a target: it becomes Q).
function writesFile(input) {
  const cmd = input.replace(/"[^"]*"|'[^']*'/g, 'Q');
  return /(?:^|[\s;&|(])(?:cp|copy-item|cpi|copy)\s/i.test(cmd) || /(?:^|[\s;&|(])sed\s(?:[^;&|\n]*\s)?(?:-[^-\s]*i|--in-place)/.test(cmd)
    || [...cmd.matchAll(/\d?>>?\s*(&?\S*)/g)].some((m) => !/^&\d$/.test(m[1]) && !writable(m[1]));
}

/** The class of a command (sorted segment kinds) or null when one segment fits no kind or redirects outside a safe root. */
function classOf(cmd, c) {
  const heredoc = cmd.match(/^((?:cat|git commit -F -|gh [\w ]+--body-file -))\s*<<'(\w+)'\n[\s\S]*\n\2\s*$/);
  if (heredoc) return 'heredoc-data';
  const segs = splitTop(cmd);
  if (!segs) return null;
  const kinds = new Set();
  for (const seg of segs) {
    const outs = [...seg.matchAll(/(?:^|\s)\d?>>?\s*(\S+)/g)].map((m) => m[1]).filter((t) => !/^&\d$/.test(t));
    if (!outs.every((t) => writable(unquote(t)))) return null;
    const w = (seg.replace(/(?:^|\s)\d?>>?\s*\S+/g, ' ').match(/"[^"]*"|'[^']*'|\S+/g) || []);
    if (!w.length) return null;
    const kind = Object.keys(SEGMENT_KINDS).find((k) => SEGMENT_KINDS[k](w));
    if (!kind) return null;
    if (outs.length && kind !== 'read-only') return null; // redirections (to /dev/null) only on readers
    kinds.add(kind);
  }
  return [...kinds].sort().join('+');
}

const auditCorpus = require('./fixtures/audit-corpus.cjs');

function differential(entries, c) {
  const gained = [];
  const writing = [];
  const byClass = {};
  let baselineDenied = 0;
  for (const cmd of entries) {
    if (!decide(baseline, cmd, c)) continue;
    baselineDenied += 1;
    if (decide(lib, cmd, c)) continue;
    const cls = classOf(cmd, c);
    const safe = lib.provablySafe(cmd, c) || (cls === 'heredoc-data' && lib.provablySafe(lib.stripDataHeredoc(cmd), c))
      || (cls === 'heredoc-data' && !decide(baseline, lib.stripDataHeredoc(cmd), c));
    // a heredoc body is data: only its first line can write
    if (writesFile(cls === 'heredoc-data' ? cmd.split('\n')[0] : cmd)) writing.push(cmd);
    if (!(safe && cls)) gained.push(cmd);
    else byClass[cls] = (byClass[cls] || 0) + 1;
  }
  return { gained, writing, byClass, baselineDenied };
}

test('the gain check sees every file write form', () => {
  for (const w of ['cp a b', 'x && cp a b', 'Copy-Item a b', 'sed -i s/a/b/ f', 'sed -n -i s/a/b/ f', 'cat a > b', 'cat a >> /tmp/b', 'cat a 2> b',
    'cat a > nul.txt', 'cat a > "b"']) assert.ok(writesFile(w), w);
  for (const r of ['cat a', 'cat a > /dev/null', 'cat a 2>/dev/null', 'cat a 2>&1', 'sed -n s/a/b/ f', 'factory run human r "x; cp a b > c"']) {
    assert.ok(!writesFile(r), r);
  }
});

test('differential (own corpus): every baseline denial allowed now is provably safe and in a checked class', () => {
  const entries = corpus().map((e) => e.cmd);
  const { gained, writing, byClass, baselineDenied } = differential(entries, hctx(lib));
  assert.ok(entries.length > 3000, `corpus too small: ${entries.length}`);
  assert.deepEqual([...new Set(gained)], [], `${gained.length} of ${baselineDenied} baseline denials are now allowed outside the classes`);
  assert.deepEqual([...new Set(writing)], [], 'a gain writes a file (cp, sed -i or a redirection to a file)');
  process.stdout.write(`# differential own corpus ${entries.length}, baseline denied ${baselineDenied}, allowed now by class ${JSON.stringify(byClass)}, unexplained 0\n`);
});

test('differential (audit corpus): every baseline denial allowed now is provably safe and in a checked class', () => {
  const entries = auditCorpus(HOME).map((p) => p.cmd);
  const { gained, writing, byClass, baselineDenied } = differential(entries, auditCtx);
  assert.ok(entries.length > 6000, `corpus too small: ${entries.length}`);
  assert.deepEqual([...new Set(gained)], [], `${gained.length} of ${baselineDenied} baseline denials are now allowed outside the classes`);
  assert.deepEqual([...new Set(writing)], [], 'a gain writes a file (cp, sed -i or a redirection to a file)');
  process.stdout.write(`# differential audit corpus ${entries.length}, baseline denied ${baselineDenied}, allowed now by class ${JSON.stringify(byClass)}, unexplained 0\n`);
});

test('property: a regression row or a baseline-denied corpus command is never provably safe unless it is in a checked class', () => {
  const bad = [];
  const rows = [...corpus().map((e) => [e.cmd, hctx(lib)]), ...M_HOME.map((cmd) => [cmd, auditCtx]),
    ...auditCorpus(HOME).map((p) => [p.cmd, auditCtx])];
  for (const [cmd, c] of rows) if (decide(baseline, cmd, c) && lib.provablySafe(cmd, c) && !classOf(cmd, c)) bad.push(cmd);
  for (const tpl of REGRESSION) {
    for (const { R } of SPELLINGS) {
      for (const sep of ['/', '\\']) {
        const cmd = subst(tpl, R, sep);
        if (lib.provablySafe(cmd, hctx(lib)) && !classOf(cmd, hctx(lib))) bad.push(cmd);
      }
    }
  }
  assert.deepEqual([...new Set(bad)], []);
});

