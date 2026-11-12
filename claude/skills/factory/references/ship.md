# Ship how-to (main session only)

`R` = `FACTORY_HOME/runs/<run-id>`. Phase is `ship` after `gate review` passes.

1. `git -C <worktree> fetch origin` for every repo of the run.
2. `factory ship plan <run>`. It fails when a branch is behind `origin/<target>`, a dependency MR is not merged (from `R/remote/`), a plan file changed, or the worktree moved after prove (commit, dirty file). Behind: a builder rebases/merges, then `factory run rewind <run> prove "<reason>"` and run prove → review → ship again; or ask the user (`--allow-behind` only with their explicit yes). Never commit, rebase or merge at ship without rewinding: `ship plan`, `ship verify` and `gate ship` re-check the proof against the current HEAD.
3. Read `R/ship-plan.json`: `mrs[]` (repo, target, source/target branch, `head_sha` = the proven commit, title, draft, description, `depends_on`, readback path), `jira_comment` (ADF), and `actions[]` in order, each with `needs_user`.
4. **Gate S** for every action with `needs_user: true`: one chat briefing listing the MRs (repo → target, draft, title, Depends on), ACs × evidence status, `human` checks, the Jira comment and the transition. Wait for an explicit yes. The approval covers only the listed actions.
5. Execute the actions in order:
   - `push`: `git -C <worktree> push -u origin <branch>`. Never force. The pushed tip must be the plan's `head_sha`.
   - `mr`: `gitlab_save_merge_request` (create, or update when an MR already exists for the source → target) with title, description, `draft`, source/target branch, assignee/reviewer from the workspace. Keep the `Draft: ` title prefix when draft: the MCP may ignore the flag. Stacked MRs: fill `Depends on !N` from the readbacks of earlier MRs in the plan.
   - `readback`: `gitlab_get_merge_request` with `save_to=<the action's save_to made absolute: R/remote/mr-<repo>-<target>.json>` and `overwrite: true`. The saved file wraps the MR as `{merge_request: {...}}`; `ship verify` reads both shapes. Never paste or rewrite it.
   - `jira_comment`: `jira_add_comment` with the plan's ADF on each issue. Replace `{mr_url:<repo>:<target>}` with `web_url` from the readbacks. If the epic has a `ship_comment` template, use that instead.
   - `jira_transition`: `jira_get_transitions`, then `jira_transition_issue` to the named transition.
6. `factory ship verify <run>` compares the readbacks with the plan, including the MR head `sha` against the proven `head_sha`; for a missing readback it prints the call (`FETCH gitlab_get_merge_request ... save_to=... overwrite=true`). Fix title/draft mismatches with another `gitlab_save_merge_request` and readback; a head mismatch means unproven commits are on the branch: rewind to prove.
7. `factory gate <run> ship`. Then go to feedback.

Never un-draft, merge, or close an MR without an explicit user request for that MR.

## Hotfix runs

`ship plan` orders MRs base target first (homolog), then mirrors (pack). Descriptions end with `Mirrored to {mr_url:<repo>:<mirror>}` (base MR) and `Mirror of {mr_url:<repo>:<base>}` (mirror MR): after creating both MRs, replace the placeholders with `!N`/URLs in the descriptions (update the first MR once the second exists). Post one Jira comment listing both MRs. Push and create the base MR first.
