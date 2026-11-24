// Computed-style probe of the `style` command and the comparison rules of the property table [L-54, L-187].

const PX = new Set(["width", "height", "borderWidth", "borderRadius", "borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius",
  "borderBottomLeftRadius", "gap", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "lineHeight", "letterSpacing"]);
const COLORS = new Set(["color", "backgroundColor", "borderColor"]);

export const hex = (css) => {
  const m = /rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+))?/.exec(css || "");
  if (!m) return null;
  return "#" + [m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("");
};

function lab(h) {
  const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  const x = (0.4124 * c[0] + 0.3576 * c[1] + 0.1805 * c[2]) / 0.95047, y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2], z = (0.0193 * c[0] + 0.1192 * c[1] + 0.9505 * c[2]) / 1.08883;
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
}

/** CIE76 distance between two #rrggbb colors. */
export const deltaE = (a, b) => Math.hypot(...lab(a).map((v, i) => v - lab(b)[i]));

const first = (family) => String(family || "").split(",")[0].trim().replace(/^["']|["']$/g, "").toLowerCase();

/** Compare one property. Returns { ok, note }. `tol` has px, deltaE, fontPx, and `entryPx` for the entry's own tolerance. */
export function compareProp(prop, expected, actual, tol) {
  if (prop === "text") {
    if (String(expected).trim() === "") return { ok: false, note: "the expected text is empty, which proves nothing" };
    return { ok: String(actual).trim() === String(expected), note: "" };
  }
  if (actual === null || actual === undefined) return { ok: false, note: "the element has no such value" };
  if (prop === "lineHeight" && actual === "normal") return { ok: false, note: "computed line height is `normal`" };
  if (COLORS.has(prop)) {
    const d = actual && deltaE(String(expected).toLowerCase(), actual);
    return { ok: Boolean(actual) && d <= tol.deltaE, note: actual ? `deltaE ${d.toFixed(1)}` : "no color" };
  }
  if (prop === "fontFamily") return { ok: first(actual) === first(expected), note: "" };
  if (prop === "textAlign") return { ok: actual === expected, note: "" };
  if (prop === "fontWeight") return { ok: Number(actual) === Number(expected), note: "" };
  if (prop === "opacity") return { ok: Math.abs(Number(actual) - Number(expected)) <= 0.01, note: "" };
  const limit = prop === "fontSize" ? tol.fontPx : PX.has(prop) ? tol.px : 0;
  const diff = Math.abs(parseFloat(actual) - Number(expected));
  return { ok: diff <= limit + 1e-9, note: diff ? `off by ${diff.toFixed(2)} (tolerance ${limit})` : "" };
}

/** Run in the page: read what the style map may ask about one element. */
export function readElement(selector) {
  const el = document.querySelector(selector);
  if (!el) return null;
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  const radii = [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius];
  return {
    width: r.width, height: r.height, color: s.color, backgroundColor: s.backgroundColor, borderColor: s.borderTopColor,
    borderWidth: s.borderTopStyle === "none" ? null : s.borderTopWidth, borderStyle: s.borderTopStyle,
    borderRadius: radii.every((v) => v === radii[0]) ? radii[0] : null,
    borderTopLeftRadius: radii[0], borderTopRightRadius: radii[1], borderBottomRightRadius: radii[2], borderBottomLeftRadius: radii[3],
    opacity: s.opacity, gap: s.rowGap === "normal" ? "0px" : s.rowGap, paddingTop: s.paddingTop, paddingRight: s.paddingRight, paddingBottom: s.paddingBottom,
    paddingLeft: s.paddingLeft, fontFamily: s.fontFamily, fontSize: s.fontSize, fontWeight: s.fontWeight, lineHeight: s.lineHeight,
    letterSpacing: s.letterSpacing === "normal" ? "0px" : s.letterSpacing, textAlign: s.textAlign, text: (el.textContent || "").trim(),
  };
}

export function judge(entry, actual, tol) {
  const own = { ...tol, px: entry.tolerance_px ?? tol.px };
  if (actual === null) return [{ selector: entry.selector, property: "(element)", expected: "present", actual: "not found", ok: false, note: "no element matches the selector" }];
  return Object.entries(entry.expect).map(([property, expected]) => {
    let value = actual[property];
    if (COLORS.has(property)) value = hex(value);
    let res;
    if (property === "borderWidth" && Number(expected) === 0 && value === null) res = { ok: true, note: "" };
    else if (property === "borderWidth" && Number(expected) > 0 && (value === null || actual.borderStyle === "none")) res = { ok: false, note: "the element has no border" };
    else res = compareProp(property, expected, value, own);
    return { selector: entry.selector, property, expected, actual: value, ok: res.ok, note: res.note };
  });
}

export async function probe(page, map, tol) {
  const results = [];
  for (const entry of map.entries) results.push(...judge(entry, await page.evaluate(readElement, entry.selector), tol));
  return results;
}
