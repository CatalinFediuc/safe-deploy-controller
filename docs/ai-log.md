# AI log

Running notes for `AI_PROCESS.md` and the time note (see section 12.2 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)). Add an entry at the end of each phase. [README.md](README.md) has the technical detail of each phase, so this file only records how the AI was used.

## Tools and models

| Used for | Tool | Model |
|---|---|---|
| Implementation plan | AI-generated; tool to be confirmed | to be confirmed |
| Code, tests and docs, from Phase 1 | Claude Code in VS Code | Claude Opus 5.5 |

Phase 0 commits carry no AI attribution, so whether AI helped there is still to be confirmed.

## Time

All on 2026-10-31, local time (UTC+2). These windows come from commit and PR timestamps. They show elapsed time, including any breaks, so treat them as upper bounds and correct them if you tracked the real time.

| Work | Window | Elapsed |
|---|---|---|
| Phases 0 and 1 | 07:19 to 09:10 | 1h 51m |
| Phase 2 | 09:10 to 10:27 | 1h 17m |
| Review and pre-Phase 3 adjustments | 10:27 to 10:43 | 16m |
| Plan update | 10:43 to 10:49 | 6m |
| **Total so far** | 07:19 to 10:49 | **3h 30m** |

The 07:19 start is the repository directory's creation time. Git can't separate Phases 0 and 1.

## Log

### Phases 0 and 1

The prompts for these phases were not logged. Add what you remember. The accepted and rejected decisions below come from [README.md](README.md).

- **Accepted beyond the plan:** stricter validation, including nested unknown keys, duplicate JSON keys, booleans used as integers and unsafe symlinks.
- **Fixed after tests failed:** a misleading cross-field error for an out-of-range `attempts`.
- **Changed:** `ruff` findings led to a `chr()` constant in the test data and a targeted `noqa` for the `PASS` enum value.

### Phase 2: state, lock, audit, clock

- **Asked:** start Phase 2 from the plan, using the Phase 1 notes in `docs/README.md`. Then add the Phase 2 notes and open a PR to merge by hand.
- **Accepted:**
  - two transitions the plan's diagram had missed, both needed by `recover`
  - `Recorder.bind_from_version()`, because the manifest can't say what is running
  - additions beyond the plan, such as strict ledger loading and an owner check on lock release
- **Changed:** Claude's `elif` chain of corrupt-file cases became the `with_value`/`without` helpers already used in `test_manifest.py`.
- **Verification:** besides `make ci` and Python 3.11, Claude injected 14 bugs into a throwaway copy, because the tests had all passed first time. All 14 were caught.

### Review and pre-Phase 3 adjustments

- **Asked:** review Phases 0 to 2 for anything missed from the plan or anything that would make later phases easier.
- **Claude found:**
  - the missing `ai-log.md`
  - a rebuilt artifact being treated as a `NO_OP`
  - `recover` having no health thresholds
  - a `.pyz` that isn't reproducible, confirmed by building it twice
- **Decided by the user:** do the four pre-Phase 3 items now and handle `ai-log.md` as a separate PR.
- **Not yet decided:** Claude's other suggestions are listed as proposed in section 16.3 of the plan.
- **Caught by verification:** two of the 10 injected bugs survived at first, because one of Claude's tests used default thresholds. The test was fixed before the commit.

### Plan update

- **Asked:** update the plan and append what was done, so nothing is lost.
- **Claude's approach:** update the affected sections in place as well as appending, so that later phases follow current contracts. Section 16 holds status, changes and open items.

## Useful for `AI_PROCESS.md`

- Keeping "proposed" separate from "agreed" in the plan stopped AI suggestions from reading as decisions.
- Passing tests were not treated as enough. Injected bugs found a real gap that 100% coverage had missed.
- CI results were confirmed from each job's conclusion, because a pipeline's exit code can hide the real result.
