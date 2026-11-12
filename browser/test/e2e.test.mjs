import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const cli = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist", "cli.js");

const PAGE = `<!doctype html><meta charset="utf-8"><title>T</title>
<style>html,body{margin:0;background:#fff}
main{width:320px;height:180px;color:#112233;font:600 16px/24px sans-serif;padding:8px;background:#eeeeee;border-radius:4px}</style>
<main id="m">Pay now</main>`;
const LOGIN = `<!doctype html><form onsubmit="event.preventDefault();location.href='/app?ok=1'">
<input id="u"><input id="p" type="password"><button type="submit">go</button></form>`;

let server;
let base;
let skipReason;

test.before(async () => {
  try {
    const b = await chromium.launch();
    await b.close();
  } catch (e) {
    skipReason = `Chromium unavailable (run: npx playwright install chromium): ${String(e.message).split("\n")[0]}`;
    console.warn(`SKIP e2e: ${skipReason}`);
    return;
  }
  server = http.createServer((req, res) => {
    res.setHeader("content-type", "text/html");
    res.end(req.url.startsWith("/login") ? LOGIN : req.url.startsWith("/app") ? "<title>app</title><p>in</p>" : PAGE);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});
test.after(() => server?.close());

// async spawn: the in-process static server must keep serving while the CLI runs
const run = (...a) =>
  new Promise((resolve) => {
    const c = spawn(process.execPath, [cli, ...a]);
    let stdout = "";
    let stderr = "";
    c.stdout.on("data", (d) => (stdout += d));
    c.stderr.on("data", (d) => (stderr += d));
    c.on("close", (status) => resolve({ status, stdout, stderr }));
  });
const result = async (dir) => JSON.parse(await readFile(path.join(dir, "result.json"), "utf8"));
const tmp = () => mkdtemp(path.join(os.tmpdir(), "fb-"));

test("capture, compare, style-probe", async (t) => {
  if (skipReason) return t.skip(skipReason);
  const dir = await tmp();
  const cap = path.join(dir, "cap");
  let p = await run("capture", "--url", `${base}/`, "--out", cap, "--viewport", "400x300", "--selector", "#m", "--wait-for", "#m", "--settle-ms", "50");
  assert.equal(p.status, 0, p.stderr);
  assert.equal((await result(cap)).status, "pass");
  assert.ok(existsSync(path.join(cap, "app.png")));

  const cmp = path.join(dir, "cmp");
  p = await run("compare", "--figma", path.join(cap, "app.png"), "--app", path.join(cap, "app.png"), "--out", cmp, "--copies", "Pay now");
  assert.equal(p.status, 0, p.stderr);
  assert.equal((await result(cmp)).status, "pass");
  p = await run("compare", "--figma", path.join(cap, "app.png"), "--app", path.join(cap, "app.png"), "--out", cmp, "--copies", "Nope");
  assert.equal(p.status, 1);
  assert.equal((await result(cmp)).details.reason, "copy");

  const map = path.join(dir, "map.json");
  const entry = (expect) => ({ node: "1:2", entries: [{ id: "e1", selector: "#m", expect, tolerance_px: 1 }] });
  await writeFile(map, JSON.stringify(entry({ fontSize: 16, lineHeight: 24, fontWeight: 600, color: "#112233", backgroundColor: "#EEEEEE", borderRadius: 4, paddingTop: 8, width: 336, height: 196 })));
  const good = path.join(dir, "good");
  p = await run("style-probe", "--url", `${base}/`, "--map", map, "--out", good, "--viewport", "400x300");
  assert.equal(p.status, 0, p.stdout + p.stderr);
  const r = await result(good);
  assert.equal(r.status, "pass");
  assert.ok(existsSync(path.join(good, r.artifacts[0])));

  await writeFile(map, JSON.stringify({ node: "1:2", entries: [{ id: "e1", selector: "#m", expect: { fontSize: 20 } }, { id: "e2", selector: "#nope", expect: { fontSize: 1 } }] }));
  const bad = path.join(dir, "bad");
  p = await run("style-probe", "--url", `${base}/`, "--map", map, "--out", bad);
  assert.equal(p.status, 1);
  const rb = await result(bad);
  assert.equal(rb.status, "fail");
  assert.equal(rb.details.entries[1].note, "element not found");
});

test("run-spec", async (t) => {
  if (skipReason) return t.skip(skipReason);
  const dir = await tmp();
  const spec = path.join(dir, "ok.spec.ts");
  await writeFile(spec, `import { test, expect } from "@playwright/test";\ntest("shows", async ({ page }) => { await page.goto("/"); await expect(page.locator("#m")).toHaveText("Pay now"); });\n`);
  const out = path.join(dir, "out");
  let p = await run("run-spec", "--spec", spec, "--base-url", base, "--out", out);
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.equal((await result(out)).status, "pass");
  const failing = path.join(dir, "bad.spec.ts");
  await writeFile(failing, `import { test, expect } from "@playwright/test";\ntest("wrong", async ({ page }) => { await page.goto("/"); await expect(page.locator("#m")).toHaveText("Nope", { timeout: 500 }); });\n`);
  const out2 = path.join(dir, "out2");
  p = await run("run-spec", "--spec", failing, "--base-url", base, "--out", out2);
  assert.equal(p.status, 1);
  assert.equal((await result(out2)).status, "fail");
  const empty = path.join(dir, "empty.spec.ts");
  await writeFile(empty, `import { test } from "@playwright/test";\n`);
  const out3 = path.join(dir, "out3");
  p = await run("run-spec", "--spec", empty, "--base-url", base, "--out", out3);
  assert.equal(p.status, 1);
  assert.equal((await result(out3)).status, "error");
});

test("login saves storage state without leaking credentials", async (t) => {
  if (skipReason) return t.skip(skipReason);
  const dir = await tmp();
  const cfg = path.join(dir, "login.json");
  await writeFile(cfg, JSON.stringify({ url: "/login", user_field: "#u", pass_field: "#p", submit: "button[type=submit]", success_url_contains: "/app" }));
  const out = path.join(dir, "storage.json");
  const p = await run("login", "--base-url", base, "--login-json", cfg, "--user", "someuser", "--pass", "s3cretpw", "--out", out);
  assert.equal(p.status, 0, p.stderr);
  assert.ok(!(p.stdout + p.stderr).includes("s3cretpw"));
  assert.ok(JSON.parse(await readFile(out, "utf8")).cookies);
});
