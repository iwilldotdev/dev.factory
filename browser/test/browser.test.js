// The commands against a real headless browser and local static pages [L-51, L-54, L-55, L-57, L-79, L-80].
import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { PNG } from "pngjs";
import { launch } from "../src/session.js";
import { parseFlags, runOne } from "../src/cli.js";
import { cli, serve, solid, tmp, writeJson, writePng } from "./support.js";

let site, browser;
before(async () => {
  site = await serve();
  browser = await launch(false);
});
after(async () => {
  await browser.close();
  await site.close();
});

const run = (cmd, args) => {
  const out = tmp();
  return runOne(cmd, parseFlags(["--timeout-ms", "20000", ...args, "--base-url", site.url, "--out", out]), browser).then((r) => ({ ...r, out }));
};
const shot = async (selector) => {
  const r = await run("screenshot", ["--url", "/index.html", "--selector", selector]);
  return PNG.sync.read(fs.readFileSync(path.join(r.out, "shot.png")));
};

test("visual passes against its own capture, with the design cropped inside an export margin [L-55]", async () => {
  const dir = tmp();
  const design = solid(124, 64, [240, 240, 240]);
  PNG.bitblt(await shot("#box"), design, 0, 0, 100, 40, 12, 12);
  writePng(path.join(dir, "design.png"), design);
  const r = await run("visual", ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "design.png"), "--crop", "12,12,100,40", "--max-diff-ratio", "0.01"]);
  assert.equal(r.status, "pass", r.summary);
  assert.equal(r.exit, 0);
  assert.deepEqual(r.details.crop, [12, 12, 100, 40]);
  assert.deepEqual(r.artifacts.sort(), ["app.png", "design.png", "diff.png"]);
  const noCrop = await run("visual", ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "design.png"), "--crop", "0,0,124,64"]);
  assert.equal(noCrop.details.reason, "size");
});

test("visual fails on a different color and on a size mismatch, and fit scales the capture and records the scale [L-55]", async () => {
  const dir = tmp();
  writePng(path.join(dir, "blue.png"), solid(100, 40, [0, 0, 255]));
  const diff = await run("visual", ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "blue.png"), "--crop", "0,0,100,40", "--max-diff-ratio", "0.03"]);
  assert.equal(diff.status, "fail");
  assert.equal(diff.exit, 1);
  assert.ok(diff.details.diff_ratio > 0.9);
  writePng(path.join(dir, "big.png"), solid(200, 80, [200, 30, 30]));
  const size = await run("visual", ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "big.png"), "--crop", "0,0,200,80"]);
  assert.match(size.summary, /size mismatch/);
  const fit = await run("visual", ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "big.png"), "--crop", "0,0,200,80", "--fit"]);
  assert.equal(fit.status, "pass", fit.summary);
  assert.deepEqual(fit.details.scale, { x: 2, y: 2 });
});

test("a mask excludes its rectangle from the diff and is applied to both images", async () => {
  const dir = tmp();
  const design = solid(100, 40);
  PNG.bitblt(solid(10, 10, [0, 0, 255]), design, 0, 0, 10, 10, 0, 0);
  writePng(path.join(dir, "d.png"), design);
  const base = ["--url", "/index.html", "--selector", "#box", "--design", path.join(dir, "d.png"), "--crop", "0,0,100,40", "--max-diff-ratio", "0"];
  assert.equal((await run("visual", base)).status, "fail");
  assert.equal((await run("visual", [...base, "--mask", "0,0,10,10"])).status, "pass");
});

test("style probes computed styles against the map, and reports each fact", async () => {
  const dir = tmp();
  const map = writeJson(path.join(dir, "map.json"), { frame: "f", entries: [
    { selector: "#t", node: "n", expect: { fontSize: 20, fontWeight: 600, lineHeight: 28, color: "#1f2933", text: "No invoices yet", textAlign: "start" } },
    { selector: "#box", node: "m", expect: { width: 100, height: 40, backgroundColor: "#c81e1e", borderWidth: 0 } }] });
  const ok = await run("style", ["--url", "/index.html", "--map", map, "--px", "1"]);
  assert.equal(ok.status, "pass", JSON.stringify(ok.details.results.filter((x) => !x.ok)));
  const bad = writeJson(path.join(dir, "bad.json"), { frame: "f", entries: [{ selector: "#t", node: "n", expect: { fontSize: 24, text: "Other" } }, { selector: "#nope", node: "n", expect: { width: 1 } }] });
  const r = await run("style", ["--url", "/index.html", "--map", bad]);
  assert.equal(r.status, "fail");
  assert.equal(r.details.results.filter((x) => !x.ok).length, 3);
});

test("style fails an empty expected text, an expected border that is missing and a normal line height [L-54]", async () => {
  const map = writeJson(path.join(tmp(), "m.json"), { frame: "f", entries: [
    { selector: "#p", node: "n", expect: { text: "" } }, { selector: "#b", node: "n", expect: { borderWidth: 2 } }, { selector: "#empty", node: "n", expect: { lineHeight: 20 } }] });
  const r = await run("style", ["--url", "/plain.html", "--map", map]);
  assert.deepEqual(r.details.results.map((x) => x.ok), [false, false, false]);
  assert.match(r.details.results.map((x) => x.note).join("|"), /empty.*no border.*normal/);
});

test("a state spec reaches a state the page does not load in, and an unmet expectation is a failure [L-51]", async () => {
  const dir = tmp();
  const open = writeJson(path.join(dir, "open.json"), { actions: [{ click: "#open" }, { expect_visible: "#drawer" }] });
  const map = writeJson(path.join(dir, "m.json"), { frame: "f", entries: [{ selector: "#drawer", node: "n", expect: { width: 60, height: 30, backgroundColor: "#0000ff" } }] });
  assert.equal((await run("style", ["--url", "/index.html", "--map", map, "--state-spec", open])).status, "pass");
  assert.equal((await run("style", ["--url", "/index.html", "--map", map])).status, "fail");
  const never = writeJson(path.join(dir, "never.json"), { actions: [{ expect_visible: "#ghost" }] });
  const r = await run("screenshot", ["--url", "/index.html", "--state-spec", never, "--timeout-ms", "3000"]);
  assert.equal(r.exit, 1);
});

test("login fills the form from the environment, writes the state, and the state carries the session [L-80]", async () => {
  const dir = tmp();
  process.env.RUNNER_TEST_USER = "ana";
  const spec = writeJson(path.join(dir, "login.json"), { actions: [{ goto: "/login.html" }, { fill: { selector: "#u", value: "${RUNNER_TEST_USER}" } }, { click: "#go" }, { expect_url: "/home.html" }] });
  const state = path.join(dir, "state.json");
  const r = await run("login", ["--spec", spec, "--state-out", state]);
  assert.equal(r.status, "pass", r.summary);
  assert.match(fs.readFileSync(state, "utf8"), /ok-ana/);
  assert.doesNotMatch(JSON.stringify(r), /ana/); // the credential is in the state file only, never in the result
  const home = await run("screenshot", ["--url", "/home.html", "--storage-state", state, "--selector", "#who"]);
  assert.equal(home.status, "pass");
  const missing = writeJson(path.join(dir, "m.json"), { actions: [{ goto: "/login.html" }, { fill: { selector: "#u", value: "${RUNNER_NOT_SET}" } }] });
  const bad = await run("login", ["--spec", missing, "--state-out", path.join(dir, "s2.json")]);
  assert.equal(bad.exit, 2);
  assert.match(bad.summary, /RUNNER_NOT_SET is not set/);
});

test("a login that ends on another origin is env, and a path that leaves the origin is a usage error [L-79]", async () => {
  const dir = tmp();
  const away = writeJson(path.join(dir, "away.json"), { actions: [{ goto: "/away.html" }, { wait: 1500 }] });
  const r = await run("login", ["--spec", away, "--state-out", path.join(dir, "s.json")]);
  assert.equal(r.exit, 3, r.summary);
  const leave = await run("screenshot", ["--url", "http://127.0.0.2:9/x"]);
  assert.equal(leave.exit, 2);
});

test("spec runs a JSON action spec, and a test file with the check's assertion timeout [L-59]", async () => {
  const dir = tmp();
  const ok = writeJson(path.join(dir, "ok.json"), { actions: [{ goto: "/index.html" }, { expect_text: { selector: "#t", text: "No invoices" } }] });
  assert.equal((await run("spec", ["--spec", ok])).status, "pass");
  const no = writeJson(path.join(dir, "no.json"), { actions: [{ goto: "/index.html" }, { expect_text: { selector: "#t", text: "Something else" } }] });
  assert.equal((await run("spec", ["--spec", no, "--timeout-ms", "4000"])).exit, 1);
  const file = path.join(dir, "page.spec.js");
  fs.writeFileSync(file, `import { test, expect } from "@playwright/test";\ntest("home", async ({ page }) => { await page.goto("/index.html"); await expect(page.locator("#t")).toHaveText("No invoices yet"); });\n`);
  const r = await run("spec", ["--spec", file, "--expect-timeout-ms", "2000"]);
  assert.equal(r.status, "pass", r.summary + JSON.stringify(r.details));
});

test("batch runs entries in one browser: one failure does not stop the others and a stuck entry times out alone [L-57]", async () => {
  const dir = tmp();
  const ok = writeJson(path.join(dir, "ok.json"), { actions: [{ goto: "/index.html" }, { expect_visible: "#t" }] });
  const bad = writeJson(path.join(dir, "bad.json"), { actions: [{ goto: "/index.html" }, { expect_visible: "#ghost" }] });
  const slow = writeJson(path.join(dir, "slow.json"), { actions: [{ wait: 20000 }] });
  const base = ["--base-url", site.url];
  const manifest = writeJson(path.join(dir, "manifest.json"), [
    { id: "A", argv: ["spec", "--spec", ok, ...base], out: path.join(dir, "a"), timeout_ms: 15000 },
    { id: "B", argv: ["spec", "--spec", bad, ...base], out: path.join(dir, "b"), timeout_ms: 3000 },
    { id: "C", argv: ["screenshot", "--url", "/index.html", "--selector", "#box", ...base], out: path.join(dir, "c"), timeout_ms: 15000 },
    { id: "D", argv: ["spec", "--spec", slow, ...base], out: path.join(dir, "d"), timeout_ms: 1500 }] );
  const r = await cli(["batch", "--manifest", manifest, "--timeout-ms", "60000"]);
  assert.equal(r.code, 0, r.text);
  const by = Object.fromEntries(r.result.details.entries.map((e) => [e.id, e]));
  assert.deepEqual([by.A.exit, by.B.exit, by.C.exit, by.D.exit], [0, 1, 0, 1]);
  assert.equal(by.D.timed_out, true);
  assert.ok(fs.existsSync(path.join(dir, "c", "shot.png")));
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "b", "result.json"), "utf8")).status, "fail");
});

test("exit codes: usage 2, env 3 for an unreachable origin, setup 4 for a missing browser, and a watchdog [DESIGN section 10]", async () => {
  assert.equal((await cli(["nonsense", "--base-url", site.url])).code, 2);
  assert.equal((await cli(["screenshot"])).code, 2);
  const closed = await cli(["screenshot", "--base-url", "http://127.0.0.1:9", "--url", "/x", "--timeout-ms", "8000"]);
  assert.equal(closed.code, 3, closed.text);
  const none = await cli(["screenshot", "--base-url", site.url, "--url", "/index.html"], { PLAYWRIGHT_BROWSERS_PATH: tmp() });
  assert.equal(none.code, 4, none.text);
  const dir = tmp();
  const slow = writeJson(path.join(dir, "slow.json"), { actions: [{ wait: 30000 }] });
  const t = await cli(["spec", "--spec", slow, "--base-url", site.url, "--timeout-ms", "2500"]);
  assert.equal(t.code, 1);
  assert.match(t.result.summary, /exceeded/);
});

test("the page cannot reach another origin: requests to it are aborted", async () => {
  const { openPage, go } = await import("../src/session.js");
  const { page, base, blocked } = await openPage(browser, { baseUrl: site.url, navTimeout: 5000 });
  await go(page, base, "/index.html", 5000);
  await page.evaluate(() => fetch("http://127.0.0.2:9/leak").catch(() => "blocked"));
  assert.deepEqual(blocked, ["http://127.0.0.2:9/leak"]);
  await page.context().close();
});
