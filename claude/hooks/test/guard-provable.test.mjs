import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { FH, FH_POSIX, TMP, WIN, runHook, denied, bash } from './helpers.mjs';

// The positive grammar (provablySafe) that is the only way out of a coarse denial, rule by rule. Each block names the
// rule and pairs a SAFE control with variants that differ by that one construct and must not be provably safe; a
// variant the frozen baseline denies must stay denied (one it allowed is a pre-existing gap, not this rule's job).
// Decision functions only: no command here is ever executed.
process.env.FACTORY_HOME = FH;
const require = createRequire(import.meta.url);
const lib = require('../protect-lib.js');
const baseline = require('./fixtures/protect-lib.baseline.js');
const ctx = { home: lib.normPath(FH, os.homedir()), cwd: '/work' };
const safe = (cmd, c = ctx) => lib.provablySafe(cmd, c);
const denies = (cmd, c = ctx) => Boolean(lib.bashWriteReason(cmd, c));
const oldDenies = (cmd, c = ctx) => Boolean(baseline.bashWriteReason(cmd, c));
const R = `${FH_POSIX}/runs/r1`;
const S = `${TMP.replace(/\\/g, '/')}/scratch`;

function rule(name, control, variants) {
  test(`grammar: ${name}`, () => {
    for (const c of [].concat(control)) {
      assert.ok(safe(c), `control should be provably safe: ${JSON.stringify(c)}`);
      assert.ok(!denies(c), `control should be allowed: ${JSON.stringify(c)}`);
    }
    for (const v of variants) {
      assert.ok(!safe(v), `should NOT be provably safe: ${JSON.stringify(v)}`);
      if (oldDenies(v)) assert.ok(denies(v), `should be denied: ${JSON.stringify(v)}`);
    }
  });
}

test('the frozen baseline inside protect-lib.js is byte-identical to the test oracle', () => {
  const live = fs.readFileSync(new URL('../protect-lib.js', import.meta.url), 'utf8');
  const oracle = fs.readFileSync(new URL('./fixtures/protect-lib.baseline.js', import.meta.url), 'utf8');
  const start = '// >>> frozen baseline\n';
  const block = live.slice(live.indexOf(start) + start.length, live.indexOf('\n// <<< frozen baseline'));
  assert.equal(block, oracle.slice(oracle.indexOf("'use strict';")).replace(/\n$/, ''));
});

test('a baseline denial that the later rules relaxed stays denied unless the grammar accepts the command', () => {
  // the later interpreter rule ignores sys.stdout.write; the baseline did not, and python -c is outside the grammar
  const cmd = `python -c "import os,sys; sys.stdout.write(os.environ['FACTORY_HOME'] + '/runs/r1/run.json')"`;
  assert.ok(oldDenies(cmd));
  assert.ok(!safe(cmd));
  assert.ok(denies(cmd));
});

// Documented false positives: forms the grammar no longer accepts (rm, rmdir, mkdir, touch, mv, rsync, git, python -c,
// cp, sed -i, any redirection to a file, a quoted word with a space). The baseline denied each, so each stays denied;
// the remedy is `cat <file> > <dest>` as its own command, the Read tool, or the Edit/Write tools.
const STILL_DENIED = [
  // once allowed through a write target in a safe root: no file write target is provably safe any more
  `cp ${R}/run.json ${S}/x.json`, `cp -r ${R}/evidence ${S}/ev`, `cp ${R}/remote/x ${S}/y`, `cp ${FH_POSIX}/env/ws/React.storage.json ${S}/x.json`,
  `sed -i s/a/b/ ${S}/f && cat ${R}/run.json && cp ${R}/run.json ${S}/x`, `gzip -c ${R}/run.json > ${S}/r.gz`,
  `cat ${R}/run.json > ${S}/x && cp ${R}/run.json ${S}/y`, `cp ${R}/run.json ${S}/x && factory run show r1`,
  `python -c "import json,sys; sys.stdout.write(json.load(open('${R}/run.json'))['k'])"`,
  `rsync -a ${R.replace(/\//g, '\\')}\\evidence\\ ${S}/ev/`, `rsync -av ${R}/evidence/ ${S}/ev/`, `rsync ${R}/run.json`,
  `cp ${R}/run.json "${S}/a b/x"`, `cp ${R}/run.json ${R}/notes/x.json`, `rm -rf ${R}/remote && cat ${R}/run.json`,
  `ls ${R}/evidence && cp ${R}/run.json ${S}/x && rm ${S}/x`, `git status && cp ${R}/run.json ${S}/x`, `git -C /other/repo checkout main && cp ${R}/run.json ${S}/x`,
  `git checkout main && cp ${R}/evidence/x ${S}/y`,
  `python -c "import json,sys; d=json.load(open('${R}/run.json')); sys.stdout.write(d['key'])" && cp ${R}/run.json ${S}/x`,
  ...(WIN ? [`cd /other && cp ${R}/run.json x.json`] : []),
];
test('documented false positives: forms outside the grammar keep the baseline denial', () => {
  for (const cmd of STILL_DENIED) {
    assert.ok(!safe(cmd), `not provably safe: ${JSON.stringify(cmd)}`);
    assert.ok(oldDenies(cmd) && denies(cmd), `still denied: ${JSON.stringify(cmd)}`);
  }
  const spaced = { home: lib.normPath('C:/fh test/factory-home'), cwd: '/work' };
  assert.ok(!safe(`cp "C:/fh test/factory-home/runs/r1/run.json" ${S}/x`, spaced), 'a quoted word with a space');
});

// The workaround the deny message names, and the reads that must stay allowed.
test('reads stay allowed, and a copy out names the workaround (cat > dest, allowed by the baseline and the new rule)', () => {
  const home = { ...ctx, cwd: '/work' };
  for (const cmd of [`cat ${R}/remote/x > ${S}/x`, `cat ${FH_POSIX}/env/ws/React.storage.json > ${S}/x.json`,
    `cat ~/.factory/runs/r1/remote/x > ${S}/x`, `cat ~/.factory/env/ws/React.storage.json > ${S}/x.json`]) {
    assert.ok(!oldDenies(cmd, home), `baseline allows: ${cmd}`);
    assert.ok(!denies(cmd, home), `allowed: ${cmd}`);
  }
  for (const cmd of [`cat ${R}/evidence/CHK-1.json`, `grep pass ${R}/evidence/CHK-1.json 2>/dev/null`, `jq .status ${R}/run.json 2>/dev/null`,
    `Get-Content ${R}/run.json`, `cat ${R}/run.json 2>/dev/null`, `factory run human r1 "delete the spec; rm ${R}/run.json"`,
    `cd ${WIN ? 'C:/other/repo' : '/other/repo'} && cat ${R}/run.json`]) {
    assert.ok(!denies(cmd), `allowed: ${cmd}`);
  }
  for (const cmd of [`cp ${R}/run.json ${S}/x`, `cp ${FH_POSIX}/env/ws/React.storage.json ${S}/x.json`, `Copy-Item -Path ${R}/remote/x -Destination ${S}/x`]) {
    assert.match(lib.bashWriteReason(cmd, ctx), /use `cat <file> > <dest>` or the Read tool; run it as a separate command from any write/, cmd);
  }
  assert.doesNotMatch(lib.bashWriteReason(`cp ${S}/x ${R}/run.json`, ctx), /cat <file>/, 'no hint for a copy INTO the run');
});

// Rule 1: the lexer refuses every construct whose meaning it cannot pin down in both bash and PowerShell.
rule('substitutions, expansions, globs, groups, comments and odd characters are outside the grammar', `cat ${R}/run.json`, [
  `cp ${R}/run.json $(echo ${S}/x)`, `cp ${R}/run.json \`echo ${S}/x\``, `cp ${R}/run.json \${D}`, `cp ${R}/run.json $'${S}/x'`,
  `cp ${R}/run.json $"${S}/x"`, `cp ${R}/run.json <(cat)`, `cp ${R}/run.json >(cat)`, `cp ${R}/run.json $D`, `cp ${R}/run.json %D%`,
  `cp ${R}/run.json ${S}/*`, `cp ${R}/run.json ${S}/x?`, `cp ${R}/run.json ${S}/[x]`, `cp ${R}/run.json ${S}/{a,b}`, `(cp ${R}/run.json ${S}/x)`,
  `cp ${R}/run.json ${S}/x # ${R}/evidence/y`, `! cp ${R}/run.json ${S}/x`, `cp ${R}/run.json ${S}/x^`, `cp ${R}/run.json @x`,
  `cp ${R}/run.json ${S}/x &`, `cp ${R}/run.json ${S}/x |& cat`, `cat <<EOF\nx\nEOF\ncp ${R}/run.json ${R}/evidence/x`, `cp ${R}/run.json ${S}/x\r`,
  `cp ${R}/run.json ‘${S}/x’`, `cp –t ${R}/evidence ${R}/run.json`, `cp ${R}/run.json "${S}/x`,
  // PowerShell: ‘ ends a '…' string and – is a parameter dash (here -Destination)
  `cat 'x‘ ; rm ${R}/run.json ; ’y'`, `cp –d ${R}/evidence ${S}/x`,
]);
rule('a variable is a known one at the start of a READ word only; ~ only as a leading ~/ in a read word',
  [`cat $HOME/.factory/runs/r1/run.json`, `cat ~/.factory/runs/r1/run.json`, `cat %USERPROFILE%\\.factory\\runs\\r1\\run.json`], [
    `cp ${R}/run.json $HOME/x`, `cp ${R}/run.json ~/x`, `cp ${R}/run.json %USERPROFILE%/x`, `cp ~${path.basename(os.homedir())}/.factory/runs/r1/run.json ${S}/x; rm ${R}/x`,
    `cp a$HOME/.factory/runs/r1/run.json ${S}/x`, `cp $HOMEDRIVE/.factory/runs/r1/run.json ${S}/x`, `dd of=~/x if=${R}/run.json`,
  ]);
rule('PowerShell splits "a"/b and reads "a"-b as a parameter: only a read word may be split, never on a dash or dot',
  `cat "${R}"/run.json`, [`cp ${R}/run.json "${S}"/x`, `cp "${R}/run.json"-Destination ${R}/evidence/x ${S}/y`, `cp "${R}/run.json".x ${S}/y`]);
rule('an unquoted backslash is read the bash way AND the PowerShell way; both readings must be safe',
  [`cat ${R.replace(/\//g, '\\')}\\run.json`], [
    `cat ${R}/run.json\\;rm ${R}/run.json`, `cp ${R}/run.json ${S.replace(/\//g, '\\')}\\x`, `cp ${S}/a\\ ${R}/evidence/x`,
  ]);

// Rule 2: known command words only; Rule 3: directory changes.
rule('every command word is a bare name from the lists (no paths, wrappers, assignments, shells, interpreters with code)',
  `cat ${R}/run.json && ls ${R}/evidence`, [
    `./cp ${R}/run.json ${S}/x`, `/bin/cp ${R}/run.json ${S}/x`, `env cp ${R}/run.json ${S}/x`, `X=1 cp ${R}/run.json ${S}/x`,
    `command cp ${R}/run.json ${S}/x`, `cp.exe ${R}/run.json ${S}/x`, `"cp" ${R}/run.json ${S}/x`, `xargs cp ${R}/run.json`, `bash -c "cp ${R}/run.json ${S}/x"`,
    `node -e "1" && cp -l ${R}/run.json ${S}/x`, `eval cp ${R}/run.json ${S}/x`, `source x && cp ${R}/run.json ${S}/x`, `ln -s ${R}/run.json ${S}/x`,
    `install ${R}/run.json ${S}/x`, `Copy-Item ${R}/run.json ${S}/x`, `tee ${S}/x < ${R}/run.json; rm ${R}/run.json`,
    // removed from the grammar: every writer but cp and sed, rsync, git, python -c
    `rm ${S}/x && cat ${R}/run.json`, `rmdir ${S}/d && cat ${R}/run.json`, `mkdir ${S}/d && cat ${R}/run.json`,
    `touch ${S}/x && cat ${R}/run.json`, `mv ${S}/a ${S}/b && cat ${R}/run.json`, `rsync -a ${R}/evidence/ ${S}/ev/`,
    `git status && cat ${R}/run.json`, `python -c "print(1)" && cat ${R}/run.json`, `rmdir -p ${R}/remote/a`,
  ]);
rule('directory changes: cd with one absolute literal, no run path',
  `cd /other/repo && cat ${R}/run.json`, [
    `cd ${R} && cp ${R}/run.json x.json`, `cd ${R}/evidence && cp ${R}/run.json ${S}/x`, `cd other && cp ${R}/run.json x.json`, `cd - && cp ${R}/run.json x`,
    `popd && cp ${R}/run.json x`, `Push-Location ${R}; cp ${R}/run.json x`, `cd ${FH_POSIX}/runs && cp ${R}/run.json r1/evidence/x`,
    `sl ${R}/evidence; cp ${R}/run.json ${S}/x`, `cd && cp ${R}/run.json x.json`, `pushd ${S} && cp ${R}/run.json ${S}/x`,
    `Set-Location ${S}; cp ${R}/run.json ${S}/x`, `chdir ${S} && cp ${R}/run.json ${S}/x`, `cd ${S} ${S} && cp ${R}/run.json ${S}/x`,
    `cd /proc/cygdrive/c/x && cp ${R}/run.json x.json`,
  ]);
test('grammar: a relative write target is refused from any session cwd (no write target is provably safe)', () => {
  for (const cwd of [`${R}/evidence`, S, '/work']) {
    for (const cmd of [`cp ${R}/run.json x.json`, `cd ${S} && cp ${R}/run.json x.json`, `cat ${R}/run.json > x.json`]) {
      assert.ok(!safe(cmd, { ...ctx, cwd }), `${cwd}: ${cmd}`);
      if (oldDenies(cmd, { ...ctx, cwd })) assert.ok(denies(cmd, { ...ctx, cwd }), `${cwd}: ${cmd}`);
    }
  }
});

// Rules 4-6: roles and write targets.
rule('a run path is acceptable only as a read operand or factory CLI data; never in a write target',
  [`cat ${R}/run.json | head -3`, `factory run human r "rm ${R}/run.json" && cat ${R}/run.json`, `gzip -c ${R}/run.json > /dev/null`], [
    `cp ${S}/a ${R}/run.json`, `cp ${R}/run.json ${R}/evidence`, `sed -i s/a/b/ ${R}/run.json`, `sed -i s#a#b# ${S}/f && cat ${R}/run.json`,
    `rm ${R}/evidence/x && cat ${R}/run.json`, `git -C ${R} status && cp ${R}/run.json ${S}/x`, `sed -i s/a/b/w${R}/run.json ${S}/f`,
    `cp ${R}/run.json ${R}/remote/x.json`, `sed -i s/a/b/ ${S}/f ${R}/remote/x.json`, `cat ${R}/run.json > ${R}/notes/n.md`,
    // a write target that only LOOKS like a run path (another .factory tree in the temp dir) is refused too
    `cp ${R}/run.json ${S}/.factory/runs/r9/notes/x.json`, `cat ${R}/run.json > ${S}/x/runs/r9/evidence/x.json`,
  ]);
rule('factory CLI data: no character cmd.exe reads in factory.cmd arguments (PowerShell 5.1 re-quotes only on whitespace)',
  `factory run human r "rm ${R}/run.json" && cat ${R}/run.json`, [
    `factory run show r1 'x&echo>${R}/run.json'; cp ${R}/remote/x ${S}/y`, `factory.cmd run show r1 'a" & echo x > ${R}/run.json & "'; cp ${R}/remote/x ${S}/y`,
    `factory run human r 'a|b' && cp ${R}/run.json ${S}/x`, `factory run human r '%COMSPEC%' && cp ${R}/run.json ${S}/x`,
    `factory run human r 'a^b' && cp ${R}/run.json ${S}/x`, `factory run human r 'a<b' && cp ${R}/run.json ${S}/x`,
    `python "-m" factory run x && cp ${R}/run.json ${S}/x`, `factory run show r1 > ${S}/x && cp ${R}/run.json ${S}/y`,
  ]);
rule('PowerShell 5.1 re-quoting: a quoted word is not empty, holds no whitespace or quote, does not start with a dash',
  [`sed -n 's/a/b/' ${R}/run.json`, `cat '${R}/run.json'`], [
    `sed s/orig/forged/ 'q "q -i" q' ${R}/run.json && cp ${R}/run.json ${S}/x`, `sed -n s/orig/forged/ 'q "q -i" q' ${R}/run.json && cp ${R}/run.json ${S}/x`,
    `cat 'a b' ${R}/run.json && cp ${R}/run.json ${S}/x`, `cat "a'b" && cp ${R}/run.json ${S}/x`, `cat 'a"b' && cp ${R}/run.json ${S}/x`,
    `cp '-t' ${S} ${R}/run.json`, `cp "-r" ${R}/run.json ${S}/x`, `cat '' ${R}/run.json && cp ${R}/run.json ${S}/x`, `cp ${R}/run.json '' ${S}/x`,
    `grep "a b" ${R}/run.json && cp ${R}/run.json ${S}/x`,
  ]);
rule('write targets: a bare /dev/null (or NUL) is the only one; any other, whatever it names or wherever it lands, is refused',
  [`cat ${R}/run.json > /dev/null`, `cat ${R}/run.json 2>/dev/null`, `grep x ${R}/evidence/CHK-1.json 2>/dev/null >/dev/null`, ...(WIN ? [`cat ${R}/run.json > NUL`] : [])], [
    `cat ${R}/run.json > ${S}/x.json`, `cat ${R}/run.json >> ${S}/x.json`, `cat ${R}/run.json 2>${S}/e`, `cp ${R}/run.json ${S}/x.json`,
    `cp ${R}/run.json /dev/null`, `cat ${R}/run.json > "/dev/null"`, `cat ${R}/run.json > /dev/null/x`, `cat ${R}/run.json > ${S}/ck.lnk`,
    `cat ${R}/run.json > ${S}/ck`, `cat ${R}/run.json > NUL.txt`,
    `cp ${R}/run.json ${S}/xı`, `cp ${R}/run.json ${S}/ſx`, `cat ${R}/run.json > ${S}/é`, `cp ${R}/run.json ${FH_POSIX}/x`,
    `cp ${R}/run.json ${R}/notes/x.json`, `cp ${R}/run.json ${FH_POSIX}/runs/_inbox/s/x`, `cp ${R}/run.json ${path.dirname(FH_POSIX)}`,
    `cp ${R}/run.json ${S}/../x`, `cp ${R}/run.json ${R}/remote/../evidence/x`, `cp ${R}/run.json /${S}`, `cp ${R}/run.json ${S}/x.`,
    `cp ${R}/run.json "${S}/x "`, `cp ${R}/run.json ${S}/x:s`, `cp ${R}/run.json ${S}/PROGRA~1`, `rm ${S}/x,${R}/run.json && cat ${R}/run.json`,
    `cp ${R}/run.json ${R}/evidence/x`, `cp ${R}/run.json ${R}`, `rm -rf ${R} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/runs && cat ${R}/run.json`,
    `rm -rf ${FH_POSIX} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/env && cat ${R}/run.json`, `cp ${R}/run.json ${FH_POSIX}/env/ws/x.json`,
    `rm -rf /fh-test && cat ${R}/run.json`, `cat ${R}/run.json > ${R}/evidence/x`, `cp ${R}/run.json ~/.factory/runs/r2/run.json`,
  ]);
rule('cp is outside the grammar, whatever its flags (its destination is a file write target)',
  [], [
    `cp -r ${R}/evidence ${S}/ev`, `cp -a ${R}/evidence ${S}/ev`, `cp -- ${R}/run.json ${S}/x`,
    `cp ${R}/run.json -L ${S}/x`, `cp -L ${R}/run.json ${S}/x`, `cp -pv ${R}/run.json ${S}/x`, `cp -p ${R}/run.json ${S}/x`, `cp -H ${R}/run.json ${S}/x`,
    `cp ${R}/run.json , ${S}/x`, `cp ${R}/run.json,${S}/a ${S}/x`, `cp --preserve ${R}/run.json ${S}/x`,
    `cp -l ${R}/run.json ${S}/x`, `cp -s ${R}/run.json ${S}/x`, `cp -t ${S} ${R}/run.json`, `cp --target-directory=${S} ${R}/run.json`,
    `cp -i ${R}/run.json ${S}/x`, `cp -x ${R}/run.json ${S}/x`, `cp -T ${R}/run.json ${S}/x`, `cp --parents ${R}/run.json ${S}`, `cp -d ${R}/evidence ${S}/x`,
    `rsync --remove-source-files ${R}/run.json ${S}/x`, `rsync --delete ${R}/ ${S}/x`, `rsync -e sh ${R}/run.json ${S}/x`,
    `rsync --log-file=${R}/evidence/l ${R}/run.json ${S}/x`, `rsync -b ${R}/run.json ${S}/x`, `cp ${R}/run.json`,
  ]);
rule('readers with dangerous flags take flags from a list (sort -o/-T, diff --output, gzip without -c)',
  [`sort -n ${R}/run.json`, `diff -u ${R}/run.json ${S}/x`], [
    `sort -o ${S}/x ${R}/run.json && cp ${R}/run.json ${S}/y`, `sort -T ${R}/evidence ${R}/run.json && cp ${R}/run.json ${S}/y`,
    `diff --output=${S}/x ${R}/run.json a && cp ${R}/run.json ${S}/y`, `gzip ${R}/run.json`, `gzip -k ${R}/run.json`, `gunzip ${R}/evidence/x.gz`,
  ]);
rule('cp is outside the grammar, piped or not',
  `ls ${R}/evidence | head -3`, [`ls ${R}/evidence && cp ${R}/run.json ${S}/x`,`ls ${R}/evidence | cp ${R}/run.json ${S}/x`,`ls ${R}/evidence | rm ${S}/x`, `cat ${R}/run.json | cp ${R}/run.json ${S}/x`, `ls ${R} | mkdir ${S}/x`,
    `ls ${R}/evidence |\nrm ${S}/x`, `cat ${R}/run.json |\n\ncp ${R}/run.json ${S}/x`]);
rule('sed is a reader only: one s/// script with g, I or a number as flags, -n/-E/-r only; -i is outside the grammar',
  [`sed -n s/a/b/2 ${R}/run.json`, `sed -E s/a/b/g ${R}/run.json && cat ${R}/run.json`], [
    `sed -i s/a/b/g ${S}/f && cat ${R}/run.json`, `sed -E -i s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -n s/a/b/2 ${R}/run.json && cp ${R}/run.json ${S}/x`,
    `sed -e s/a/b/ -i ${S}/f && cat ${R}/run.json`, `sed --in-place s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -s -i s/a/b/ ${S}/f && cat ${R}/run.json`,
    `sed s/a/b/ ${S}/f -i ${R}/evidence/x`, `sed -i -- s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -z -i s/a/b/ ${S}/f && cat ${R}/run.json`,
    `sed -i 's/a/b/w x' ${S}/f && cat ${R}/run.json`, `sed -i s/a/b/e ${S}/f && cat ${R}/run.json`, `sed -i 1d ${S}/f && cat ${R}/run.json`,
    `sed -i.bak s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -i -e s/a/b/ -e s/c/d/ ${S}/f && cat ${R}/run.json`, `sed -i s/a/b/ ${R}/evidence/x`,
    `sed -f ${S}/s ${S}/f && cp ${R}/run.json ${S}/x`,
  ]);
rule('git and python -c are outside the grammar (a repository may point its work tree anywhere; PowerShell 5.1 strips the inner quotes of -c code)',
  [], [
    `git status && cp ${R}/run.json ${S}/x`, `git -C /other/repo checkout main && cp ${R}/run.json ${S}/x`,
    `git -c core.hooksPath=x commit && cp ${R}/run.json ${S}/x`, `git --work-tree=/x checkout . && cp ${R}/run.json ${S}/x`,
    `git --git-dir=/x status && cp ${R}/run.json ${S}/x`, `git restore . && cp ${R}/run.json ${S}/x`, `git log --output=${S}/x && cp ${R}/run.json ${S}/y`,
    `git -C other checkout x && cp ${R}/run.json ${S}/y`, `git -C ${R} checkout . && cat ${R}/run.json`, `git commit -m "rm ${R}/run.json"`,
    `git -C ${S}/repo checkout -f HEAD -- run.json && cp ${R}/remote/x ${S}/y`,
    `python -c "import json,sys; d=json.load(open('${R}/run.json')); sys.stdout.write(d['key'])" && cp ${R}/run.json ${S}/x`,
    `python -c 'print("exec(bytes([111]).decode())")'; cp ${R}/remote/x ${S}/y`, `python -c "print(1)" && cp ${R}/run.json ${S}/x`,
  ]);

test('grammar: a run path in the raw text that no single word names (split by a space) is not provably safe', () => {
  const spaced = { home: lib.normPath('C:/fh test/factory-home'), cwd: '/work' };
  assert.ok(!safe(`cp ${S}/a C:/fh test/factory-home/runs/r1/remote/x`, spaced));
});

// No write target is provably safe, whatever is on disk: the tests that built junctions, hard links, MSYS cookie files
// and `.lnk` shortcuts to check where a write lands are reduced to the verdict (the commands alone, nothing created).
test('grammar: no write target is provably safe, wherever it lands (safe roots, junctions, hard links, MSYS cookies, .lnk)', () => {
  const B = `${S}/fg`;
  const work = WIN ? 'C:/fg-work' : '/fg-work';
  const cases = [
    [`cp ${R}/run.json ${work}/x.json`, work], [`cp ${R}/run.json x.json`, work], [`cp ${R}/run.json ${path.dirname(FH_POSIX)}/x`, path.dirname(FH_POSIX)],
    [`cp ${R}/run.json ${B}/plain.json`], [`cp -r ${B}/A ${B}/C && cat ${R}/run.json`], [`cp ${R}/run.json ${B}/alias/run.json`],
    [`cat ${R}/run.json > ${B}/alias/run.json`], [`cp -a ${B}/A ${B}/C && cat ${B}/f > ${B}/C/J/run.json && cat ${R}/run.json`],
    [`cat ${B}/f > ${B}/hard && cat ${R}/run.json`], [`sed -i s/a/b/ ${B}/hard && cat ${R}/run.json`], [`cp ${R}/run.json ${B}/ck`],
    [`cat ${R}/run.json > ${B}/ck`], [`cp ${R}/run.json ${B}/sc.lnk`], [`cp ${R}/remote/x ${B}/ck`], [`cat ${B}/forged.json > ${B}/ck.lnk && cat ${R}/run.json`],
    [`cp ${FH_POSIX}/env/ws/React.storage.json ${B}/ik`],
  ];
  for (const [cmd, cwd] of cases) {
    const c = cwd ? { ...ctx, cwd } : ctx;
    assert.ok(!safe(cmd, c), cmd);
    if (oldDenies(cmd, c)) assert.ok(denies(cmd, c), cmd);
  }
});
test('grammar: on Windows a /x write target is the drive root in PowerShell and a Git Bash mount: unknown mounts are refused', { skip: !WIN && 'Windows paths' }, () => {
  const tail = `${S.replace(/^[A-Za-z]:/, '')}/x`;
  for (const pre of ['/proc/cygdrive/c', '/cygdrive/c', '/mnt/c', '/dev', '/home/u', '/usr', '/tmp', '/c']) {
    assert.ok(!safe(`cp ${R}/run.json ${pre}${tail}`), pre);
    assert.ok(!safe(`cat ${R}/run.json > ${pre}${tail}`), pre);
  }
  assert.ok(!safe(`cd /proc/cygdrive/c${S.replace(/^[A-Za-z]:/, '')} && cp ${R}/run.json x.json`));
});
test('grammar: Windows drive spellings of a POSIX path are all checked (/c/, /mnt/c/, /cygdrive/c/)', { skip: !WIN && 'Windows paths' }, () => {
  const tail = FH_POSIX.replace(/^C:/i, '');
  for (const pre of ['/c', '/mnt/c', '/cygdrive/c', '/C']) {
    assert.ok(!safe(`cp ${R}/run.json ${pre}${tail}/runs/r1/run.json`), pre);
    assert.ok(denies(`rm ${pre}${tail}/runs/r1/run.json`), pre);
  }
});

// The heredoc-as-data relief: only the whole-command form, only for data consumers, the rest judged in full.
test('heredoc: the body is data only for a data consumer, as the whole command, with a safe delimiter', () => {
  const keep = (cmd) => assert.equal(lib.stripDataHeredoc(cmd), cmd, `body must be kept: ${JSON.stringify(cmd)}`);
  const body = `rm -rf ${R}/evidence`;
  assert.equal(lib.stripDataHeredoc(`git commit -F - <<'EOF'\n${body}\nEOF`), 'git commit -F - ');
  keep(`acme-cli change create --title t --body-file - <<"EOF"\n${body}\nEOF\n`); // an unknown CLI is not a proven data consumer
  keep(`cat <<'EOF' > ${S}/n.md\n${body}\nEOF`); // a file write target: the body is judged with it
  keep(`tee ${S}/n.md <<'EOF'\n${body}\nEOF`);
  assert.equal(lib.stripDataHeredoc(`cat <<EOF\nplain text\nEOF`), 'cat ');
  keep(`cat <<EOF\n$(${body})\nEOF`);
  keep(`cat <<EOF\n\`${body}\`\nEOF`);
  keep(`cat <<'EOF'\nx\nEOF\n${body}`);
  keep(`cat <<'EOF' | sh\n${body}\nEOF`);
  keep(`cat <<'EOF' |\n${body}\nEOF\nsh`);
  keep(`bash <<'EOF'\n${body}\nEOF`);
  keep(`python - <<'EOF'\n${body}\nEOF`);
  keep(`cat x <<'EOF'\n${body}\nEOF`);
  keep(`echo "<<EOF"\n${body}\nEOF`);
  keep(`git log <<'EOF'\n${body}\nEOF`);
  keep(`cat <<'EOF'\nEOF\n${body}\nEOF`);
  keep(`cat <<'EOF'\n${body}\n EOF`);
  assert.ok(denies(`cat <<'EOF' > ${R}/run.json\nx\nEOF`), 'the first line is still judged');
  assert.ok(!denies(`git commit -F - <<'EOF'\nfix: do not ${body}\nEOF`));
});

test('extra rules read parens and lone braces as separators: a cd inside a subshell or group still moves the cwd', () => {
  assert.ok(denies(`(cd ${R} && echo x > run.json)`));
  assert.ok(denies(`{ cd ${R}; echo x > run.json; }`));
  assert.ok(!denies(`(cd /other && echo x > run.json)`));
});

// End to end through the real entry points, default FACTORY_HOME (~/.factory) as on a user machine.
const U = path.basename(os.homedir());
const T = TMP.replace(/\\/g, '/');
const CYG = os.homedir().replace(/\\/g, '/').replace(/^([A-Za-z]):/, (m, d) => `/proc/cygdrive/${d.toLowerCase()}`);
const E2E = [
  ['rm ~/.factory/runs/r1/run.json', true],
  ['zap() { rm -rf "$1"; }; zap ~/.factory/runs/r1/evidence', true],
  [`rm "C:/Documents and Settings/${U}/.factory/runs/r1/run.json"`, WIN && fs.existsSync('C:/Documents and Settings')],
  ['echo run.json | xargs -I% cp /tmp/x ~/.factory/runs/r1/%', true],
  ['Push-Location ~/.factory/runs/r1; Remove-Item run.json', true],
  ['Set-Alias wipe Clear-Content; wipe ~/.factory/runs/r1/run.json', true],
  [`dd if=/dev/null of=//localhost/c$/Users/${U}/.factory/runs/r1/run.json`, true],
  [`rm C:\\\\Users\\\\${U}\\\\.factory\\\\runs\\\\r1\\\\run.jso\\n`, true],
  [`cmd /c del C:\\Users\\${U.slice(0, 3)}^${U.slice(3)}\\.factory\\runs\\r1\\run.json`, true],
  ['(cd ~/.factory/runs/r1 && echo x > run.json)', true],
  // documented false positives: no file write target is provably safe (use cat > dest as its own command)
  ['cp ~/.factory/runs/r1/run.json /tmp/x', true],
  [`cp ~/.factory/runs/r1/remote/x ${T}/x`, true],
  [`cp ~/.factory/env/ws/React.storage.json ${T}/x.json`, true],
  [`cp ~/.factory/runs/r1/run.json ${T}/x && factory run show r1`, true],
  // sixth audit: C1 `.exe` twin cookie, B2 explicit `.lnk` shortcut, E1 Interix cookie, C3/B4 a redirection after a copy
  [`cp ~/.factory/env/ws/React.storage.json ${T}/ck`, true], [`cp ~/.factory/env/ws/React.storage.json ${T}/ck.lnk`, true],
  [`cp ~/.factory/env/ws/React.storage.json ${T}/ik`, true], [`cp ~/.factory/runs/r1/remote/x ${T}/ck`, true],
  [`cp ~/.factory/runs/r1/remote/x ${T}/z && cat ${T}/forged.json > ${T}/ck`, true],
  [`cp ~/.factory/runs/r1/remote/x ${T}/z && cat ${T}/forged.json > ${T}/ck.lnk`, true],
  // the frozen baseline allows these (no grammar involved)
  ['cat ~/.factory/runs/r1/run.json && sed -i s/a/b/ /tmp/f', false],
  [`sed -i 's/a/b/' ${T}/f && cat ~/.factory/runs/r1/run.json`, false],
  // must stay allowed
  ['cat ~/.factory/runs/r1/run.json && grep -r pass ~/.factory/runs/r1/evidence', false],
  ['jq .status ~/.factory/runs/r1/evidence/CHK-1.json && Get-Content ~/.factory/runs/r1/run.json', false],
  ['cat ~/.factory/runs/r1/evidence/CHK-1.json 2>/dev/null', false],
  ['grep pass ~/.factory/runs/r1/run.json 2>/dev/null', false],
  [`cat ~/.factory/runs/r1/remote/x > ${T}/x`, false],
  [`cat ~/.factory/env/ws/React.storage.json > ${T}/x.json`, false],
  [`cd ${WIN ? 'C:/other/repo' : '/other/repo'} && cat ~/.factory/runs/r1/run.json`, false],
  ['factory run human r1 "delete the spec; rm ~/.factory/runs/r1/run.json"', false],
  // fourth audit: R1 /proc/cygdrive alias (Windows), R2 PowerShell 5.1 re-quoting, R3 mv before cp through a junction,
  // R4 git with a work tree elsewhere, and the minors (rmdir -p, bare cd, non-ASCII letters)
  ...(WIN ? [
    `rm ${CYG}/.factory/runs/r1/run.json`, `cp ${T}/forged.json ${CYG}/.factory/runs/r1/run.json`, `mv ${T}/forged.json ${CYG}/.factory/runs/r1/run.json`,
    `sed -i s/orig/forged/ ${CYG}/.factory/runs/r1/run.json`, `touch ${CYG}/.factory/runs/r1/evidence/CHK-9.json`, `rm -rf ${CYG}/.factory/runs/r1/evidence`,
    `cd ${CYG} && rm .factory/runs/r1/run.json`, `cd ${CYG} && cp ${T}/forged.json .factory/runs/r1/run.json && cat ~/.factory/runs/r1/run.json`,
  ].map((c) => [c, true]) : []),
  [`sed s/orig/forged/ 'q "q -i" q' ~/.factory/runs/r1/run.json`, true],
  [`sed -n s/orig/forged/ 'q "q -i" q' ~/.factory/runs/r1/run.json`, true],
  [`python -c 'print("exec(bytes([111]).decode())")'; cp ~/.factory/runs/r1/remote/x ${T}/y`, true],
  [`mv ${T}/D ${T}/E && cp ${T}/forged.json ${T}/E/J/run.json && cp ~/.factory/runs/r1/remote/x ${T}/y`, true],
  [`cp -r ${T}/D ${T}/E && cp ${T}/forged.json ${T}/E/J/run.json && cp ~/.factory/runs/r1/remote/x ${T}/y`, true],
  [`git -C ${T}/repo checkout -f HEAD -- run.json && cp ~/.factory/runs/r1/remote/x ${T}/y`, true],
  ['rmdir -p ~/.factory/runs/r1/remote/a', true],
  ['cd && cp ~/.factory/runs/r1/remote/x x.json && cat ~/.factory/runs/r1/run.json', true],
  [`cp ~/.factory/runs/r1/remote/x ${T}/ev\u0131dence && cat ~/.factory/runs/r1/run.json`, true],
];
test('end to end: protect.js and worker-guard.js (builder, auditor) give the same verdicts', () => {
  const cwd = path.join(os.homedir(), 'Code', 'proj');
  for (const [cmd, deny] of E2E) {
    // `factory run human` is reserved to the main session by worker-guard's own rule (worker-guard.test.mjs)
    const roles = cmd.includes('run human') ? [] : ['builder', 'auditor'];
    const verdicts = [
      denied(runHook('protect.js', [], bash(cmd, cwd), { FACTORY_HOME: '' })),
      ...roles.map((role) => denied(runHook('worker-guard.js', [role], bash(cmd, cwd), { FACTORY_HOME: '' }))),
    ];
    assert.deepEqual(verdicts, verdicts.map(() => Boolean(deny)), cmd);
  }
});
