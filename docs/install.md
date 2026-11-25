# Install

dev.factory v1 is a standard Python application: install it from a git tag, apart from any working tree, so a branch under edit never serves a live run [L-149, L-130].

Needs: Python 3.11 or later and `git`. Node 18 or later and a browser only when a plan uses `browser`, `visual`, `style` or `screenshot` checks (see the end).
The CLI has no other dependency.

## Install the CLI

Pick one. `<repo-url>` is the repository you cloned from; `1.0.0-alpha` is the tag.

```
pipx install "git+<repo-url>@1.0.0-alpha"
uv tool install "git+<repo-url>@1.0.0-alpha"
python -m venv ~/.venvs/factory && ~/.venvs/factory/bin/pip install "git+<repo-url>@1.0.0-alpha"   # Windows: Scripts\pip.exe
```

The first two put a `factory` entry point on PATH (`pipx ensurepath`, or the uv tool directory). With a venv, put its `bin` (Windows: `Scripts`) on PATH or call it by full path.
Check it: `factory --version` prints the version, the build and the Python it runs. The tag `1.0.0-alpha` installs as version `1.0.0a1` (the PEP 440 spelling of the same release).

Upgrade: install the new tag again (`pipx install --force ...`, `uv tool install --force ...`). Pin a project to a CLI range with `[project] cli = ">=1.0.0a1,<1.1"` in `factory.toml`; `factory check` fails outside it.

## Wire a project to the chat harness

```
cd my-project                                # or the workspace directory that holds several repos
factory init --harness claude-code           # factory.toml, .factory/, the skill, the worker agent and a short CLAUDE.md block
factory init --harness claude-code --hooks   # also the optional hook entry
```

`init` never overwrites `factory.toml`. In a directory that already has one, `init --harness ...` writes the adapter only (this is also how you refresh it after a CLI upgrade).

- `--scope project` (default) writes into the directory: `.claude/skills/factory/SKILL.md`, `.claude/agents/factory-worker.md` and the managed block of `CLAUDE.md`.
- `--scope user` writes the same three files into the user's host home (`~/.claude`, or the folder named by `CLAUDE_CONFIG_DIR`) and creates no project. The skill then works in every project that has a `factory.toml`.
- `--hooks` merges one entry (`factory hook pre-tool`) into `.claude/settings.json` (the user's `settings.json` with `--scope user`). It refuses hand writes to a run's events, artifacts, reviews, contracts and raw payloads. It is optional: the gates already refuse what does not match the evidence.
- Re-running is safe. Unchanged files stay as they are, a changed generated file or settings file is saved first as `<name>.factory-bak-<n>`, and a file without the generated marker is left alone. Entries in the settings that the factory did not write are never touched.
- `--harness agents-md` writes the full prose into a block of `AGENTS.md` for hosts that read it (project scope only).

Then, in the host chat: `/factory <ticket key, request or change URL>`.

## Browser runner (optional)

The runner is a Node project in `browser/` of the repository and is not part of the installed package. From a checkout of the same tag, run the command in `browser/install-command.txt`, then point the project at the runner in `.factory/local.toml`:

```
[checks.browser]
bundled = "<checkout>/browser/src/cli.js"
```

`factory check` warns while a plan needs the runner and it is missing.
