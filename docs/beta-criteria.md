# Beta criteria

What has to be true before dev.factory leaves alpha. Each criterion is checkable: it names the evidence that closes it. A criterion is `open` until that evidence exists; the beta tag needs all of them `done`.

The alpha promise is "it works on the runs we have done, and anything may still change". The beta promise is different: the formats are stable, a new user can get to a first run from the docs alone, and a run finishes without fixing the factory on the way.

## 1. Stable formats

The files a user or an older run depends on stop changing without notice.

- `factory.toml` (`schema = 1`), `plan.json` (`schema: 1`), the contracts (`ticket/1`, `change/1`, ...) and `events.jsonl` (`"v": 1`) are frozen at their current version. A change that an older file cannot satisfy bumps the version and ships with a migration, or with an error that names the old version and the fix.
- A compatibility test opens runs recorded by the previous release (a fixture folder per release, kept in `tests/`) and checks that `status`, `next`, `log` and the gates still read them.
- A config or plan locked by the previous release does not drift after an upgrade: `plan.config_hash` and the spec hashes are unchanged for an unchanged workspace.

Evidence: the compatibility test in the suite, and one upgrade of a workspace with an open run.

Status: `open`. The versions exist; nothing tests an older run yet.

## 2. A track record on real tickets

The factory is fixed between runs, not during them.

- Five consecutive tickets, at least one feature, one bug and one hotfix, across at least two projects, reach a draft change request with no change to dev.factory while they run.
- Each of them stays within its loop budget without an extra loop granted by the user.
- Friction found in those runs is logged and fixed after them; a friction that forced a factory change mid-run resets the count.

Evidence: `factory metrics --all` over those runs (loops, gate failures, blocked hours) and the release notes that list no mid-run fix.

Status: `open`.

## 3. Feature freeze

From the first beta tag until 1.0.0, a release carries fixes, clearer messages and documentation only. A new command, config key or check type waits for 1.0.0, unless a beta criterion cannot be met without it.

Status: applies from the beta tag.

## 4. Platforms

- The suite passes on Windows and Linux with Python 3.11, 3.12 and 3.13 in CI. Status: `done` since 1.0.0-alpha.2.
- The suite with the real browser runner also runs on a Windows CI runner (today it runs on Ubuntu only). Status: `open`.
- One real run on a Linux machine, end to end, not only the suite. Status: `open`.
- macOS: either supported with a CI job, or documented as unsupported. Status: `open` (decision needed).

## 5. Install and first run from the docs

- On a clean user profile, a person who has never used the factory installs it from a tag, wires a harness and reaches the first gate of a real ticket by following `docs/install.md` and the setup interview alone.
- A troubleshooting page covers the failures seen most: env start and health, login, a port held by another process, a quality `parse` that reads noise, a check that writes its worktree.
- `factory check` names every missing required setting with the command that sets it.

Evidence: a recorded walkthrough on a clean profile, with every place the person got stuck turned into a doc or a message fix.

Status: `open`. The setup interview exists; the quickstart and the troubleshooting page do not.

## 6. Security review

A reviewer who did not write the code reviews, and every finding is fixed or recorded with a reason:

- where secrets live, and that they never reach events, evidence, artifacts, change descriptions or logs (redaction);
- the hook that refuses hand writes to CLI-owned run files, and what an agent can still run;
- the env processes the factory starts, stops and refuses to kill;
- what the adapters receive (secrets only as environment variables).

Evidence: the review report and its fixes.

Status: `open`.

## 7. Versioning and deprecation

- Versions follow semantic versioning from the beta on: `1.0.0-beta.N` tags, PEP 440 package versions (`1.0.0bN`).
- The CHANGELOG has a "Breaking" section per release; anything removed is first deprecated for one release, with a warning that names the replacement.
- Release notes say how to upgrade, including whether `factory init` must run again.

Status: `open`. The release notes of 1.0.0-alpha.2 already follow the format.

## 8. Measured outcomes

The beta has targets, read from `factory metrics --all` over the track-record runs of criterion 2:

- at most one extra loop per ticket on average;
- no gate failure caused by the factory itself (a wrong message, a wrong rule, a crash);
- intake is the only phase that asks the user a question, except for remote writes and loop budget decisions.

Status: `open`.

## Out of scope for the beta

- New integrations beyond the adapters that exist.
- A package index release; installing from a tag stays the supported path.
- Speed work beyond the current budgets (suite 300 s, CLI overhead 1 s).
