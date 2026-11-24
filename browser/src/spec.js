// `spec`: a project browser spec. A `.json` spec is the action language of session.js; any other file is a test file
// run by the test runner of the browser library, with the assertion timeout of the check (a per-check, hashed value [L-59]).
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fail, readActions, act } from "./session.js";

export async function runJsonSpec(page, base, file, timeout) {
  let n = 0;
  for (const a of readActions(file)) {
    await act(page, base, a, timeout);
    n++;
  }
  return `${n} action(s) passed`;
}

export function summarise(report) {
  const tests = [];
  const walk = (s) => {
    for (const sp of s.specs || []) {
      const ts = sp.tests || [];
      if (ts.length && ts.every((t) => t.status === "skipped")) continue;
      const err = ts.flatMap((t) => t.results || []).find((r) => r.error)?.error?.message;
      tests.push({ title: sp.title, ok: sp.ok, error: err ? err.slice(0, 500) : undefined });
    }
    (s.suites || []).forEach(walk);
  };
  (report.suites || []).forEach(walk);
  const failed = tests.filter((t) => !t.ok).length;
  if (!tests.length) return { status: "fail", summary: "no test ran in the spec", details: { tests } };
  return { status: failed ? "fail" : "pass", summary: `${tests.length - failed} passed, ${failed} failed`, details: { tests } };
}

export async function runTestFile(file, f, outDir) {
  const spec = path.resolve(file), report = path.join(outDir, "report.json"), cfg = path.join(outDir, "test.config.mjs");
  const cap = Math.max(1000, f.timeoutMs - 1000);
  fs.writeFileSync(cfg, `export default {testDir:${JSON.stringify(path.dirname(spec))},testMatch:${JSON.stringify(path.basename(spec))},` +
    `outputDir:${JSON.stringify(path.join(outDir, "artifacts"))},reporter:[["json",{outputFile:${JSON.stringify(report)}}]],retries:0,workers:1,timeout:${cap},` +
    `expect:{timeout:${f.expectTimeoutMs || 10000}},use:{baseURL:${JSON.stringify(f.baseUrl)},navigationTimeout:${f.navTimeout},` +
    `${f.storageState ? `storageState:${JSON.stringify(path.resolve(f.storageState))},` : ""}screenshot:"only-on-failure",trace:"off"}};\n`);
  const cli = path.join(path.dirname(createRequire(import.meta.url).resolve("@playwright/test/package.json")), "cli.js");
  const log = await new Promise((resolve) => {
    const modules = path.dirname(path.dirname(path.dirname(cli))); // spec files live in the run folder: let them resolve the library from here
    const child = spawn(process.execPath, [cli, "test", "--config", cfg], { cwd: path.dirname(spec), env: { ...process.env, FORCE_COLOR: "0", NODE_PATH: modules }, stdio: ["ignore", "pipe", "pipe"] });
    let buf = "";
    child.stdout.on("data", (d) => (buf += d));
    child.stderr.on("data", (d) => (buf += d));
    child.on("close", () => resolve(buf));
    child.on("error", (e) => resolve(String(e)));
  });
  fs.writeFileSync(path.join(outDir, "run.log"), log);
  try {
    return summarise(JSON.parse(fs.readFileSync(report, "utf8")));
  } catch {
    throw fail(`the test runner produced no report: ${log.slice(-300)}`);
  }
}
