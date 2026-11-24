# AGENTS.md — contributing to dev.factory

Read [DESIGN.md](DESIGN.md) first. It is the contract; change it in the same commit as any behaviour it describes.

## Principles

- **Thin prose, thick mechanisms.** If a rule matters, enforce it in the CLI, a gate, a collector, or a hook. The skill and agent prompts say *when* to call a mechanism, not *how to judge*. Keep `SKILL.md` ≤150 lines and push detail into `references/`.
- **Collectors are the only evidence writers.** Only `factory prove` (through `core.write_evidence`) writes `evidence/`. Gates read only evidence, reviews recorded through `factory review record`, and files the CLI validates. No gate passes on a model's claim.
- **The CLI never calls remote services.** It only does local env health checks, local API checks, and `git` on local repos. Remote facts come from raw provider payloads that the agent saved under `runs/<id>/raw/<kind>/` (any MCP server or CLI) and that `factory ingest` maps, through a declarative profile, into `runs/<id>/contracts/`. Gates read contracts only.
- **No provider knowledge in the repo.** No tracker, SCM, pipeline, design-source, vendor or concrete tool name appears in code, agents, skill, hooks, installer, tests or docs. Provider knowledge is data in `FACTORY_HOME/profiles/<name>/profile.json` (approved by the user via `factory profile approve`; workers write drafts in `FACTORY_HOME/profile-drafts/`). `tests/test_provider_agnostic.py` greps the repo for banned terms and fails on any hit; fixtures use the synthetic `acme` and `zeta` profiles under `tests/fixtures/profiles/`.
- **Generic files stay generic.** No company, product, project, ticket key, branch name, or person in the skill, agents, hooks, CLI, or tests. Project specifics belong in `workspace.json` under `FACTORY_HOME`. Test fixtures use neutral names (`acme`, `DEV-1`).

## Code

- Python ≥3.10, **stdlib only**, for `factory/`, `install.py`, and `tests/`. Exit codes: 0 ok, 1 gate/check failed, 2 usage/config error.
- Node ≥20 for `browser/` (Playwright, pixelmatch) and `claude/hooks/`. Hooks use only Node built-ins and fail closed on unreadable input.
- Command modules expose `register(subparsers)` (see `factory/cli.py`). Collectors expose `collect(check, ctx) -> dict` and never write evidence themselves.
- Cross-platform: paths work on Windows (drive letters, backslashes, Git Bash `/c/…`) and POSIX. Use `python`, not `python3`, in Windows-facing docs.
- Hooks are guardrails, not sandboxes. When you widen what a hook allows, add both allow and deny test cases.

## Tests

```sh
python -m unittest discover -s tests
node --test claude/hooks/test/
cd browser && npm test
```

Each new gate rule, collector, or hook rule needs a test that fails without it.

## Git

Use conventional commit subjects (`feat:`, `fix:`, `docs:`, `chore:`), and keep one logical change per commit. No force-push to `main`. Update `CHANGELOG.md` for user-visible changes.
