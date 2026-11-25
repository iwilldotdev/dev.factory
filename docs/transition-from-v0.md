# Transition from v0

v0 (tag `v0-final`) stays usable while you move. v1 shares nothing with it on disk, so both can be installed at once. The owner archives the v0 artifacts when the first real ticket has gone through v1; nothing in this repository touches `~/.claude` or `~/.claude-perfil2`.

## Coexistence

- **Entry point.** v0 is the shim `~/.claude/bin/factory` (and its copy in the second profile), a snapshot made by its `install.py`. v1 is the `factory` console script of the Python installer (`pipx`, `uv tool`, a venv: `docs/install.md`). Both are named `factory`, so PATH order decides which one runs. While you use both, call v1 by its full path or put its folder first, and check with `factory --version` (v1 prints `factory 1.0.0a1 (...) python 3.x`).
- **State.** v0 keeps workspaces and runs under `~/.factory` (`FACTORY_HOME`). v1 keeps `factory.toml` and a `.factory/` folder inside the project or workspace directory, and nothing global. A run started in one is invisible to the other: finish v0 runs in v0.
- **Skill.** Both register `/factory`. When a user-level skill and a project skill share a name, the host decides which one answers (as far as known, the user-level one wins), so while the v0 global skill is installed `/factory` may still run v0. Archive the v0 skill first (or rename its folder), then wire v1 per project (`factory init --harness claude-code`) or globally (`--scope user`). The v1 adapter skips a file at its path that it did not generate, and says so.

## What replaces each v0 artifact

| v0 | v1 |
|---|---|
| `install.py` and the `bin/factory` shim, a snapshot per profile | The Python console script, installed from a git tag. `factory init --harness claude-code` writes the adapter, so no copy drifts in a profile (L-149, L-160). |
| Global skill `skills/factory/SKILL.md` and its `references/` | `.claude/skills/factory/SKILL.md` per project (or `--scope user`), at most 100 lines. The protocol lives in the CLI: `factory next` prints the required action. |
| Agents `factory-scout`, `factory-author`, `factory-builder`, `factory-auditor` | One `.claude/agents/factory-worker.md` with the four roles as sections (the adapters have a 3-file cap). The request names the role; the reviewer's independence is enforced by the CLI through `--actor`. |
| Hooks `protect.js`, `worker-guard.js`, `failfast.js`, `protect-lib.js` | Optional `factory init --harness claude-code --hooks`: one entry, `factory hook pre-tool`, refusing hand edits of run events and evidence. Fail-fast on repeated environment failures is in `factory next` (it reports the run blocked). The role guard is not ported: evidence binds to commands and commits, not to who typed them. |
| `CLAUDE.md` block "dev.factory" | A short managed block between `<!-- factory:begin -->` and `<!-- factory:end -->`. Remove the v0 block yourself; v1 does not know it. |
| `~/.factory` workspaces (`workspace.json`, secrets, runs) | `factory.toml` per repo and per workspace directory, `local.toml` beside it, secrets in the environment. `factory init --import <workspace.json>` converts a v0 workspace and lists what it could not convert. |
| Tracker, change and design calls made by the skill | Adapters in `[providers.*]` (a command that prints JSON), or the agent saves a payload to a file and runs `factory ingest`. See `docs/schema/contracts.md`. |

## Moving a project

1. Install v1 (`docs/install.md`). Keep the v0 shim reachable under another name if you want it.
2. In the project: `factory init --import <path to the v0 workspace.json> --harness claude-code`, read "not imported", then `factory check`.
3. Open the chat and run `/factory <ticket>`. Resume with `/factory` and the run id, review queues with "revisar MR", parallel sessions with `/factory session <run-ids>`.
4. When v1 has delivered, archive the v0 files yourself: the global skill, the four `factory-*` agents, the hook entries in the settings, the `dev.factory` block of the global `CLAUDE.md`, and `~/.factory` (its runs hold history you may want to keep).
