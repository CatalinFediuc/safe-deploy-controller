"""Regenerate example artifacts and manifests; output is deterministic across re-runs."""

import hashlib
import json
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ARTIFACTS = EXAMPLES / "artifacts"
MANIFESTS = EXAMPLES / "manifests"

SERVICE = "orders-api"
CHANGE_IDS = {"1.4.2": "CHG-1041", "1.5.0": "CHG-1042", "1.5.1": "CHG-1043", "1.6.0": "CHG-1044"}
WRONG_SHA256 = "0" * 64
HEALTH_CHECK = {
    "attempts": 5,
    "required_consecutive": 3,
    "interval_seconds": 0.1,
    "timeout_seconds": 2,
    "max_error_rate": 0.01,
    "max_p99_latency_ms": 500,
}


def write_artifact(version: str) -> tuple[str, str]:
    name = f"{SERVICE}-{version}.bin"
    content = f"{SERVICE} {version} simulated artifact\n".encode()
    (ARTIFACTS / name).write_bytes(content)
    return name, hashlib.sha256(content).hexdigest()


def manifest(version: str, path: str, sha256: str) -> dict[str, Any]:
    return {
        "service": SERVICE,
        "environment": "staging",
        "version": version,
        "artifact": {"path": path, "sha256": sha256},
        "change_id": CHANGE_IDS[version],
        "requested_by": "catalin",
        "health_check": HEALTH_CHECK,
    }


def write_manifest(name: str, document: dict[str, Any]) -> None:
    (MANIFESTS / f"{name}.json").write_text(json.dumps(document, indent=2) + "\n")


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    MANIFESTS.mkdir(parents=True, exist_ok=True)

    artifacts = {version: write_artifact(version) for version in CHANGE_IDS}
    for version, (path, sha256) in artifacts.items():
        write_manifest(f"{SERVICE}-{version}", manifest(version, path, sha256))

    baseline_path, _ = artifacts["1.4.2"]
    write_manifest("checksum-mismatch", manifest("1.4.2", baseline_path, WRONG_SHA256))
    write_manifest("path-traversal", manifest("1.4.2", "../../etc/hosts", WRONG_SHA256))
    write_manifest(
        "invalid-schema",
        {
            "service": SERVICE,
            "environment": "qa",
            "version": "1.5",
            "verison": "1.5.0",
            "artifact": {"path": baseline_path},
            "change_id": "1042",
        },
    )


if __name__ == "__main__":
    main()
