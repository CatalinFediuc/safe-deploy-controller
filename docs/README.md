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
