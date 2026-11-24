# Design pack how-to

The design source is a provider like any other: gates and collectors read two contracts per frame, never the provider's payload. `R` = `FACTORY_HOME/runs/<run-id>`.

| Path | Writer | Content |
|---|---|---|
| `R/raw/design_frame/<id>.json` | author (the design source's read tool or CLI saves it at the printed path) | Untouched frame payload. For an approx frame: the native-format spec the author writes. |
| `R/raw/design_image/<id>.png` | author (same) | The frame rendered at scale 1 (CSS pixels); must decode as PNG. |
| `R/contracts/design_image/<id>.json` | `factory ingest` | `{path, sha256, width, height}`. |
| `R/contracts/design_frame/<id>.json` | `factory ingest` | `design_frame.v1`: `fidelity` (`exact` or `approx`), `frame.image`, `source.{file,version}`, node tree (`box`, `render_box`, fills, strokes, effects, `corner_radius`, optional `corner_radii` `[tl,tr,br,bl]` (flagged in `estimated` for approx frames), `layout`, `text`, `estimated`), `unknowns`, `provenance`. |

Ids are the frame id as the source writes it, made filename-safe (characters outside `[A-Za-z0-9._-]` become `-`: `12:34` → `12-34`). Plan checks name the frame in `node` (alias `frame`); `source_file` names the design file or document (hint placeholder `{file}`, `--ctx file=<source_file>`).

`factory design check <run>` and `factory gate <run> plan` print every missing item as a `FETCH` line: `design_image <id> -> <raw_path> | <hint from the design profile>`. Run the hint as-is with whatever read tool or read-only CLI it names, saving at `raw_path`. Never paste a payload into chat or rewrite it with Write.

## Steps (author)

1. Collect the frame ids cited by the ticket (links, attachments) and by the ACs. Every screen cited stays in the pack, including states (empty, error, hover) when the ticket shows them.
2. Per frame, fetch the image first, then the frame description, each at its `raw_path`. If a payload is truncated, refetch the missing children as the source documents say; keep every raw file untouched.
3. Ingest, image first (the frame contract records the image path):
   - `factory ingest <run> design_image raw/design_image/<id>.png --id <id> --source-hint "<what you ran>"`
   - `factory ingest <run> design_frame raw/design_frame/<id>.json --id <id> --ctx file=<source_file> --source-hint "<what you ran>"`

   Put every `unknowns[]` entry of the contracts in your report. An `exact` frame needs `source.version` (the revision it was read at); without one the gate refuses `exact`.
4. For each UI AC: `factory design expect <run> <frame> --selector "<css selector in the app>"` writes a style-map skeleton at `checks/style/<id>.skeleton.json`, built from the contract (node properties through a fixed property table). Copy it to `checks/style/<name>.json`, fill in selectors, keep only the properties the AC is about. Do not add values that are not in the contract: the plan gate rejects them.
5. `factory design check <run>` must pass.

## Approximate frames (mockup image, no machine-readable source)

`factory design add <run> <image.png> --name N --fidelity approx [--id ID] [--source-hint "<conversion you ran>"]` normalises the PNG into `raw/design_image/`, ingests it and writes a skeleton frame (one root frame, `estimated: {box: true}`). A non-PNG image is converted by you first, and the conversion command goes in `--source-hint`. Then write `raw/design_frame/<id>.json` in native format (regions, measures, sampled colours, texts) with EVERY number and colour listed in the node's `estimated` map, and run `factory ingest <run> design_frame <raw> --id <id> --format native`: it is rejected when a value is not flagged.

`--format native` is accepted only for approx frames (and design images); an exact frame always comes from the design profile (`factory ingest <run> design_frame <raw>` through the profile), never from a hand-written native file.

Approx never counts as exact:

- A check that uses an approx frame carries `"fidelity": "approx"`, and ONLY then: the approx ceilings and tolerances follow the frame CONTRACT's fidelity, never the check's claim. A check with `fidelity: "approx"` on an exact frame is a plan error (remove it; exact ceilings apply). Style checks on an approx frame use the approx tolerances (`limits.approx`: `px`, `color_delta_e`, `font_size_px`, `max_diff_ratio`; defaults 4, 6, 2, 0.10, still capped by the workspace limits).
- The review gate needs, per approx frame an AC references, the user's design review, recorded by the main session: `factory run decide <run> "<frame>: design reviewed" --by user --user-said "<gist>" --design-review <frame>`.
- The ship description lists approx frames under "Design fidelity: approximate".

## Reading the pack

Do not read whole payloads. Use `Grep` on the raw file or the contract (names, `characters`, `font_size`, `corner_radius`, ids) or `Read` with `offset`/`limit`; the CLI (`design expect`, `design check`) does the bulk extraction.

## Freshness

Before ship, re-read the frame (same raw path, overwrite) and run `factory ingest <run> --check`: a contract is stale when the sha256 of its raw file changed. If `source.version` moved, re-ingest the cited frames and re-run prove for the `visual`/`style` checks. Record it with `factory run decide`.
