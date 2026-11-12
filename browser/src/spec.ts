import { mkdir, readFile, writeFile } from "node:fs/promises";
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

export async function runSpec(o: { spec: string; baseUrl: string; storageState?: string; outDir: string }): Promise<Summary> {
  await mkdir(o.outDir, { recursive: true });
  const spec = path.resolve(o.spec);
  const outDir = path.resolve(o.outDir);
  const reportFile = path.join(outDir, "report.json");
  const configFile = path.join(outDir, "playwright.config.mjs");
  const config = `export default {
  testDir: ${JSON.stringify(path.dirname(spec))},
  testMatch: ${JSON.stringify(path.basename(spec))},
  outputDir: ${JSON.stringify(path.join(outDir, "artifacts"))},
  reporter: [["json", { outputFile: ${JSON.stringify(reportFile)} }]],
  retries: 0,
  workers: 1,
  use: {
    baseURL: ${JSON.stringify(o.baseUrl)},
    ${o.storageState ? `storageState: ${JSON.stringify(path.resolve(o.storageState))},` : ""}
    trace: "off",
    screenshot: "only-on-failure",
  },
};
`;
  await writeFile(configFile, config, "utf8");
  const require = createRequire(import.meta.url);
  const cli = path.join(path.dirname(require.resolve("@playwright/test/package.json")), "cli.js");
  const log = await new Promise<string>((resolve) => {
    const child = spawn(process.execPath, [cli, "test", "--config", configFile], {
      cwd: path.dirname(spec),
      env: { ...process.env, FORCE_COLOR: "0", NODE_PATH: path.join(path.dirname(path.dirname(cli)), "..") },
    });
    let buf = "";
    child.stdout.on("data", (d) => (buf += d));
    child.stderr.on("data", (d) => (buf += d));
    child.on("close", () => resolve(buf));
    child.on("error", (e) => resolve(String(e)));
  });
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
