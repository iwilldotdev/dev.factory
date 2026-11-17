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

test("compareStyles: fontFamily, borderColor, text, borderWidth", () => {
  const actual = { fontFamily: '"Inter", Arial, sans-serif', borderColor: "rgb(255, 0, 0)", borderWidth: "1px", text: "  Salvar \n" };
  const ok = compareStyles({ fontFamily: "inter", borderColor: "#FF0000", borderWidth: 1, text: "Salvar" }, actual);
  assert.ok(ok.every((r) => r.ok), JSON.stringify(ok));
  assert.ok(compareStyles({ fontFamily: "'INTER', serif" }, actual)[0].ok);
  const bad = compareStyles({ fontFamily: "Roboto", borderColor: "#00FF00", text: "Cancelar" }, actual);
  assert.deepEqual(bad.map((r) => r.ok), [false, false, false]);
  assert.ok(bad.every((r) => r.note !== "unknown property"));
});

test("compareStyles: no false PASS for empty expected text or a missing border", () => {
  // empty expected text never passes, even against an empty element
  const [t] = compareStyles({ text: "   " }, { text: "" });
  assert.equal(t.ok, false);
  assert.equal(t.note, "expected text is empty");
  // a 1px border is not satisfied by 0px (border absent / style none), despite the 1px tolerance
  for (const actual of ["0px", "0", 0]) {
    const [b] = compareStyles({ borderWidth: 1 }, { borderWidth: actual });
    assert.equal(b.ok, false, String(actual));
    assert.equal(b.note, "border missing (0px or style none)");
  }
  // the tolerance still applies to a present border, and an expected 0 still matches 0
  assert.ok(compareStyles({ borderWidth: 1 }, { borderWidth: "1.5px" })[0].ok);
  assert.ok(compareStyles({ borderWidth: 0 }, { borderWidth: "0px" })[0].ok);
});
