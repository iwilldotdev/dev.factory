# dev.factory v1 (1.0.0-alpha, in progress)

A local CLI that takes one ticket from intake to a draft change request. No step passes on a model's claim: evidence is bound to what was proved, and gates only read it.

Design: `DESIGN.md`. Lessons behind every rule: `LESSONS.md`. Schemas: `docs/schema/`. Parity with v0 and milestones: `docs/parity-map.md`.

## Install

```
python -m pip install .            # Python 3.11 or later, standard library only
factory --version
```

## Try it (M1: project, run, contracts, intake)

```
factory init --harness agents-md   # factory.toml, .factory/, AGENTS.md section
factory check
factory run start ABC-123 --kind feature
factory next                       # prints the one required action; run it, then call next again
```

## Tests

```
python tests/run_tests.py          # parallel, one process per module, output in .tmp/test-output
```
