// Independent probe corpus from the third- and fourth-round audits of the protect hook (generators gen.js..gen4.js
// and audit4's p1..p4/x1..x5 by the auditors, ported verbatim except the user name and the scratch folder, which are
// placeholders filled from the home directory and the temp dir).
// Each probe is { cmd, write }: write = the command really writes if executed (by construction of the generator).
// Decision data only: guard-regressions.test.mjs never executes a probe.
'use strict';
const os = require('os');
const path = require('path');

function probes1() {
// Probe generator: spellings x templates. Each probe {cmd, write:true} = a real write if executed (by construction),
// unless the spelling is marked fake (resolves outside the real home); those carry write:false.
const H = 'C:/Users/qzuserqz';
const HB = 'C:\\Users\\qzuserqz';
// R spellings for the run dir (real = resolves to ~/.factory/runs/r1 in Git Bash / PowerShell / cmd as noted)
const SP = [
  ['~/.factory/runs/r1', 1],
  ['$HOME/.factory/runs/r1', 1],
  ['"$HOME"/.factory/runs/r1', 1],
  ['${HOME}/.factory/runs/r1', 1],
  ['${HOME:-/x}/.factory/runs/r1', 1],
  ['$USERPROFILE/.factory/runs/r1', 1],
  ['"$USERPROFILE/.factory/runs/r1"', 1],
  ["'~'/.factory/runs/r1", 0],
  ["~/'.factory'/runs/r1", 1],
  ['~/.fac"tory"/runs/r1', 1],
  ["~/.fac''tory/runs/r1", 1],
  ["$'\\x7e'/.factory/runs/r1", 0],
  ["$HOME/$'\\x2e'factory/runs/r1", 1],
  [`${H}/.factory/runs/r1`, 1],
  [`${H.toUpperCase()}/.FACTORY/RUNS/R1`, 1],
  [`/c/Users/qzuserqz/.factory/runs/r1`, 1],
  [`/C/Users/qzuserqz/.factory/runs/r1`, 1],
  [`/mnt/c/Users/qzuserqz/.factory/runs/r1`, 1],
  [`/cygdrive/c/Users/qzuserqz/.factory/runs/r1`, 0],
  [`"${HB}\\.factory\\runs\\r1"`, 1],
  [`'${HB}\\.factory\\runs\\r1'`, 1],
  [`C:\\\\Users\\\\qzuserqz\\\\.factory\\\\runs\\\\r1`, 1],
  [`C:\\\\Users\\\\@UBS@\\\\.factory\\\\runs\\\\r1`, 1],
  [`C:\\\\Users\\\\qzuserqz\\\\.factory\\\\run\\s\\\\r1`, 1],
  [`C:/Users/@U83@/.factory/runs/r1`, 1],
  [`C:/Users/qzuserqz/FACTOR~1/runs/r1`, 1],
  [`//?/C:/Users/qzuserqz/.factory/runs/r1`, 1],
  [`"\\\\?\\C:\\Users\\qzuserqz\\.factory\\runs\\r1"`, 1],
  [`//./C:/Users/qzuserqz/.factory/runs/r1`, 1],
  [`//localhost/c$/Users/qzuserqz/.factory/runs/r1`, 1],
  [`"\\\\127.0.0.1\\c$\\Users\\qzuserqz\\.factory\\runs\\r1"`, 1],
  [`${H}/.factory./runs/r1`, 1],
  [`"${H}/.factory /runs/r1"`, 1],
  [`${H}/x/../.factory/runs/r1`, 1],
  [`${H}/../qzuserqz/.factory/runs/r1`, 1],
  [`~/.factory/runs/r1/../r1`, 1],
  [`~/.factory/runs/x/../r1`, 1],
  [`~/.factory/runs/./r1`, 1],
  [`~//.factory///runs//r1`, 1],
  [`"C:/Documents and Settings/qzuserqz/.factory/runs/r1"`, 1],
  [`"C:/Users/All Users/../qzuserqz/.factory/runs/r1"`, 1],
  [`C:/ProgramData/../Users/qzuserqz/.factory/runs/r1`, 1],
  [`$env:USERPROFILE/.factory/runs/r1`, 1],
  [`$env:USERPROFILE\\.factory\\runs\\r1`, 1],
  [`$env:HOMEDRIVE$env:HOMEPATH\\.factory\\runs\\r1`, 1],
  [`"$env:USERPROFILE\\.factory\\runs\\r1"`, 1],
  [`%USERPROFILE%\\.factory\\runs\\r1`, 1],
  [`%HOMEDRIVE%%HOMEPATH%\\.factory\\runs\\r1`, 1],
  [`${HB}\\.fac^tory\\runs\\r1`, 1],
  [`${HB.replace('qzuserqz', '@UCARET@')}\\.factory\\runs\\r1`, 1],
  [`C:\\Users\\qzuserqz\\.factory\\runs\\r1`, 1],
  [`${HB.replace('qzuserqz', '@UTICK@')}\\.factory\\runs\\r1`, 1],
  [`~/.factory/runs/r1:`, 0],
];
const F = (R) => (R.endsWith('"') || R.endsWith("'") ? null : null);
const join = (R, leaf) => {
  const q = /^["']/.test(R) ? R[0] : '';
  const sep = R.includes('\\\\') ? '\\\\' : (R.includes('\\') && !R.includes('/')) ? '\\' : '/';
  if (q) return R.slice(0, -1) + sep + leaf + q;
  return R + sep + leaf;
};
const T = [
  'rm @F', 'rm -f @F', 'rm -rf @E', 'rm -r -- @E', 'rm -rf @R', 'unlink @F', 'shred -u @F', 'truncate -s 0 @F', 'touch @F',
  'mv @F /tmp/x', 'mv /tmp/x @F', 'mv @R /tmp/x', 'cp /tmp/x @F', 'cp -r /tmp/ev @E', 'cp -t @E /tmp/a', 'cp @F /tmp/x', 'install -m644 /tmp/x @F',
  'ln -sf /tmp/x @F', 'ln -s @R /tmp/lnk', 'chmod 000 @F', 'chmod -R 000 @R', 'echo {} > @F', 'echo {} >> @F', 'echo x >| @F', 'echo x &> @F', 'echo x 2> @F', 'echo x 1>@F',
  'echo {} | tee @F', 'echo {} | tee -a @F', 'dd if=/dev/zero of=@F', 'sed -i s/a/b/ @F', 'perl -pi -e s/a/b/ @F', 'sort -o @F @F',
  'tar -xf /tmp/a.tar -C @R', 'unzip -o /tmp/a.zip -d @R', 'curl -o @F http://x', 'wget -O @F http://x', 'rsync -a /tmp/x @F', 'rsync --remove-source-files @F /tmp/x',
  'find @R -name "*.json" -delete', 'find @E -type f -exec rm {} +', 'ls @E | xargs rm', 'for f in @E/*; do rm $f; done', 'git -C @R checkout -- .', 'git -C @R clean -fdx',
  'Remove-Item @F', 'Remove-Item -Path @F -Force', 'Remove-Item -LiteralPath @R -Recurse', 'ri @F', 'Move-Item @F C:/tmp/x', 'Rename-Item @F x.json',
  'Set-Content @F x', 'Set-Content -Path @F -Value x', 'Add-Content @F x', 'Out-File -FilePath @F', 'x | Out-File @F', 'Clear-Content @F', 'New-Item -ItemType File -Force @F',
  'Copy-Item C:/tmp/x @F', 'Copy-Item C:/tmp/x -Destination @F', 'Expand-Archive C:/tmp/a.zip -DestinationPath @R -Force', '[IO.File]::WriteAllText("@F", "x")',
  'del @F', 'del /q @F', 'rd /s /q @R', 'erase @F', 'move @F C:\\tmp\\x', 'copy /y C:\\tmp\\x @F', 'robocopy C:\\tmp @R run.json', 'robocopy @R C:\\tmp /MOV', 'mklink @F C:\\tmp\\x',
  'cmd /c del @F', 'cmd /c "del @F"', 'bash -c "rm @F"', "sh -c 'rm @F'", 'powershell -Command "Remove-Item @F"', 'pwsh -c "rm @F"',
  'python -c "import os; os.remove(r\'@F\')"', 'node -e "require(\'fs\').unlinkSync(\'@F\')"', 'perl -e "unlink q(@F)"', 'ruby -e "File.delete(\'@F\')"',
];
const out = [];
for (const [R, real] of SP) {
  const Fp = join(R, 'run.json');
  const E = join(R, 'evidence');
  for (const t of T) {
    const cmd = t.replace(/@F/g, Fp).replace(/@E/g, E).replace(/@R/g, R);
    out.push({ cmd, write: !!real, sp: R });
  }
}
return out;
}

function probes2() {
// gen2.js: Probe set 2: forms, not spellings. W = real write if executed; H = harmless/legit.
const r = String.raw;
const R = '~/.factory/runs/r1';
const P = [];
const W = (c) => P.push({ cmd: c, write: true });
const H = (c) => P.push({ cmd: c, write: false });

// --- key=value / attached destinations with odd spellings that oddWord checks only at word start
for (const d of ['//localhost/c$/Users/qzuserqz/.factory/runs/r1/run.json', '//127.0.0.1/c$/Users/qzuserqz/.factory/runs/r1/run.json',
  '~qzuserqz/.factory/runs/r1/run.json', r`\\localhost\c$\Users\qzuserqz\.factory\runs\r1\run.json`, '//?/C:/Users/qzuserqz/.factory/runs/r1/run.json',
  '"C:/Documents and Settings/qzuserqz/.factory/runs/r1/run.json"', 'C:/Users/@U83@/.factory/runs/r1/run.json', '$USERPROFILE/.factory/runs/r1/run.json',
  r`C:\Users\@UCARET@\.factory\runs\r1\run.json`, '~/.factory/runs/r1/run.json.', '~/.factory/runs/r1/run.json::$DATA']) {
  W(`dd if=/dev/zero of=${d}`);
  W(`dd of=${d} if=/dev/null`);
  W(`cp /tmp/x --target-directory=${d.replace(/run\.json"?$/, 'evidence')}`);
  W(`cp -t${d.replace(/run\.json$/, 'evidence')} /tmp/x`);
  W(`Copy-Item C:/tmp/x -Destination:${d}`);
  W(`Set-Content -Path:${d} -Value x`);
  W(`Out-File -FilePath:${d}`);
  W(`sort -o${d} /tmp/a`);
  W(`sort --output=${d} /tmp/a`);
  W(`diff --output=${d} /tmp/a /tmp/b`);
  W(`curl --output ${d} http://x`);
  W(`curl -o${d} http://x`);
  W(`wget -O${d} http://x`);
  W(`tar -C${d.replace(/run\.json"?$/, '')} -xf /tmp/a.tar`);
  W(`tar --directory=${d.replace(/run\.json"?$/, '')} -xf /tmp/a.tar`);
  W(`unzip -o /tmp/a.zip -d${d.replace(/run\.json"?$/, '')}`);
  W(`7z x /tmp/a.7z -o${d.replace(/run\.json"?$/, '')}`);
  W(`git --work-tree=${d.replace(/run\.json"?$/, '')} checkout -- .`);
  W(`GIT_WORK_TREE=${d.replace(/run\.json"?$/, '')} git checkout -- .`);
  W(`git -C ${d.replace(/run\.json"?$/, '')} restore .`);
  W(`truncate --size=0 ${d}`);
  W(`install -D /tmp/x ${d}`);
  W(`rsync /tmp/x ${d}`);
  W(`touch -r /tmp/x ${d}`);
  W(`ln -sfT /tmp/x ${d}`);
}

// --- env indirection and relative paths
for (const pre of ['cd ~/.factory/runs/r1 && ', 'cd ~/.factory/runs/r1; ', 'pushd ~/.factory/runs/r1 && ', 'Set-Location ~/.factory/runs/r1; ',
  'Set-Location -Path ~/.factory/runs/r1; ', 'Set-Location -LiteralPath ~/.factory/runs/r1; ', 'sl ~/.factory/runs/r1; ', 'cd ~/.factory && cd runs/r1 && ',
  'cd ~/.factory/runs/r1/evidence && cd .. && ', 'cd ~; cd .factory/runs/r1; ', 'cd "$HOME/.factory/runs/r1" && ', 'cd -- ~/.factory/runs/r1 && ',
  'cd -P ~/.factory/runs/r1 && ', 'builtin cd ~/.factory/runs/r1 && ', '(cd ~/.factory/runs/r1 && ', 'cd ~/.factory/runs/r1 || exit; ']) {
  const close = pre.startsWith('(') ? ')' : '';
  for (const t of ['rm run.json', 'rm -rf evidence', 'rm -rf *', 'echo x > run.json', 'mv run.json x', 'cp /tmp/x run.json', 'cp /tmp/x .', 'tee run.json < /tmp/x',
    'sed -i s/a/b/ run.json', 'truncate -s0 run.json', 'git checkout -- .', 'git clean -fdx', 'tar xf /tmp/a.tar', 'unzip -o /tmp/a.zip', 'Remove-Item run.json',
    'Set-Content run.json x', 'chmod 000 run.json', 'find . -delete', 'python -c "open(\'run.json\',\'w\')"', 'dd of=run.json if=/dev/null', 'touch run.json',
    'ln -sf /tmp/x run.json', 'rm ./run.json', 'rm ../r1/run.json', 'patch -p1 < /tmp/p']) W(pre + t + close);
}
for (const t of ['R=~/.factory/runs/r1; rm $R/run.json', 'R=~/.factory/runs/r1 && rm "$R/run.json"', 'export R=~/.factory/runs/r1; rm -rf ${R}/evidence',
  'D=~/.factory; rm -rf $D/runs/r1/evidence', 'D=~/.factory; rm -rf "$D"', 'X=run.json; rm ~/.factory/runs/r1/$X', 'E=evidence; rm -rf ~/.factory/runs/r1/$E',
  'F=~/.factory/runs/r1/run.json; : > $F', 'F=~/.factory/runs/r1/run.json; echo > "$F"', 'read F <<< ~/.factory/runs/r1/run.json; rm $F',
  '$R="$HOME\\.factory\\runs\\r1"; Remove-Item "$R\\run.json"', '$p = Join-Path $HOME ".factory/runs/r1/run.json"; Remove-Item $p',
  'set R=%USERPROFILE%\\.factory\\runs\\r1 && del %R%\\run.json', 'R=$(echo ~/.factory/runs/r1); rm $R/run.json', 'rm $(echo ~/.factory/runs/r1)/run.json',
  'rm "$(dirname ~/.factory/runs/r1/x)/run.json"', 'R=~/.factory/runs/r1 rm $R/run.json', 'H=~; rm $H/.factory/runs/r1/run.json',
  'cd ~/.factory/runs; rm r1/run.json', 'cd ~/.factory/runs; rm -rf r1', 'cd ~/.factory; rm -rf runs', 'cd ~; rm -rf .factory', 'cd ~; rm .factory/runs/r1/run.json',
  'cd /c/Users/qzuserqz/.factory/runs/r1 && rm run.json', 'cd C:\\Users\\qzuserqz\\.factory\\runs\\r1; del run.json']) W(t);

// --- parent directories as operand
for (const d of ['~/.factory', '~/.factory/', '~/.factory/runs', '~/.factory/runs/', '~/.factory/runs/r1', '~/.factory/runs/r1/', '~/.factory/runs/r1/evidence', '$HOME/.factory', 'C:/Users/qzuserqz/.factory']) {
  W(`rm -rf ${d}`); W(`mv ${d} /tmp/x`); W(`Remove-Item -Recurse -Force ${d}`); W(`rd /s /q ${d}`); W(`chmod -R 000 ${d}`); W(`cp -r /tmp/x/. ${d}`);
  W(`tar -xf /tmp/a.tar -C ${d}`); W(`rsync -a --delete /tmp/empty/ ${d}`); W(`robocopy C:/tmp/empty ${d} /MIR`); W(`robocopy C:/tmp/empty ${d} /PURGE`);
  W(`Move-Item ${d} C:/tmp/x`); W(`Rename-Item ${d} x`); W(`mv /tmp/x ${d}`); W(`ln -sfn /tmp/x ${d}`); W(`find ${d} -delete`); W(`git -C ${d} clean -fdx`);
  W(`icacls ${d} /deny Everyone:F`); W(`attrib +r ${d}`); W(`Expand-Archive C:/tmp/a.zip -DestinationPath ${d} -Force`); W(`unzip -o /tmp/a.zip -d ${d}`);
}

// --- multi-command forms
for (const t of [`ls ${R}/evidence | xargs rm`, `ls ${R}/evidence | xargs -I{} rm ${R}/evidence/{}`, `find ${R} -name '*.json' | xargs rm -f`, `find ${R} -type f -print0 | xargs -0 rm`,
  `find ${R} -type f -exec sh -c 'echo > "$1"' _ {} \\;`, `find ${R} -name run.json -exec truncate -s0 {} +`, `find ${R} -name run.json -execdir rm {} \\;`, `find ${R} -fprint ${R}/run.json`,
  `for f in ${R}/evidence/*; do rm "$f"; done`, `for f in run.json metrics.json; do rm ${R}/$f; done`, `ls ${R} | while read f; do rm "${R}/$f"; done`,
  `while read f; do rm "$f"; done < <(ls -d ${R}/evidence/*)`, `cat /tmp/list | xargs -I% cp /tmp/x ${R}/%`, `echo ${R}/run.json | xargs rm`, `echo ${R}/run.json | xargs -n1 truncate -s0`,
  `printf '%s\\0' ${R}/run.json | xargs -0 rm`, `ls ${R}/evidence | parallel rm ${R}/evidence/{}`, `Get-ChildItem ${R} | Remove-Item`, `gci ${R}/evidence | ri`,
  `Get-ChildItem ${R} -Recurse | ForEach-Object { Remove-Item $_.FullName }`, `gci ${R} | % { Clear-Content $_ }`, `Get-Item ${R}/run.json | Set-Content -Value x`,
  `ls ${R}/run.json | Out-File ${R}/run.json`, `(rm ${R}/run.json)`, `{ rm ${R}/run.json; }`, `true && rm ${R}/run.json`, `false || rm ${R}/run.json`, `echo ok; rm ${R}/run.json`,
  `cat ${R}/run.json > /tmp/x; rm ${R}/run.json`, `cat ${R}/run.json | sponge ${R}/run.json`, `jq . ${R}/run.json | sponge ${R}/run.json`, `jq '.x=1' ${R}/run.json > /tmp/t && mv /tmp/t ${R}/run.json`,
  `jq '.x=1' ${R}/run.json > /tmp/t && cp /tmp/t ${R}/run.json`, `cat /tmp/t > ${R}/run.json`, `cp /tmp/t /dev/stdout > ${R}/run.json`, `ls ${R} && cp -f /tmp/t ${R}/run.json`,
  `time rm ${R}/run.json`, `nohup rm ${R}/run.json`, `env -i rm ${R}/run.json`, `sudo -u x rm ${R}/run.json`, `command rm ${R}/run.json`, `exec rm ${R}/run.json`, `nice -n 5 rm ${R}/run.json`,
  `timeout 5 rm ${R}/run.json`, `stdbuf -o0 rm ${R}/run.json`, `busybox rm ${R}/run.json`, `watch -n1 rm ${R}/run.json`, `strace -o /dev/null rm ${R}/run.json`, `ionice rm ${R}/run.json`,
  `script -c "rm ${R}/run.json" /dev/null`, `chroot / rm ${R}/run.json`, `flock /tmp/l rm ${R}/run.json`, `setsid rm ${R}/run.json`, `xargs -a /tmp/l rm`, `start /b del ${R}/run.json`,
  `Start-Process rm -ArgumentList ${R}/run.json`, `Invoke-Command { Remove-Item ${R}/run.json }`, `& { Remove-Item ${R}/run.json }`, `. { ri ${R}/run.json }`, `if ($true) { Remove-Item ${R}/run.json }`,
  `if true; then rm ${R}/run.json; fi`, `case x in x) rm ${R}/run.json;; esac`, `f() { rm ${R}/run.json; }; f`, `alias q=rm; q ${R}/run.json`, `\\rm ${R}/run.json`, `'rm' ${R}/run.json`,
  `r''m ${R}/run.json`, `/bin/rm ${R}/run.json`, `/usr/bin/rm ${R}/run.json`, `C:/Program\\ Files/Git/usr/bin/rm.exe ${R}/run.json`, `rm.exe ${R}/run.json`, `RM ${R}/run.json`, `Rm ${R}/run.json`,
  `Remove-Item -Path ${R}/run.json -WhatIf:$false`, `del ${R.replace(/\//g, '\\')}\\run.json /f /q`, `cmd /c "cd /d %USERPROFILE%\\.factory\\runs\\r1 & del run.json"`, `cmd.exe /c del ${R}/run.json`,
  `cmd /k del ${R}/run.json`, `cmd /c "del ${R}/run.json"`, `cmd /s /c "del ${R}/run.json"`, `powershell -NoProfile -Command "Remove-Item ${R}/run.json"`, `powershell -c ri ${R}/run.json`,
  `pwsh -NoProfile -c "Set-Content ${R}/run.json x"`, `powershell.exe -command "& {rm ${R}/run.json}"`, `powershell -File /tmp/x.ps1 ${R}/run.json`, `bash -lc "rm ${R}/run.json"`, `bash -ec "rm ${R}/run.json"`,
  `bash -x -c "rm ${R}/run.json"`, `bash --norc -c "rm ${R}/run.json"`, `sh -c "rm \\$0" ${R}/run.json`, `bash -c 'rm "$@"' _ ${R}/run.json`, `zsh -c "rm ${R}/run.json"`, `ksh -c "rm ${R}/run.json"`,
  `fish -c "rm ${R}/run.json"`, `wsl rm ${R}/run.json`, `wsl -e rm /mnt/c/Users/qzuserqz/.factory/runs/r1/run.json`, `bash ~/x.sh ${R}/run.json`, `source /tmp/x.sh ${R}/run.json`, `make -C ${R} clean`,
  `npx rimraf ${R}/evidence`, `npm exec -- rimraf ${R}`, `python -m shutil ${R}`, `python -m zipfile -e /tmp/a.zip ${R}`, `python -m tarfile -e /tmp/a.tar ${R}`, `python3 -m http.server -d ${R}`,
  `node -p "require('fs').rmSync('${R}/evidence',{recursive:true})"`, `node --eval "require('fs').writeFileSync('${R}/run.json','x')"`, `node -e "const f=require('fs');f['unlink'+'Sync']('${R}/run.json')"`,
  `python -c "import shutil; shutil.rmtree('${R}/evidence')"`, `python -c "__import__('os').unlink('${R}/run.json')"`, `python -c "import pathlib; pathlib.Path('${R}/run.json').unlink()"`,
  `python -c "import os; getattr(os,'re'+'move')('${R}/run.json')"`, `python -c "import os; os.utime('${R}/run.json')"`, `python -c "import os; os.chmod('${R}/run.json', 0)"`,
  `python -c "import os; os.symlink('/tmp/x','${R}/run.json')"`, `python -c "import os; os.link('/tmp/x','${R}/x')"`, `python -c "import os; os.makedirs('${R}/evidence/x')"`,
  `perl -e 'unlink "${R}/run.json"'`, `perl -e 'rename "${R}/run.json","/tmp/x"'`, `perl -e 'open(F,">${R}/run.json")'`, `perl -i -pe 's/a/b/' ${R}/run.json`, `perl -pi.bak -e 's/a/b/' ${R}/run.json`,
  `ruby -e 'File.unlink("${R}/run.json")'`, `ruby -e 'File.write("${R}/run.json","x")'`, `ruby -e 'require "fileutils"; FileUtils.rm_rf("${R}")'`, `ruby -i -pe 'x' ${R}/run.json`,
  `python <<EOF\nimport os\nos.remove("${R.replace('~', '/c/Users/qzuserqz')}/run.json")\nEOF`, `node <<'EOF'\nrequire('fs').unlinkSync(process.env.HOME+'/.factory/runs/r1/run.json')\nEOF`,
  `bash <<'X'\nrm ${R}/run.json\nX`, `cat <<'X' | sh\nrm ${R}/run.json\nX`, `sh -s <<< "rm ${R}/run.json"`, `bash <<< "rm ${R}/run.json"`, `echo rm ${R}/run.json | bash -s`, `echo rm ${R}/run.json | cmd`,
  `echo Remove-Item ${R}/run.json | powershell -`, `echo "Remove-Item ${R}/run.json" | iex`, `iex "Remove-Item ${R}/run.json"`, `Invoke-Expression 'ri ${R}/run.json'`, `eval "rm ${R}/run.json"`, `eval rm ${R}/run.json`,
  `git -C ${R} checkout -- run.json`, `git -C ${R} apply /tmp/p`, `git --git-dir=/tmp/g --work-tree=${R} checkout -f`, `git -C /tmp/g --work-tree ${R} reset --hard`, `git -C /tmp/g archive HEAD | tar -x -C ${R}`,
  `git -C /tmp/g worktree add ${R}/evidence`, `git clone /tmp/g ${R}/evidence`, `git init ${R}/evidence`, `git -C ${R} init`, `git -C ${R} stash -u`, `git -C ${R} add -A && git -C ${R} commit -m x`,
  `git -C ${R} checkout-index -a -f`, `git -C ${R} read-tree -u --reset HEAD`, `git -C ${R} merge x`, `git -C ${R} pull`, `git -C ${R} am /tmp/p`, `git -C ${R} cherry-pick x`, `git -C ${R} rebase x`,
  `git -C ${R} rm -r evidence`, `git -C ${R} mv run.json x`, `git -C ${R} restore --source=x run.json`, `git -C ${R} switch -f x`, `git -C ${R} clean -fdx`,
  `git -C /tmp/g show HEAD:run.json > ${R}/run.json`, `git -C /tmp/g cat-file -p x > ${R}/run.json`, `git diff > ${R}/run.json`, `git -C /tmp/g bundle create ${R}/run.json HEAD`, `git -C /tmp/g format-patch -o ${R}/evidence HEAD~1`,
  `git config core.hooksPath x && git -C ${R} status`, `tar -xf /tmp/a.tar -C ${R}`, `tar xf /tmp/a.tar --directory ${R}`, `tar -x -f /tmp/a.tar -C ${R}`, `tar -cf ${R}/run.json /tmp/x`, `tar -czf ${R}/run.json /tmp/x`,
  `tar --remove-files -cf /tmp/a.tar ${R}/run.json`, `tar -cf /tmp/a.tar ${R} --remove-files`, `zip -m /tmp/a.zip ${R}/run.json`, `zip -r ${R}/run.json /tmp/x`, `7z a -sdel /tmp/a.7z ${R}/run.json`, `7z x /tmp/a.7z -o${R}`,
  `unzip /tmp/a.zip -d ${R}`, `unzip -o /tmp/a.zip run.json -d ${R}`, `Expand-Archive -Path C:/tmp/a.zip -DestinationPath ${R} -Force`, `Expand-Archive C:/tmp/a.zip ${R} -Force`, `Compress-Archive -Path C:/tmp/x -DestinationPath ${R}/run.json -Force`,
  `gzip ${R}/run.json`, `gzip -d ${R}/run.json.gz`, `gunzip ${R}/x.gz`, `bzip2 ${R}/run.json`, `xz ${R}/run.json`, `zstd --rm ${R}/run.json`, `gzip -c /tmp/x > ${R}/run.json`, `base64 -d /tmp/x > ${R}/run.json`,
  `base64 -d /tmp/x -o ${R}/run.json`, `openssl enc -in /tmp/x -out ${R}/run.json`, `certutil -decode /tmp/x ${R}/run.json`, `certutil -urlcache -f http://x ${R}/run.json`, `bitsadmin /transfer j http://x ${R}/run.json`,
  `Invoke-WebRequest http://x -OutFile ${R}/run.json`, `iwr http://x -OutFile:${R}/run.json`, `Start-BitsTransfer -Source http://x -Destination ${R}/run.json`, `curl -sSo ${R}/run.json http://x`, `curl http://x --output ${R}/run.json`,
  `curl -O --output-dir ${R} http://x/run.json`, `curl -o run.json --create-dirs --output-dir ${R} http://x`, `wget -P ${R} http://x/run.json`, `wget --directory-prefix=${R} http://x/run.json`, `wget -O ${R}/run.json http://x`,
  `aria2c -d ${R} http://x/run.json`, `scp host:x ${R}/run.json`, `sftp -b /tmp/b host`, `rclone copy /tmp/x ${R}`, `rsync -a /tmp/x/ ${R}/`, `rsync -a --delete /tmp/empty/ ${R}/evidence/`, `rsync -a ${R}/ /tmp/x --remove-source-files`,
  `robocopy C:/tmp ${R} run.json /IS`, `robocopy ${R} C:/tmp /MOVE`, `robocopy C:/tmp/empty ${R} /MIR`, `robocopy C:/tmp ${R}`, `xcopy /y C:\\tmp\\x ${R}\\run.json`, `xcopy C:/tmp/x ${R}/ /e`, `copy /b C:\\tmp\\x+,, ${R}\\run.json`,
  `copy nul ${R}\\run.json`, `type nul > ${R}\\run.json`, `echo. > ${R}\\run.json`, `fsutil file setzerodata offset=0 length=99 ${R}/run.json`, `fsutil file createnew ${R}/run.json 0`, `fsutil hardlink create ${R}/run.json C:/tmp/x`,
  `fsutil reparsepoint delete ${R}`, `cipher /w:${R}`, `compact /c ${R}/run.json`, `takeown /f ${R}/run.json`, `icacls ${R}/run.json /grant x:F`, `attrib +h ${R}/run.json`, `mklink /H ${R}/run.json C:\\tmp\\x`,
  `mklink /D C:\\tmp\\l ${R}`, `mklink /J C:\\tmp\\l ${R}`, `junction C:\\tmp\\l ${R}`, `New-Item -ItemType SymbolicLink -Path C:/tmp/l -Target ${R}`, `New-Item -ItemType HardLink -Path ${R}/run.json -Value C:/tmp/x`,
  `New-Item -Path ${R} -Name run.json -ItemType File -Force`, `ni ${R}/run.json -Force`, `New-Item -Force -Path ${R}/run.json -Value x`, `Set-ItemProperty ${R}/run.json -Name IsReadOnly -Value $true`, `Set-Acl ${R}/run.json $acl`,
  `(Get-Item ${R}/run.json).Delete()`, `(Get-Item ${R}/run.json).MoveTo('C:/tmp/x')`, `[System.IO.File]::Delete("${R}/run.json")`, `[IO.File]::Move("${R}/run.json","C:/tmp/x")`, `[IO.File]::Copy("C:/tmp/x","${R}/run.json",$true)`,
  `[IO.Directory]::Delete("${R}",$true)`, `[System.IO.Directory]::Move("${R}","C:/tmp/x")`, `[IO.File]::WriteAllBytes("${R}/run.json",@())`, `[IO.File]::Create("${R}/run.json").Close()`, `[IO.File]::Open("${R}/run.json","Truncate").Close()`,
  `Remove-Item ${R}/run.json -ea 0`, `Remove-Item -Path:${R}/run.json`, `Remove-Item -Pa ${R}/run.json`, `Remove-Item -LiteralPath:${R}/run.json`, `Remove-Item -Recurse ${R}/evidence`, `rm -Recurse -Force ${R}/evidence`,
  `del -Force ${R}/run.json`, `rmdir /s /q ${R}`, `rmdir ${R}/evidence`, `Move-Item -Path C:/tmp/x -Destination ${R}/run.json -Force`, `Move-Item C:/tmp/x ${R}/run.json -Force`, `mi ${R}/run.json C:/tmp/x`,
  `Rename-Item -Path ${R}/run.json -NewName x`, `rni ${R}/run.json x`, `ren ${R}\\run.json x`, `Copy-Item -Path C:/tmp/x -Destination ${R}/run.json -Force`, `cpi C:/tmp/x ${R}/run.json`, `Copy-Item C:/tmp/ev ${R} -Recurse -Force`,
  `Clear-Content -Path ${R}/run.json`, `clc ${R}/run.json`, `Set-Content -LiteralPath ${R}/run.json x`, `sc ${R}/run.json x`, `Add-Content -Path ${R}/run.json -Value x`, `ac ${R}/run.json x`, `'x' | Out-File ${R}/run.json`,
  `'x' | Out-File -Append -FilePath ${R}/run.json`, `'x' > ${R}/run.json`, `'x' >> ${R}/run.json`, `'x' | Tee-Object -FilePath ${R}/run.json`, `'x' | Tee-Object ${R}/run.json`, `Start-Transcript -Path ${R}/run.json`,
  `Export-Csv -Path ${R}/run.json`, `$x | Export-Clixml ${R}/run.json`, `$x | ConvertTo-Json | Set-Content ${R}/run.json`, `Get-Content C:/tmp/x | Set-Content ${R}/run.json`,
  `Get-Content ${R}/run.json | Set-Content ${R}/run.json`, `(Get-Content ${R}/run.json) -replace 'a','b' | Set-Content ${R}/run.json`, `gc ${R}/run.json | sc ${R}/run.json`,
  `vim -c 'wq' ${R}/run.json`, `vim -es -c '%d|x' ${R}/run.json`, `vi +wq ${R}/run.json`, `nvim --headless -c 'w!' ${R}/run.json`, `ex -sc 'wq' ${R}/run.json`, `ed -s ${R}/run.json <<< w`, `emacs --batch ${R}/run.json -f save-buffer`,
  `nano ${R}/run.json`, `code ${R}/run.json`, `notepad ${R}/run.json`, `sed -i '' s/a/b/ ${R}/run.json`, `sed -i.bak 's/a/b/' ${R}/run.json`, `sed --in-place s/a/b/ ${R}/run.json`, `sed -ni p ${R}/run.json`,
  `sed -e s/a/b/ -i ${R}/run.json`, `gsed -i s/a/b/ ${R}/run.json`, `awk -i inplace '{print}' ${R}/run.json`, `sed 's/a/b/w ${R}/run.json' /tmp/x`, `awk '{print > "${R}/run.json"}' /tmp/x`, `sed -n 'w ${R}/run.json' /tmp/x`,
  `sort -o ${R}/run.json /tmp/x`, `sort /tmp/x -o ${R}/run.json`, `uniq /tmp/x ${R}/run.json`, `xxd -r /tmp/x ${R}/run.json`, `split -b 1 /tmp/x ${R}/evidence/`, `csplit -f ${R}/evidence/x /tmp/x 1`, `tee ${R}/run.json < /tmp/x`,
  `tee -a ${R}/run.json < /tmp/x`, `cat /tmp/x | tee /dev/null ${R}/run.json`, `cat /tmp/x | tee --append ${R}/run.json`, `cat /tmp/x | tee -- ${R}/run.json`, `truncate -s0 ${R}/run.json`, `truncate --size 0 ${R}/run.json`,
  `: > ${R}/run.json`, `> ${R}/run.json`, `>${R}/run.json`, `echo x>${R}/run.json`, `echo x 1>> ${R}/run.json`, `echo x 2>>${R}/run.json`, `echo x &>>${R}/run.json`, `echo x >& ${R}/run.json`, `echo x 3> ${R}/run.json`,
  `exec 3> ${R}/run.json`, `exec 3<>${R}/run.json`, `cat <> ${R}/run.json`, `echo x >${R}/run.json 2>&1`, `echo x 2>&1 >${R}/run.json`, `cat /tmp/x | dd of=${R}/run.json`, `dd if=/tmp/x of=${R}/run.json conv=notrunc`,
  `cp /tmp/x ${R}/run.json`, `cp -f /tmp/x ${R}/run.json`, `cp --force /tmp/x ${R}/run.json`, `cp -T /tmp/x ${R}/run.json`, `cp -t ${R} /tmp/run.json`, `cp --target-directory ${R} /tmp/run.json`, `cp -at ${R}/evidence /tmp/a`,
  `cp -r /tmp/ev/. ${R}/evidence`, `cp --remove-destination /tmp/x ${R}/run.json`, `cp -u /tmp/x ${R}/run.json`, `cp --backup=t /tmp/x ${R}/run.json`, `cp --reflink=auto /tmp/x ${R}/run.json`, `cp -- /tmp/x ${R}/run.json`,
  `cp /tmp/x /tmp/y ${R}`, `cp ${R}/metrics.json ${R}/run.json`, `cp ${R}/run.json ${R}/evidence/x`, `install /tmp/x ${R}/run.json`, `install -d ${R}/evidence/x`, `install -Dm644 /tmp/x ${R}/evidence/x`,
  `mv /tmp/x ${R}/run.json`, `mv -f /tmp/x ${R}/run.json`, `mv -t ${R} /tmp/run.json`, `mv ${R}/run.json /tmp/x`, `mv ${R}/evidence /tmp/x`, `mv -T /tmp/x ${R}/evidence`, `mv -- ${R}/run.json /tmp/x`,
  `ln /tmp/x ${R}/run.json`, `ln -f /tmp/x ${R}/run.json`, `ln -s /tmp/x ${R}/evidence/a`, `ln -sf /tmp/x ${R}/run.json`, `ln -s ${R} /tmp/l`, `ln ${R}/run.json /tmp/l`, `ln -s ${R}/evidence /tmp/l`,
  `link /tmp/x ${R}/run.json`, `link ${R}/run.json /tmp/x`, `unlink ${R}/run.json`, `shred ${R}/run.json`, `shred -n1 ${R}/run.json`, `wipe ${R}/run.json`, `srm ${R}/run.json`, `trash ${R}/run.json`, `gio trash ${R}/run.json`,
  `chmod 444 ${R}/run.json`, `chmod u-w ${R}/evidence`, `chmod -R a-w ${R}`, `chmod --recursive 000 ${R}`, `chown -R x ${R}`, `chgrp x ${R}/run.json`, `chattr +i ${R}/run.json`, `setfacl -m u:x:--- ${R}/run.json`,
  `touch ${R}/run.json`, `touch -d 2000-01-01 ${R}/run.json`, `touch -t 200001010000 ${R}/run.json`, `touch ${R}/evidence/x`, `mkdir ${R}/evidence/x`, `mkdir -p ${R}/evidence/x`, `md ${R}\\evidence\\x`, `mkfifo ${R}/run.json`,
  `mknod ${R}/run.json p`, `mktemp -p ${R}/evidence`, `mktemp ${R}/evidence/XXXX`, `rmdir ${R}/evidence`, `rmdir --ignore-fail-on-non-empty ${R}/evidence`, `rm -d ${R}/evidence`, `rm --recursive --force ${R}/evidence`,
  `rm -i ${R}/run.json`, `rm -v ${R}/run.json`, `rm -- ${R}/run.json`, `rm ./x ${R}/run.json`, `rm /tmp/x ${R}/run.json`, `rm ${R}/evidence/*`, `rm -rf ${R}/evidence/`, `rm -rf ${R}/evidence/.`, `rm -rf ${R}/evidence/..`,
  `rm ${R}/reviews/code-1.json`, `rm ${R}/ship-plan.json`, `rm ${R}/metrics.json`, `rm ${R}/baseline/x`, `rm ${R}/review-report.md`, `rm ${R}/acs.json`, `rm ${R}/checks/x`, `rm ${R}/design/x.png`, `rm ${R}/diagnosis.md`,
  `rm ~/.factory/env/ws/a.pid`, `echo 1 > ~/.factory/env/ws/a.pid`, `rm ~/.factory/env/ws/a.storage.json`]) W(t);

return P;
}

function probes3() {
// gen3.js: Probe set 3: indirection through names the guard does not model.
const R = '~/.factory/runs/r1';
const F = `${R}/run.json`;
const P = [];
const W = (c) => P.push({ cmd: c, write: true });
for (const t of [
  `q() { rm "$@"; }; q ${F}`, `function q { rm -f "$1"; }; q ${F}`, `q(){ cp /tmp/x "$1"; }; q ${F}`, `q() { : > "$1"; }; q ${F}`,
  `function q { Remove-Item $args }; q ${F}`, `function q($p) { Remove-Item $p }; q ${F}`, `Set-Alias q Remove-Item; q ${F}`, `New-Alias q Remove-Item; q ${F}`,
  `sal q ri; q ${F}`, `Set-Alias -Name q -Value Clear-Content; q ${F}`, `$c = 'Remove-Item'; & $c ${F}`, `$c = Get-Command Remove-Item; & $c ${F}`, `& (gcm ri) ${F}`, `& ('Remove'+'-Item') ${F}`,
  `X=rm; $X ${F}`, `X=rm; "$X" ${F}`, `c=(rm -f); "\${c[@]}" ${F}`, `set -- rm; "$@" ${F}`, `cmd=rm; eval $cmd ${F}`, `\${X:-rm} ${F}`, `$(echo rm) ${F}`, `\`echo rm\` ${F}`, `$(printf rm) ${F}`,
  `xargs rm <<< ${F}`, `xargs -a <(echo ${F}) rm`, `echo ${F} | xargs -I@ rm @`, `echo run.json | xargs -I@ rm ${R}/@`, `echo run.json | xargs -I% truncate -s0 ${R}/%`, `echo run.json | xargs -IQ rm ${R}/Q`,
  `echo evidence | xargs -I% rm -rf ${R}/%`, `ls /tmp | xargs -I% mv /tmp/% ${R}/%`, `echo x | xargs -I% sh -c "rm ${R}/run.json"`, `ls ${R} | xargs -n1 basename | xargs -I% rm ${R}/%`,
  `find /tmp/x -name run.json -exec cp {} ${R}/ \\;`, `find /tmp/x -exec cp {} ${R}/run.json \\;`, `find /tmp/x -name '*.json' -exec mv {} ${R}/evidence \\;`, `find /tmp/x -type f -exec install {} ${R}/evidence \\;`,
  `find ${R} -maxdepth 0 -exec rm -rf {}/evidence \\;`, `find ~/.factory -name run.json -exec rm {} +`, `find ~/.factory/runs -delete`, `find ~ -path '*/.factory/runs/*' -delete`,
  `for f in run.json; do rm ${R}/$f; done`, `for f in run metrics; do rm ${R}/$f.json; done`, `for x in 1; do rm ${F}; done`, `while true; do rm ${F}; break; done`, `until rm ${F}; do :; done`,
  `parallel rm ::: ${F}`, `env rm ${F}`, `env -- rm ${F}`, `env A=1 rm ${F}`, `sudo rm ${F}`, `doas rm ${F}`, `runas /user:x "cmd /c del ${F}"`, `start "" cmd /c del ${F}`,
  `Start-Process -FilePath cmd -ArgumentList '/c','del',"${F}"`, `Start-Process powershell "-c Remove-Item ${F}"`, `Invoke-Item ${F}`, `ii ${F}`,
  `Remove-ItemProperty ${F} -Name x`, `Set-Item ${F} x`, `si ${F} x`, `Set-Item -Path ${F} -Value x`, `Clear-Item ${F}`, `cli ${F}`, `Copy-ItemProperty ${F}`, `Move-ItemProperty`,
  `Get-ChildItem ${R} | Rename-Item -NewName x`, `Get-ChildItem ${R} -Filter run.json | Move-Item -Destination C:/tmp`, `Get-Item ${F} | Clear-Content`, `Get-Item ${F} | Remove-Item`, `gi ${F} | ri`,
  `ls ${F} | rm`, `dir ${R} | del`, `Resolve-Path ${F} | Remove-Item`, `Join-Path ${R} run.json | Remove-Item`, `"${F}" | Remove-Item`, `'${F}' | Set-Content -Value x`, `'${F}' | ri`,
  `Write-Output ${F} | Remove-Item`, `echo ${F} | Remove-Item`, `echo ${F} | Clear-Content`, `echo ${F} | Out-Null; rm ${F}`,
  `Get-Content /tmp/x | Set-Content -Path ${F}`, `Copy-Item C:/tmp/x -Destination ${R}`, `Copy-Item C:/tmp/x ${R}/evidence`, `Copy-Item -Destination ${F} -Path C:/tmp/x`, `Copy-Item -Path C:/tmp/x -Dest ${F}`, `Copy-Item C:/tmp/x -D ${F}`,
  `Copy-Item C:/tmp/x -ToSession $s -Destination ${F}`, `Copy-Item -LiteralPath C:/tmp/x -Destination ${F}`, `Copy-Item ${F} C:/tmp/x`, `Copy-Item -Path ${F} -Destination C:/tmp/x`, `Copy-Item ${R} C:/tmp/x -Recurse`,
  `copy ${F} C:\\tmp\\x`, `copy C:\\tmp\\x ${F}`, `copy /y ${F} C:\\tmp\\x`, `xcopy ${F} C:\\tmp\\x`, `xcopy /s ${R} C:\\tmp\\x`, `robocopy ${R} C:\\tmp`, `robocopy ${R} C:\\tmp run.json`, `robocopy ${R} C:\\tmp /E`,
  `robocopy ${R} C:\\tmp /MOV`, `robocopy ${R} C:\\tmp /move`, `robocopy C:\\tmp ${R} /XO`, `robocopy ${R}/evidence C:\\tmp /MIR`, `robocopy C:\\tmp\\x ${R}/evidence /MIR /NFL`,
  `cp ${F} /tmp/x`, `cp -r ${R} /tmp/x`, `cp -a ${R}/evidence /tmp/x`, `cp -p ${F} /tmp/x`, `cp -v ${F} /tmp/x`, `cp -n ${F} /tmp/x`, `cp -L ${F} /tmp/x`, `cp -H ${F} /tmp/x`, `cp -P ${F} /tmp/x`,
  `cp --parents ${F} /tmp/x`, `cp -l ${F} /tmp/x`, `cp -s ${F} /tmp/x`, `cp --link ${F} /tmp/x`, `cp --symbolic-link ${F} /tmp/x`, `cp -rl ${R} /tmp/x`, `cp -x ${F} /tmp/x`, `cp -b ${F} /tmp/x`,
  `cp -S .bak ${F} /tmp/x`, `cp --suffix .bak ${F} /tmp/x`, `cp -Z ${F} /tmp/x`, `cp --preserve=all ${F} /tmp/x`, `cp --sparse=always ${F} /tmp/x`, `cp ${F} ${R}/evidence/x`, `cp ${F} ${F}`,
  `cp ${F} /tmp/a /tmp/b`, `cp /tmp/a ${F} /tmp/b`, `cp -t /tmp/b ${F}`, `cp -t /tmp/b ${F} /tmp/a`, `cp -T ${F} /tmp/x`, `cp --no-target-directory ${F} /tmp/x`, `cp -r ${R} /tmp/x/`,
  `cp -R ${R}/. /tmp/x`, `cp -u ${F} /tmp/x`, `cp --update=none ${F} /tmp/x`, `cp --attributes-only ${F} /tmp/x`, `cp --remove-destination ${F} /tmp/x`, `cp -f ${F} /tmp/x`,
  `cp -i ${F} /tmp/x`, `cp -d ${F} /tmp/x`, `cp -t ${R}/evidence ${F}`, `cp -r /tmp/x ${R}/..`, `cp /tmp/x ${R}/../r1`, `cp /tmp/x ${R}/../../runs/r1/run.json`,
  `install ${F} /tmp/x`, `install -m 600 ${F} /tmp/x`, `install -o x ${F} /tmp/x`, `install --mode=600 ${F} /tmp/x`, `install -s ${F} /tmp/x`, `install -C ${F} /tmp/x`, `install -d /tmp/x ${R}/evidence`,
  `install -t /tmp/x ${F}`, `install -D ${F} /tmp/x/y`, `install -b ${F} /tmp/x`, `install -S x ${F} /tmp/x`,
  `rsync ${F} /tmp/x`, `rsync -a ${R}/ /tmp/x`, `rsync -av --delete ${R}/ /tmp/x`, `rsync -a --remove-source-files ${R}/ /tmp/x`, `rsync -a --delete-after /tmp/x ${R}`, `rsync --exclude x ${F} /tmp/x`,
  `rsync --link-dest ${R} /tmp/a /tmp/b`, `rsync --copy-dest=${R} /tmp/a /tmp/b`, `rsync -e ssh ${F} host:x`, `rsync --rsh=ssh ${F} host:x`, `rsync -b ${F} /tmp/x`, `rsync --backup-dir=${R} /tmp/a /tmp/b`,
  `rsync --temp-dir=${R} /tmp/a /tmp/b`, `rsync --log-file=${F} /tmp/a /tmp/b`, `rsync --write-batch=${F} /tmp/a /tmp/b`, `rsync -T ${R} /tmp/a /tmp/b`, `rsync --partial-dir=${R} /tmp/a /tmp/b`,
  `rsync --files-from=${F} / /tmp/x`, `rsync --chmod=000 /tmp/a ${R}`, `rsync -I /tmp/a ${F}`, `rsync --inplace /tmp/a ${F}`, `rsync --dry-run -a /tmp/a ${R}`,
  `scp ${F} host:x`, `scp -r ${R} host:x`, `scp -o x ${F} host:x`, `scp -S x ${F} host:x`, `scp -F ${F} a b`, `scp host:x ${F}`, `scp -3 a:x ${F}`,
  `tar -cf /tmp/a.tar ${R}`, `tar -tf ${F}`, `tar -xf ${F} -C /tmp`, `tar -xf /tmp/a.tar -C /tmp ${F}`, `tar -xvf ${R}/x.tar`, `tar xf /tmp/a.tar x`, `tar -xPf /tmp/abs.tar`,
  `tar -cf ${R}/../x.tar /tmp`, `tar -czf /tmp/a.tgz -C ${R} .`, `tar -cf - ${R} | tar -xf - -C /tmp`, `tar -xf - -C ${R} < /tmp/a.tar`, `tar --to-command=sh -xf /tmp/a.tar`, `tar -x --one-top-level=${R} -f /tmp/a.tar`,
  `unzip ${R}/x.zip -d /tmp`, `unzip -d /tmp ${R}/x.zip`, `unzip -o ${R}/x.zip`, `unzip ${R}/x.zip`, `unzip -p ${R}/x.zip > /tmp/x`, `7z x ${R}/x.7z -o/tmp`, `7z a /tmp/a.7z ${R}`, `7z a ${F} /tmp/x`,
  `Expand-Archive ${R}/x.zip -DestinationPath C:/tmp`, `Expand-Archive -LiteralPath ${R}/x.zip C:/tmp`, `Compress-Archive ${R} C:/tmp/a.zip`, `Compress-Archive -Path C:/tmp/x -DestinationPath ${R}/x.zip`,
  `gzip -c ${F} > /tmp/x.gz`, `gzip -k ${F}`, `gzip --keep ${F}`, `gzip -dc ${R}/x.gz > /tmp/x`, `zcat ${R}/x.gz`, `gzip -f ${F}`, `gzip -S .z ${F}`, `gzip -r ${R}`, `xz -k ${F}`, `bzip2 -k ${F}`, `zstd ${F}`, `zstd -o ${F} /tmp/x`,
  `zstd /tmp/x -o ${F}`, `xz -d ${R}/x.xz`, `gunzip -c ${R}/x.gz`, `gzip -t ${F}`, `gzip -l ${F}`, `gzip --stdout ${F} > /tmp/x`, `gzip -cd /tmp/x.gz > ${F}`,
  `dd if=${F} of=/tmp/x`, `dd if=${F} | cat`, `dd if=/tmp/x of=${R}/evidence/x`, `dd if=/tmp/x oflag=append conv=notrunc of=${F}`, `dd if=/tmp/x of=${F}.`, `dd if=/tmp/x 'of=${F}'`, `dd if=/tmp/x "of=${F}"`,
  `dd if=/tmp/x of="${F}"`, `dd if=/tmp/x of=$HOME/.factory/runs/r1/run.json`, `dd if=/tmp/x of=\${HOME}/.factory/runs/r1/run.json`, `dd if=/tmp/x of=%USERPROFILE%/.factory/runs/r1/run.json`,
  `dd if=/tmp/x of=$USERPROFILE/.factory/runs/r1/run.json`, `dd if=/tmp/x of=${R}/ru?.json`, `dd if=/tmp/x of=${R}/run.js\\on`, `dd if=/tmp/x of=$(echo ${F})`, `dd if=/tmp/x of=${R}/run.json::$DATA`,
  `dd if=/tmp/x of=//?/C:/Users/qzuserqz/.factory/runs/r1/run.json`, `dd if=/tmp/x of=//./C:/Users/qzuserqz/.factory/runs/r1/run.json`, `dd if=/tmp/x of=C:/Users/@U83@/.factory/runs/r1/run.json`,
  `dd if=/tmp/x of=C:/Users/qzuserqz/FACTOR~1/runs/r1/run.json`, `dd if=/tmp/x of="C:/Documents and Settings/qzuserqz/.factory/runs/r1/run.json"`, `dd if=/tmp/x of=${R}/run.json\\ `,
  `truncate -s0 ${F}`, `truncate -r /tmp/x ${F}`, `truncate --reference=/tmp/x ${F}`, `truncate -s 0 -- ${F}`, `truncate -cs0 ${F}`, `truncate -s0 /tmp/x ${F}`,
  `touch -c ${F}`, `touch -a ${F}`, `touch -m ${F}`, `touch --date=x ${F}`, `touch -h ${F}`, `touch --no-create ${F}`,
  `ln -s /tmp/x -t ${R}/evidence`, `ln -sr /tmp/x ${F}`, `ln -s -T /tmp/x ${F}`, `ln --symbolic --force /tmp/x ${F}`, `ln -b /tmp/x ${F}`, `ln -s ${F}`, `ln ${F}`, `ln -P ${F} /tmp/x`, `ln -L ${F} /tmp/x`,
  `mklink /J C:\\tmp\\j ${R}`, `mklink /D C:\\tmp\\j ${R}`, `mklink C:\\tmp\\j ${F}`, `mklink /H C:\\tmp\\j ${F}`, `cmd /c mklink /J C:\\tmp\\j ${R}`, `New-Item -ItemType Junction C:/tmp/j -Value ${R}`,
  `New-Item -ItemType SymbolicLink -Path C:/tmp/l -Value ${F}`, `New-Item -Type HardLink C:/tmp/h -Target ${F}`, `ni -it junction C:/tmp/j -tar ${R}`,
  `chmod 644 ${F}`, `chmod +w ${F}`, `chmod -w ${F}`, `chmod a=r ${F}`, `chmod --reference=/tmp/x ${F}`, `chmod -R u+w ${R}`, `chmod -v 000 ${F}`, `chmod 000 -- ${F}`, `chmod -c 000 ${F}`,
  `chown x ${F}`, `chown x: ${F}`, `chown -h x ${F}`, `chown --reference=/tmp/x ${F}`,
  `icacls ${F} /reset`, `icacls ${R} /T /deny x:F`, `icacls ${R}\\* /remove x`, `attrib -r ${F}`, `attrib +r +s ${R} /s /d`, `takeown /r /f ${R}`, `cacls ${F} /e /p x:n`,
  `git -C ${R} status`, `git -C ${R} log`, `git -C ${R} diff`, `git log -- ${F}`, `git diff --no-index ${F} /tmp/x`, `git diff --no-index --output=${F} /tmp/a /tmp/b`, `git diff --output=/tmp/x ${F}`, `git show HEAD:x > ${F}`,
  `git -C /tmp/g checkout x -- ${F}`, `git checkout -- ${F}`, `git restore ${F}`, `git restore --worktree ${F}`, `git rm ${F}`, `git mv ${F} /tmp/x`, `git clean -f ${R}`, `git clean -fdx -- ${R}`,
  `git stash push -- ${F}`, `git apply --directory=${R} /tmp/p`, `git apply --unsafe-paths --directory=${R} /tmp/p`, `git apply -p0 /tmp/p`, `git -c core.worktree=${R} checkout -f`, `git --work-tree ${R} checkout -f`,
  `git --git-dir ${R}/.git status`, `git --git-dir=${R}/x init`, `git -C ${R}/.. clean -fdx`, `git -C ~/.factory clean -fdx`, `git -C ~/.factory/runs checkout .`, `git -C ~/.factory reset --hard`,
  `git worktree add ${R}/x`, `git clone x ${R}`, `git clone x ${R}/evidence`, `git submodule add x ${R}/evidence`, `git archive -o ${F} HEAD`, `git archive --output=${F} HEAD`, `git bundle create ${F} HEAD`,
  `git fetch x && git -C ${R} merge`, `git -C ${R} pull`, `git -C ${R} reset --hard`, `git -C ${R} stash`, `git -C ${R} switch x`, `git -C ${R} restore .`, `git -C ${R} rm -rf .`, `git -C ${R} mv a b`,
  `git -C ${R} apply x`, `git -C ${R} checkout x`, `git -C ${R} checkout-index -fa`, `git -C ${R} am x`, `git -C ${R} merge x`, `git -C ${R} rebase x`, `git -C ${R} cherry-pick x`, `git -C ${R} revert x`, `git -C ${R} read-tree -um x`,
  `git -C ${R} update-index --chmod=+x run.json`, `git -C ${R} sparse-checkout set x`, `git -C ${R} lfs pull`, `git -C ${R} filter-branch`, `git -C ${R} gc`,
]) W(t);
return P;
}

function probes4() {
const r = String.raw;
const R = '~/.factory/runs/r1';
const F = `${R}/run.json`;
const P = [];
const W = (c) => P.push({ cmd: c, write: true });
for (const t of [
  // cwd tracking gaps
  `cd ${R}; cd /tmp; cd -; rm run.json`, `cd ${R} && cd /tmp && cd - && rm -rf evidence`, `pushd ${R}; pushd /tmp; popd; rm run.json`, `pushd ${R} && pushd /tmp && popd && echo x > run.json`,
  `cd ${R}; cd /tmp; cd "$OLDPWD"; rm run.json`, `cd ${R}; cd /tmp; cd $OLDPWD && rm run.json`, `Push-Location ${R}; rm run.json`, `Push-Location ${R}; Remove-Item run.json`,
  `pushd ${R}; cd /tmp; popd; Remove-Item run.json`, `Set-Location ${R}; Set-Location /tmp; Set-Location -; Remove-Item run.json`, `cd ${R}; cd /tmp; cd ~-; rm run.json`,
  `cmd /c "cd /d ${R} && del run.json"`, `cmd /c "cd /d %USERPROFILE%\\.factory\\runs\\r1 && del run.json"`, `cmd /c "pushd ${R} && del run.json"`, `cmd /c "cd /d ${R} & rd /s /q evidence"`,
  `cd /d ${R} && del run.json`, `chdir /d ${R} & del run.json`, `cd ${R} && cmd /c del run.json`, `cd ${R}; bash -c "rm run.json"`, `cd ${R}; sh -c 'rm -rf evidence'`,
  `cd ${R}; tar xf /tmp/a.tar`, `cd ${R}; curl -O http://x/run.json`, `cd ${R}; wget http://x/run.json`, `cd ${R}; git apply /tmp/p`, `cd ${R}; 7z x /tmp/a.7z -y`,
  `cd ${R}; Expand-Archive C:/tmp/a.zip . -Force`, `cd ${R}; split -b1 /tmp/x run.json`, `cd ${R}; mkdir -p evidence/x`, `cd ${R}; ln -sf /tmp/x run.json`,
  `cd ${R}; zip -m /tmp/a.zip run.json`, `cd ${R}; gzip run.json`, `cd ${R}; python -m zipfile -e /tmp/a.zip .`, `cd ${R}; npx rimraf evidence`, `cd ${R} && make clean`,
  `cd ${R}/evidence; rm *`, `cd ${R}/evidence && rm -f -- *`, `cd ${R}/evidence && find . -type f -delete`, `cd ${R}/evidence && ls | xargs rm`,
  `cd ${R} && for f in *; do rm "$f"; done`, `cd ${R}; rm -rf -- "$(pwd)"`, `cd ${R}; rm -rf $PWD/evidence`, `cd ${R}; rm -rf "$PWD"`, `cd ${R} && rm -rf ../r1`,
  // function / alias / positional indirection (bash and PowerShell)
  `zap() { rm -rf "$1"; }; zap ${R}/evidence`, `w() { tee "$1" >/dev/null; }; echo x | w ${F}`, `put() { cp "$1" "$2"; }; put /tmp/x ${F}`, `t() { truncate -s0 "$@"; }; t ${F}`,
  `mvv() { mv "$@"; }; mvv ${F} /tmp/x`, `function z() { Remove-Item -Recurse -Force $args[0] }; z ${R}/evidence`, `filter z { Remove-Item $_ }; '${F}' | z`,
  `Set-Alias wipe Clear-Content; wipe ${F}`, `Set-Alias -Name sv2 -Value Set-Content; sv2 ${F} x`, `New-Alias mm Move-Item; mm ${F} C:/tmp/x`, `Set-Alias cpx Copy-Item; cpx C:/tmp/x ${F}`,
  `set -- rm -rf; "$@" ${R}/evidence`, `set -- truncate -s0; "$@" ${F}`, `set -- cp /tmp/x; "$@" ${F}`, `a=(rm -f); "\${a[@]}" ${F}`,
  // xargs -I replacement
  `echo run.json | xargs -I % cp /tmp/x ${R}/%`, `echo run.json | xargs -I% tee ${R}/% < /tmp/x`, `echo run.json | xargs -iX rm ${R}/X`, `echo run.json | xargs --replace=Z rm ${R}/Z`,
  `printf 'run.json\\n' | xargs -I_ ln -sf /tmp/x ${R}/_`, `echo evidence | xargs -I% chmod -R 000 ${R}/%`, `echo run | xargs -I% rm ${R}/%.json`,
  // quote loss in nested shell bodies
  `cmd /c del "C:/Users/All Users/../qzuserqz/.factory/runs/r1/run.json"`, `cmd /c rd /s /q "C:/Users/All Users/../qzuserqz/.factory/runs/r1/evidence"`,
  `bash -c "rm '/c/Users/All Users/../qzuserqz/.factory/runs/r1/run.json'"`, `bash -c 'rm "/c/Users/All Users/../qzuserqz/.factory/runs/r1/run.json"'`,
  `powershell -Command "Remove-Item 'C:/Users/All Users/../qzuserqz/.factory/runs/r1/run.json'"`, `cmd /c "del \\"C:/Program Files/../Users/qzuserqz/.factory/runs/r1/run.json\\""`,
  `bash -c 'rm "/c/Program Files/../Users/qzuserqz/.factory/runs/r1/run.json"'`, `rm "/c/Program Files/../Users/qzuserqz/.factory/runs/r1/run.json"`, `rm "C:/Users/All Users/../qzuserqz/.factory/runs/r1/run.json"`,
  // escapes per shell
  r`rm C:\\Users\\qzuserqz\\.factory\\runs\\r1\\run.jso\n`, r`rm -rf C:\\Users\\qzuserqz\\.factory\\runs\\r1\\evidenc\e`, r`rm /c/Users/qzuserqz/.factory/runs/r1/run.jso\n`, r`rm \~/.factory/runs/r1/run.json`,
  r`Remove-Item C:\Users\qzuserqz\.fac` + '`' + r`tory\runs\r1\run.json`, r`Remove-Item "C:\Users\@UTK3@\.factory\runs\r1\run.json"`, r`cmd /c del C:\Users\qzuserqz\.factory\runs\r1\run.jso^n`,
  r`cmd /c "del C:\Users\qzuserqz\.factory\runs\r1\run.jso^n"`, r`cmd /c del C:\Users\@UCARET@\.factory\runs\r1\run.json`, r`cmd /c rd /s /q C:\Users\@UCARET@\.factory\runs\r1\evidence`,
  r`cmd /c "copy /y C:\tmp\x C:\Users\@UCARET@\.factory\runs\r1\run.json"`, r`cmd /c "echo x > C:\Users\@UCARET@\.factory\runs\r1\run.json"`, r`cmd /c "type nul > C:\Users\qzuserqz\.factory\runs\r1\run.js^on"`,
  r`cmd /c del "C:\Users\qzuserqz\.factory\runs\r1\run.json"`, r`cmd /c del %USERPROFILE:~0%\.factory\runs\r1\run.json`, r`cmd /c del %USERPROFILE:x=x%\.factory\runs\r1\run.json`,
  r`cmd /c "set R=C:\Users\qzuserqz\.factory\runs\r1&& call del %R%\run.json"`, r`cmd /v /c "set R=C:\Users\qzuserqz\.factory\runs\r1&& del !R!\run.json"`,
  `rm $'\\x7e'/.factory/runs/r1/run.json`, `rm $"$HOME"/.factory/runs/r1/run.json`, `rm "$HOME"/.factory/runs/r1/run.json`, `rm "\${HOME}/.factory/runs/r1/run.json"`,
  `rm ~/.factory/runs/r1/run.json\\ `, `rm "~/.factory/runs/r1/run.json"`, `rm ~/.factory/runs/R1/RUN.JSON`, `rm ~/.factory/runs/r1//run.json`, `rm ~/./.factory/./runs/r1/run.json`,
  // dd and other key=value with odd spellings
  `dd if=/dev/null of=~qzuserqz/.factory/runs/r1/run.json`, `dd if=/dev/null of=~/.factory/runs/r1/run.json`, `dd if=/dev/null of=//localhost/c$/Users/qzuserqz/.factory/runs/r1/run.json`,
  `dd if=/dev/null of=//127.0.0.1/C$/Users/qzuserqz/.factory/runs/r1/run.json`, `dd if=/dev/null of=C:\\\\Users\\\\@UBS@\\\\.factory\\\\runs\\\\r1\\\\run.json`,
]) W(t);
return P;
}

// Fourth audit (generators audit4/p1..p4, x1..x5, legit; the fake home of the audit becomes this machine's home,
// its scratch folder the temp dir as @TMP@). The junction, hard-link and repository set-up of x5/p4 is not reproduced
// here (decision data only): guard-provable.test.mjs builds those on disk.
function probes5() {
const H = 'C:/Users/qzuserqz';
const R = `${H}/.factory/runs/r1`;
const CR = '/proc/cygdrive/c/Users/qzuserqz/.factory/runs/r1';
const S = '@TMP@/scratch';
const ST = `${H}/.factory/env/ws/React.storage.json`;
const RB = R.replace(/\//g, '\\');
const bytes = (s) => `bytes([${[...Buffer.from(s)].join(',')}]).decode()`;
const P = [];
const W = (c) => P.push({ cmd: c, write: true });
const L = (c) => P.push({ cmd: c, write: false });
for (const t of [
  // p1 / x1: the /proc/cygdrive mount (R1), other mounts, case and non-ASCII letters
  `rm ${CR}/run.json`, `cp ${S}/forged.json ${CR}/run.json`, `mv ${S}/forged.json ${CR}/run.json`, `rm -rf ${CR}/evidence`, `touch ${CR}/evidence/CHK-9.json`,
  `sed -i s/orig/forged/ ${CR}/run.json`, `echo x > ${CR}/run.json`, `cat ${S}/forged.json > ${CR}/run.json`, `cd /proc/cygdrive/c/Users/qzuserqz && rm .factory/runs/r1/run.json`,
  `mkdir ${CR}/reviews/x`, `rm -r ${CR}`, `rm -r /proc/cygdrive/c/Users/qzuserqz/.factory/runs`, `cp -r ${S}/ev ${CR}/evidence`, `rsync -a ${S}/forged.json ${CR}/run.json`,
  `rm ${CR.replace('/c/', '/C/')}/run.json`, `rm /cygdrive/c/Users/qzuserqz/.factory/runs/r1/run.json`, `rm /c/Users/qzuserqz/.factory/runs/r1/run.json`,
  `rm /Users/qzuserqz/.factory/runs/r1/run.json`, `rm /proc/self/root/../c/Users/qzuserqz/.factory/runs/r1/run.json`, `rm /proc/self/cwd/run.json`,
  `rm ${R.toUpperCase()}/RUN.JSON`, `rm ${R.replace('/runs/', '/run\u017f/')}/run.json`, `rm ${R}/run.j\u017fon`, `touch ${R}/ev\u0131dence/CHK-9.json`,
  `rm ${R}/run\uFF0Ejson`, `mkdir ${R}/ev\u0131dence/x`,
  `cp ${S}/forged.json ${CR}/run.json && cat ${R}/run.json`, `cd /proc/cygdrive/c/Users/qzuserqz && cp ${S}/x .factory/runs/r1/run.json && cat ${R}/run.json`,
  // p2: readers with write flags, cp/rsync flags, writers, sed, python, git, variables, cd, pipes
  `sort -o ${R}/run.json ${S}/forged.json`, `sort -o${R}/run.json ${S}/f`, `sort --output=${R}/run.json ${S}/f`, `sort -mo ${R}/run.json ${S}/f`,
  `sort -to ${S}/f > ${R}/notes/x`, `diff --to-file=${S}/f ${R}/run.json`, `diff -D X ${S}/f ${R}/run.json`,
  `gzip -cf ${R}/run.json`, `gzip -d ${R}/evidence/x.gz`, `gzip -cd ${R}/evidence/x.gz -S x`, `gunzip -c -f ${R}/run.json.gz`,
  `gzip -c --keep ${R}/run.json`, `tee ${R}/run.json`, `cat ${S}/f | tee ${R}/run.json`,
  `jq . ${R}/run.json > ${R}/run.json`, `jq --in-place . ${R}/run.json`, `grep -r x ${R} > ${R}/notes/n`,
  `find ${R} -delete`, `xxd -r ${S}/f ${R}/run.json`, `head -c 0 ${R}/run.json > ${R}/run.json`,
  `echo x >> ${R}/run.json`, `echo x 1>${R}/run.json`, `echo x >| ${R}/run.json`, `echo x 2> ${R}/run.json`, `echo x >"${R}/run.json"`,
  `Get-Content ${R}/run.json | Set-Content ${R}/run.json`,
  `cp -t ${R}/evidence ${S}/f`, `cp --target-directory=${R}/evidence ${S}/f`, `cp ${S}/f --target-directory ${R}`, `cp -T ${S}/f ${R}/run.json`,
  `cp -l ${S}/f ${R}/run.json`, `cp -s ${S}/f ${R}/x`, `cp --backup ${S}/f ${R}/remote/x`, `cp -rf ${S}/ev ${R}/evidence`,
  `cp ${R}/remote/x ${R}/remote/y`, `cp ${R}/run.json ${R}/remote/run.json`, `cp ${S}/f ${R}/remote`,
  `cp ${S}/f ${R}`, `cp ${S}/f ${H}/.factory/runs`, `cp ${S}/f ${H}/.factory/env/ws/React.pid`, `cp ${S}/f ${H}/.factory/env/ws/new.txt`,
  `cp -- ${S}/f ${R}/run.json`, `cp ${S}/f -- ${R}/run.json`, `cp -a ${S}/f ${R}/remote/../run.json`, `cp ${S}/f ${R}/./run.json`,
  `cp ${S}/f ${R}/remote//../run.json`, `cp ${S}/f ${R}/run.json.`, `cp ${S}/f '${R}/run.json '`, `cp ${S}/f ${R}/run.json::DATA`,
  `cp ${S}/f "${R}"/run.json`, `cp ${S}/f ${R}/"run.json"`, `cp ${S}/f '${R}/run.json'`, `cp -L ${S}/f ${R}/run.json`,
  `rsync --delete ${S}/e/ ${R}/remote/`, `rsync --remove-source-files ${R}/run.json ${S}/x`,
  `rsync --files-from=${S}/l ${S} ${R}`, `rsync --temp-dir=${R}/evidence ${S}/f ${S}/g`, `rsync --log-file=${R}/run.json ${S}/f ${S}/g`,
  `rsync -n ${S}/f ${R}/run.json`, `rsync ${S}/f ${R}/remote/`,
  `rm -rf ${R}/remote`, `rm -rf ${R}/remote/..`, `rm -r ${R}/notes ${R}/evidence`, `rm -- ${R}/run.json`, `rm -rf ${H}/.factory/runs`,
  `rmdir ${R}/reviews`, `rmdir -p ${R}/remote/a`, `mkdir ${R}/evidence2`, `mkdir -p ${R}/evidence/x`, `mkdir ${R}/checks`, `touch ${R}/run.json.tmp`,
  `touch ${R}/run.json`, `touch -r ${R}/run.json ${S}/f`, `mv ${R}/remote ${R}/evidence`, `mv ${R}/remote/a ${R}/remote/b`, `mv ${R}/evidence ${S}/e`,
  `mv ${R}/remote ${S}/r`, `mv ${S}/r ${R}/remote`, `mv -t ${R} ${S}/f`, `mv ${S}/f ${R}/remote/run.json`, `mv ${R} ${S}/r1`, `mv ${S}/x ${R}/acs.json`,
  `mv ${S}/x ${R}/diagnosis.md`, `mkdir ${R}/design`, `touch ${R}/checks/x`, `mv ${S}/x ${R}/review-report.md`, `mv ${S}/x ${R}/metrics.json`,
  `rm ${ST}`, `rm -r ${H}/.factory/env`, `rm -r ${H}/.factory/env/ws`, `touch ${H}/.factory/env/ws/x.pid`,
  `rm -r ${R}/../_inbox`, `rm -r ${H}/.factory/runs/_inbox/s1`, `rm -r ${H}/.factory/runs/_inbox`,
  `sed -i s/a/b/w${R}/run.json ${S}/f`, `sed -i 's/a/b/w ${R}/run.json' ${S}/f`, `sed -i s,a,b, ${R}/run.json`,
  `sed -f ${S}/s ${R}/run.json`, `sed s/a/b/ ${R}/run.json -i`, `sed s/a/b/ -i ${R}/run.json`, `sed s/a/b/ ${R}/run.json > ${R}/run.json`,
  `sed -i s/a/b/ ${S}/f ${R}/run.json`, `sed -i -- s/a/b/ ${R}/run.json`, `sed -z -i s/a/b/ ${R}/run.json`,
  `sed -i s/a/b/I ${R}/run.json`, `sed -ni s/a/b/ ${R}/run.json`, `sed -in s/a/b/ ${R}/run.json`, `sed -i -n s/a/b/ ${R}/notes/x`,
  `python -c "open('${R}/run.json','w')"`, `python -c "import json;json.dump(1,open('${R}/run.json'))"`, `python -c "import sys;sys.stdout=open('${R}/run.json')"`,
  `python -m json.tool ${R}/run.json ${R}/run.json`, `python -m factory run show r1 > ${R}/run.json`,
  `python3 -c "print(1)" > ${R}/run.json`, `python -c "a=open;a('${R}/run.json','w')"`,
  `python -c "import json,sys;json.dumps(1)" > ${R}/notes/x`, `python -c "import sys;sys.stdout.write(open('${S}/f').read())" > ${R}/run.json`,
  `git -C ${S}/repo checkout -- ${R}/run.json`, `git checkout -- ${R}/run.json`, `git -C ${R} checkout .`, `git -C ${H}/.factory checkout .`,
  `git -c core.worktree=${R} checkout .`, `git log --output=${R}/run.json`, `git log --output ${R}/run.json`, `git diff > ${R}/run.json`, `git show HEAD:x > ${R}/run.json`,
  `git commit -F ${R}/run.json`, `git restore ${R}/run.json`, `git -C ${S}/repo add ${R}/run.json`,
  `HOME=${S} rm ~/run.json`, `FACTORY_HOME=${S} factory run show r1; rm ${R}/run.json`, `rm $HOME/.factory/runs/r1/remote/x`, `rm ~/.factory/runs/r1/remote/x`,
  `cd ${R} && rm run.json`, `cd ${H}/.factory && rm -r runs`, `cd ${H} && rm -r .factory/runs/r1/evidence`, `cd && rm -r .factory/runs/r1`,
  `Set-Location -Path ${R}; rm run.json`, `sl ${R}; rm run.json`, `cd ${S}; cd ${H}; rm .factory/runs/r1/run.json`,
  `echo ${R}/run.json | xargs rm`, `factory.cmd run show r1 > ${R}/run.json`, `factory run show r1 | tee ${R}/run.json`,
  `rm ${S}/a,${R}/run.json`, `rm ${S}/a ${R}/run.json`, `Remove-Item ${R}/run.json`, `ri ${R}/run.json`,
  // p3: indirection, xargs, junction aliases, escapes, flag-attached values, shells, cwd tracking, heredocs
  `wipe() { rm -r "$1"; }; wipe ${R}/evidence`, `alias k=rm; k ${R}/run.json`, `alias k='rm -f'; k ${R}/run.json`, `Set-Alias k Remove-Item; k ${R}/run.json`,
  `nal k mv; k ${S}/f ${R}/run.json`, `set -- cp ${S}/f; "$@" ${R}/run.json`, `f() { "$@"; }; f rm ${R}/run.json`, `function g { Clear-Content $args }; g ${R}/run.json`,
  `echo run.json | xargs -IZ rm ${R}/Z`, `ls ${S} | xargs -I{} cp ${S}/{} ${R}/evidence/{}`, `echo run.json | xargs -n1 -I_ touch ${R}/_`, `echo x | xargs --replace rm ${R}/run.json`,
  `rm ${R.replace('.factory', '.fact\\ory')}/run.json`, `rm ${RB}\\run.json`, `rm ${R}/ru\\n.json`, `cp ${S}/f ${R}/evidenc\\e/x`, `rm ${R}/run.js\\\non`,
  `cp -t${R}/evidence ${S}/f`, `cp --target-directory ${R}/reviews ${S}/f`, `mv --target-directory=${R} ${S}/run.json`, `rm --interactive=never ${R}/run.json`,
  `Remove-Item -Path:${R}/run.json`, `Copy-Item ${S}/f -Destination:${R}/run.json`, `sort -o${R}/metrics.json ${S}/f`, `touch -d2020-01-01 ${R}/run.json`,
  `cmd //c del ${RB}\\run.json`, `cmd.exe /C erase ${RB}\\run.json`, `cmd /k "rd /s /q ${RB}\\evidence"`, `powershell -c "rm ${R}/run.json"`, `bash -c "rm ${R}/run.json"`,
  `sh -c 'mv ${S}/f ${R}/run.json'`, `pushd ${R}; rm run.json`, `Push-Location -LiteralPath ${R}; Remove-Item run.json`, `(cd ${R}; rm run.json)`, `{ cd ${R}; rm run.json; }`,
  `cd ${R} || rm run.json`, `sl -Path ${R}; ri run.json`, `chdir ${R} && touch evidence/x`,
  `cat <<'EOF' > ${R}/run.json\n{"h":1}\nEOF`, `tee ${R}/run.json <<'EOF'\nx\nEOF`, `cat <<'EOF' > ${CR}/run.json\n{"h":1}\nEOF`,
  // x2 / x3 / x4: PowerShell 5.1 re-quoting (R2)
  `python -c 'print("exec(${bytes(`open(r'${R}/run.json','w').write('{"ps51":1}')`)})")'; cp ${R}/remote/x ${S}/y`,
  `python -c 'print("getattr(open(${bytes(`${R}/run.json`)},chr(119)),${bytes('write')})(${bytes('{"ps51b":1}')})")'; cp ${R}/remote/x ${S}/y`,
  `python -c 'print("getattr(open(${bytes(`${R}/run.json`)},chr(119)),${bytes('write')})(${bytes('{"ps51b":1}')})")'; rm -r ${R}/remote`,
  `sed s/orig/forged/ 'q "q -i" q' ${R}/run.json`, `sed -n s/orig/forged/ 'q "q -i" q' ${R}/run.json`,
  `sed s/orig/forged/ 'q "q -i" q' ${R}/run.json && cp ${R}/remote/x ${S}/y`,
  // x5 / p4: TOCTOU through a moved or copied junction (R3), git with a foreign work tree (R4), PowerShell cp -L, factory.cmd
  `mv ${S}/D ${S}/E && cp ${S}/forged.json ${S}/E/J/run.json && cp ${R}/remote/x ${S}/y`, `mv ${S}/D ${S}/E; cp ${S}/forged.json ${S}/E/J/run.json; cp ${R}/remote/x ${S}/y`,
  `cp ${S}/forged.json ${S}/D/J/run.json && cp ${R}/remote/x ${S}/y`, `git -C ${S}/repo checkout -f HEAD -- run.json && cp ${R}/remote/x ${S}/y`,
  `git -C C:/Users/qzuserqz/Code/dev.factory log -1 --output=${R}/run.json`,
  `cp -r ${S}/A ${S}/B && cp ${S}/forged.json ${S}/B/J/run.json && cat ${R}/run.json`, `cp -a ${S}/A ${S}/B && cat ${S}/forged.json > ${S}/B/J/run.json && cat ${R}/run.json`,
  `cp -r ${S}/A ${S}/B && cd ${S}/B/J && cat ${S}/forged.json > run.json && cat ${R}/run.json`, `cp ${R}/run.json -L ${S}/forged.json`, `cp -pv ${R}/run.json ${S}/x`,
  `factory run show r1 'x&echo>${R}/run.json'; cp ${R}/remote/x ${S}/y`, `factory.cmd run show r1 'a" & echo x > ${R}/run.json & "'; cp ${R}/remote/x ${S}/y`,
]) W(t);
for (const t of [
  // legit: what must stay allowed (or is a documented false positive)
  `cat ${R}/run.json`, `grep -r pass ${R}/evidence`, `jq .status ${R}/evidence/CHK-1.json`, `Get-Content ${R}/run.json`, `cat ~/.factory/runs/r1/run.json`,
  `cp ${R}/remote/x ${S}/x`, `cp ${ST} ${S}/x.json`, `sed -i 's/a/b/' ${S}/f && cat ${R}/run.json`,
  `factory run human r1 "delete the spec; rm ${R}/run.json"`, `git -C C:/Users/qzuserqz/Code/dev.factory checkout main; cat ${R}/run.json`,
  `cp ${R}/run.json ${S}/run.json`, `cat ${R}/run.json | jq .id`, `ls ${R}/evidence && cat ${R}/evidence/CHK-1.json`,
  `python -c "import json; print(json.load(open('${R}/run.json'))['orig'])"`, `cp -r ${R}/evidence ${S}/ev`, `cp ${S}/n.md ${R}/notes/n.md`,
  `gzip -c ${R}/run.json > ${S}/x.gz`, `ls -R ${R} > ${S}/l`, `cat $HOME/.factory/runs/r1/run.json > ${S}/x`, `cp $env:USERPROFILE/.factory/runs/r1/run.json ${S}/x`,
  `cat ${R}/run.json | sed -i s/a/b/ ${S}/f`, `cd ${S} && echo x > run.json && cat ${R}/run.json`, `cp ${R}/remote/x ${S}/y`,
]) L(t);
return P;
}

/** Every probe, with the placeholders filled for `home` (default: this machine's home directory). */
module.exports = function auditCorpus(home = os.homedir()) {
  const u = path.basename(home);
  const at = (i, c) => u.slice(0, i) + c + u.slice(i);
  const forms = [['@U83@', `${u.toUpperCase().slice(0, 6)}~1`], ['@UCARET@', at(3, '^')], ['@UBS@', at(3, String.fromCharCode(92))],
    ['@UTICK@', at(2, '`')], ['@UTK3@', at(3, '`')], ['qzuserqz', u], ['QZUSERQZ', u.toUpperCase()], ['@TMP@', os.tmpdir().replace(/\\/g, '/')]];
  const fill = (s) => forms.reduce((acc, [k, v]) => acc.split(k).join(v), s);
  return [...probes1(), ...probes2(), ...probes3(), ...probes4(), ...probes5()].map((p) => ({ ...p, cmd: fill(p.cmd) }));
};
