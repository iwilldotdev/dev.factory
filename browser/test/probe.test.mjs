import assert from "node:assert/strict";
import test from "node:test";
import { compareStyles, normalizeColor, parsePx, pxWithin } from "../dist/probe.js";
import { summariseReport } from "../dist/spec.js";

test("color normalisation", () => {
  assert.equal(normalizeColor("rgb(17, 34, 51)"), "#112233");
  assert.equal(normalizeColor("rgba(17, 34, 51, 1)"), "#112233");
  assert.equal(normalizeColor("rgba(255, 0, 0, 0.5)"), "#FF000080");
  assert.equal(normalizeColor("#abc"), "#AABBCC");
  assert.equal(normalizeColor("#112233ff"), "#112233");
  assert.equal(normalizeColor("rgba(0, 0, 0, 0)"), "#00000000");
  assert.equal(normalizeColor("nonsense"), null);
});

test("px parsing and tolerance", () => {
  assert.equal(parsePx("16px"), 16);
  assert.equal(parsePx("normal"), null);
  assert.ok(pxWithin(16, 16.9, 1));
  assert.ok(!pxWithin(16, 17.5, 1));
});

test("compareStyles: pass, fail, lineHeight normal, colors exact", () => {
  const actual = { fontSize: "16px", lineHeight: "24px", fontWeight: "600", color: "rgb(17, 34, 51)", width: 320.4, opacity: "1" };
  const ok = compareStyles({ fontSize: 16, lineHeight: "24px", fontWeight: 600, color: "#112233", width: 320, opacity: 1 }, actual);
  assert.ok(ok.every((r) => r.ok), JSON.stringify(ok));
  const bad = compareStyles({ fontSize: 18, color: "#112234", fontWeight: 700 }, actual);
  assert.deepEqual(bad.map((r) => r.ok), [false, false, false]);
  const normal = compareStyles({ lineHeight: 24 }, { lineHeight: "normal" });
  assert.equal(normal[0].ok, false);
  assert.match(normal[0].note, /normal/);
  assert.equal(compareStyles({ fontSize: 17.5 }, actual, 2)[0].ok, true);
  assert.equal(compareStyles({ bogus: 1 }, actual)[0].ok, false);
});

test("summariseReport: zero tests is an error", () => {
  assert.equal(summariseReport({ suites: [] }).status, "error");
  const r = summariseReport({ suites: [{ specs: [{ title: "a", ok: true, tests: [{ status: "expected" }] }, { title: "b", ok: false, tests: [{ status: "unexpected", results: [{ error: { message: "boom" } }] }] }] }] });
  assert.equal(r.status, "fail");
  assert.equal(r.details.failed, 1);
});
