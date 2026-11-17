import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { captureSurface, DEFAULT_GOTO_TIMEOUT_MS, gotoOptions, runAdapter, WAIT_UNTIL, type WaitUntil } from "./capture.js";
import { comparePng, type Mask } from "./compare.js";
import { compareStyles, type Expect } from "./probe.js";
import { runSpec, SCREENSHOT_MODES, type ScreenshotMode } from "./spec.js";

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
    case "login":
      return doLogin();
    default:
      throw new Error("usage: browser-cli capture|compare|style-probe|run-spec|login");
  }
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
  const figmaPath = required("--figma");
  const appPath = required("--app");
  const figma = await readFile(figmaPath);
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
  const r = comparePng(figma, app, { maxDiffRatio: optionalNumber("--max-diff-ratio"), masks, fit: args.includes("--fit") });
  const artifacts: string[] = [];
  if (r.diffPng) {
    await writeFile(path.join(out, "diff.png"), r.diffPng);
    artifacts.push("diff.png");
  }
  for (const [src, name] of [[figmaPath, "figma.png"], [appPath, "app.png"]] as const) {
    if (path.resolve(src) !== path.resolve(path.join(out, name))) await copyFile(src, path.join(out, name));
    artifacts.push(name);
  }
  const details: Record<string, unknown> = { reason: r.reason, width: r.width, height: r.height, mismatchedPixels: r.mismatchedPixels, diffRatio: r.diffRatio, figmaSize: r.figmaSize, appSize: r.appSize };
  let summary: string;
  if (r.status === "pass") summary = `diffRatio ${r.diffRatio?.toFixed(4)} <= ${optionalNumber("--max-diff-ratio") ?? 0.002}`;
  else if (r.reason === "size") summary = `size mismatch: figma ${r.figmaSize?.join("x")} vs app ${r.appSize?.join("x")}`;
  else if (r.reason === "pixel-diff") summary = `diffRatio ${r.diffRatio?.toFixed(4)} > ${optionalNumber("--max-diff-ratio") ?? 0.002}`;
  else summary = `compare failed: ${r.reason}`;
  return { status: r.status, summary, artifacts, details };
}

type MapEntry = { id: string; selector: string; expect: Expect; tolerance_px?: number };

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
      const props = compareStyles(e.expect ?? {}, actual as Record<string, unknown>, e.tolerance_px ?? 1);
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
