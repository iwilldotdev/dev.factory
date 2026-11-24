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
// a run-folder write target, a quoted word with a space, /tmp and other mounts on Windows). The baseline denied each,
// so each stays denied; the remedy is a separate command or the Edit/Write tools.
const STILL_DENIED = [
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

// Rule 1: the lexer refuses every construct whose meaning it cannot pin down in both bash and PowerShell.
rule('substitutions, expansions, globs, groups, comments and odd characters are outside the grammar', `cp ${R}/run.json ${S}/x`, [
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
  [`cp $HOME/.factory/runs/r1/run.json ${S}/x`, `cp ~/.factory/runs/r1/run.json ${S}/x`, `cp %USERPROFILE%\\.factory\\runs\\r1\\run.json ${S}/x`], [
    `cp ${R}/run.json $HOME/x`, `cp ${R}/run.json ~/x`, `cp ${R}/run.json %USERPROFILE%/x`, `cp ~${path.basename(os.homedir())}/.factory/runs/r1/run.json ${S}/x; rm ${R}/x`,
    `cp a$HOME/.factory/runs/r1/run.json ${S}/x`, `cp $HOMEDRIVE/.factory/runs/r1/run.json ${S}/x`, `dd of=~/x if=${R}/run.json`,
  ]);
rule('PowerShell splits "a"/b and reads "a"-b as a parameter: only a read word may be split, never on a dash or dot',
  `cp "${R}"/run.json ${S}/x`, [`cp ${R}/run.json "${S}"/x`, `cp "${R}/run.json"-Destination ${R}/evidence/x ${S}/y`, `cp "${R}/run.json".x ${S}/y`]);
rule('an unquoted backslash is read the bash way AND the PowerShell way; both readings must be safe',
  [`cp ${R.replace(/\//g, '\\')}\\run.json ${S}/x`], [
    `cat ${R}/run.json\\;rm ${R}/run.json`, `cp ${R}/run.json ${S.replace(/\//g, '\\')}\\x`, `cp ${S}/a\\ ${R}/evidence/x`,
  ]);

// Rule 2: known command words only; Rule 3: directory changes.
rule('every command word is a bare name from the lists (no paths, wrappers, assignments, shells, interpreters with code)',
  `cat ${R}/run.json && cp ${R}/run.json ${S}/x`, [
    `./cp ${R}/run.json ${S}/x`, `/bin/cp ${R}/run.json ${S}/x`, `env cp ${R}/run.json ${S}/x`, `X=1 cp ${R}/run.json ${S}/x`,
    `command cp ${R}/run.json ${S}/x`, `cp.exe ${R}/run.json ${S}/x`, `"cp" ${R}/run.json ${S}/x`, `xargs cp ${R}/run.json`, `bash -c "cp ${R}/run.json ${S}/x"`,
    `node -e "1" && cp -l ${R}/run.json ${S}/x`, `eval cp ${R}/run.json ${S}/x`, `source x && cp ${R}/run.json ${S}/x`, `ln -s ${R}/run.json ${S}/x`,
    `install ${R}/run.json ${S}/x`, `Copy-Item ${R}/run.json ${S}/x`, `tee ${S}/x < ${R}/run.json; rm ${R}/run.json`,
    // removed from the grammar: every writer but cp and sed, rsync, git, python -c
    `rm ${S}/x && cat ${R}/run.json`, `rmdir ${S}/d && cat ${R}/run.json`, `mkdir ${S}/d && cat ${R}/run.json`,
    `touch ${S}/x && cat ${R}/run.json`, `mv ${S}/a ${S}/b && cat ${R}/run.json`, `rsync -a ${R}/evidence/ ${S}/ev/`,
    `git status && cat ${R}/run.json`, `python -c "print(1)" && cat ${R}/run.json`, `rmdir -p ${R}/remote/a`,
  ]);
rule('directory changes: cd with one absolute literal, no run path; relative writes are resolved against every cwd the command may be in',
  `cd /other/repo && cp ${R}/run.json ${S}/x`, [
    `cd ${R} && cp ${R}/run.json x.json`, `cd ${R}/evidence && cp ${R}/run.json ${S}/x`, `cd other && cp ${R}/run.json x.json`, `cd - && cp ${R}/run.json x`,
    `popd && cp ${R}/run.json x`, `Push-Location ${R}; cp ${R}/run.json x`, `cd ${FH_POSIX}/runs && cp ${R}/run.json r1/evidence/x`,
    `sl ${R}/evidence; cp ${R}/run.json ${S}/x`, `cd && cp ${R}/run.json x.json`, `pushd ${S} && cp ${R}/run.json ${S}/x`,
    `Set-Location ${S}; cp ${R}/run.json ${S}/x`, `chdir ${S} && cp ${R}/run.json ${S}/x`, `cd ${S} ${S} && cp ${R}/run.json ${S}/x`,
    `cd /proc/cygdrive/c/x && cp ${R}/run.json x.json`,
  ]);
test('grammar: a relative destination is resolved against the session cwd too', () => {
  const inRun = { ...ctx, cwd: `${R}/evidence` };
  assert.ok(!safe(`cp ${R}/run.json x.json`, inRun));
  assert.ok(safe(`cp ${R}/run.json x.json`, { ...ctx, cwd: S }));
  assert.ok(safe(`cd ${S} && cp ${R}/run.json x.json`, { ...ctx, cwd: S }));
  assert.ok(!safe(`cd ${S} && cp ${R}/run.json x.json`, { ...ctx, cwd: `${R}/evidence` }), 'the cd may fail: the session cwd is a candidate');
});

// Rules 4-6: roles and write targets.
rule('a run path is acceptable only as a read operand, a cp source, or factory CLI data; never in a write target',
  [`cat ${R}/run.json | head -3`, `factory run human r "rm ${R}/run.json" && cp ${R}/run.json ${S}/x`, `gzip -c ${R}/run.json > ${S}/r.gz`], [
    `cp ${S}/a ${R}/run.json`, `cp ${R}/run.json ${R}/evidence`, `sed -i s/a/b/ ${R}/run.json`, `sed -i s#a#b# ${S}/f && cat ${R}/run.json`,
    `rm ${R}/evidence/x && cat ${R}/run.json`, `git -C ${R} status && cp ${R}/run.json ${S}/x`, `sed -i s/a/b/w${R}/run.json ${S}/f`,
    `cp ${R}/run.json ${R}/remote/x.json`, `sed -i s/a/b/ ${S}/f ${R}/remote/x.json`, `cat ${R}/run.json > ${R}/notes/n.md`,
    // a write target that only LOOKS like a run path (another .factory tree in the temp dir) is refused too
    `cp ${R}/run.json ${S}/.factory/runs/r9/notes/x.json`, `cat ${R}/run.json > ${S}/x/runs/r9/evidence/x.json`,
  ]);
rule('factory CLI data: no character cmd.exe reads in factory.cmd arguments (PowerShell 5.1 re-quotes only on whitespace)',
  `factory run human r "rm ${R}/run.json" && cp ${R}/run.json ${S}/x`, [
    `factory run show r1 'x&echo>${R}/run.json'; cp ${R}/remote/x ${S}/y`, `factory.cmd run show r1 'a" & echo x > ${R}/run.json & "'; cp ${R}/remote/x ${S}/y`,
    `factory run human r 'a|b' && cp ${R}/run.json ${S}/x`, `factory run human r '%COMSPEC%' && cp ${R}/run.json ${S}/x`,
    `factory run human r 'a^b' && cp ${R}/run.json ${S}/x`, `factory run human r 'a<b' && cp ${R}/run.json ${S}/x`,
    `python "-m" factory run x && cp ${R}/run.json ${S}/x`, `factory run show r1 > ${S}/x && cp ${R}/run.json ${S}/y`,
  ]);
rule('PowerShell 5.1 re-quoting: a quoted word is not empty, holds no whitespace or quote, does not start with a dash',
  [`sed -i 's/a/b/' ${S}/f && cat ${R}/run.json`, `cp '${R}/run.json' '${S}/x'`], [
    `sed s/orig/forged/ 'q "q -i" q' ${R}/run.json && cp ${R}/run.json ${S}/x`, `sed -n s/orig/forged/ 'q "q -i" q' ${R}/run.json && cp ${R}/run.json ${S}/x`,
    `cat 'a b' ${R}/run.json && cp ${R}/run.json ${S}/x`, `cat "a'b" && cp ${R}/run.json ${S}/x`, `cat 'a"b' && cp ${R}/run.json ${S}/x`,
    `cp '-t' ${S} ${R}/run.json`, `cp "-r" ${R}/run.json ${S}/x`, `cat '' ${R}/run.json && cp ${R}/run.json ${S}/x`, `cp ${R}/run.json '' ${S}/x`,
    `grep "a b" ${R}/run.json && cp ${R}/run.json ${S}/x`,
  ]);
rule('write targets: strict ASCII literal (no .., //, trailing dot or space, stream, short name, comma) in a safe root, outside every home',
  [`cp ${R}/run.json ${S}/x.json`, `cp ${R}/run.json /dev/null`, `cat ${R}/run.json > /dev/null`], [
    `cp ${R}/run.json ${S}/xı`, `cp ${R}/run.json ${S}/ſx`, `cat ${R}/run.json > ${S}/é`, `cp ${R}/run.json ${FH_POSIX}/x`,
    `cp ${R}/run.json ${R}/notes/x.json`, `cp ${R}/run.json ${FH_POSIX}/runs/_inbox/s/x`, `cp ${R}/run.json ${path.dirname(FH_POSIX)}`,
    `cp ${R}/run.json ${S}/../x`, `cp ${R}/run.json ${R}/remote/../evidence/x`, `cp ${R}/run.json /${S}`, `cp ${R}/run.json ${S}/x.`,
    `cp ${R}/run.json "${S}/x "`, `cp ${R}/run.json ${S}/x:s`, `cp ${R}/run.json ${S}/PROGRA~1`, `rm ${S}/x,${R}/run.json && cat ${R}/run.json`,
    `cp ${R}/run.json ${R}/evidence/x`, `cp ${R}/run.json ${R}`, `rm -rf ${R} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/runs && cat ${R}/run.json`,
    `rm -rf ${FH_POSIX} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/env && cat ${R}/run.json`, `cp ${R}/run.json ${FH_POSIX}/env/ws/x.json`,
    `rm -rf /fh-test && cat ${R}/run.json`, `cat ${R}/run.json > ${R}/evidence/x`, `cp ${R}/run.json ~/.factory/runs/r2/run.json`,
  ]);
rule('cp flags come from a list: no link, target or backup flag, none PowerShell binds to a parameter that swaps or takes a value',
  [`cp -r ${R}/evidence ${S}/ev`, `cp -a ${R}/evidence ${S}/ev`, `cp -- ${R}/run.json ${S}/x`], [
    `cp ${R}/run.json -L ${S}/x`, `cp -L ${R}/run.json ${S}/x`, `cp -pv ${R}/run.json ${S}/x`, `cp -p ${R}/run.json ${S}/x`, `cp -H ${R}/run.json ${S}/x`,
    `cp ${R}/run.json , ${S}/x`, `cp ${R}/run.json,${S}/a ${S}/x`, `cp --preserve ${R}/run.json ${S}/x`,
    `cp -l ${R}/run.json ${S}/x`, `cp -s ${R}/run.json ${S}/x`, `cp -t ${S} ${R}/run.json`, `cp --target-directory=${S} ${R}/run.json`,
    `cp -i ${R}/run.json ${S}/x`, `cp -x ${R}/run.json ${S}/x`, `cp -T ${R}/run.json ${S}/x`, `cp --parents ${R}/run.json ${S}`, `cp -d ${R}/evidence ${S}/x`,
    `rsync --remove-source-files ${R}/run.json ${S}/x`, `rsync --delete ${R}/ ${S}/x`, `rsync -e sh ${R}/run.json ${S}/x`,
    `rsync --log-file=${R}/evidence/l ${R}/run.json ${S}/x`, `rsync -b ${R}/run.json ${S}/x`, `cp ${R}/run.json`,
  ]);
rule('readers with dangerous flags take flags from a list (sort -o/-T, diff --output, gzip without -c)',
  [`sort -n ${R}/run.json && cp ${R}/run.json ${S}/x`, `diff -u ${R}/run.json ${S}/x && cp ${R}/run.json ${S}/y`], [
    `sort -o ${S}/x ${R}/run.json && cp ${R}/run.json ${S}/y`, `sort -T ${R}/evidence ${R}/run.json && cp ${R}/run.json ${S}/y`,
    `diff --output=${S}/x ${R}/run.json a && cp ${R}/run.json ${S}/y`, `gzip ${R}/run.json`, `gzip -k ${R}/run.json`, `gunzip ${R}/evidence/x.gz`,
  ]);
rule('cp never takes piped input (PowerShell binds piped objects to the path parameters)',
  `ls ${R}/evidence && cp ${R}/run.json ${S}/x`, [`ls ${R}/evidence | cp ${R}/run.json ${S}/x`,`ls ${R}/evidence | rm ${S}/x`, `cat ${R}/run.json | cp ${R}/run.json ${S}/x`, `ls ${R} | mkdir ${S}/x`,
    `ls ${R}/evidence |\nrm ${S}/x`, `cat ${R}/run.json |\n\ncp ${R}/run.json ${S}/x`]);
rule('sed: one s/// script with g, I or a number as flags, -i/-n/-E/-r only; -i writes every file operand',
  [`sed -i s/a/b/g ${S}/f && cat ${R}/run.json`, `sed -E -i s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -n s/a/b/2 ${R}/run.json && cp ${R}/run.json ${S}/x`], [
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

// Safe roots: a write lands in the real temp dir, or in the real session cwd when that cwd is outside every factory home.
test('grammar: a write target lands in a safe root (temp dir, session cwd outside the homes) and outside and above no home', () => {
  const work = WIN ? 'C:/fg-work' : '/fg-work';
  const other = WIN ? 'C:/fg-elsewhere' : '/fg-elsewhere';
  assert.ok(safe(`cp ${R}/run.json ${work}/x.json`, { ...ctx, cwd: work }));
  assert.ok(safe(`cp ${R}/run.json x.json`, { ...ctx, cwd: work }));
  assert.ok(!safe(`cp ${R}/run.json ${other}/x.json`, { ...ctx, cwd: work }), 'outside every safe root');
  assert.ok(!safe(`cp ${R}/run.json x.json`, { ...ctx, cwd: `${R}/notes` }), 'a cwd inside a home is no safe root');
  assert.ok(!safe(`cp ${R}/run.json ${FH_POSIX}/runs/r1/notes/x.json`, { ...ctx, cwd: FH_POSIX }));
  assert.ok(safe(`cp ${R}/run.json ${path.dirname(FH_POSIX)}/x`, { ...ctx, cwd: path.dirname(FH_POSIX) }), 'beside a home');
  assert.ok(!safe(`cp ${R}/run.json ${path.dirname(FH_POSIX)}`, { ...ctx, cwd: path.dirname(FH_POSIX) }), 'the folder above a home');
});

// Junctions and symbolic links: the real path of the nearest existing ancestor decides.
test('grammar: a write through a junction or link into the guarded tree is not provably safe (real path), a dangling one neither', () => {
  const base = fs.mkdtempSync(path.join(TMP, 'fg-real-'));
  try {
    const home = path.join(base, 'home', '.factory');
    const run = path.join(home, 'runs', 'r1');
    fs.mkdirSync(path.join(run, 'notes'), { recursive: true });
    fs.writeFileSync(path.join(run, 'run.json'), '{}');
    const type = WIN ? 'junction' : 'dir';
    fs.symlinkSync(run, path.join(base, 'alias'), type);
    fs.symlinkSync(path.join(run, 'evidence'), path.join(base, 'dangling'), type);
    fs.mkdirSync(path.join(base, 'A'));
    fs.symlinkSync(run, path.join(base, 'A', 'J'), type);
    fs.writeFileSync(path.join(base, 'f'), '{}');
    fs.linkSync(path.join(run, 'run.json'), path.join(base, 'hard'));
    const c = { home: lib.normPath(home), cwd: base };
    const B = base.replace(/\\/g, '/');
    const RR = run.replace(/\\/g, '/');
    for (const ok of [`cp ${RR}/run.json ${B}/plain.json`, `cp -r ${B}/A ${B}/C && cat ${RR}/run.json`]) assert.ok(safe(ok, c), ok);
    for (const cmd of [`cp ${RR}/run.json ${B}/alias/run.json`, `cp ${RR}/run.json ${B}/alias/evidence/x.json`, `cp ${RR}/run.json ${B}/dangling/x.json`,
      `cp ${RR}/run.json alias/run.json`, `cat ${RR}/run.json > ${B}/alias/run.json`, `rm -rf ${B}/alias/evidence && cat ${RR}/run.json`,
      `cp ${RR}/run.json ${B}/alias/notes/x.json`,
      // a later write at or under an earlier cp destination may go through a link that cp copied (R3 with cp alone)
      `cp -r ${B}/A ${B}/C && cp ${B}/f ${B}/C/J/run.json && cat ${RR}/run.json`, `cp -a ${B}/A ${B}/C && cat ${B}/f > ${B}/C/J/run.json && cat ${RR}/run.json`,
      `cp -r ${B}/A ${B}/C && cd ${B}/C/J && cat ${B}/f > run.json && cat ${RR}/run.json`, `cp -r ${B}/A ${B}/C && cp -r ${B}/A ${B}/C && cat ${RR}/run.json`,
      // an existing destination folder receives <dest>/<source name>: here a junction into the run folder
      `cp -r ${B}/x/J ${B}/A && cat ${RR}/run.json`, `cp ${RR}/run.json ${B}/A`,
      // a hard link is a second name of a run file
      `cp ${B}/f ${B}/hard && cat ${RR}/run.json`, `cat ${B}/f > ${B}/hard && cat ${RR}/run.json`, `sed -i s/a/b/ ${B}/hard && cat ${RR}/run.json`]) {
      assert.ok(!safe(cmd, c), cmd);
      if (oldDenies(cmd, c)) assert.ok(denies(cmd, c), cmd);
    }
    if (WIN) {
      // from a session cwd at <drive>:/proc, the PowerShell reading of /proc/cygdrive/... lands in that safe root while
      // Git Bash goes through the junction into the run folder: an unknown mount is never mapped by guess
      const root = { ...c, cwd: `${B.slice(0, 2)}/proc` };
      assert.ok(!safe(`cp ${RR}/run.json /proc/cygdrive/${B[0].toLowerCase()}${B.slice(2)}/alias/notes/x.json`, root), '/proc/cygdrive is not mapped');
      assert.ok(!safe(`cp ${RR}/run.json /${B[0].toLowerCase()}${B.slice(2)}/alias/notes/x.json`, root), 'the Git Bash drive mount is mapped');
    }
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});
// MSYS/Cygwin follow two link kinds that node sees as plain files: a "magic cookie" file and a `<name>.lnk` shortcut.
test('grammar: a write target that is an MSYS cookie link, sits under one, or has a .lnk twin is not provably safe', () => {
  const base = fs.mkdtempSync(path.join(TMP, 'fg-cookie-'));
  try {
    const home = path.join(base, 'home', '.factory');
    const run = path.join(home, 'runs', 'r1');
    fs.mkdirSync(run, { recursive: true });
    fs.writeFileSync(path.join(run, 'run.json'), '{}');
    const cookie = (name, target) => fs.writeFileSync(path.join(base, name),
      Buffer.concat([Buffer.from('!<symlink>\xff\xfe', 'latin1'), Buffer.from(`${target}\0`, 'utf16le')]));
    cookie('ck', path.join(run, 'run.json'));
    cookie('ckdir', run);
    fs.writeFileSync(path.join(base, 'sc.lnk'), 'L');
    fs.writeFileSync(path.join(base, 'f'), '{}');
    const c = { home: lib.normPath(home), cwd: base };
    const B = base.replace(/\\/g, '/');
    const RR = run.replace(/\\/g, '/');
    for (const ok of [`cp ${RR}/run.json ${B}/new.json`, `sed -i s/a/b/ ${B}/f && cat ${RR}/run.json`]) assert.ok(safe(ok, c), ok);
    for (const cmd of [`cp ${RR}/run.json ${B}/ck`, `cat ${RR}/run.json > ${B}/ck`, `sed -i s/a/b/ ${B}/ck && cat ${RR}/run.json`,
      `cp ${RR}/run.json ${B}/ckdir/run.json`, `cp ${RR}/run.json ${B}/sc`, `cp ${RR}/run.json ${B}/sc/run.json`]) {
      assert.ok(!safe(cmd, c), cmd);
      if (oldDenies(cmd, c)) assert.ok(denies(cmd, c), cmd);
    }
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
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
  assert.equal(lib.stripDataHeredoc(`gh pr create --title t --body-file - <<"EOF"\n${body}\nEOF\n`), 'gh pr create --title t --body-file - ');
  assert.equal(lib.stripDataHeredoc(`cat <<'EOF' > ${S}/n.md\n${body}\nEOF`), `cat  > ${S}/n.md`);
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
  // /tmp is C:\tmp to PowerShell: on Windows it is no safe root (a documented false positive)
  ['cp ~/.factory/runs/r1/run.json /tmp/x', WIN],
  ['cat ~/.factory/runs/r1/run.json && sed -i s/a/b/ /tmp/f', false],
  // must stay allowed
  ['cat ~/.factory/runs/r1/run.json && grep -r pass ~/.factory/runs/r1/evidence', false],
  ['jq .status ~/.factory/runs/r1/evidence/CHK-1.json && Get-Content ~/.factory/runs/r1/run.json', false],
  [`cp ~/.factory/runs/r1/remote/x ${T}/x`, false],
  [`cp ~/.factory/env/ws/React.storage.json ${T}/x.json`, false],
  [`sed -i 's/a/b/' ${T}/f && cat ~/.factory/runs/r1/run.json`, false],
  ['factory run human r1 "delete the spec; rm ~/.factory/runs/r1/run.json"', false],
  [`cp ~/.factory/runs/r1/run.json ${T}/x && factory run show r1`, false],
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
