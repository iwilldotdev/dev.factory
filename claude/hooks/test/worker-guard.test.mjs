import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { runHook, denied, bash, write, RUN, FH_POSIX, TMP } from './helpers.mjs';

const guard = (role, input) => runHook('worker-guard.js', [role], input);

// Ported from the dev.harness proposed guard tests (scout Bash allowlist).
const SCOUT_CASES = [
  ['git status', 1], ['git -C "C:/Users/x/repo" log --oneline -5', 1], ['cd C:/repo && git log -3', 1],
  ['git worktree list', 1], ['git log --oneline | head -20', 1], ['git branch -a --merged origin/main', 1],
  ['git status && git log -1', 1], ['git for-each-ref refs/heads | grep DEV', 1], ['git tag --list', 1],
  ['git push', 0], ['git -c core.pager=evil log', 0], ['git log --output=x', 0], ['git grep -Ofoo bar', 0],
  ['git diff --ext-diff', 0], ['git log > out.txt', 0], ['git log; rm -rf .', 0], ['git log | sort -o x', 0],
  ['git log $(whoami)', 0], ['git log & rm x', 0], ['cd x', 0], ['cd x && rm y', 0], ['git worktree add x', 0],
  ['git tag -d v1', 0], ['git branch -D foo', 0], ['git config user.name x', 0], ['git log | xargs rm', 0],
  ['git remote add x y', 0], ['git log || rm x', 0], ['git stash', 0],
];

test('scout Bash allowlist (ported cases)', () => {
  for (const [cmd, allow] of SCOUT_CASES) {
    const res = guard('scout', bash(cmd));
    assert.equal(!denied(res), Boolean(allow), `${allow ? 'should allow' : 'should deny'} ${JSON.stringify(cmd)}`);
  }
});

test('scout writes only its report under runs/<id>/notes/', () => {
  assert.ok(!denied(guard('scout', write(`${RUN}/notes/scout-1.md`))));
  assert.ok(denied(guard('scout', write(`${RUN}/acs.json`))));
  assert.ok(denied(guard('scout', write(path.join(TMP, 'x.md')))));
  assert.ok(denied(guard('scout', write('/repo/README.md', '/repo', 'Edit'))));
  assert.ok(denied(guard('scout', write(`${RUN}/evidence/CHK-1.json`))));
});

test('fails closed on unknown role and unreadable input', () => {
  assert.ok(denied(guard('boss', bash('git status'))));
  assert.ok(denied(guard('builder', 'nope')));
});

test('forbidden git and remote CLIs for every writing role', () => {
  const cmds = ['git push', 'git push origin HEAD', 'git -C x push', 'git reset --hard HEAD~1', 'git stash',
    'git checkout -- src/a.ts', 'git checkout -f main', 'git restore src/a.ts', 'git clean -fd',
    'curl -X POST https://api.example/x -d {}', 'npm test && curl --request DELETE https://api.example/x'];
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of cmds) assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  }
});

test('auditor and author never commit or move the worktree', () => {
  for (const role of ['auditor', 'author']) {
    for (const cmd of ['git add .', 'git commit -m x', 'git checkout main', 'git switch x', 'git rebase main',
      'git cherry-pick abc']) {
      assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
    }
    assert.ok(!denied(guard(role, bash('git log -3 && git diff main...HEAD'))));
  }
  assert.ok(!denied(guard('builder', bash('git add -A && git commit -m "[X] - y"'))));
  assert.ok(!denied(guard('builder', bash('git checkout -b feature/x'))));
});

test('factory subcommands reserved for the main session', () => {
  const reserved = ['factory gate r prove', 'factory ship plan r', 'factory feedback ingest r',
    'factory run human r "x"', 'factory run human r "ok" --type approval', 'factory run start acme DEV-1', 'python -m factory gate r plan',
    'factory --json gate r build', 'C:/p/bin/factory.cmd ship verify r', 'factory workspace import a --from x',
    'factory run replan r "x"', 'factory run rewind r prove "x"', 'factory run decide r "x" --by user',
    'python -m factory run unblock r 0', 'factory run delete r --yes', 'factory run claim r', 'factory run session r', 'factory run session r abc-123',
    'factory run target-ticket r pack BUG-2', 'factory run env-only r React', 'factory run env-only r React --off',
    'factory run mirror r React --to pack'];
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of reserved) assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
  }
  assert.ok(!denied(guard('builder', bash('factory prove r --check CHK-1'))));
  assert.ok(!denied(guard('author', bash('factory design expect r frame-1 --selector "[data-testid=d]"'))));
  assert.ok(denied(guard('builder', bash('factory env up acme React --run r'))), 'env up is the main session');
  assert.ok(!denied(guard('builder', bash('factory env check acme React --run r'))));
  assert.ok(!denied(guard('auditor', bash('factory run status r'))));
});

test('mirror-diff is read-only for workers; mirroring and the divergence allowance stay main-session', () => {
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of ['factory run mirror-diff r', 'factory run mirror-diff r --json', 'python -m factory run mirror-diff r']) {
      assert.ok(!denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
    }
  }
  for (const role of ['builder', 'author', 'auditor']) {
    for (const cmd of ['factory run mirror r React --to pack', 'factory gate r build --allow-mirror-divergence 2',
      'factory ship plan r --allow-mirror-divergence 2',
      'factory run decide r "mirror diff: x" --by user --user-said "ok"']) {
      assert.ok(denied(guard(role, bash(cmd))), `${role}: ${cmd}`);
    }
  }
});

test('only the auditor records reviews', () => {
  const cmd = `factory review record r --kind qa --file ${TMP}/qa.json`;
  assert.ok(!denied(guard('auditor', bash(cmd))));
  assert.ok(denied(guard('builder', bash(cmd))));
  assert.ok(denied(guard('author', bash(cmd))));
});

test('no role writes protected run files', () => {
  for (const role of ['scout', 'author', 'builder', 'auditor']) {
    for (const rel of ['run.json', 'metrics.json', 'ship-plan.json', 'evidence/CHK-1.json', 'reviews/qa-1.json',
      'baseline/React.json']) {
      assert.ok(denied(guard(role, write(`${RUN}/${rel}`))), `${role} ${rel}`);
      assert.ok(denied(guard(role, write(`${RUN}/${rel}`, undefined, 'Edit'))), `${role} edit ${rel}`);
    }
  }
  for (const role of ['author', 'builder', 'auditor']) {
    assert.ok(denied(guard(role, bash(`echo '{"status":"pass"}' > ${RUN}/evidence/CHK-1.json`))), role);
    assert.ok(denied(guard(role, bash(`cp /tmp/r.json ${RUN}/reviews/code-1.json`))), role);
  }
});

test('author write scope', () => {
  for (const rel of ['acs.json', 'checks/browser/ac-1.spec.ts', 'checks/style/drawer.json', 'design/1-2.json',
    'raw/ticket/ACME-1.json', 'notes/diagnosis.md', 'diagnosis.md']) {
    assert.ok(!denied(guard('author', write(`${RUN}/${rel}`))), rel);
  }
  assert.ok(!denied(guard('author', write(path.join(TMP, 'scratch', 'plan.json')))));
  assert.ok(!denied(guard('author', write('/repo/docs/design.md', '/repo'))));
  assert.ok(denied(guard('author', write('/repo/src/app.ts', '/repo'))));
  assert.ok(denied(guard('author', write(`${FH_POSIX}/secrets/acme.env`))));
  assert.ok(denied(guard('author', write(`${FH_POSIX}/workspaces/acme/workspace.json`))));
});

test('builder write scope', () => {
  assert.ok(!denied(guard('builder', write('/wt/dev-1-react/src/app.tsx', '/wt/dev-1-react'))));
  assert.ok(denied(guard('builder', write(`${RUN}/checks/browser/ac-1.spec.ts`))));
  assert.ok(denied(guard('builder', write(`${RUN}/acs.json`))));
  assert.ok(denied(guard('builder', write(`${RUN}/diagnosis.md`))));
  assert.ok(denied(guard('builder', write(`${RUN}/design/1-2.json`))));
  assert.ok(denied(guard('builder', write(`${RUN}/contracts/ticket/ACME-1.json`))));
  assert.ok(!denied(guard('builder', write(`${RUN}/notes/b.md`))));
  assert.ok(!denied(guard('builder', write(`${RUN}/raw/ticket/ACME-1.json`))));
  assert.ok(denied(guard('builder', write(`${FH_POSIX}/secrets/acme.env`))));
});

test('auditor write scope', () => {
  assert.ok(!denied(guard('auditor', write(path.join(TMP, 'review-code-1.json')))));
  assert.ok(!denied(guard('auditor', write(`${RUN}/notes/qa-1.json`))));
  assert.ok(denied(guard('auditor', write(`${RUN}/acs.json`))));
  assert.ok(denied(guard('auditor', write(`${RUN}/checks/style/drawer.json`))));
  assert.ok(denied(guard('auditor', write('/repo/src/app.ts', '/repo'))));
});

test('no role can save into protected run paths via an MCP destination arg; raw/ and notes/ are free', () => {
  const mcp = (args) => ({ tool_name: 'mcp__acme__get_ticket', tool_input: args });
  for (const role of ['scout', 'builder', 'auditor', 'author']) {
    for (const arg of ['save_to', 'output_path', 'output_file', 'out', 'dest']) {
      assert.ok(denied(guard(role, mcp({ [arg]: `${RUN}/design/1-2.json` }))), `${role} ${arg} design`);
      assert.ok(denied(guard(role, mcp({ [arg]: `${RUN}/acs.json` }))), `${role} ${arg} acs`);
      assert.ok(denied(guard(role, mcp({ [arg]: `${RUN}/contracts/ticket/ACME-1.json` }))), `${role} ${arg} contracts`);
      assert.ok(denied(guard(role, mcp({ [arg]: `${RUN}/evidence/CHK-1.json` }))), `${role} ${arg} evidence`);
      assert.ok(!denied(guard(role, mcp({ [arg]: `${RUN}/raw/ticket/ACME-1.json` }))), `${role} ${arg} raw`);
    }
    assert.ok(!denied(guard(role, mcp({ save_to: `${RUN}/notes/a.json` }))), role);
  }
});

test('FACTORY_ROLE=auditor only for the auditor; encoded PowerShell denied', () => {
  const cmd = 'FACTORY_ROLE=auditor factory review record x';
  for (const role of ['builder', 'author', 'scout']) {
    assert.ok(denied(guard(role, bash(cmd))), role);
    assert.ok(denied(guard(role, bash('echo $FACTORY_ROLE'))), `${role} mention`);
    assert.ok(denied(guard(role, bash('python -c "import os; print(os.environ.get(\"FACTORY_ROLE\"))"'))), `${role} py`);
  }
  assert.ok(!denied(guard('auditor', bash(cmd))));
  for (const role of ['builder', 'author', 'auditor']) {
    assert.ok(denied(guard(role, bash('powershell -NoProfile -EncodedCommand ZQBjAGgAbwA='))), role);
    assert.ok(denied(guard(role, bash('pwsh -enc ZQBjAGgAbwA='))), role);
    assert.ok(!denied(guard(role, bash('pwsh -NoProfile -Command "Get-Date"'))), role);
  }
});

test('env pid and storage records are denied to every worker', () => {
  const pid = `${FH_POSIX}/env/acme/web.pid`;
  for (const role of ['scout', 'author', 'builder', 'auditor']) {
    assert.ok(denied(guard(role, write(pid))), role);
    assert.ok(denied(guard(role, write(`${FH_POSIX}/env/acme/web.storage.json`))), role);
  }
  for (const role of ['author', 'builder', 'auditor']) assert.ok(denied(guard(role, bash(`echo 1 > ${pid}`))), role);
});
