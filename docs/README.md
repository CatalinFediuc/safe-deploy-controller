# Implementation notes

What happened in each phase of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Phase 1: models and manifest validation

Merged to `main` in [PR #1](https://github.com/CatalinFediuc/safe-deploy-controller/pull/1).

### What we built

- `models.py` and `errors.py`: the enums, dataclasses, exit codes and exception types the later phases build on.
- `manifest.py`: `load_manifest()`, which checks a manifest and reports every problem at once. It runs before any lock is taken or state is read, so a bad manifest changes nothing.
- `scripts/gen_examples.py`: regenerates the example artifacts and manifests. Its output is the same on every run.
- `tests/unit/`: tests for each validation rule and for the example manifests.

Nothing calls `load_manifest()` yet. The controller and CLI arrive in Phases 4 and 5.

### Where we went beyond the plan

These gaps could have let a bad manifest through without an error, so validation also rejects:

- unknown keys inside `artifact` and `health_check`, not just at the top level
- duplicate JSON keys
- booleans used as integers, and non-finite numbers such as `1e999`
- trailing newlines and non-ASCII digits in versions and change IDs, and leading zeros in versions
- symlinks that leave the artifact directory, symlink loops and NUL bytes in paths

### Problems along the way

- **Misleading validation error:** an out-of-range `attempts` also produced `3 must not exceed attempts (-1)`. Three tests caught this. We fixed the validator so the cross-field check only runs when `attempts` is valid.
- **Lint rules:** `ruff` flagged the literal Arabic-Indic digits in the test data, so the tests now use a `chr()` constant. It also read the `PASS` enum value as a hardcoded password, which is a false positive and has a targeted `noqa`.
- **`.vscode/launch.json`:** the file lost its trailing newline during the phase. We restored the committed version. We don't know what removed the newline.

### How it was verified

- `make ci` passed locally on Python 3.12, with 148 tests and 100% branch coverage.
- GitHub CI passed on Python 3.11, 3.12 and 3.13.
- The test suite was also run locally on Python 3.11, because symlink loop handling differs there.

### Left for later phases

- Health profiles in `examples/health/` belong to Phase 3.
- `ManifestError` carries only the error list. Phase 4 needs it to carry the parsed service and environment, so rejected runs can be audited with them.
- `make smoke` stays out of `make ci` until the `validate` command exists in Phase 5.

## Phase 2: state, lock, audit, clock

Delivered in [PR #2](https://github.com/CatalinFediuc/safe-deploy-controller/pull/2).

### What we built

- `clock.py`: the `Clock` protocol, `SystemClock`, and the one timestamp format used in state, audit and results.
- `state.py`: `StateStore`, which saves the ledger atomically and refuses corrupt or unknown-schema files without overwriting them. It also holds `FileLock`, `break_lock()` and the `ALLOWED_TRANSITIONS` table.
- `audit.py`: `AuditLog`, an append-only JSONL file with an fsync per event, and `Recorder`, which numbers a run's steps and builds its result.
- `tests/conftest.py`: `FakeClock` and shared fixtures.

Nothing calls these modules yet. The controller arrives in Phase 4.

### Where we went beyond the plan

The plan left these gaps, so we closed them:

- **Two extra transitions:** the state diagram had no path for two things `recover` must do. `STABLE -> ROLLING_BACK` covers drift (T13), and `ROLLING_BACK -> ROLLING_BACK` covers a crash during rollback (T11).
- **`Recorder.bind_from_version()`:** the skeleton's `in_flight()` and the result both need `from_version`, but the manifest can't say what is running. The controller sets it once state is read.

These go further than the plan asked:

- `state.json` must match the schema exactly. An unknown key counts as corrupt, because saving would silently drop it.
- `schema_version` must be the integer `1`. `true` and `1.0` both compare equal to `1`, so they are refused explicitly.
- The state directory is fsynced after each rename, so saved intent survives power loss.
- A failed save removes its temporary file.
- `release()` only removes a lock the run still owns. After a `--break-lock`, the file may belong to another run.
- An empty or unreadable lockfile counts as held by `unknown`, so it blocks runs until someone breaks it.

### How it was verified

- `make ci` passed locally on Python 3.12, with 251 tests and 100% branch coverage.
- GitHub CI passed on Python 3.11, 3.12 and 3.13.
- We put 14 deliberate bugs into a throwaway copy, such as dropping `O_EXCL` or skipping an fsync. The tests caught all 14.
- A concurrency test has 64 attempts race for the lock and checks that exactly one wins.

### Left for later phases

- `in_flight.to_version` must be a string, but a drift `recover` with no last known-good version has no target to record. Phase 4 needs to decide what goes there.
- `Recorder.bind()` only accepts a full `Manifest`. Rejected runs and `recover` both need to bind partial identity in Phase 4.
- A hard kill during an append can leave a partial last line in `audit.jsonl`. The `history` reader in Phase 5 should tolerate this.
- `seq` restarts for each run, as in the plan's examples, so `history` must group events by `run_id`.

## Pre-Phase 3 adjustments

Delivered in [PR #3](https://github.com/CatalinFediuc/safe-deploy-controller/pull/3).

A review of Phases 0 to 2 found these were much cheaper to change before Phase 3 than after it. The plan did not ask for them.

### What we changed

- **`active_artifact_sha256` in the ledger:** the ledger only stored the version, so a rebuilt artifact with the same version number would have been a `NO_OP` under section 4.4 of the plan, with nothing deployed.
- **`health_check` in `InFlight`:** `recover` has no manifest, so it had no thresholds to verify a rollback against. Using the defaults could verify a rollback against looser limits than the deploy that failed.
- **`parse_health_check()` in `manifest.py`:** stored thresholds now pass the same rules as manifest thresholds. A hand-edited `state.json` with a threshold out of range or missing counts as corrupt, so it can't loosen a rollback.
- **`fileio.py`:** `write_atomic()` moved out of `state.py`, so Phase 3's `SimulatedTarget` can write `environment.json` without importing the ledger.
- **Test setup:**
  - `make_manifest()` and a deterministic `run_ids` fixture now live in `tests/conftest.py`.
  - `test_cli.py` moved to `tests/cli/`, matching the plan's layout.
  - mypy now also covers `tests` and `scripts`, so a fake that drifts from its protocol fails the build.

`schema_version` stays at `1`. The `state.json` format changed, but no real state files exist yet.

### Problems along the way

**Weak test:** two of the injected bugs below survived at first. `test_in_flight_carries_the_run_identity` used a default manifest, so a `Recorder` that dropped the manifest's thresholds still compared equal to it. The test now uses non-default thresholds.

### How it was verified

- `make ci` passed locally on Python 3.12, with 265 tests and 100% branch coverage.
- The test suite also passed locally on Python 3.11.
- GitHub CI passed on Python 3.11, 3.12 and 3.13.
- We put 10 deliberate bugs into a throwaway copy. The tests caught all 10 after the fix above.
- We renamed `FakeClock.sleep` in a throwaway copy, and mypy rejected it as no longer matching `Clock`.

### Left for later phases

The new fields exist, but nothing uses them yet. Phase 4 needs to:

- return `BLOCKED` from preflight when the requested version is active with a different sha
- set `active_artifact_sha256` on success and clear it when a first-ever deploy is removed
- keep `in_flight` when an environment moves to `NEEDS_ATTENTION`, so `recover` still has the thresholds
- have `recover` use `in_flight.health_check`, and fall back to the defaults for drift, which has no in-flight record

[Section 16 of the plan](IMPLEMENTATION_PLAN.md#16-progress-and-changes-since-the-original-plan) tracks these and the other open items.

## Phase 3: target and health

Delivered in [PR #6](https://github.com/CatalinFediuc/safe-deploy-controller/pull/6). The decision review after it is in [PR #7](https://github.com/CatalinFediuc/safe-deploy-controller/pull/7).

### What we built

- `target.py`: the `DeploymentTarget` protocol and `SimulatedTarget`, which keeps the running version per environment in `environment.json`.
- `health.py`: the `HealthProbe` protocol, `FixtureHealthProbe`, and `verify()`, the check that decides whether a version is healthy.
- `examples/health/`: `default.json` from section 5.2 of the plan, and `dependency-outage.json`, where every version is unhealthy.
- `tests/unit/test_target.py` and `tests/unit/test_health.py`.

Nothing calls these modules yet. The controller arrives in Phase 4.

### Where we went beyond the plan

The plan left these open, so we decided them:

- **Corrupt `environment.json`:** it raises `StateCorruptError` and is never overwritten, the same as the ledger. `apply` rewrites the whole file, so writing over something it can't read would lose other environments.
- **Bad health profile:** a new `HealthProfileError` is raised when the profile is loaded. Treating a bad entry as "no data" would turn a typo into a failed deploy and a rollback. A review after PR #6 found that keys weren't checked, so `1.6.O` got through, and service and version keys now follow the manifest rules. A valid but wrong key, such as `1.6.1` for `1.6.0`, still can't be detected.
- **Slow rollouts:** `verify()` first waits for the expected version to be running, polling once a second for up to `health_check.rollout_timeout_seconds`, which uses no health attempts. The field defaults to 60 and sits with the other thresholds, so `recover` waits the same way. Timing out fails verification with no probes run. This came out of the decision review after PR #6, because real targets finish rollouts after `apply` returns.
- **Running-version mismatch:** after the rollout, the version is still checked before every probe. A mismatch counts as a failed attempt and skips the probe.
- **Nothing running:** `remove()` deletes the entry, and `running_version: null` counts as corrupt.

These close ways a bad signal could have read as healthy:

- `verify()` only accepts metrics from 0 up to the limit. NaN, infinite and negative values count as unhealthy and are reported as, for example, `error_rate invalid (nan)`. This covers any probe, not just the fixture: the decision review after PR #6 found that a real probe returning `-1` would have read as healthy.
- A `ProbeError` with no message is reported as `probe failed with no detail`. An empty message would otherwise have counted as a healthy probe.
- Profiles refuse negative, non-finite and boolean metrics.

### Behavior to know about

- The verify loop can only stop on a pass or straight after a failed probe, so a failed summary always reads `0/N`.
- A failed summary names the first problem only, matching the plan's example, so a sample over both limits reports `error_rate`.
- A fixture sequence restarts in each process. `recover` on 1.5.1 therefore sees the `timeout` entry again, which needs 4 probes and still passes with the example thresholds of 5 attempts and 3 required.

### How it was verified

- `make ci` passed locally on Python 3.12, with 347 tests and 100% branch coverage.
- The test suite also passed locally on Python 3.11.
- GitHub CI passed on Python 3.11, 3.12 and 3.13.
- We put 37 deliberate bugs into a throwaway copy, such as letting NaN pass or ignoring the running version. The tests caught all 37. One of them looped forever, so the harness counted that timeout as caught.
- Before that run, we added a test that a failed write keeps the old version. Nothing was checking that `SimulatedTarget` writes atomically.
- After the decision review, `make ci` passed locally on Python 3.12 with 374 tests and 100% branch coverage, and the test suite passed on Python 3.11. GitHub CI passed on Python 3.11, 3.12 and 3.13.
- We injected 27 more bugs into the key checks, the rollout wait and the metric checks, and the tests catch 26. One survived at first: a default of 0 would have turned the rollout wait off, so a test now covers the default. The last one changes nothing: replacing `problem is None` with `not problem` has no effect now that a problem is never empty.

### Left for later phases

- `verify()` doesn't catch errors from reading the target. Phase 4 must route them through the unexpected-exception path.
- Phase 5 must build `FixtureHealthProbe` before taking the lock and report `HealthProfileError` as exit 2, the same as a missing `--health-profile`. The refused attempt won't appear in `audit.jsonl`.
- Refusing a corrupt `environment.json` was reviewed and kept. `recover` can't fix that file, so Phase 4 must test the manual repair and Phase 7 must document it in the README. Section 12.1 of the plan has the steps.

[Section 16 of the plan](IMPLEMENTATION_PLAN.md#16-progress-and-changes-since-the-original-plan) tracks these and the other open items.

## Phase 4: controller

Delivered on branch `feat/phase-4-controller`.

### What we built

- `controller.py`: `DeploymentController`, with `deploy()` and `recover()`. It runs validate, preflight, deploy, verify and complete, rolls back automatically, and owns every status change.
- `tests/scenarios/`: T1 to T14, T17, T18, T20 and T21 from section 11 of the plan, plus the full `make demo` sequence against the real example files.
- `tests/conftest.py`: `World`, which gives each scenario a state dir and a controller, and the `RecordingTarget`, `FaultyTarget` and `SimulatedCrash` test doubles.

### Decided before building

The user agreed all four proposed items:

- **Known-bad list:** a successful `--allow-known-bad` deploy removes the mark, and retries don't add duplicates.
- **`recover` path:** `recover` has its own logic and shares only the restore step with a deploy's rollback. It never marks a version known-bad.
- **Measured failures:** a version is marked only if at least one probe reached it. A stalled rollout, failed `apply`, crash or overwritten version still rolls back, without a mark.
- **Probe count:** a passed verification reads `3/3 consecutive healthy after 4 probes`.

### What we decided along the way

- **Timeouts and "no data" still mark a version.** A probe reached it and got no usable answer, so this fails closed, as the plan says. If that blocks a healthy version during a monitoring outage, `--allow-known-bad` gets past it.
- **`in_flight.to_version` and `deployment_key` can be `null`.** The first means `recover` is removing what's running, because there is no known-good version. The second is for drift, which isn't a deployment.
- **`recover` replaces the in-flight record** with its own, keeping the thresholds and the deployment key. A crash during `recover` is therefore recoverable too.
- **`RecoveryRequired`:** if a run can't finish its rollback, for example because the target can't be read, it leaves the in-flight state in place and raises this error, which carries the `recover` instruction.
- **A failed `apply` during rollback is `ROLLBACK_FAILED`, exit 5.** An error raised while verifying the rollback is `RecoveryRequired`, exit 1, as sections 4.1 and 4.5 of the plan say.
- **Refused corrupt files are audited**, so the audit trail shows who tried and why it was refused.

`active_artifact_sha256` needed no clearing logic. It is only set when a deploy completes, so a failed first deploy never sets it, and a rollback keeps the last known-good version's sha.

### Problems along the way

- **Double save:** the first draft saved `STABLE` and then saved again to clear `in_flight`, so a crash between the two would have left a stable environment with a run in flight. `_transition` now clears it in the same write. This was found by reading the draft, and no test would have caught it, because the window is between two saves.
- **Lost instruction:** in the first draft, a failed save while `recover` was giving up would have lost the `recover` instruction. It was also found by reading the draft.
- **Weak tests:** one "recorded artifact" test checked nothing, and a few others were sloppy. Coverage was already 100%, so we rewrote them by hand.

### How it was verified

- `make ci` passed locally on Python 3.12, with 488 tests and 100% branch coverage. The test suite also passed locally on Python 3.11.
- We put 56 deliberate bugs into a throwaway copy, such as skipping the write-ahead save, removing each gate, or ignoring the stored thresholds. Three survived at first and each showed a missing assertion: the `SKIP` result on a `NO_OP`, the deployment key on `recover`, and `updated_at`. The tests now catch all 56.
- The repair steps in section 12.1 of the plan now have scenarios, for repairing the file and for deleting it, including that each environment needs its own `recover` after a delete.

### Left for later phases

- Phase 5 must report `RecoveryRequired` and `StateCorruptError` as exit 1, and handle SIGINT during manifest validation, because the controller only handles it once it holds the lock.
- A service with no traffic can produce NaN, fail verification and be marked known-bad. This is a known limitation for now.

[Section 16 of the plan](IMPLEMENTATION_PLAN.md#16-progress-and-changes-since-the-original-plan) tracks these and the other open items.
