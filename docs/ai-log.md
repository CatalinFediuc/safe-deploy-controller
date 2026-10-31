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
| AI log | 10:49 to 10:52 | 3m |
| Phase 3 | 10:52 to 11:08 | 16m |
| Phase 3 decision review | 11:08 to 11:37 | 29m |
| Phase 4 | 11:37 to 11:56 | 19m |
| **Total to end of Phase 4** | 07:19 to 11:56 | **4h 37m** |
| Phase 5 review and build | 12:01 to 14:25 | 2h 24m |
| **Total so far** | 07:19 to 14:25 | **7h 6m** |

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

### Phase 3: target and health

- **Asked:** implement Phase 3 from the plan, using the documents under `docs/`.
- **Claude decided, for review:**
  - a corrupt `environment.json` is refused and never overwritten
  - a bad health profile fails when loaded, through a new `HealthProfileError`
  - a running-version mismatch counts as a failed attempt
- **Caught while designing:** the plan's `ok = error_rate <= max` had to stay in that form, because the negated `>` lets NaN read as healthy. An empty `ProbeError` message had the same risk.
- **Verification:** the tests passed first time again, so Claude injected 37 bugs into a throwaway copy, and all 37 were caught. Claude's first harness hung on an injected infinite loop and needed a timeout. Writing the bug list also showed that no test covered atomic writes, so one was added.
- **Process slip:** Claude's first `make ci` check printed no exit code, because `PIPESTATUS` doesn't exist in zsh. It reran the command and read the real exit code rather than trust the output.

### Phase 3 decision review

- **Asked:** walk through each decision Claude made in Phase 3, so the user can decide.
- **Corrupt `environment.json`:** Claude compared four options and recommended refusing the file. The user agreed, and asked that the manual repair be documented so it isn't forgotten. It is now a required Phase 7 item, with a Phase 4 scenario to prove the steps work.
- **Bad health profile:** while explaining this decision, Claude found its own claim was overstated: it had said a typo couldn't look like an unhealthy release, but keys weren't checked. Claude recommended adding key checks and using exit 2, which matches a missing `--health-profile`. The user agreed, and preferred keeping the exit code simple to the audited alternative of exit 3.
- **Running-version mismatch:** Claude recommended keeping a mismatch as a failed attempt, partly because the simulation can't exhibit a slow rollout. The user rejected that and chose a separate rollout wait, because they are preparing to roll this out for production. Claude's recommendation had been judged for a take-home, not for real targets.
- **Bad metric values:** while listing the pros and cons, Claude found that `verify()` didn't refuse negative values, so a real probe returning `-1` would have read as healthy. The user chose the small fix and declined Claude's suggestion to carry a separate "inconclusive" result into Phase 4.

### Phase 4: controller

- **Asked:** after reviewing the docs, implement Phase 4.
- **Decided by the user:** all four items that the plan listed as proposed, asked in one question before any code was written.
- **Claude decided, for review:**
  - probe timeouts and "no data" still mark a version known-bad
  - `in_flight.to_version` and `deployment_key` can be `null`
  - a run that can't finish its rollback raises the new `RecoveryRequired`
- **Caught by reading the draft:** a double save that could have left `STABLE` with a run in flight, and a lost `recover` instruction. Neither had failed a test.
- **Verification:** the first test run gave 100% branch coverage, but Claude's own review still found weak tests, including one that checked nothing. Of 56 injected bugs, 3 survived at first, and each one showed a missing assertion.

### Phase 5: CLI and rendering

- **Asked:** start Phase 5, but first review all the documentation and settle any open points.
- **Claude found, beyond the tracked items:** by trying suspect paths against the merged code, it found that `recover` could remove a running service, that the audit log recorded no outcome or override, that a torn line swallowed the next event, and that SIGHUP left the lock behind. Reading the docs alone had not shown these.
- **Decided by the user:** nine questions in three batches. They followed Claude's recommendation on most, chose the minimal JSON error over one with `run_id`, and chose run IDs with a timestamp over random hex. They kept real adapters, a cross-host lock and durable state as limitations for now, against Claude's recommendation of a new phase, and may take them up after a working demo.
- **Changed from Phase 4:** the user accepted Claude's recommendation to reverse its own Phase 4 decision, so a rollback that can't finish exits 5, not 1.
- **Claude decided, for review:** `--state-dir` is not on `validate`, and a missing `$USER` makes `--actor` required. Claude also put `--actor` on `recover` only, which the user accepted, along with a deploy's audit showing the manifest's `requested_by` rather than who ran it.
- **Caught by reading the tests:** Claude's first CLI tests included an assertion that could never fail. Fixing it showed its expected list was wrong.
- **Verification:** 2 of 55 injected bugs survived at first, and each showed a weak test. Stop signals are tested against a real process.

## Useful for `AI_PROCESS.md`

- Keeping "proposed" separate from "agreed" in the plan stopped AI suggestions from reading as decisions.
- Passing tests were not treated as enough. Injected bugs found a real gap that 100% coverage had missed.
- CI results were confirmed from each job's conclusion, because a pipeline's exit code can hide the real result.
- Trying suspect paths against the real code found problems that a documentation review had missed, including one that could remove a running service.
