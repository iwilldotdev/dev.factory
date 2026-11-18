import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

export type Summary = { status: "pass" | "fail" | "error"; summary: string; details: Record<string, unknown> };

type Suite = {
  suites?: Suite[];
  specs?: Array<{ title: string; ok: boolean; tests?: Array<{ status: string; results?: Array<{ error?: { message?: string } }> }> }>;
};

export function summariseReport(report: { suites?: Suite[] }): Summary {
  const tests: Array<{ title: string; ok: boolean; error?: string }> = [];
  const walk = (s: Suite): void => {
    for (const sp of s.specs ?? []) {
      const ts = sp.tests ?? [];
      if (ts.length > 0 && ts.every((t) => t.status === "skipped")) continue;
      const err = ts.flatMap((t) => t.results ?? []).find((r) => r.error)?.error?.message;
      tests.push({ title: sp.title, ok: sp.ok, error: err ? err.slice(0, 600) : undefined });
    }
    (s.suites ?? []).forEach(walk);
  };
  (report.suites ?? []).forEach(walk);
  if (tests.length === 0) return { status: "error", summary: "no tests ran", details: { tests } };
  const failed = tests.filter((t) => !t.ok).length;
  return {
    status: failed ? "fail" : "pass",
    summary: `${tests.length - failed} passed, ${failed} failed`,
    details: { passed: tests.length - failed, failed, tests },
  };
}

export const SCREENSHOT_MODES = ["only-on-failure", "on", "off"];
export type ScreenshotMode = "only-on-failure" | "on" | "off";

export const DEFAULT_GOTO_TIMEOUT_MS = 30000;

export function expectTimeoutMs(): number | undefined {
  const value = Number(process.env.FACTORY_EXPECT_TIMEOUT_MS);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : undefined;
}

/** Per-test and global caps: a spec cannot outlive `timeoutMs` (the check's wall-clock cap minus a margin). */
export function timeoutConfig(timeoutMs?: number, gotoTimeoutMs?: number): { test: number; global: number; nav: number } {
  const total = timeoutMs && timeoutMs > 0 ? Math.floor(timeoutMs) : 0;
  const nav = Math.max(1, Math.min(gotoTimeoutMs && gotoTimeoutMs > 0 ? gotoTimeoutMs : DEFAULT_GOTO_TIMEOUT_MS, total || Infinity));
  return { test: total ? Math.max(1, total - 1000) : 0, global: total, nav };
}

export async function runSpec(o: { spec: string; baseUrl: string; storageState?: string; outDir: string; screenshot?: ScreenshotMode; timeoutMs?: number; gotoTimeoutMs?: number }): Promise<Summary> {
  await mkdir(o.outDir, { recursive: true });
  const spec = path.resolve(o.spec);
  const outDir = path.resolve(o.outDir);
  const reportFile = path.join(outDir, "report.json");
  const configFile = path.join(outDir, "playwright.config.mjs");
  const to = timeoutConfig(o.timeoutMs, o.gotoTimeoutMs);
  const config = `export default {
  testDir: ${JSON.stringify(path.dirname(spec))},
  testMatch: ${JSON.stringify(path.basename(spec))},
  outputDir: ${JSON.stringify(path.join(outDir, "artifacts"))},
  reporter: [["json", { outputFile: ${JSON.stringify(reportFile)} }]],
  retries: 0,
  workers: 1,
  ${to.test ? `timeout: ${to.test},` : ""}
  ${to.global ? `globalTimeout: ${to.global},` : ""}
  ${expectTimeoutMs() ? `expect: { timeout: ${expectTimeoutMs()} },` : ""}
  use: {
    baseURL: ${JSON.stringify(o.baseUrl)},
    navigationTimeout: ${to.nav},
    actionTimeout: ${to.nav},
    ${o.storageState ? `storageState: ${JSON.stringify(path.resolve(o.storageState))},` : ""}
    trace: "off",
    screenshot: ${JSON.stringify(o.screenshot ?? "only-on-failure")},
  },
};
`;
  await writeFile(configFile, config, "utf8");
  const log = await playwrightRun(configFile, path.dirname(spec), []);
  await writeFile(path.join(outDir, "run.log"), log, "utf8");
  let report: { suites?: Suite[] };
  try {
    report = JSON.parse(await readFile(reportFile, "utf8"));
  } catch {
    return { status: "error", summary: "Playwright produced no report", details: { log: log.slice(-1500) } };
  }
  const s = summariseReport(report);
  if (s.status !== "pass") s.details.log = log.slice(-1500);
  return s;
}

async function playwrightRun(configFile: string, cwd: string, extra: string[]): Promise<string> {
  const require = createRequire(import.meta.url);
  const cli = path.join(path.dirname(require.resolve("@playwright/test/package.json")), "cli.js");
  return new Promise<string>((resolve) => {
    const child = spawn(process.execPath, [cli, "test", "--config", configFile, ...extra], {
      cwd,
      env: { ...process.env, FORCE_COLOR: "0", NODE_PATH: path.join(path.dirname(path.dirname(cli)), "..") },
    });
    let buf = "";
    child.stdout.on("data", (d) => (buf += d));
    child.stderr.on("data", (d) => (buf += d));
    child.on("close", () => resolve(buf));
    child.on("error", (e) => resolve(String(e)));
  });
}

// ---------- batch: several specs of one repo in one Playwright process (one project per check) ----------

type BatchTest = { projectName?: string; status: string; results?: Array<{ status?: string; duration?: number; error?: { message?: string } }> };
type BatchSuite = { suites?: BatchSuite[]; specs?: Array<{ title: string; ok?: boolean; tests?: BatchTest[] }> };
export type BatchEntry = { id: string; spec: string; out: string; timeoutMs?: number; gotoTimeoutMs?: number };
export type BatchResult = Summary & { id: string; ran: boolean; durationMs: number };

/** Result of one project (check) of a batch report. `ran` is false when none of its tests ran (stopped by
 * --max-failures) or one was interrupted by the global timeout: the caller decides what that means. */
export function summariseProject(report: { suites?: BatchSuite[] }, project: string): Omit<BatchResult, "id"> {
  const tests: Array<{ title: string; ok: boolean; error?: string }> = [];
  let ran = false;
  let interrupted = false;
  let durationMs = 0;
  const walk = (s: BatchSuite): void => {
    for (const sp of s.specs ?? []) {
      const ts = (sp.tests ?? []).filter((t) => t.projectName === project);
      if (ts.length === 0) continue;
      const results = ts.flatMap((t) => t.results ?? []);
      if (results.length > 0) ran = true;
      if (results.some((r) => r.status === "interrupted")) interrupted = true;
      durationMs += results.reduce((n, r) => n + (r.duration ?? 0), 0);
      if (results.length === 0 || ts.every((t) => t.status === "skipped")) continue;
      const err = results.find((r) => r.error)?.error?.message;
      const ok = ts.every((t) => t.status === "expected" || t.status === "flaky" || t.status === "skipped");
      tests.push({ title: sp.title, ok, error: err ? err.slice(0, 600) : undefined });
    }
    (s.suites ?? []).forEach(walk);
  };
  (report.suites ?? []).forEach(walk);
  if (interrupted) ran = false;
  if (!ran) return { status: "error", summary: "not run", details: { tests }, ran: false, durationMs };
  if (tests.length === 0) return { status: "error", summary: "no tests ran", details: { tests }, ran: true, durationMs };
  const failed = tests.filter((t) => !t.ok).length;
  return {
    status: failed ? "fail" : "pass",
    summary: `${tests.length - failed} passed, ${failed} failed`,
    details: { passed: tests.length - failed, failed, tests },
    ran: true,
    durationMs,
  };
}

function projectReport(report: { suites?: BatchSuite[] }, project: string): unknown {
  const keep = (s: BatchSuite): BatchSuite => ({
    ...s,
    specs: (s.specs ?? []).map((sp) => ({ ...sp, tests: (sp.tests ?? []).filter((t) => t.projectName === project) })).filter((sp) => sp.tests.length > 0),
    suites: (s.suites ?? []).map(keep),
  });
  return { ...report, suites: (report.suites ?? []).map(keep) };
}

/** One Playwright process, one worker, one project per entry (own per-test timeout and output dir).
 * The global timeout is the sum of the entries' caps. Returns null when Playwright wrote no report (caller falls back). */
export async function runSpecs(o: { entries: BatchEntry[]; baseUrl: string; storageState?: string; outDir: string; maxFailures?: number; screenshot?: ScreenshotMode }): Promise<BatchResult[] | null> {
  await mkdir(o.outDir, { recursive: true });
  const outDir = path.resolve(o.outDir);
  const reportFile = path.join(outDir, "report.json");
  const configFile = path.join(outDir, "playwright.config.mjs");
  await rm(reportFile, { force: true });
  const names = o.entries.map((_, i) => `c${i}`);
  const caps = o.entries.map((e) => timeoutConfig(e.timeoutMs, e.gotoTimeoutMs));
  const total = caps.reduce((n, c) => n + c.global, 0);
  const projects = o.entries.map((e, i) => {
    const spec = path.resolve(e.spec);
    return `{
    name: ${JSON.stringify(names[i])},
    testDir: ${JSON.stringify(path.dirname(spec))},
    testMatch: ${JSON.stringify(path.basename(spec))},
    outputDir: ${JSON.stringify(path.join(path.resolve(e.out), "artifacts"))},
    ${caps[i].test ? `timeout: ${caps[i].test},` : ""}
    use: {
      baseURL: ${JSON.stringify(o.baseUrl)},
      navigationTimeout: ${caps[i].nav},
      actionTimeout: ${caps[i].nav},
      ${o.storageState ? `storageState: ${JSON.stringify(path.resolve(o.storageState))},` : ""}
      trace: "off",
      screenshot: ${JSON.stringify(o.screenshot ?? "only-on-failure")},
    },
  }`;
  });
  const config = `export default {
  reporter: [["json", { outputFile: ${JSON.stringify(reportFile)} }]],
  retries: 0,
  workers: 1,
  ${total ? `globalTimeout: ${total},` : ""}
  ${expectTimeoutMs() ? `expect: { timeout: ${expectTimeoutMs()} },` : ""}
  projects: [${projects.join(",")}],
};
`;
  await writeFile(configFile, config, "utf8");
  const extra = ["--workers=1", ...(o.maxFailures && o.maxFailures > 0 ? [`--max-failures=${Math.floor(o.maxFailures)}`] : [])];
  const log = await playwrightRun(configFile, path.dirname(path.resolve(o.entries[0].spec)), extra);
  await writeFile(path.join(outDir, "run.log"), log, "utf8");
  let report: { suites?: BatchSuite[] };
  try {
    report = JSON.parse(await readFile(reportFile, "utf8"));
  } catch {
    return null;
  }
  const results: BatchResult[] = [];
  for (const [i, e] of o.entries.entries()) {
    const r = summariseProject(report, names[i]);
    if (r.ran && r.status !== "pass") r.details.log = log.slice(-1500);
    await mkdir(e.out, { recursive: true });
    await writeFile(path.join(e.out, "report.json"), JSON.stringify(projectReport(report, names[i]), null, 2), "utf8");
    await writeFile(path.join(e.out, "run.log"), log, "utf8");
    results.push({ id: e.id, ...r });
  }
  return results;
}
