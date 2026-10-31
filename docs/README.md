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
