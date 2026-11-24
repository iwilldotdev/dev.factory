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
  // the later interpreter rule ignores sys.stdout.write; the baseline did not, and `import os` is outside the python grammar
  const cmd = `python -c "import os,sys; sys.stdout.write(os.environ['FACTORY_HOME'] + '/runs/r1/run.json')"`;
  assert.ok(oldDenies(cmd));
  assert.ok(!safe(cmd));
  assert.ok(denies(cmd));
  assert.ok(!denies(`python -c "import json,sys; sys.stdout.write(json.load(open('${R}/run.json'))['k'])"`), 'the grammar accepts the read-only form');
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
  [`cp ${R.replace(/\//g, '\\')}\\run.json ${S}/x`, `rsync -a ${R.replace(/\//g, '\\')}\\evidence\\ ${S}/ev/`], [
    `cat ${R}/run.json\\;rm ${R}/run.json`, `cp ${R}/run.json ${S.replace(/\//g, '\\')}\\x`, `cp ${S}/a\\ ${R}/evidence/x`,
  ]);

// Rule 2: known command words only; Rule 3: directory changes.
rule('every command word is a bare name from the lists (no paths, wrappers, assignments, shells, interpreters with code)',
  `cat ${R}/run.json && cp ${R}/run.json ${S}/x`, [
    `./cp ${R}/run.json ${S}/x`, `/bin/cp ${R}/run.json ${S}/x`, `env cp ${R}/run.json ${S}/x`, `X=1 cp ${R}/run.json ${S}/x`,
    `command cp ${R}/run.json ${S}/x`, `cp.exe ${R}/run.json ${S}/x`, `"cp" ${R}/run.json ${S}/x`, `xargs cp ${R}/run.json`, `bash -c "cp ${R}/run.json ${S}/x"`,
    `node -e "1" && cp -l ${R}/run.json ${S}/x`, `eval cp ${R}/run.json ${S}/x`, `source x && cp ${R}/run.json ${S}/x`, `ln -s ${R}/run.json ${S}/x`,
    `install ${R}/run.json ${S}/x`, `Copy-Item ${R}/run.json ${S}/x`, `tee ${S}/x < ${R}/run.json; rm ${R}/run.json`,
  ]);
rule('directory changes: absolute literal, no run path; relative writes are resolved against every cwd the command may be in',
  [`cd /other/repo && cp ${R}/run.json ${S}/x`, `cd /other && cp ${R}/run.json x.json`], [
    `cd ${R} && cp ${R}/run.json x.json`, `cd ${R}/evidence && cp ${R}/run.json ${S}/x`, `cd other && cp ${R}/run.json x.json`, `cd - && cp ${R}/run.json x`,
    `popd && cp ${R}/run.json x`, `Push-Location ${R}; cp ${R}/run.json x`, `cd ${FH_POSIX}/runs && cp ${R}/run.json r1/evidence/x`,
    `sl ${R}/evidence; cp ${R}/run.json ${S}/x`,
  ]);
test('grammar: a relative destination is resolved against the session cwd too', () => {
  const inRun = { ...ctx, cwd: `${R}/evidence` };
  assert.ok(!safe(`cp ${R}/run.json x.json`, inRun));
  assert.ok(safe(`cp ${R}/run.json x.json`, { ...ctx, cwd: S }));
});

// Rules 4-6: roles and write targets.
rule('a run path is acceptable only as a read operand, a copier source, or factory CLI / read-only python data',
  [`cat ${R}/run.json | head -3`, `factory run human r "rm ${R}/run.json" && cp ${R}/run.json ${S}/x`, `gzip -c ${R}/run.json > ${S}/r.gz`], [
    `cp ${S}/a ${R}/run.json`, `cp ${R}/run.json ${R}/evidence`, `sed -i s/a/b/ ${R}/run.json`, `sed -i s#a#b# ${S}/f && cat ${R}/run.json`,
    `rm ${R}/evidence/x && cat ${R}/run.json`, `git -C ${R} status && cp ${R}/run.json ${S}/x`, `sed -i s/a/b/w${R}/run.json ${S}/f`,
  ]);
rule('write targets: strict literal (no .., //, trailing dot or space, stream, short name, comma) outside the guarded tree',
  [`cp ${R}/run.json ${S}/x.json`, `cp ${R}/run.json "${S}/a b/x"`, `cp ${R}/run.json ${R}/notes/x.json`, `rm -rf ${R}/remote && cat ${R}/run.json`], [
    `cp ${R}/run.json ${S}/../x`, `cp ${R}/run.json ${R}/remote/../evidence/x`, `cp ${R}/run.json /${S}`, `cp ${R}/run.json ${S}/x.`,
    `cp ${R}/run.json "${S}/x "`, `cp ${R}/run.json ${S}/x:s`, `cp ${R}/run.json ${S}/PROGRA~1`, `rm ${S}/x,${R}/run.json && cat ${R}/run.json`,
    `cp ${R}/run.json ${R}/evidence/x`, `cp ${R}/run.json ${R}`, `rm -rf ${R} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/runs && cat ${R}/run.json`,
    `rm -rf ${FH_POSIX} && cat ${R}/run.json`, `rm -rf ${FH_POSIX}/env && cat ${R}/run.json`, `cp ${R}/run.json ${FH_POSIX}/env/ws/x.json`,
    `rm -rf /fh-test && cat ${R}/run.json`, `cat ${R}/run.json > ${R}/evidence/x`, `cp ${R}/run.json ~/.factory/runs/r2/run.json`,
  ]);
rule('copier flags come from a list: no link, target, move, delete, backup, log or remote-shell flag',
  [`cp -r ${R}/evidence ${S}/ev`, `rsync -av ${R}/evidence/ ${S}/ev/`, `rsync ${R}/run.json`], [
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
rule('a writer or copier never takes piped input (PowerShell binds piped objects to the path parameters)',
  `ls ${R}/evidence && rm ${S}/x`, [`ls ${R}/evidence | rm ${S}/x`, `cat ${R}/run.json | cp ${R}/run.json ${S}/x`, `ls ${R} | mkdir ${S}/x`,
    `ls ${R}/evidence |\nrm ${S}/x`, `cat ${R}/run.json |\n\ncp ${R}/run.json ${S}/x`]);
rule('sed: one s/// script with g, I or a number as flags; -i writes every file operand',
  `sed -i s/a/b/g ${S}/f && cat ${R}/run.json`, [
    `sed -i 's/a/b/w x' ${S}/f && cat ${R}/run.json`, `sed -i s/a/b/e ${S}/f && cat ${R}/run.json`, `sed -i 1d ${S}/f && cat ${R}/run.json`,
    `sed -i.bak s/a/b/ ${S}/f && cat ${R}/run.json`, `sed -i -e s/a/b/ -e s/c/d/ ${S}/f && cat ${R}/run.json`, `sed -i s/a/b/ ${R}/evidence/x`,
    `sed -f ${S}/s ${S}/f && cp ${R}/run.json ${S}/x`,
  ]);
rule('git: -C <absolute> and --no-pager only, a listed subcommand, no --output, a work tree outside the guarded tree',
  [`git status && cp ${R}/run.json ${S}/x`, `git -C /other/repo checkout main && cp ${R}/run.json ${S}/x`], [
    `git -c core.hooksPath=x commit && cp ${R}/run.json ${S}/x`, `git --work-tree=/x checkout . && cp ${R}/run.json ${S}/x`,
    `git --git-dir=/x status && cp ${R}/run.json ${S}/x`, `git restore . && cp ${R}/run.json ${S}/x`, `git log --output=${S}/x && cp ${R}/run.json ${S}/y`,
    `git -C other checkout x && cp ${R}/run.json ${S}/y`, `git -C ${R} checkout . && cat ${R}/run.json`, `git commit -m "rm ${R}/run.json"`,
  ]);
test('grammar: git writes from a cwd inside the guarded tree or in a repository that contains it are not provably safe', () => {
  const cmd = `git checkout main && cp ${R}/evidence/x ${S}/y`;
  assert.ok(safe(cmd));
  assert.ok(!safe(cmd, { ...ctx, cwd: R }));
  assert.ok(!safe(cmd, { ...ctx, cwd: FH_POSIX }));
  assert.ok(!safe(`git status && git checkout x && cp ${R}/evidence/x ${S}/y`, { ...ctx, cwd: path.dirname(FH_POSIX) }));
  const base = fs.mkdtempSync(path.join(TMP, 'fg-git-'));
  try {
    fs.mkdirSync(path.join(base, 'repo', '.git'), { recursive: true });
    fs.mkdirSync(path.join(base, 'repo', 'sub'), { recursive: true });
    const home = path.join(base, 'repo', 'home', '.factory');
    const c = { home: lib.normPath(home), cwd: path.join(base, 'repo', 'sub') };
    const run = `${home.replace(/\\/g, '/')}/runs/r1`;
    assert.ok(!safe(`git checkout x && cp ${run}/run.json ${S}/y`, c), 'the repository root contains the factory home');
    assert.ok(safe(`git status && cp ${run}/run.json ${S}/y`, c), 'a read-only git subcommand does not change the work tree');
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});
rule('python -c: ASCII, json/sys only, open() with one literal, names from a short list or one letter',
  `python -c "import json,sys; d=json.load(open('${R}/run.json')); sys.stdout.write(d['key'])" && cp ${R}/run.json ${S}/x`, [
    `python -c "import os; print(1)" && cp ${R}/run.json ${S}/x`, `python -c "open('${S}/x','w')" && cp ${R}/run.json ${S}/y`,
    `python -c "print(f'{1}')" && cp ${R}/run.json ${S}/x`, `python -c "print(1, file=sys.stderr)" && cp ${R}/run.json ${S}/x`,
    `python -c "exec('1')" && cp ${R}/run.json ${S}/x`, `python -c "__import__('os')" && cp ${R}/run.json ${S}/x`,
    `python -c "getattr(json,'dump')" && cp ${R}/run.json ${S}/x`, `python -c "ｅxec(1)" && cp ${R}/run.json ${S}/x`,
    `python -c "d=open(p)" && cp ${R}/run.json ${S}/x`, `python -c "x=1" -v && cp ${R}/run.json ${S}/x`, `python x.py && cp ${R}/run.json ${S}/x`,
  ]);

test('grammar: a run path in the raw text that no single word names (split by a space) is not provably safe', () => {
  const spaced = { home: lib.normPath('C:/fh test/factory-home'), cwd: '/work' };
  assert.ok(safe(`cp "C:/fh test/factory-home/runs/r1/run.json" ${S}/x`, spaced));
  assert.ok(!safe(`cp ${S}/a C:/fh test/factory-home/runs/r1/remote/x`, spaced));
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
    const c = { home: lib.normPath(home), cwd: base };
    const B = base.replace(/\\/g, '/');
    const RR = run.replace(/\\/g, '/');
    assert.ok(safe(`cp ${RR}/run.json ${B}/plain.json`, c));
    assert.ok(safe(`cp ${RR}/run.json ${B}/alias/notes/x.json`, c), 'an unguarded part of the run folder through the alias');
    for (const cmd of [`cp ${RR}/run.json ${B}/alias/run.json`, `cp ${RR}/run.json ${B}/alias/evidence/x.json`, `cp ${RR}/run.json ${B}/dangling/x.json`,
      `cp ${RR}/run.json alias/run.json`, `cat ${RR}/run.json > ${B}/alias/run.json`, `rm -rf ${B}/alias/evidence && cat ${RR}/run.json`]) {
      assert.ok(!safe(cmd, c), cmd);
      if (oldDenies(cmd, c)) assert.ok(denies(cmd, c), cmd);
    }
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
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
  ['cp ~/.factory/runs/r1/run.json /tmp/x', false],
  ['cat ~/.factory/runs/r1/run.json && sed -i s/a/b/ /tmp/f', false],
];
test('end to end: protect.js and worker-guard.js (builder, auditor) give the same verdicts', () => {
  const cwd = path.join(os.homedir(), 'Code', 'proj');
  for (const [cmd, deny] of E2E) {
    const verdicts = [
      denied(runHook('protect.js', [], bash(cmd, cwd), { FACTORY_HOME: '' })),
      denied(runHook('worker-guard.js', ['builder'], bash(cmd, cwd), { FACTORY_HOME: '' })),
      denied(runHook('worker-guard.js', ['auditor'], bash(cmd, cwd), { FACTORY_HOME: '' })),
    ];
    if (deny) assert.deepEqual(verdicts, [true, true, true], cmd);
    else assert.deepEqual(verdicts, [false, false, false], cmd);
  }
});
