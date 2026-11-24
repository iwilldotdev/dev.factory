// Browser session: launch, a context that only talks to the base origin, and the small action language of specs [L-51, L-79, L-80].
import fs from "node:fs";
import { chromium } from "playwright";

export class RunnerError extends Error {
  constructor(message, exit) {
    super(message);
    this.exit = exit; // 1 fail, 2 usage, 3 env, 4 setup
  }
}
export const fail = (m) => new RunnerError(m, 1), usage = (m) => new RunnerError(m, 2), env = (m) => new RunnerError(m, 3);

export async function launch(headed) {
  try {
    return await chromium.launch({ headless: !headed });
  } catch (e) {
    throw new RunnerError(`the browser cannot start: ${String(e.message).split("\n")[0]}`, 4);
  }
}

export function origin(base) {
  const u = new URL(base);
  if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(u.hostname)) throw usage(`base url ${base} is not a loopback origin`);
  return u.origin;
}

/** A page whose context aborts every request to another origin, so a check can neither leak cookies nor depend on the network. */
export async function openPage(browser, f) {
  const base = origin(f.baseUrl);
  const [w, h] = (f.viewport || "1280x720").split("x").map(Number);
  const opts = { viewport: { width: w, height: h }, deviceScaleFactor: 1, acceptDownloads: false };
  if (f.storageState) opts.storageState = f.storageState;
  const context = await browser.newContext(opts);
  const blocked = [];
  await context.route("**/*", (route) => {
    const u = route.request().url();
    if (u.startsWith(base) || u.startsWith("data:") || u.startsWith("blob:") || u === "about:blank") return route.continue();
    blocked.push(u);
    return route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(f.navTimeout);
  page.setDefaultNavigationTimeout(f.navTimeout);
  return { context, page, base, blocked };
}

export async function go(page, base, path, timeout) {
  const url = new URL(path, base);
  if (url.origin !== base) throw usage(`${path} leaves the env origin ${base}`);
  try {
    await page.goto(url.href, { waitUntil: "load", timeout });
  } catch (e) {
    throw env(`cannot load ${url.href}: ${String(e.message).split("\n")[0]}`);
  }
}

const interpolate = (text) =>
  String(text).replace(/\$\{([A-Z][A-Z0-9_]*)\}/g, (_, k) => {
    if (process.env[k] === undefined) throw usage(`environment variable ${k} is not set (credentials come only from the environment)`);
    return process.env[k];
  });

/** One action: goto, click, fill, press, wait, expect_visible, expect_text, expect_url. A failed expectation is exit 1. */
export async function act(page, base, action, timeout) {
  const [name, arg] = Object.entries(action)[0];
  const t = { timeout };
  const wrap = async (fn, what) => {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof RunnerError) throw e;
      throw fail(`${what}: ${String(e.message).split("\n")[0]}`);
    }
  };
  if (name === "goto") return go(page, base, arg, timeout);
  if (name === "click") return wrap(() => page.click(arg, t), `click ${arg}`);
  if (name === "fill") return wrap(() => page.fill(arg.selector, interpolate(arg.value), t), `fill ${arg.selector}`);
  if (name === "press") return wrap(() => (arg.selector ? page.press(arg.selector, arg.key, t) : page.keyboard.press(arg.key)), `press ${arg.key}`);
  if (name === "wait") return typeof arg === "number" ? page.waitForTimeout(arg) : wrap(() => page.waitForSelector(arg.selector || arg, { state: arg.state || "visible", ...t }), `wait ${arg.selector || arg}`);
  if (name === "expect_visible") return wrap(() => page.waitForSelector(arg, { state: "visible", ...t }), `expected ${arg} to be visible`);
  if (name === "expect_text") {
    return wrap(() => page.waitForFunction(([s, x]) => (document.querySelector(s)?.textContent || "").includes(x), [arg.selector, arg.text], t), `expected ${arg.selector} to contain ${JSON.stringify(arg.text)}`);
  }
  if (name === "expect_url") return wrap(() => page.waitForURL((u) => u.pathname === arg || new RegExp(arg).test(u.href), t), `expected the url to be ${arg}`);
  throw usage(`unknown action ${name}`);
}

export const readActions = (file) => {
  const doc = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!Array.isArray(doc.actions)) throw usage(`${file}: expected {"actions": [...]}`);
  return doc.actions;
};

export async function runActions(page, base, file, timeout) {
  for (const a of readActions(file)) await act(page, base, a, timeout);
}

/** Open the page of a visual, style or screenshot check and bring it to the state to capture [L-51]. */
export async function prepare(page, base, f) {
  if (f.url) await go(page, base, f.url, f.navTimeout);
  for (const spec of f.stateSpecs) await runActions(page, base, spec, f.navTimeout);
  if (f.waitFor) await act(page, base, { expect_visible: f.waitFor }, f.navTimeout);
  if (f.settleMs) await page.waitForTimeout(f.settleMs);
}
