#!/usr/bin/env node
// factory-browser <command> --out <dir> --base-url <url> [flags]   (DESIGN section 10)
// Commands: login, visual, style, screenshot, spec, batch. It writes <out>/result.json = {status, summary, details, artifacts}.
// Exit codes: 0 pass, 1 fail, 2 usage, 3 env (navigation or connection), 4 setup (browser missing). No network but the base origin.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { RunnerError, fail, usage, launch, openPage, prepare, runActions } from "./session.js";
import { compare, crop, decode, encode, parseMask } from "./compare.js";
import { probe } from "./probe.js";
import { runJsonSpec, runTestFile } from "./spec.js";

const BOOL = new Set(["fit", "headed"]);
const MULTI = new Set(["mask", "state-spec"]);
const camel = (k) => k.replace(/-(\w)/g, (_, c) => c.toUpperCase());

export function parseFlags(argv) {
  const f = { stateSpecs: [], masks: [] };
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) throw usage(`unexpected argument ${argv[i]}`);
    const key = argv[i].slice(2);
    if (BOOL.has(key)) f[camel(key)] = true;
    else if (i + 1 >= argv.length) throw usage(`--${key} needs a value`);
    else if (key === "state-spec") f.stateSpecs.push(argv[++i]);
    else if (key === "mask") f.masks.push(parseMask(argv[++i]));
    else f[camel(key)] = argv[++i];
  }
  f.timeoutMs = Number(f.timeoutMs || 60000);
  f.navTimeout = Math.max(1000, Math.min(30000, f.timeoutMs - 1000));
  f.settleMs = f.settleMs ? Number(f.settleMs) : 0;
  f.expectTimeoutMs = Number(f.expectTimeoutMs || process.env.FACTORY_EXPECT_TIMEOUT_MS || 0) || undefined;
  if (!f.out) throw usage("--out <dir> is required");
  return f;
}

const need = (f, ...keys) => keys.forEach((k) => { if (!f[k]) throw usage(`--${k.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())} is required`); });
const save = (dir, name, png) => (fs.writeFileSync(path.join(dir, name), encode(png)), name);

async function capture(page, f) {
  try {
    return decode(await (f.selector ? page.locator(f.selector).first().screenshot({ timeout: f.navTimeout }) : page.screenshot()));
  } catch (e) {
    throw fail(`cannot capture ${f.selector || "the viewport"}: ${String(e.message).split("\n")[0]}`);
  }
}

const commands = {
  async login(browser, f) {
    need(f, "baseUrl", "spec", "stateOut");
    const { context, page, base } = await openPage(browser, { ...f, storageState: undefined });
    await runActions(page, base, f.spec, f.navTimeout);
    // Success is the spec's `done` ({"cookie": name} or {"url": regex}) when given; an app may redirect elsewhere after login.
    const done = JSON.parse(fs.readFileSync(f.spec, "utf8")).done;
    const where = page.url();
    if (done?.cookie) {
      if (!(await context.cookies(base)).some((c) => c.name === done.cookie)) throw new RunnerError(`login set no ${done.cookie} cookie for ${base} (ended on ${where})`, 3);
    } else if (done?.url) {
      if (!new RegExp(done.url).test(where)) throw new RunnerError(`login ended on ${where}, which does not match ${done.url}`, 3);
    } else if (new URL(where).origin !== base) throw new RunnerError(`login ended on ${new URL(where).origin}, not the served origin ${base}; a spec whose app redirects elsewhere declares "done": {"cookie": "<session cookie>"}`, 3);
    fs.mkdirSync(path.dirname(f.stateOut), { recursive: true });
    await context.storageState({ path: f.stateOut });
    return { status: "pass", summary: "login state written", details: { state: path.basename(f.stateOut) }, artifacts: [] };
  },

  async visual(browser, f) {
    need(f, "baseUrl", "design", "crop");
    const [x, y, w, h] = f.crop.split(",").map(Number);
    const design = crop(decode(fs.readFileSync(f.design)), x, y, w, h);
    const { page, base } = await openPage(browser, f);
    await prepare(page, base, f);
    const app = await capture(page, f);
    const r = compare(app, design, { masks: f.masks, fit: Boolean(f.fit), maxDiffRatio: Number(f.maxDiffRatio ?? 0.03) });
    const artifacts = [save(f.out, "app.png", r.images.app), save(f.out, "design.png", r.images.design), ...(r.images.diff ? [save(f.out, "diff.png", r.images.diff)] : [])];
    const details = { diff_ratio: r.ratio, max_diff_ratio: Number(f.maxDiffRatio ?? 0.03), crop: [x, y, w, h], size: [app.width, app.height], fidelity: f.fidelity || "exact", ...(r.scale ? { scale: r.scale } : {}), ...(r.reason ? { reason: r.reason } : {}) };
    return { status: r.ok ? "pass" : "fail", summary: r.text, details, artifacts };
  },

  async style(browser, f) {
    need(f, "baseUrl", "map");
    const map = JSON.parse(fs.readFileSync(f.map, "utf8"));
    const { page, base } = await openPage(browser, f);
    await prepare(page, base, f);
    const results = await probe(page, map, { px: Number(f.px ?? 2), deltaE: Number(f.deltaE ?? 0), fontPx: Number(f.fontPx ?? 0) });
    const bad = results.filter((r) => !r.ok);
    return { status: bad.length ? "fail" : "pass", summary: `${results.length - bad.length} of ${results.length} style fact(s) hold`, details: { fidelity: f.fidelity || "exact", results }, artifacts: [] };
  },

  async screenshot(browser, f) {
    need(f, "baseUrl");
    const { page, base } = await openPage(browser, f);
    if (f.spec) await runActions(page, base, f.spec, f.navTimeout);
    await prepare(page, base, f);
    return { status: "pass", summary: "captured", details: {}, artifacts: [save(f.out, "shot.png", await capture(page, f))] };
  },

  async spec(browser, f) {
    need(f, "baseUrl", "spec");
    if (path.extname(f.spec) !== ".json") {
      const r = await runTestFile(f.spec, f, f.out);
      return { ...r, artifacts: [] };
    }
    const { page, base } = await openPage(browser, f);
    return { status: "pass", summary: await runJsonSpec(page, base, f.spec, f.navTimeout), details: {}, artifacts: [] };
  },
};

const EXIT = { pass: 0, fail: 1 };
function settle(e) {
  if (e instanceof RunnerError) return { exit: e.exit, status: e.exit === 1 ? "fail" : "error", summary: e.message };
  if (e && e.code === "ENOENT") return { exit: 2, status: "error", summary: `usage: ${e.message}` };
  return { exit: 1, status: "error", summary: `runner error: ${String(e && e.message).split("\n")[0]}` };
}

/** Run one command to a result (never throws); the entry's own timeout is a race, so one stuck entry cannot stall a batch. */
export async function runOne(cmd, f, browser) {
  fs.mkdirSync(f.out, { recursive: true });
  const start = Date.now();
  let timer, res;
  try {
    if (!commands[cmd]) throw usage(`unknown command ${cmd} (login, visual, style, screenshot, spec, batch)`);
    const limit = new Promise((_, rej) => (timer = setTimeout(() => rej(fail(`exceeded --timeout-ms ${f.timeoutMs}`)), f.timeoutMs)));
    const r = await Promise.race([commands[cmd](browser, f), limit]);
    res = { exit: EXIT[r.status], ...r };
  } catch (e) {
    res = { ...settle(e), details: {}, artifacts: [] };
  } finally {
    clearTimeout(timer);
  }
  fs.writeFileSync(path.join(f.out, "result.json"), JSON.stringify({ status: res.status, summary: res.summary, details: res.details || {}, artifacts: res.artifacts || [] }));
  return { ...res, secs: (Date.now() - start) / 1000 };
}

async function batch(f) {
  need(f, "manifest");
  const entries = JSON.parse(fs.readFileSync(f.manifest, "utf8"));
  const browser = await launch(Boolean(f.headed));
  const done = [];
  try {
    for (const e of entries) {
      const ef = parseFlags([...e.argv.slice(1), "--out", e.out, "--timeout-ms", String(e.timeout_ms || f.timeoutMs)]);
      process.env.FACTORY_EXPECT_TIMEOUT_MS = e.env?.FACTORY_EXPECT_TIMEOUT_MS || "";
      const r = await runOne(e.argv[0], { ...ef, expectTimeoutMs: Number(process.env.FACTORY_EXPECT_TIMEOUT_MS) || ef.expectTimeoutMs }, browser);
      done.push({ id: e.id, exit: r.exit, status: r.status, secs: r.secs, timed_out: /exceeded --timeout-ms/.test(r.summary) });
    }
  } finally {
    await browser.close();
  }
  return { status: "pass", summary: `batch of ${done.length}`, details: { entries: done }, artifacts: [] };
}

async function main(argv) {
  const [cmd, ...rest] = argv;
  let f;
  try {
    if (!cmd) throw usage("usage: factory-browser <login|visual|style|screenshot|spec|batch> --out <dir> [flags]");
    f = parseFlags(rest);
  } catch (e) {
    const r = settle(e);
    const out = (rest[rest.indexOf("--out") + 1] || ".");
    fs.mkdirSync(out, { recursive: true });
    fs.writeFileSync(path.join(out, "result.json"), JSON.stringify({ status: "error", summary: r.summary, details: {}, artifacts: [] }));
    process.stderr.write(r.summary + "\n");
    return r.exit;
  }
  const watchdog = setTimeout(() => {
    fs.writeFileSync(path.join(f.out, "result.json"), JSON.stringify({ status: "fail", summary: `watchdog: exceeded --timeout-ms ${f.timeoutMs}`, details: {}, artifacts: [] }));
    process.exit(1);
  }, f.timeoutMs + 5000);
  let browser, res;
  try {
    if (cmd === "batch") {
      fs.mkdirSync(f.out, { recursive: true });
      res = { exit: 0, ...(await batch(f)) };
      fs.writeFileSync(path.join(f.out, "result.json"), JSON.stringify(res));
    } else {
      browser = cmd === "spec" && path.extname(f.spec || "") !== ".json" ? null : await launch(Boolean(f.headed));
      res = await runOne(cmd, f, browser);
    }
  } catch (e) {
    res = settle(e);
    fs.mkdirSync(f.out, { recursive: true });
    fs.writeFileSync(path.join(f.out, "result.json"), JSON.stringify({ status: "error", summary: res.summary, details: {}, artifacts: [] }));
  } finally {
    clearTimeout(watchdog);
    if (browser) await browser.close();
  }
  process.stderr.write(`${cmd}: ${res.status}: ${res.summary}\n`);
  return res.exit;
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  process.stdin.destroy();
  main(process.argv.slice(2)).then((code) => process.exit(code));
}
