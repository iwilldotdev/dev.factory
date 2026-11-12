# Changelog

## Unreleased

- dev.mcp `save_to` integration: gates and commands that miss a remote file (`gate intake|plan`, `figma check`, `ship verify`, `feedback ingest`, `review start|queue`) print the exact dev.mcp call with an absolute, forward-slash `save_to` and `overwrite=true` (`FETCH ...` lines; `fetch: [{tool, args}]` with `--json`; `<fileKey>`/`<iid>` placeholders when unknown). Skill, references, and agents now say "call the tool with `save_to`" instead of writing responses by hand.
- Pack and readback readers accept the raw tool output: `figma/meta-<fileKey>.json` per file, `{merge_request: ...}` readbacks, MR notes plus `pipeline-<repo>-<target>-jobs.json` for feedback.
- `protect.js` denies any `mcp__*` call whose `save_to` targets `run.json`, `metrics.json`, `ship-plan.json`, `review-report.json|md`, `evidence/`, `reviews/`, `baseline/`; `review-report.json|md` are now protected for file tools and shell too. The installed matcher adds `mcp__.*`.
- `install.py --mcp-config`: merges `DEV_MCP_SAVE_ROOTS`, `DEV_MCP_SAVE_ALLOW_DOT`, `DEV_MCP_SPILL_KB` into the `dev.mcp` server `env` (missing keys only, backup first, `--dry-run` diff).

- Review runs (DESIGN §7): `review queue|start|report|fix`, `run branch --mr` (detached worktree at the MR head), `kind: review` / `review-batch` phase machines (intake, plan, prove, review, report, done; no build or ship), `review-report.json` and `.md` with a verdict per MR (`ok`, `not-ok`, `incomplete`). Nothing is posted remotely without the user's approval. Skill: `references/review.md` and a short section in `SKILL.md`.

## 0.1.0 — 2026-11-12

First release, succeeding dev.harness.

- `factory` CLI (Python stdlib): workspaces, runs, epics (parent + child runs, waves, stacked branches), env up/down/check/login, Figma pack check and style-map skeletons, baseline, `prove` with collectors (test, red-green, api, browser, visual, style), phase gates, review records, ship plan/verify, feedback ingest, metrics.
- `browser/` collectors: Playwright spec runner, visual diff against Figma screenshots, computed-style probe.
- Claude Code layer: `factory` skill (orchestrator runbook with phase, worker, Figma pack, ship, feedback, and epic references), workers `factory-scout|author|builder|auditor`, `worker-guard.js` role hook, and the all-session `protect.js` hook that keeps run state, evidence, reviews, and baseline CLI-written only.
- `install.py`: installs into a Claude profile (skill, agents, hooks, settings hook, CLAUDE.md block, `factory` shims, `FACTORY_HOME`). `--remove-harness` backs up and removes dev.harness, unlinking linked skills without touching their targets.
