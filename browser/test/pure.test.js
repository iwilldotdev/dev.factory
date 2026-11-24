// Pure logic, no browser: crop, size mismatch and fit, masks, the style comparison rules [L-54, L-55].
import test from "node:test";
import assert from "node:assert/strict";
import { PNG } from "pngjs";
import { compare, crop, parseMask } from "../src/compare.js";
import { compareProp, deltaE, hex, judge } from "../src/probe.js";
import { parseFlags } from "../src/cli.js";
import { solid } from "./support.js";

const bordered = (w, h, margin) => {
  // a w x h red box inside a margin of light grey: what an export with a shadow margin looks like
  const png = solid(w + 2 * margin, h + 2 * margin, [240, 240, 240]);
  PNG.bitblt(solid(w, h), png, 0, 0, w, h, margin, margin);
  return png;
};

test("the design crop skips the export margin, so the margin does not count as a difference [L-55]", () => {
  const design = bordered(100, 40, 12);
  const res = compare(solid(100, 40), crop(design, 12, 12, 100, 40), { maxDiffRatio: 0.01 });
  assert.equal(res.ok, true);
  assert.equal(res.ratio, 0);
  assert.equal(compare(solid(100, 40), design, { maxDiffRatio: 0.01 }).reason, "size");
});

test("a crop outside the image is refused", () => {
  assert.throws(() => crop(solid(10, 10), 5, 5, 10, 10), /does not fit/);
});

test("a size mismatch fails with reason size unless fit scales the capture, and the scale is reported [L-55]", () => {
  const design = solid(100, 40);
  const res = compare(solid(50, 20), design, {});
  assert.deepEqual([res.ok, res.reason], [false, "size"]);
  const fitted = compare(solid(50, 20), design, { fit: true });
  assert.equal(fitted.ok, true);
  assert.deepEqual(fitted.scale, { x: 2, y: 2 });
  assert.equal(fitted.images.app.width, 100);
});

test("the diff ratio is compared with max_diff_ratio, and a mask hides its rectangle from both images", () => {
  const app = solid(100, 40);
  PNG.bitblt(solid(10, 10, [0, 0, 255]), app, 0, 0, 10, 10, 0, 0);
  const design = solid(100, 40);
  const bad = compare(app, design, { maxDiffRatio: 0.01 });
  assert.equal(bad.ok, false);
  assert.ok(bad.ratio > 0.02 && bad.ratio < 0.03);
  assert.equal(compare(app, design, { maxDiffRatio: 0.03 }).ok, true);
  const masked = compare(app, design, { maxDiffRatio: 0, masks: [{ x: 0, y: 0, w: 10, h: 10 }] });
  assert.equal(masked.ok, true);
  assert.deepEqual(parseMask("1,2,3,4"), { x: 1, y: 2, w: 3, h: 4 });
  assert.throws(() => parseMask("1,2,0,4"), /bad mask/);
});

const tol = { px: 1, deltaE: 0, fontPx: 0 };

test("an empty expected text, a missing border and a `normal` line height fail [L-54]", () => {
  assert.equal(compareProp("text", "", "anything", tol).ok, false);
  assert.match(compareProp("text", " ", "x", tol).note, /empty/);
  assert.equal(compareProp("lineHeight", 28, "normal", tol).ok, false);
  const missing = judge({ selector: "#b", expect: { borderWidth: 2 } }, { borderWidth: null, borderStyle: "none" }, tol);
  assert.deepEqual([missing[0].ok, missing[0].note], [false, "the element has no border"]);
  assert.equal(judge({ selector: "#b", expect: { borderWidth: 0 } }, { borderWidth: null, borderStyle: "none" }, tol)[0].ok, true);
  assert.equal(judge({ selector: "#x", expect: { fontSize: 1 } }, null, tol)[0].ok, false);
});

test("px values use the tolerance, colors use delta E, font family compares the first family", () => {
  assert.equal(compareProp("width", 100, "101px", tol).ok, true);
  assert.equal(compareProp("width", 100, "103px", tol).ok, false);
  assert.equal(compareProp("fontSize", 20, "21px", { ...tol, fontPx: 2 }).ok, true);
  assert.equal(compareProp("color", "#1f2933", "#1f2934", { ...tol, deltaE: 0 }).ok, false);
  assert.equal(compareProp("color", "#1f2933", "#1f2934", { ...tol, deltaE: 6 }).ok, true);
  assert.ok(deltaE("#000000", "#ffffff") > 99);
  assert.equal(hex("rgb(31, 41, 51)"), "#1f2933");
  assert.equal(compareProp("fontFamily", "Inter, sans-serif", '"Inter", Arial', tol).ok, true);
  assert.equal(compareProp("opacity", 0.5, "0.505", tol).ok, true);
});

test("flags: values, repeated masks and state specs, and the usage errors", () => {
  const f = parseFlags(["--out", "o", "--mask", "1,2,3,4", "--mask", "5,6,7,8", "--state-spec", "a.json", "--state-spec", "b.json", "--fit", "--timeout-ms", "5000"]);
  assert.equal(f.masks.length, 2);
  assert.deepEqual(f.stateSpecs, ["a.json", "b.json"]);
  assert.equal(f.fit, true);
  assert.equal(f.navTimeout, 4000);
  assert.throws(() => parseFlags(["--out"]), /needs a value/);
  assert.throws(() => parseFlags(["x"]), /unexpected/);
  assert.throws(() => parseFlags([]), /--out/);
});
