# Figma pack how-to

The pack is the design truth the plan gate and the visual/style collectors read. It lives in `R/figma/` (`R` = `FACTORY_HOME/runs/<run-id>`). The author builds it; the orchestrator only checks it with `factory figma check <run>`.

## Files

| File | From | Notes |
|---|---|---|
| `figma/meta.json` | `figma_get_file_meta` per file | `{"files": {"<fileKey>": {"fileKey", "name", "lastModified", "version"}}}`. Keep the tool's values unedited. |
| `figma/<node-dashed>.json` | `figma_get_design_context` with `nodeIds: "<node>"`, `includeScreenshot: false` | Raw payload, unedited. `3544:5710` → `3544-5710.json`. |
| `figma/<node-dashed>.png` | `figma_fetch_screenshot` with `nodeIds: "<node>"`, `scale: 1`, `format: "png"` | Must decode as PNG at CSS-pixel scale (scale 1). |

## Steps (author)

1. Collect the node ids cited by the ticket (Figma URLs: `node-id=3544-5710` → node `3544:5710`) and by the ACs. Every screen cited stays in the pack, including states (empty, error, hover) when the ticket shows them.
2. `figma_get_file_meta` → write `figma/meta.json`.
3. For each node: `figma_get_design_context` → `figma/<node-dashed>.json`. If the payload is truncated, refetch `truncation.nextChildIds` and keep them as extra files `figma/<child-dashed>.json`.
4. For each node: write the screenshot bytes to `figma/<node-dashed>.png`. If the tool returns only a URL, download it with a single `curl -sSfL -o <path> "<url>"` (temporary URLs expire; do it right away). `figma_download_assets` with `nodeIds` and `format: png` is the fallback.
5. For each UI AC: `factory figma expect <run> <node> --selector "<css selector in the app>"` prints a style-map skeleton built from the payload. Save it under `checks/style/<name>.json`. Fill in selectors and keep only the properties the AC is about. Do not add values that are not in the payload: the plan gate rejects them.
6. `factory figma check <run>` must pass.

## Freshness

Before ship, `figma_get_file_meta` again: if `lastModified` changed, re-pack the cited nodes and re-run prove for the `visual`/`style` checks. Record it with `factory run decide`.
