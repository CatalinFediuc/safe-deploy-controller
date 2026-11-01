# AI process

How AI was used to build `deployctl`. This comes from [docs/ai-log.md](docs/ai-log.md), which was kept during the work. Its phase-by-phase notes have more detail.

## Tools and models

| Used for | Tool | Model |
|---|---|---|
| Implementation plan | Claude Cowork | not recorded |
| Phase 0 scaffold | none, done by hand | none |
| Code, tests and docs, from Phase 1 | Claude Code in VS Code | Claude Opus 5.5 |

The plan is in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). It was kept up to date as the code changed, and section 16 records each change and why it was made. Claude-authored commits carry a `Co-Authored-By` line.

## What I asked and how I iterated

I worked one phase at a time from the plan, usually with a short prompt such as "implement Phase 4 after reviewing the docs". The value came from what followed, more than from the first prompt. The prompts for Phases 0 and 1 weren't logged.

- **Phase 2:** build from the plan, add notes, and open a PR for me to merge.
- **Review before Phase 3:** I asked what had been missed from the plan, and what would make later phases easier.
- **Phase 3 review:** I asked Claude to walk through each decision it had made, one at a time, so that I could decide them.
- **Phase 4:** the open proposals were all decided in one question before any code was written.
- **Phase 5:** I asked for a review of the documentation before building. The questions it raised took three batches to settle.
- **Phase 7:** the four remaining proposals, and the facts only I could supply, were asked in one batch before any changes were made.

Proposals stayed marked "proposed" in the plan until I had agreed to them, so an AI suggestion couldn't pass for a decision.

## Accepted, changed and rejected

**Rejected, or decided against Claude's recommendation:**

- Claude recommended treating a running-version mismatch as a failed attempt, partly because the simulation can't show a slow rollout. I chose a separate rollout wait, because I'm preparing this for production. Its recommendation had been judged for an exercise rather than for real targets.
- Claude recommended a new phase for real adapters, a cross-host lock and durable state. I kept these as documented limitations for now.
- I declined a separate "inconclusive" verification result, and chose the small fix for bad metric values instead.
- I chose the minimal JSON error over one with a `run_id`, and run IDs with a timestamp over random hex.

**Changed after review:**

- A bad health profile: while explaining the decision, Claude found its own claim was overstated, because profile keys weren't checked. We added key checks and used exit 2.
- A rollback that can't finish now exits 5, not 1, which reverses a Phase 4 decision of Claude's, because exit 1 wouldn't page anyone.
- Claude's chain of corrupt-file test cases became the helpers already used in `test_manifest.py`.

**Accepted beyond the plan:**

- Stricter manifest validation: nested unknown keys, duplicate JSON keys, booleans used as integers, and unsafe symlinks.
- `recover` refuses an environment with no ledger record. Claude showed that without this, an empty ledger made `recover` remove a running service and report success.
- A deterministic build script in place of `python -m zipapp`.

## Verification

- **Injected bugs.** Tests that passed first time weren't taken as proof. Claude injected deliberate bugs into a throwaway copy, and any survivor was treated as a gap in the tests or as dead code.

| Phase | Bugs injected | Survived at first | What survivors showed |
|---|---|---|---|
| 2 | 14 | 0 | |
| 2a | 10 | 2 | A test used default thresholds |
| 3 | 37 | 0 | Writing the list showed no test covered atomic writes |
| 4 | 56 | 3 | Each was a missing assertion, despite 100% coverage |
| 5 | 55 | 2 | Each was a weak test, despite 100% coverage |
| 6 | 6 | 0 | |
| 7 | 10 | 3 | One exposed a `zipfile` argument with no effect, now removed. Two changed no behavior in the copy tested |

- **Trying the real code.** Running suspect paths against the merged code found problems that a documentation review hadn't: `recover` removing a running service, an audit log with no outcome, a torn line swallowing the next event, and SIGHUP leaving the lock behind.
- **Reading the output.** The demo's test passed while the demo printed a Python `None` and the OS username. Reading it as a published document caught both.
- **Tooling.** `mypy` runs in strict mode over `src`, `tests` and `scripts`. Coverage has a 90% branch gate and sits at 100%. Stop signals are tested against real processes, and CI runs on Python 3.11, 3.12 and 3.13.
- **Results checked directly.** CI was confirmed from each job's conclusion, and local checks from the real exit code, because a pipeline can hide a failure.

## Where the AI went wrong

- It judged trade-offs for a take-home exercise until I said this was headed for production.
- It wrote tests that couldn't fail: a demo assertion that always held, and a test that checked nothing.
- A snippet it gave me stored flags in a shell variable, which zsh doesn't split, and it gave other causes for the result before finding its own mistake. It made the same slip again in Phase 7, in its own verification command.
- A first `make ci` check printed no exit code, because `PIPESTATUS` doesn't exist in zsh. It reran the command rather than trust the output.

## Limitations and risks

I wouldn't ship this to production without more work:

- The lockfile is single-host, with no lease or fencing.
- The ledger and audit log are local files, so they share the host's fate.
- Health is simulated, and how the gates behave against real metrics is unproven, especially with low traffic.
- The audit log is append-only by convention, not tamper-evident, and a deploy's actor is self-declared.

The [README](README.md#known-limitations) has the full list.
