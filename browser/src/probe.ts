export const PX_PROPS = [
  "fontSize", "lineHeight", "letterSpacing", "borderRadius",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "gap", "width", "height", "borderWidth",
] as const;
export const COLOR_PROPS = ["color", "backgroundColor", "borderColor"] as const;
export const NUM_PROPS = ["fontWeight", "opacity"] as const;

export type Expect = Record<string, string | number>;
export type PropResult = { property: string; expected: unknown; actual: unknown; ok: boolean; note?: string };

const hex2 = (n: number): string => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0").toUpperCase();

/** rgb()/rgba()/#hex -> #RRGGBB or #RRGGBBAA (alpha omitted when 1). null when unparseable. */
export function normalizeColor(input: unknown): string | null {
  if (typeof input !== "string") return null;
  const s = input.trim().toLowerCase();
  if (s === "transparent") return "#00000000";
  let m = s.match(/^#([0-9a-f]{3,4})$/);
  if (m) {
    const [r, g, b, a] = m[1].split("").map((c) => c + c);
    return `#${r}${g}${b}${a && a !== "ff" ? a : ""}`.toUpperCase();
  }
  m = s.match(/^#([0-9a-f]{6}|[0-9a-f]{8})$/);
  if (m) {
    const h = m[1].toUpperCase();
    return h.length === 8 && h.endsWith("FF") ? `#${h.slice(0, 6)}` : `#${h}`;
  }
  m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/);
  if (m) {
    let alpha = 1;
    if (m[4] !== undefined) alpha = m[4].endsWith("%") ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
    const base = `#${hex2(+m[1])}${hex2(+m[2])}${hex2(+m[3])}`;
    return alpha >= 1 ? base : `${base}${hex2(alpha * 255)}`;
  }
  return null;
}

/** First family of a font-family list, unquoted and lowercased. */
export function primaryFamily(input: unknown): string {
  if (typeof input !== "string") return "";
  const first = input.split(",")[0] ?? "";
  return first.trim().replace(/^["']+|["']+$/g, "").trim().toLowerCase();
}

/** "16px" | 16 -> number; null for non-numeric ("normal", "auto"). */
export function parsePx(v: unknown): number | null {
  if (typeof v === "number") return v;
  if (typeof v !== "string") return null;
  const m = v.trim().match(/^(-?[\d.]+)(px)?$/);
  return m ? parseFloat(m[1]) : null;
}

export function pxWithin(expected: number, actual: number, tol: number): boolean {
  return Math.abs(expected - actual) <= tol + 1e-9;
}

/** Compare one expected map against actual computed values. */
export function compareStyles(expect: Expect, actual: Record<string, unknown>, tolerancePx = 1): PropResult[] {
  const out: PropResult[] = [];
  for (const [prop, exp] of Object.entries(expect)) {
    const act = actual[prop];
    if ((COLOR_PROPS as readonly string[]).includes(prop)) {
      const e = normalizeColor(exp);
      const a = normalizeColor(act);
      out.push({ property: prop, expected: e ?? exp, actual: a ?? act, ok: e !== null && a !== null && e === a, ...(e === null ? { note: "expected color unparseable" } : {}) });
    } else if ((NUM_PROPS as readonly string[]).includes(prop)) {
      const e = Number(exp);
      const a = Number(act);
      const tol = prop === "opacity" ? 0.01 : 0;
      out.push({ property: prop, expected: e, actual: a, ok: Number.isFinite(e) && Number.isFinite(a) && Math.abs(e - a) <= tol + 1e-9 });
    } else if ((PX_PROPS as readonly string[]).includes(prop)) {
      const e = parsePx(exp);
      const a = parsePx(act);
      if (prop === "lineHeight" && a === null) {
        out.push({ property: prop, expected: exp, actual: act, ok: false, note: "computed lineHeight is 'normal'; set an explicit line-height" });
      } else if (prop === "letterSpacing" && String(act) === "normal") {
        out.push({ property: prop, expected: e, actual: 0, ok: e === 0 });
      } else if (prop === "gap" && String(act) === "normal") {
        out.push({ property: prop, expected: e, actual: 0, ok: e === 0 });
      } else {
        out.push({ property: prop, expected: e, actual: a, ok: e !== null && a !== null && pxWithin(e, a, tolerancePx) });
      }
    } else if (prop === "fontFamily") {
      const e = primaryFamily(exp);
      const a = primaryFamily(act);
      out.push({ property: prop, expected: e, actual: a, ok: e !== "" && e === a });
    } else if (prop === "text") {
      const e = String(exp).trim();
      const a = typeof act === "string" ? act.trim() : "";
      out.push({ property: prop, expected: e, actual: a, ok: e === a });
    } else {
      out.push({ property: prop, expected: exp, actual: act, ok: false, note: "unknown property" });
    }
  }
  return out;
}
