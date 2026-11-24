import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import { createRequire } from 'node:module';
import { FH, FH_POSIX, TMP } from './helpers.mjs';

// Bypasses that were already open before the write-target rule (the review compared HEAD with cb48b49 and found these
// allowed in both). Decision functions only: no command is executed.
process.env.FACTORY_HOME = FH;
const require = createRequire(import.meta.url);
const lib = require('../protect-lib.js');
const ctx = { home: lib.normPath(FH, os.homedir()), cwd: '/work' };
const denies = (cmd, c = ctx) => Boolean(lib.bashWriteReason(cmd, c));
const R = `${FH_POSIX}/runs/r1`;
const SCRATCH = `${TMP.replace(/\\/g, '/')}/scratch`;

const DENY = [
  // sed / awk scripts that write a file themselves
  `sed 'w ${R}/run.json' x`, `sed -n 's/a/b/w ${R}/run.json' x`, `awk '{print > "${R}/run.json"}' x`, `gawk '{print >> "${R}/evidence/a"}' x`,
  // process substitution
  `tee >(cat > ${R}/run.json) < x`, `echo x | tee >(tee ${R}/metrics.json)`,
  // text piped or here-documented into a shell or interpreter
  `echo "rm ${R}/run.json" | sh`, `echo "rm ${R}/run.json" | bash`, `printf 'rm ${R}/run.json' | zsh`,
  `echo "import os; os.remove('${R}/run.json')" | python`, `cat <<EOF | bash\nrm ${R}/run.json\nEOF`, `bash <<EOF\nrm ${R}/run.json\nEOF`,
  `cat <<EOF > /tmp/s.sh\nrm ${R}/run.json\nEOF\nbash /tmp/s.sh`,
  // the factory CLI name as cover
  `./factory run human x "rm ${R}/run.json"`, `/tmp/factory run human x "cp a ${R}/run.json"`, `factory.py x; rm ${R}/run.json`,
  `./factory run x > ${R}/run.json`, `factory run x > ${R}/run.json`, `factory run show $(rm ${R}/run.json)`, `factory run show \`rm ${R}/run.json\``,
  `factory run show x && rm ${R}/run.json`,
  // PowerShell writers that were not modelled
  `Tee-Object -FilePath ${R}/run.json`, `Tee-Object -FilePath:${R}/run.json`, `Start-Transcript -Path ${R}/run.json`, `Start-Transcript -Path:${R}/evidence/t.txt`,
  // patch and compressors rewrite the file in place
  `patch ${R}/run.json x.diff`, `patch -p0 -d ${R} < x.diff`, `gzip ${R}/run.json`, `gunzip ${R}/run.json.gz`, `bzip2 ${R}/evidence/CHK-1.json`, `xz -k ${R}/run.json`,
  // git pointed at the protected tree by option or environment
  `git --work-tree=${R} checkout .`, `git --git-dir=${R}/evidence reset`, `GIT_WORK_TREE=${R} git checkout .`,
  // interpreter modules and inline code that shell out
  `python -m zipfile -e x.zip ${R}`, `python -m tarfile -e x.tar ${R}/evidence`, `python -m shutil ${R}/run.json /tmp/x`,
  `python -c "import os; os.system('rm ${R}/run.json')"`, `python -c "import subprocess; subprocess.run(['rm','${R}/run.json'])"`,
  `node -e "require('child_process').execSync('rm ${R}/run.json')"`, `ruby -e 'File.delete("${R}/run.json")'`,
  // a glob in a destructive command run from inside the protected tree
  `cd ${R} && rm *`, `pushd ${R} && rm -rf *`, `cd ${R}/evidence && Remove-Item *.json`,
  // odd spellings that normalise onto a protected file
  `echo x > ${R}/run.json.`, `echo x > ${R}/run.json::$DATA`, `tee ${R}/run.json:s < x`,
];

const ALLOW = [
  `factory run show acme-dev-1`, `python -m factory run show acme-dev-1`, `factory.cmd run show acme-dev-1`, `FACTORY_X=1 factory run human acme-dev-1 "rm ${R}/run.json"`,
  `cat ${R}/run.json | python -m json.tool`, `python -m json.tool ${R}/run.json`, `cat ${R}/run.json | jq .`,
  `sed -n p ${R}/run.json`, `awk '{print $1}' ${R}/run.json`, `gzip -c ${R}/run.json > ${SCRATCH}/r.gz`, `gzip ${SCRATCH}/x.json`,
  `git -C /other/repo checkout main`, `cd /other/repo && rm *`, `echo hello | sh -c 'cat'`, `echo '{}' | python -m json.tool`,
  `tee >(cat > ${SCRATCH}/a) < x`, `patch ${SCRATCH}/f x.diff`, `python -c "import json; print(json.load(open('${R}/remote/x.json')))"`,
];

test('bypasses that predate the write-target rule are denied', () => {
  for (const cmd of DENY) assert.ok(denies(cmd), `should deny: ${JSON.stringify(cmd)}`);
});

test('the same fixes leave ordinary commands alone', () => {
  for (const cmd of ALLOW) assert.ok(!denies(cmd), `should allow: ${JSON.stringify(cmd)}`);
});

test('isFactory accepts only the bare command name and python -m factory', () => {
  const f = (s) => lib.isFactory(s.split(' '));
  for (const ok of ['factory run x', 'factory.cmd run x', 'Factory run x', 'python -m factory run x', 'python3 -m factory run x']) assert.ok(f(ok), ok);
  for (const no of ['./factory run x', '../factory run x', '/usr/bin/factory run x', 'C:\\tools\\factory.cmd run x', 'factory.py run x', 'factory.sh', 'python factory.py', 'python -m factoryx']) {
    assert.ok(!f(no), no);
  }
});
