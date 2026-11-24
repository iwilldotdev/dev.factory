import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { captureSurface, DEFAULT_GOTO_TIMEOUT_MS, gotoOptions, runAdapter, WAIT_UNTIL, type WaitUntil } from "./capture.js";
import { comparePng, type Mask } from "./compare.js";
import { compareStyles, type Expect } from "./probe.js";
import { runSpec, runSpecs, SCREENSHOT_MODES, type BatchEntry, type ScreenshotMode } from "./spec.js";

type Status = "pass" | "fail" | "error";
type Result = { status: Status; summary: string; artifacts: string[]; details: Record<string, unknown> };

const args = process.argv.slice(2);
const command = args[0];
const secrets: string[] = [];

main().catch(async (err: unknown) => {
  const msg = scrub(err instanceof Error ? err.message : String(err));
  console.error(msg);
  const out = optional("--out");
  if (out && command !== "login") {
    await finish(out, { status: "error", summary: msg.split("\n")[0].slice(0, 300), artifacts: [], details: { error: msg } }).catch(() => undefined);
  }
  process.exit(1);
});

async function main(): Promise<void> {
  const out = required("--out");
  armWatchdog(out);
  switch (command) {
    case "capture":
      return finish(out, await doCapture(out));
    case "compare":
      return finish(out, await doCompare(out));
    case "style-probe":
      return finish(out, await doProbe(out));
    case "run-spec": {
      const shot = optional("--screenshot") ?? "only-on-failure";
      if (!SCREENSHOT_MODES.includes(shot)) throw new Error(`--screenshot must be one of ${SCREENSHOT_MODES.join("|")}`);
      const r = await runSpec({ spec: required("--spec"), baseUrl: required("--base-url"), storageState: optional("--storage-state"), outDir: out, screenshot: shot as ScreenshotMode, timeoutMs: optionalNumber("--timeout-ms"), gotoTimeoutMs: optionalNumber("--goto-timeout-ms") });
      const arts = ["report.json", "run.log"];
      return finish(out, { ...r, artifacts: arts });
    }
    case "run-specs":
      return doRunSpecs(out);
    case "login":
      return doLogin();
    default:
      throw new Error("usage: browser-cli capture|compare|style-probe|run-spec|run-specs|login");
  }
}

/** run-specs --manifest <json: [{id, spec, out, timeout_ms?, goto_timeout_ms?}]> --base-url --out [--storage-state] [--max-failures n]
 * Writes <out>/batch.json ({results: [{id, status, summary, details, ran, durationMs}]}) and, per entry, report.json + run.log in its out dir. */
async function doRunSpecs(out: string): Promise<void> {
  const raw = JSON.parse(await readFile(required("--manifest"), "utf8")) as Array<{ id: string; spec: string; out: string; timeout_ms?: number; goto_timeout_ms?: number }>;
  if (!Array.isArray(raw) || raw.length === 0) throw new Error("manifest has no entries");
  const entries: BatchEntry[] = raw.map((e) => ({ id: e.id, spec: e.spec, out: e.out, timeoutMs: e.timeout_ms, gotoTimeoutMs: e.goto_timeout_ms }));
  const results = await runSpecs({ entries, baseUrl: required("--base-url"), storageState: optional("--storage-state"), outDir: out, maxFailures: optionalNumber("--max-failures") });
  if (!results) throw new Error("Playwright produced no report");
  await mkdir(out, { recursive: true });
  await writeFile(path.join(out, "batch.json"), `${JSON.stringify({ results }, null, 2)}\n`, "utf8");
  console.log(`batch: ${results.map((r) => `${r.id}=${r.ran ? r.status : "not-run"}`).join(" ")}`);
}

async function doCapture(out: string): Promise<Result> {
  const [w, h] = viewport();
  const c = await captureSurface({
    url: required("--url"),
    outDir: out,
    width: w,
    height: h,
    selector: optional("--selector"),
    adapterPath: optional("--adapter"),
    storageState: optional("--storage-state"),
    waitFor: optional("--wait-for"),
    settleMs: optionalNumber("--settle-ms"),
    ...navOptions(),
  });
  return { status: "pass", summary: `captured ${path.basename(c.appPng)} (${w}x${h})`, artifacts: ["app.png", "text.txt"], details: { textLength: c.text.length } };
}

async function doCompare(out: string): Promise<Result> {
  await mkdir(out, { recursive: true });
  const designPath = required("--design");
  const appPath = required("--app");
  const design = await readFile(designPath);
  const app = await readFile(appPath);
  const copies = optional("--copies")?.split("|").filter(Boolean) ?? [];
  if (copies.length) {
    const text = await readFile(path.join(path.dirname(appPath), "text.txt"), "utf8").catch(() => "");
    const missing = copies.filter((copy) => !text.includes(copy));
    if (missing.length) {
      return { status: "fail", summary: `copy missing: ${missing.join(" | ")}`, artifacts: [], details: { reason: "copy", missing } };
    }
  }
  const masksFile = optional("--masks");
  const masks = masksFile ? (JSON.parse(await readFile(masksFile, "utf8")) as Mask[]) : [];
  const r = comparePng(design, app, { maxDiffRatio: optionalNumber("--max-diff-ratio"), masks, fit: args.includes("--fit") });
  const artifacts: string[] = [];
  if (r.diffPng) {
    await writeFile(path.join(out, "diff.png"), r.diffPng);
    artifacts.push("diff.png");
  }
  for (const [src, name] of [[designPath, "design.png"], [appPath, "app.png"]] as const) {
    if (path.resolve(src) !== path.resolve(path.join(out, name))) await copyFile(src, path.join(out, name));
    artifacts.push(name);
  }
  const details: Record<string, unknown> = { reason: r.reason, width: r.width, height: r.height, mismatchedPixels: r.mismatchedPixels, diffRatio: r.diffRatio, designSize: r.designSize, appSize: r.appSize };
  let summary: string;
  if (r.status === "pass") summary = `diffRatio ${r.diffRatio?.toFixed(4)} <= ${optionalNumber("--max-diff-ratio") ?? 0.002}`;
  else if (r.reason === "size") summary = `size mismatch: design ${r.designSize?.join("x")} vs app ${r.appSize?.join("x")}`;
  else if (r.reason === "pixel-diff") summary = `diffRatio ${r.diffRatio?.toFixed(4)} > ${optionalNumber("--max-diff-ratio") ?? 0.002}`;
  else summary = `compare failed: ${r.reason}`;
  return { status: r.status, summary, artifacts, details };
}

type MapEntry = { id: string; selector: string; expect: Expect; tolerance_px?: number; color_delta_e?: number; font_size_px?: number };

async function doProbe(out: string): Promise<Result> {
  const map = JSON.parse(await readFile(required("--map"), "utf8")) as { node?: string; entries: MapEntry[] };
  if (!Array.isArray(map.entries) || map.entries.length === 0) throw new Error("style map has no entries");
  const [w, h] = viewport();
  const browser = await chromium.launch({ headless: true });
  const entries: Array<Record<string, unknown>> = [];
  try {
    const context = await browser.newContext({
      viewport: { width: w, height: h },
      deviceScaleFactor: 1,
      locale: "en-US",
      timezoneId: "UTC",
      colorScheme: "light",
      reducedMotion: "reduce",
      storageState: optional("--storage-state"),
    });
    const page = await context.newPage();
    const nav = navOptions();
    page.setDefaultTimeout(nav.gotoTimeoutMs);
    await page.goto(required("--url"), gotoOptions(nav));
    await page.evaluate(() => document.fonts.ready);
    const adapterPath = optional("--adapter");
    if (adapterPath) await runAdapter(page, adapterPath, required("--url"), nav);
    const waitFor = optional("--wait-for");
    if (waitFor) await page.locator(waitFor).first().waitFor({ timeout: nav.gotoTimeoutMs });
    if (optional("--settle-ms")) await page.waitForTimeout(optionalNumber("--settle-ms") ?? 0);
    const found: string[] = [];
    for (const e of map.entries) {
      const loc = page.locator(e.selector).first();
      if ((await page.locator(e.selector).count()) === 0) {
        entries.push({ id: e.id, selector: e.selector, ok: false, note: "element not found", props: [] });
        continue;
      }
      const actual = await loc.evaluate((el) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const sides = ["Top", "Right", "Bottom", "Left"] as const;
        const side = sides.find((d) => parseFloat(cs.getPropertyValue(`border-${d.toLowerCase()}-width`)) > 0) ?? "Top";
        return {
          fontFamily: cs.fontFamily, text: (el as HTMLElement).innerText ?? el.textContent ?? "",
          borderColor: cs.getPropertyValue(`border-${side.toLowerCase()}-color`), borderWidth: cs.getPropertyValue(`border-${side.toLowerCase()}-width`),
          fontSize: cs.fontSize, lineHeight: cs.lineHeight, fontWeight: cs.fontWeight, letterSpacing: cs.letterSpacing,
          color: cs.color, backgroundColor: cs.backgroundColor, borderRadius: cs.borderTopLeftRadius,
          paddingTop: cs.paddingTop, paddingRight: cs.paddingRight, paddingBottom: cs.paddingBottom, paddingLeft: cs.paddingLeft,
          gap: cs.columnGap, width: r.width, height: r.height, opacity: cs.opacity,
        };
      });
      const props = compareStyles(e.expect ?? {}, actual as Record<string, unknown>, e.tolerance_px ?? 1, { colorDeltaE: e.color_delta_e, fontSizePx: e.font_size_px });
      const ok = props.length > 0 && props.every((p) => p.ok);
      entries.push({ id: e.id, selector: e.selector, ok, props, ...(props.length === 0 ? { note: "no expected properties" } : {}) });
      found.push(e.selector);
    }
    await page.evaluate((sels) => {
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el instanceof HTMLElement) { el.style.outline = "2px solid #ff00ff"; el.style.outlineOffset = "-2px"; }
      }
    }, found);
    await page.screenshot({ path: path.join(out, "probe.png"), fullPage: true, animations: "disabled" });
  } finally {
    await browser.close();
  }
  const bad = entries.filter((e) => !e.ok);
  return {
    status: bad.length ? "fail" : "pass",
    summary: bad.length
      ? `${bad.length}/${entries.length} entries failed: ${bad.map((e) => e.id).join(", ")}`
      : `${entries.length}/${entries.length} entries match`,
    artifacts: ["probe.png"],
    details: { node: map.node, entries },
  };
}

async function doLogin(): Promise<void> {
  const cfg = JSON.parse(await readFile(required("--login-json"), "utf8")) as {
    url: string; user_field: string; pass_field: string; submit: string; success_url_contains: string;
    handoff?: { host?: string | null; from_port: number; to_port: number };
  };
  // credentials come from the environment, never argv (process listings show argv)
  const user = process.env.FACTORY_LOGIN_USER ?? "";
  const pass = process.env.FACTORY_LOGIN_PASS ?? "";
  if (!user || !pass) throw new Error("login needs FACTORY_LOGIN_USER and FACTORY_LOGIN_PASS in the environment");
  secrets.push(user, pass);
  const base = required("--base-url");
  const target = required("--out");
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ locale: "en-US" });
    const page = await context.newPage();
    if (cfg.handoff) {
      // Slot > 0: the login redirects to a fixed origin (e.g. localhost:3300/portal/coming?token=...). The Location of
      // a navigation answered on the slot's origin, and any navigation to the fixed origin itself, is moved to the
      // slot's port (path, query and token kept), so the hand-off completes on the slot.
      const h = cfg.handoff;
      const ports = [String(h.from_port), String(h.to_port)];
      const sameHost = (u: URL) => !h.host || u.hostname.toLowerCase() === h.host.toLowerCase();
      await page.route((u) => ports.includes(u.port) && sameHost(u), async (route) => {
        const req = route.request();
        if (!req.isNavigationRequest()) return route.continue();
        const url = new URL(req.url());
        if (url.port === String(h.from_port)) {
          url.port = String(h.to_port);
          return route.fulfill({ status: 302, headers: { location: url.toString() } });
        }
        const res = await route.fetch({ maxRedirects: 0 });
        const loc = res.headers()["location"];
        if (res.status() >= 300 && res.status() < 400 && loc) {
          const next = new URL(loc, req.url());
          if (next.port === String(h.from_port) && sameHost(next)) {
            next.port = String(h.to_port);
            return route.fulfill({ response: res, headers: { ...res.headers(), location: next.toString() } });
          }
        }
        return route.fulfill({ response: res });
      });
    }
    await page.goto(new URL(cfg.url, base).toString(), { waitUntil: "load" });
    await page.locator(cfg.user_field).fill(user);
    await page.locator(cfg.pass_field).fill(pass);
    await page.locator(cfg.submit).click();
    await page.waitForURL((u) => u.toString().includes(cfg.success_url_contains), { timeout: 60000 });
    await mkdir(path.dirname(path.resolve(target)), { recursive: true });
    await context.storageState({ path: target });
    console.log(`storage state saved: ${target}`);
  } finally {
    await browser.close();
  }
}

async function finish(out: string, r: Result): Promise<void> {
  await mkdir(out, { recursive: true });
  await writeFile(path.join(out, "result.json"), `${JSON.stringify(r, null, 2)}\n`, "utf8");
  console.log(`${r.status}: ${r.summary}`);
  if (r.status !== "pass") process.exitCode = 1;
}

/** --wait-until (default load: never networkidle unless asked) and --goto-timeout-ms (default 30 s). */
function navOptions(): { waitUntil: WaitUntil; gotoTimeoutMs: number } {
  const w = optional("--wait-until") ?? "load";
  if (!(WAIT_UNTIL as readonly string[]).includes(w)) throw new Error(`--wait-until must be one of ${WAIT_UNTIL.join("|")}`);
  return { waitUntil: w as WaitUntil, gotoTimeoutMs: optionalNumber("--goto-timeout-ms") ?? DEFAULT_GOTO_TIMEOUT_MS };
}

/** --timeout-ms: the check's wall-clock cap. Past it the CLI writes an error result and exits, so a hung
 * page cannot hold the run until the caller's own kill. */
function armWatchdog(out: string): void {
  const ms = optionalNumber("--timeout-ms");
  if (!ms || !(ms > 0) || command === "login") return;
  const timer = setTimeout(() => {
    const summary = `timed out after ${Math.round(ms / 1000)}s (check cap)`;
    finish(out, { status: "error", summary, artifacts: [], details: { timeout: true } })
      .catch(() => undefined)
      .finally(() => process.exit(1));
  }, ms);
  timer.unref();
}

function scrub(s: string): string {
  return secrets.filter(Boolean).reduce((acc, v) => acc.split(v).join("***"), s);
}

function viewport(): [number, number] {
  const v = optional("--viewport");
  if (v) {
    const m = v.match(/^(\d+)x(\d+)$/);
    if (!m) throw new Error("--viewport must be WxH");
    return [+m[1], +m[2]];
  }
  return [optional("--width") ? numberArg("--width") : 1440, optional("--height") ? numberArg("--height") : 900];
}

function required(name: string): string {
  const value = optional(name);
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function optional(name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function numberArg(name: string): number {
  const value = Number(required(name));
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number`);
  return value;
}

function optionalNumber(name: string): number | undefined {
  const value = optional(name);
  return value === undefined ? undefined : Number(value);
}
