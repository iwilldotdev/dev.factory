# M4 notes: what was decided where the docs left room

M4 delivers the workspace run, `env`, `wait`, `http`, `browser`, `visual`, `style` and `screenshot` checks, login, test data, the design pillar and the bundled browser runner (`docs/parity-map.md` rows 19, 24, 27 to 30, 33 to 36, 48 to 55 and 58, plus the deferred lessons L-12, L-13, L-37 and L-46). Slots, `shareable` and the lease-style queue stay in M6; `env up --wait <s>` queues for the lock already.

Decisions:

- **Spec language.** `login_spec`, `state_spec` and `actions` use one JSON action language (`{"actions": [...]}`, DESIGN section 10), because a login or a state has to hand its page back to the runner. A `browser` check `spec` is either that JSON or a test file run by the browser library; the examples in `plan-json.md` that show `.spec.ts` stay valid for `browser` checks only.
- **`gate plan` and the env.** The plan gate does not start an env. It needs every runtime repo healthy (the user or `next` starts it with `factory env up`) and dry-runs only new or changed runtime checks. A dry run ending in `env`, `setup`, `error` or `timeout` refuses the plan; an assertion failure is allowed, since the feature is not built yet.
- **Binding.** A runtime check restarts a stale env once (`env up --restart` semantics, only a process of the same run), then records `error` kind `stale`. After any `env` result the repo's remaining runtime checks are written as `skipped` records and `verify` exits 3.
- **Design freshness.** `design refresh` re-fetches through `[providers.design_frame]` (and the image adapter when set); without an adapter it needs `--user-said`. A moved version or raw stores the new contract, so the lock drifts and `plan amend` is the only way on. A design contract never invalidates the intake gate; the plan lock covers it.
- **Approx frames.** The ingest refuses an approx frame over an exact frame of the same id. Tolerances follow the frame's fidelity: exact uses `limits.tolerance_px` and delta E 0, approx uses `limits.approx`.
- **Screenshots.** `factory reproduce` also captures before images of `screenshot` checks in feature runs. The ship body lists screenshot file names (never links) and approx frames with the user's words.
- **Retry.** `cmd`, `http`, `browser`, `visual` and `style` retry an assertion failure once; a retry that passes is `flaky`.
- **Batch.** Two or more runner-based checks of one repo run in one `batch` process unless `[checks.browser] batch = false` or the project's own `runner` handles `browser` checks.
- **Ports.** Health and "is anything listening" use a bind probe, because connecting to a closed loopback port can take a second on Windows. `env up` still names the owner of a busy port (`netstat` or `ss`/`lsof`) and never stops it.
- **UAT.** After verify passes, `next` lists the screenshot and visual images and proposes asking the user; the answer is `factory human --kind answer "uat: <words>"`, which silences the hint. It never blocks.
