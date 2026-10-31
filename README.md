# safe-deploy-controller

`deployctl` is a command-line deployment controller. It takes a manifest for a versioned artifact, validates it, deploys it, and checks its health. If the health check fails, it rolls back to the last known-good version and checks that too.

Every run ends with the environment in a known state, and leaves an audit trail and a machine-readable result. A failed run, a retry, a crash or a concurrent run can't leave it ambiguous.

The target and the health checks are simulated with local files, so nothing real is deployed.

## Quick start

You need Python 3.11 or newer.

```bash
make install demo
```

`make demo` runs the scenario below against a throwaway state directory, `.demo-state/`.

To try it by hand:

```bash
source .venv/bin/activate

deployctl deploy --manifest examples/manifests/orders-api-1.4.2.json \
  --artifact-dir examples/artifacts --health-profile examples/health/default.json

deployctl status --service orders-api --env staging
deployctl history --service orders-api --env staging
```

## Example output

`make demo` runs each of these against the same environment, in order. All the files are in [examples/](examples/).

| Step | Scenario | Outcome | Exit |
|---|---|---|---|
| 1 | First deploy of 1.4.2 | `SUCCEEDED` | 0 |
| 2 | The same manifest again | `NO_OP` | 0 |
| 3 | 1.5.0, which is unhealthy | `ROLLED_BACK` to 1.4.2 | 4 |
| 4 | 1.5.0 again | `BLOCKED`, known-bad | 6 |
| 5 | 1.5.1, flaky first and then healthy | `SUCCEEDED` | 0 |
| 6 | Manifest with several mistakes | `REJECTED`, listing all of them | 3 |
| 7 | Wrong checksum | `REJECTED` | 3 |
| 8 | Artifact path outside the artifact directory | `REJECTED` | 3 |
| 9 | 1.6.0 during a dependency outage | `ROLLBACK_FAILED`, `NEEDS_ATTENTION` | 5 |
| 10 | Any deploy while `NEEDS_ATTENTION` | `BLOCKED` | 6 |
| 11 | `recover`, once the outage is over | `RECOVERED` at 1.5.1 | 0 |

The demo ends with `status` and `history`. Step 6 isn't in that `history`, because its manifest declares the environment `qa`.

<details>
<summary>Full <code>make demo</code> output</summary>

```text
== 1. first deploy 1.4.2 (expect SUCCEEDED)
deployctl run r-20261031T124831-dbbfd1  key 9e326b3b733c
orders-api/staging  - -> 1.4.2  change CHG-1041  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:31.313  validate         PASS    manifest valid; sha256 verified
   2  12:48:31.314  preflight        PASS    STABLE @ nothing; lock acquired
   3  12:48:31.314  deploy           PASS    running version -> 1.4.2
   4  12:48:31.518  verify           PASS    3/3 consecutive healthy after 3 probes
   5  12:48:31.521  complete         PASS    1.4.2 is now known-good

OUTCOME SUCCEEDED   rollback NOT_NEEDED   env STABLE @ 1.4.2   exit 0
exit=0
== 2. same manifest again (expect NO_OP)
deployctl run r-20261031T124831-78e85b  key 9e326b3b733c
orders-api/staging  1.4.2 -> 1.4.2  change CHG-1041  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:31.611  validate         PASS    manifest valid; sha256 verified
   2  12:48:31.612  preflight        SKIP    1.4.2 already active

OUTCOME NO_OP   rollback NOT_NEEDED   env STABLE @ 1.4.2   exit 0
exit=0
== 3. unhealthy 1.5.0 (expect ROLLED_BACK to 1.4.2)
deployctl run r-20261031T124831-5eec78  key 08618223315f
orders-api/staging  1.4.2 -> 1.5.0  change CHG-1042  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:31.680  validate         PASS    manifest valid; sha256 verified
   2  12:48:31.681  preflight        PASS    STABLE @ 1.4.2; lock acquired
   3  12:48:31.681  deploy           PASS    running version -> 1.5.0
   4  12:48:31.893  verify           FAIL    0/3 consecutive healthy after 3 probes (error_rate 0.120 > 0.010)
   5  12:48:31.897  rollback         PASS    running version -> 1.4.2
   6  12:48:32.108  verify_rollback  PASS    3/3 consecutive healthy after 3 probes

OUTCOME ROLLED_BACK   rollback SUCCEEDED   env STABLE @ 1.4.2   exit 4
NEXT    1.5.0 is marked known-bad for orders-api/staging; investigate before retrying with --allow-known-bad
exit=4
== 4. retry 1.5.0 (expect BLOCKED known-bad)
deployctl run r-20261031T124832-2f57bd  key 08618223315f
orders-api/staging  1.4.2 -> 1.5.0  change CHG-1042  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:32.207  validate         PASS    manifest valid; sha256 verified
   2  12:48:32.208  preflight        FAIL    1.5.0 previously failed verification here

OUTCOME BLOCKED   rollback NOT_NEEDED   env STABLE @ 1.4.2   exit 6
NEXT    investigate, then retry with --allow-known-bad
exit=6
== 5. flaky-then-healthy 1.5.1 (expect SUCCEEDED)
deployctl run r-20261031T124832-2e12ff  key f40fb9ddf4c9
orders-api/staging  1.4.2 -> 1.5.1  change CHG-1043  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:32.275  validate         PASS    manifest valid; sha256 verified
   2  12:48:32.295  preflight        PASS    STABLE @ 1.4.2; lock acquired
   3  12:48:32.296  deploy           PASS    running version -> 1.5.1
   4  12:48:32.612  verify           PASS    3/3 consecutive healthy after 4 probes
   5  12:48:32.617  complete         PASS    1.5.1 is now known-good

OUTCOME SUCCEEDED   rollback NOT_NEEDED   env STABLE @ 1.5.1   exit 0
exit=0
== 6. invalid manifest (expect REJECTED)
deployctl run r-20261031T124832-d6e4ac  key -
orders-api/qa  - -> -  change -  by -

   #  time (UTC)    stage            result  detail
   1  12:48:32.712  validate         FAIL    manifest: unknown key 'verison'; environment: 'qa' must be one of dev, staging, prod; version: '1.5' must be MAJOR.MINOR.PATCH; artifact.sha256: required; change_id: '1042' must be CHG-<3+ digits>; requested_by: required

OUTCOME REJECTED   rollback NOT_NEEDED   env not read   exit 3
NEXT    fix the manifest; nothing changed
exit=3
== 7. checksum mismatch (expect REJECTED)
deployctl run r-20261031T124832-103197  key -
orders-api/staging  - -> -  change -  by -

   #  time (UTC)    stage            result  detail
   1  12:48:32.780  validate         FAIL    artifact.sha256: mismatch, declared 0000000000000000000000000000000000000000000000000000000000000000, file has 2b0e7c540710f0de3e8c6a1b091fb21dfcb9874c149ae9fb43c9ba2b0b47b2d5

OUTCOME REJECTED   rollback NOT_NEEDED   env not read   exit 3
NEXT    fix the manifest; nothing changed
exit=3
== 8. path traversal (expect REJECTED)
deployctl run r-20261031T124832-d155f9  key -
orders-api/staging  - -> -  change -  by -

   #  time (UTC)    stage            result  detail
   1  12:48:32.845  validate         FAIL    artifact.path: '../../etc/hosts' escapes the artifact directory

OUTCOME REJECTED   rollback NOT_NEEDED   env not read   exit 3
NEXT    fix the manifest; nothing changed
exit=3
== 9. 1.6.0 during dependency outage (expect ROLLBACK_FAILED / NEEDS_ATTENTION)
deployctl run r-20261031T124832-23e38a  key 8dfd3deddaf0
orders-api/staging  1.5.1 -> 1.6.0  change CHG-1044  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:32.912  validate         PASS    manifest valid; sha256 verified
   2  12:48:32.913  preflight        PASS    STABLE @ 1.5.1; lock acquired
   3  12:48:32.914  deploy           PASS    running version -> 1.6.0
   4  12:48:33.122  verify           FAIL    0/3 consecutive healthy after 3 probes (error_rate 0.450 > 0.010)
   5  12:48:33.125  rollback         PASS    running version -> 1.5.1
   6  12:48:33.337  verify_rollback  FAIL    0/3 consecutive healthy after 3 probes (error_rate 0.450 > 0.010)

OUTCOME ROLLBACK_FAILED   rollback FAILED   env NEEDS_ATTENTION @ 1.5.1   exit 5
NEXT    page a human; once the cause is fixed, run `deployctl recover`
exit=5
== 10. deploy while NEEDS_ATTENTION (expect BLOCKED)
deployctl run r-20261031T124833-e67fe9  key f40fb9ddf4c9
orders-api/staging  1.5.1 -> 1.5.1  change CHG-1043  by catalin

   #  time (UTC)    stage            result  detail
   1  12:48:33.420  validate         PASS    manifest valid; sha256 verified
   2  12:48:33.421  preflight        FAIL    orders-api/staging is NEEDS_ATTENTION: run r-20261031T124832-23e38a at stage verify_rollback

OUTCOME BLOCKED   rollback NOT_NEEDED   env NEEDS_ATTENTION @ 1.5.1   exit 6
NEXT    run `deployctl recover`
exit=6
== 11. operator recovers after outage (expect RECOVERED @ 1.5.1)
deployctl run r-20261031T124833-d26dac  key 8dfd3deddaf0
orders-api/staging  1.5.1 -> 1.5.1  change -  by oncall

   #  time (UTC)    stage            result  detail
   1  12:48:33.489  recover          PASS    NEEDS_ATTENTION, running 1.5.1, restoring 1.5.1; reason: outage over
   2  12:48:33.490  rollback         PASS    running version -> 1.5.1
   3  12:48:33.805  verify_rollback  PASS    3/3 consecutive healthy after 4 probes

OUTCOME RECOVERED   rollback SUCCEEDED   env STABLE @ 1.5.1   exit 0
exit=0
orders-api/staging  STABLE
  running     1.5.1
  active      1.5.1
  known-good  1.5.1
  known-bad   1.5.0, 1.6.0
  drift       no
  in flight   -
  updated     2026-10-31T12:48:33.806Z
  last run    r-20261031T124833-d26dac
orders-api/staging: latest 10 runs, newest first

started (UTC)             run                       versions        change    actor    outcome
2026-10-31T12:48:33.489Z  r-20261031T124833-d26dac  1.5.1 -> 1.5.1  -         oncall   RECOVERED
2026-10-31T12:48:33.420Z  r-20261031T124833-e67fe9  1.5.1 -> 1.5.1  CHG-1043  catalin  BLOCKED
2026-10-31T12:48:32.912Z  r-20261031T124832-23e38a  1.5.1 -> 1.6.0  CHG-1044  catalin  ROLLBACK_FAILED
2026-10-31T12:48:32.845Z  r-20261031T124832-d155f9  - -> -          -         -        REJECTED
2026-10-31T12:48:32.780Z  r-20261031T124832-103197  - -> -          -         -        REJECTED
2026-10-31T12:48:32.275Z  r-20261031T124832-2e12ff  1.4.2 -> 1.5.1  CHG-1043  catalin  SUCCEEDED
2026-10-31T12:48:32.207Z  r-20261031T124832-2f57bd  1.4.2 -> 1.5.0  CHG-1042  catalin  BLOCKED
2026-10-31T12:48:31.680Z  r-20261031T124831-5eec78  1.4.2 -> 1.5.0  CHG-1042  catalin  ROLLED_BACK
2026-10-31T12:48:31.611Z  r-20261031T124831-78e85b  1.4.2 -> 1.4.2  CHG-1041  catalin  NO_OP
2026-10-31T12:48:31.313Z  r-20261031T124831-dbbfd1  - -> 1.4.2      CHG-1041  catalin  SUCCEEDED
```

</details>

### JSON result

With `--output json`, stdout carries only JSON. This is step 3 again, from a fresh state directory:

```bash
deployctl deploy --manifest examples/manifests/orders-api-1.5.0.json \
  --artifact-dir examples/artifacts --health-profile examples/health/default.json \
  --output json
```

```json
{
  "run_id": "r-20261031T124856-6f7ee1",
  "outcome": "ROLLED_BACK",
  "rollback_status": "SUCCEEDED",
  "deployment_key": "08618223315f",
  "service": "orders-api",
  "environment": "staging",
  "change_id": "CHG-1042",
  "actor": "catalin",
  "from_version": "1.4.2",
  "to_version": "1.5.0",
  "final_state": {
    "status": "STABLE",
    "active_version": "1.4.2"
  },
  "next_action": "1.5.0 is marked known-bad for orders-api/staging; investigate before retrying with --allow-known-bad",
  "steps": [
    {
      "seq": 1,
      "ts": "2026-10-31T12:48:56.508Z",
      "stage": "validate",
      "result": "PASS",
      "message": "manifest valid; sha256 verified"
    },
    {
      "seq": 2,
      "ts": "2026-10-31T12:48:56.509Z",
      "stage": "preflight",
      "result": "PASS",
      "message": "STABLE @ 1.4.2; lock acquired"
    },
    {
      "seq": 3,
      "ts": "2026-10-31T12:48:56.510Z",
      "stage": "deploy",
      "result": "PASS",
      "message": "running version -> 1.5.0"
    },
    {
      "seq": 4,
      "ts": "2026-10-31T12:48:56.720Z",
      "stage": "verify",
      "result": "FAIL",
      "message": "0/3 consecutive healthy after 3 probes (error_rate 0.120 > 0.010)"
    },
    {
      "seq": 5,
      "ts": "2026-10-31T12:48:56.724Z",
      "stage": "rollback",
      "result": "PASS",
      "message": "running version -> 1.4.2"
    },
    {
      "seq": 6,
      "ts": "2026-10-31T12:48:56.938Z",
      "stage": "verify_rollback",
      "result": "PASS",
      "message": "3/3 consecutive healthy after 3 probes"
    }
  ],
  "exit_code": 4
}
```
