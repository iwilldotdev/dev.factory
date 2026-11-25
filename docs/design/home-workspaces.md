# Workspaces in the user's home, and the guided entry

Decided with the owner on 2026-11-25. This replaces loose workspace folders across the disk.

## Decisions

1. Everything of a workspace lives in `~/.factory/<workspace>/` (`FACTORY_HOME` overrides `~/.factory`):
   `factory.toml`, `secrets.env`, `scripts/`, `login/`, `runs/<run>/` and the run worktrees `worktrees/<run>/<repo>/`.
   The project repositories are never written to. Deleting a workspace is deleting one folder.
2. Workspace names are unique: the folder name is the name (a slug), and `[project] name` matches it.
3. A repository belongs to a workspace when its git toplevel equals a `[[repo]] path`, or one of its remote URLs equals a
   `[[repo]] remote_url` (normalized: scheme, `git@host:` vs `https://host/`, trailing `.git` and case of the host ignored).
   Both are recorded at setup, so another clone or another machine is recognized by remote.
4. The questions are interactive both in the terminal and in the app. In a TTY the CLI asks with prompts (choices and text);
   without a TTY (an agent) the CLI prints the next questions as structured JSON and takes the answers as arguments, and the
   agent asks them with its question tool (AskUserQuestion), or in writing when it has none. Both paths write the same config.
5. When a run leaves an ambiguity (destination repo, target branch, which env validates, how to log in) the question itself
   offers "only this run" or "save to the workspace"; a saved answer is not asked again.
6. Every setting is optional. A setting left unset is asked again at each run that needs it, and the setup summary says so
   plainly ("asked at every run: ...").

## Entry

`factory` with no command (or `factory start [TICKET]`), in the CLI or through the skill:

- It resolves the workspace of the current directory (decision 3). Order: `--workspace NAME`; a `factory.toml` found by
  climbing from the cwd (a folder workspace, still supported); the workspaces under `~/.factory/` whose repos match the
  current repository. More than one match is an ambiguity: ask which one (and offer to remember it for this path).
- Workspace found: with a ticket key or URL given, start the run; otherwise ask for it.
- No workspace: start the setup interview.

## Setup interview (`factory setup`)

Interactive, every step optional except the workspace name:

1. Workspace: pick an existing one (names listed) or create a new one (unique name).
2. This repository: the detected facts (remotes, toplevel path, default and likely base branches, stacks with their commands,
   CI steps, env files, ports) are shown; the user confirms its name and base branch.
3. Per repository, each optional (set now, or skip: asked at every run): target branches, the env (start command, health,
   base URL, port), test database and seed/reset, the scripts that become standing checks (from the stack and the CI steps,
   with `vars`), login steps for the browser checks (a login spec), other instructions (free text kept for the agent).
4. Other repositories of the workspace: siblings detected next to this one are offered; each added the same way.
5. Integrations: the tracker, SCM and design tools the machine already has (MCP servers, CLIs) are suggested.
6. Summary: what was saved, where (`~/.factory/<ws>/`), which secrets keys to fill in `secrets.env` (never asked in chat),
   and what will be asked at every run.

The model adapts the questions to the repository (a repo with no UI gets no login step; a repo with no database gets no
test-database step).

## Per-run questions with persistence

`factory ask "<question>" [--save-as <dotted.key>]` opens a question that may be saved; `factory answer Q-n --user-said "..."
--save` writes the answer to the workspace config at that key (through `factory config set <dotted.key> <value>`, which is
also available on its own). A saved key stops the question at later runs.

## Multi-repository workspaces

Invoked in any repository of a multi-repo workspace, the run may touch every repo of the workspace that the ticket needs;
`factory run repos` picks them. When the destination or the validation stays ambiguous, the agent asks (decision 5).

## Compatibility

A folder workspace with its own `factory.toml` keeps working (climb from the cwd). Nothing is imported automatically.
