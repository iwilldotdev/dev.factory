import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { summariseProject } from "../dist/spec.js";

const cli = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist", "cli.js");

const t = (projectName, status, results) => ({ projectName, status, results });
const report = {
  suites: [{ suites: [{ specs: [
    { title: "a", tests: [t("c0", "expected", [{ status: "passed", duration: 100 }]), t("c1", "unexpected", [{ status: "failed", duration: 40, error: { message: "expect(x)" } }])] },
    { title: "b", tests: [t("c1", "skipped", []), t("c2", "skipped", [])] },
    { title: "i", tests: [t("c3", "unexpected", [{ status: "interrupted", duration: 5 }])] },
  ] }] }],
};

test("summariseProject maps one project of a batch report", () => {
  const c0 = summariseProject(report, "c0");
  assert.deepEqual([c0.status, c0.ran, c0.durationMs, c0.summary], ["pass", true, 100, "1 passed, 0 failed"]);
  const c1 = summariseProject(report, "c1");
  assert.deepEqual([c1.status, c1.ran, c1.durationMs, c1.summary], ["fail", true, 40, "0 passed, 1 failed"]);
  assert.equal(c1.details.tests[0].error, "expect(x)");
});

test("summariseProject: no results (stopped by max-failures) or an interrupted test is not run", () => {
  assert.equal(summariseProject(report, "c2").ran, false);
  assert.equal(summariseProject(report, "c3").ran, false);
  assert.equal(summariseProject(report, "nope").ran, false);
});

test("run-specs: one Playwright process, one result per check, bail stops after the first failure", async (tc) => {
  try {
    await (await chromium.launch()).close();
  } catch (e) {
    return tc.skip(`Chromium unavailable: ${String(e.message).split("\n")[0]}`);
  }
  const server = http.createServer((_req, res) => {
    res.setHeader("content-type", "text/html");
    res.end('<title>T</title><main id="m">Pay now</main>');
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  tc.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;
  const dir = await mkdtemp(path.join(os.tmpdir(), "fb-batch-"));
  const spec = async (name, text) => {
    const file = path.join(dir, `${name}.spec.ts`);
    await writeFile(file, `import { test, expect } from "@playwright/test";\ntest("${name}", async ({ page }) => { await page.goto("/"); await expect(page.locator("#m")).toHaveText("${text}", { timeout: 500 }); });\n`);
    return file;
  };
  const manifestFor = async (name, items) => {
    const file = path.join(dir, name);
    await writeFile(file, JSON.stringify(items.map(([id, s]) => ({ id, spec: s, out: path.join(dir, id), timeout_ms: 30000, goto_timeout_ms: 10000 }))));
    return file;
  };
  const run = (...a) => new Promise((resolve) => {
    const c = spawn(process.execPath, [cli, ...a]);
    let stderr = "";
    c.stderr.on("data", (d) => (stderr += d));
    c.on("close", (status) => resolve({ status, stderr }));
  });
  const ok = await spec("ok", "Pay now");
  const bad = await spec("bad", "Nope");
  const ok2 = await spec("ok2", "Pay now");
  const items = [["B1", ok], ["B2", bad], ["B3", ok2]];

  let out = path.join(dir, "batch1");
  let p = await run("run-specs", "--manifest", await manifestFor("m1.json", items), "--base-url", base, "--out", out);
  assert.equal(p.status, 0, p.stderr);
  let results = JSON.parse(await readFile(path.join(out, "batch.json"), "utf8")).results;
  assert.deepEqual(results.map((r) => [r.id, r.status, r.ran]), [["B1", "pass", true], ["B2", "fail", true], ["B3", "pass", true]]);
  assert.ok(results.every((r) => r.durationMs > 0));
  assert.ok(JSON.parse(await readFile(path.join(dir, "B2", "report.json"), "utf8")).suites.length > 0);

  out = path.join(dir, "batch2");
  p = await run("run-specs", "--manifest", await manifestFor("m2.json", items), "--base-url", base, "--out", out, "--max-failures", "1");
  assert.equal(p.status, 0, p.stderr);
  results = JSON.parse(await readFile(path.join(out, "batch.json"), "utf8")).results;
  assert.deepEqual(results.map((r) => [r.id, r.ran]), [["B1", true], ["B2", true], ["B3", false]]);
});
