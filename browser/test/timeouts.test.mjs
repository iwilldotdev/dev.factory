import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { gotoOptions } from "../dist/capture.js";
import { timeoutConfig } from "../dist/spec.js";

const cli = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist", "cli.js");

test("timeoutConfig: per-test stays below the cap, goto never exceeds it", () => {
  assert.deepEqual(timeoutConfig(117000, 30000), { test: 116000, global: 117000, nav: 30000 });
  assert.deepEqual(timeoutConfig(10000, 30000), { test: 9000, global: 10000, nav: 10000 });
  assert.deepEqual(timeoutConfig(undefined, undefined), { test: 0, global: 0, nav: 30000 });
});

test("gotoOptions: load by default (never networkidle), 30 s", () => {
  assert.deepEqual(gotoOptions(), { waitUntil: "load", timeout: 30000 });
  assert.deepEqual(gotoOptions({ waitUntil: "networkidle", gotoTimeoutMs: 5000 }), { waitUntil: "networkidle", timeout: 5000 });
});

test("capture of a page that never answers ends at the goto timeout with an error result", async (t) => {
  try {
    await (await chromium.launch()).close();
  } catch (e) {
    return t.skip(`Chromium unavailable: ${String(e.message).split("\n")[0]}`);
  }
  const hung = [];
  const server = http.createServer((req) => hung.push(req)); // never responds
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => {
    hung.forEach((r) => r.destroy());
    server.close();
  });
  const out = await mkdtemp(path.join(os.tmpdir(), "fb-to-"));
  const started = Date.now();
  const code = await new Promise((resolve) => {
    const c = spawn(process.execPath, [cli, "capture", "--url", `http://127.0.0.1:${server.address().port}/`, "--out", out, "--goto-timeout-ms", "1500", "--timeout-ms", "20000"]);
    c.on("close", resolve);
  });
  const r = JSON.parse(await readFile(path.join(out, "result.json"), "utf8"));
  assert.equal(r.status, "error");
  assert.notEqual(code, 0);
  assert.ok(Date.now() - started < 15000, "stopped by the goto timeout, not the watchdog");
});

test("--timeout-ms watchdog writes an error result and exits", async (t) => {
  try {
    await (await chromium.launch()).close();
  } catch (e) {
    return t.skip(`Chromium unavailable: ${String(e.message).split("\n")[0]}`);
  }
  const hung = [];
  const server = http.createServer((req) => hung.push(req));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => {
    hung.forEach((r) => r.destroy());
    server.close();
  });
  const out = await mkdtemp(path.join(os.tmpdir(), "fb-wd-"));
  await new Promise((resolve) => {
    const c = spawn(process.execPath, [cli, "capture", "--url", `http://127.0.0.1:${server.address().port}/`, "--out", out, "--goto-timeout-ms", "60000", "--timeout-ms", "2000"]);
    c.on("close", resolve);
  });
  const r = JSON.parse(await readFile(path.join(out, "result.json"), "utf8"));
  assert.equal(r.status, "error");
  assert.equal(r.details.timeout, true);
});
