import json
from dataclasses import asdict
from typing import Any

from deployctl.audit import AuditHistory
from deployctl.models import DeploymentResult, EnvironmentState


def _show(value: object) -> str:
    """One printable line: rejected manifests put declared, unchecked text in front of us."""
    text = "-" if value is None else str(value)
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def to_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2)


def result_document(result: DeploymentResult) -> dict[str, Any]:
    return asdict(result) | {"exit_code": int(result.exit_code)}


def result_text(result: DeploymentResult) -> str:
    environment = f"{_show(result.service)}/{_show(result.environment)}"
    versions = f"{_show(result.from_version)} -> {_show(result.to_version)}"
    identity = f"change {_show(result.change_id)}  by {_show(result.actor)}"
    lines = [
        f"deployctl run {result.run_id}  key {_show(result.deployment_key)}",
        f"{environment}  {versions}  {identity}",
        "",
        f"  {'#':>2}  {'time (UTC)':<12}  {'stage':<15}  {'result':<6}  detail",
    ]
    lines += [
        f"  {step.seq:>2}  {step.ts[11:23]:<12}  {step.stage:<15}  {step.result:<6}  "
        f"{_show(step.message)}"
        for step in result.steps
    ]
    final = result.final_state
    env_after = "not read"
    if final is not None:
        env_after = f"{final.status} @ {final.active_version or 'nothing'}"
    lines += [
        "",
        f"OUTCOME {result.outcome}   rollback {result.rollback_status}   "
        f"env {env_after}   exit {int(result.exit_code)}",
    ]
    if result.next_action is not None:
        lines.append(f"NEXT    {_show(result.next_action)}")
    return "\n".join(lines)


def status_document(state: EnvironmentState, running: str | None) -> dict[str, Any]:
    # Only a settled environment can drift: during a run the target is meant to differ.
    drift = state.status == "STABLE" and running != state.active_version
    return asdict(state) | {"running_version": running, "drift": drift}


def status_text(document: dict[str, Any]) -> str:
    in_flight = document["in_flight"]
    rows: list[tuple[str, object]] = [
        ("running", document["running_version"]),
        ("active", document["active_version"]),
        ("known-good", document["last_known_good"]),
        ("known-bad", ", ".join(document["failed_versions"]) or None),
        ("drift", "yes" if document["drift"] else "no"),
        ("in flight", in_flight and f"{in_flight['run_id']} at {in_flight['stage']}"),
        ("updated", document["updated_at"]),
        ("last run", document["last_run_id"]),
    ]
    heading = f"{document['service']}/{document['environment']}  {document['status']}"
    return "\n".join([heading, *(f"  {name:<11} {_show(value)}" for name, value in rows)])


def history_document(service: str, environment: str, history: AuditHistory) -> dict[str, Any]:
    return {
        "service": service,
        "environment": environment,
        "runs": history.runs,
        "skipped_lines": history.skipped_lines,
    }


def history_text(service: str, environment: str, history: AuditHistory) -> str:
    title = f"{_show(service)}/{_show(environment)}"
    if not history.runs:
        return f"no recorded runs for {title}"
    rows = [
        ("started (UTC)", "run", "versions", "change", "actor", "outcome"),
        *(
            (
                _show(run["started_at"]),
                _show(run["run_id"]),
                f"{_show(run['from_version'])} -> {_show(run['to_version'])}",
                _show(run["change_id"]),
                _show(run["actor"]),
                _show(run["outcome"] or "no outcome recorded"),
            )
            for run in history.runs
        ),
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    lines = [
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths, strict=False)), row[5]])
        for row in rows
    ]
    count = len(history.runs)
    heading = f"{title}: latest {count} run{'' if count == 1 else 's'}, newest first"
    return "\n".join([heading, "", *lines])
