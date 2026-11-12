# Figma pack how-to

Every file is written by the tool itself: pass `save_to=<absolute path>` and `overwrite: true` (paths under `FACTORY_HOME/runs/<id>/figma/`, forward slashes). `factory figma check <run>` and `factory gate <run> plan` print each missing call ready to run, for example `figma_get_design_context fileKey=<k> nodeIds=3544:5710 includeScreenshot=false save_to=C:/Users/x/.factory/runs/<id>/figma/3544-5710.json overwrite=true`. Never paste a payload into chat or rewrite it with Write.

The pack is the design truth the plan gate and the visual/style collectors read. It lives in `R/figma/` (`R` = `FACTORY_HOME/runs/<run-id>`). The author builds it; the orchestrator only checks it with `factory figma check <run>`.

## Files

| File | From | Notes |
|---|---|---|
| `figma/meta.json` | `figma_get_file_meta` | The tool's own `{fileKey, name, lastModified, version}`, unedited. With several files, one `figma/meta-<fileKey>.json` per file (the first may stay `meta.json`). |
| `figma/<node-dashed>.json` | `figma_get_design_context` with `nodeIds: "<node>"`, `includeScreenshot: false` | Raw payload, unedited. `3544:5710` → `3544-5710.json`. |
| `figma/<node-dashed>.png` | `figma_fetch_screenshot` with `nodeIds: "<node>"`, `scale: 1`, `format: "png"` | Must decode as PNG at CSS-pixel scale (scale 1). |

## Steps (author)

1. Collect the node ids cited by the ticket (Figma URLs: `node-id=3544-5710` → node `3544:5710`) and by the ACs. Every screen cited stays in the pack, including states (empty, error, hover) when the ticket shows them.
2. `figma_get_file_meta` with `save_to` → `figma/meta.json`.
3. For each node: `figma_get_design_context` with `save_to` → `figma/<node-dashed>.json`. If the payload is truncated, refetch `truncation.nextChildIds` and keep them as extra files `figma/<child-dashed>.json`.
4. For each node: `figma_fetch_screenshot` with `save_to=.../figma/<node-dashed>.png` (the tool writes the decoded PNG; for several nodes pass a directory ending in `/`). Fallback: `figma_download_assets` with `nodeIds` and `format: png`; only if the tool can give nothing but a URL, a single `curl -sSfL -o <path> "<url>"` right away.
5. For each UI AC: `factory figma expect <run> <node> --selector "<css selector in the app>"` prints a style-map skeleton built from the payload. Save that skeleton (CLI output, small) under `checks/style/<name>.json`. Fill in selectors and keep only the properties the AC is about. Do not add values that are not in the payload: the plan gate rejects them.
6. `factory figma check <run>` must pass.

## Reading the pack

Do not read whole payloads. Use `Grep` on `figma/<node>.json` (names, `characters`, `fontSize`, `cornerRadius`, ids) or `Read` with `offset`/`limit`; the CLI (`figma expect`, `figma check`) does the bulk extraction.

## Freshness

Before ship, `figma_get_file_meta` again (to a temp `save_to`, or the same file with `overwrite: true`): if `lastModified` changed, re-pack the cited nodes and re-run prove for the `visual`/`style` checks. Record it with `factory run decide`.
