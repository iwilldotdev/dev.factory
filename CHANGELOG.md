# Changelog

## 0.1.0 — 2026-11-12

First release, succeeding dev.harness.

- `factory` CLI (Python stdlib): workspaces, runs, epics (parent + child runs, waves, stacked branches), env up/down/check/login, Figma pack check and style-map skeletons, baseline, `prove` with collectors (test, red-green, api, browser, visual, style), phase gates, review records, ship plan/verify, feedback ingest, metrics.
- `browser/` collectors: Playwright spec runner, visual diff against Figma screenshots, computed-style probe.
- Claude Code layer: `factory` skill (orchestrator runbook with phase, worker, Figma pack, ship, feedback, and epic references), workers `factory-scout|author|builder|auditor`, `worker-guard.js` role hook, and the all-session `protect.js` hook that keeps run state, evidence, reviews, and baseline CLI-written only.
- `install.py`: installs into a Claude profile (skill, agents, hooks, settings hook, CLAUDE.md block, `factory` shims, `FACTORY_HOME`). `--remove-harness` backs up and removes dev.harness, unlinking linked skills without touching their targets.
