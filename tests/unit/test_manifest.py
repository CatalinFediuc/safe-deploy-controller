import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deployctl.errors import ManifestError
from deployctl.manifest import load_manifest
from deployctl.models import HealthCheckSpec, Manifest

ARTIFACT_NAME = "orders-api-1.5.0.bin"
ARTIFACT_BYTES = b"orders-api 1.5.0\n"
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
EXAMPLES = Path(__file__).parents[2] / "examples"
NON_ASCII_ONE = chr(0x0661)  # matched by `\d`, but not a digit a version may contain

Load = Callable[[object], Manifest]


def valid_document() -> dict[str, Any]:
    return {
        "service": "orders-api",
        "environment": "staging",
        "version": "1.5.0",
        "artifact": {"path": ARTIFACT_NAME, "sha256": ARTIFACT_SHA256},
        "change_id": "CHG-1042",
        "requested_by": "catalin",
        "health_check": {
            "attempts": 5,
            "required_consecutive": 3,
            "interval_seconds": 0.1,
            "timeout_seconds": 2,
            "max_error_rate": 0.01,
            "max_p99_latency_ms": 500,
            "rollout_timeout_seconds": 30,
        },
    }


def with_value(field: str, value: object) -> dict[str, Any]:
    document = valid_document()
    *parents, leaf = field.split(".")
    section = document
    for parent in parents:
        section = section[parent]
    section[leaf] = value
    return document


def without(field: str) -> dict[str, Any]:
    document = valid_document()
    *parents, leaf = field.split(".")
    section = document
    for parent in parents:
        section = section[parent]
    del section[leaf]
    return document


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "artifacts"
    directory.mkdir()
    (directory / ARTIFACT_NAME).write_bytes(ARTIFACT_BYTES)
    return directory


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


@pytest.fixture
def load(manifest_path: Path, artifact_dir: Path) -> Load:
    def _load(document: object) -> Manifest:
        manifest_path.write_text(json.dumps(document))
        return load_manifest(manifest_path, artifact_dir)

    return _load


def errors_for(load: Load, document: object) -> list[str]:
    with pytest.raises(ManifestError) as caught:
        load(document)
    return caught.value.errors


def raw_errors(manifest_path: Path, artifact_dir: Path, content: str) -> list[str]:
    manifest_path.write_text(content)
    with pytest.raises(ManifestError) as caught:
        load_manifest(manifest_path, artifact_dir)
    return caught.value.errors


def test_valid_manifest_loads(load: Load, artifact_dir: Path) -> None:
    manifest = load(valid_document())

    assert manifest.service == "orders-api"
    assert manifest.environment == "staging"
    assert manifest.version == "1.5.0"
    assert manifest.artifact_path == (artifact_dir / ARTIFACT_NAME).resolve()
    assert manifest.artifact_sha256 == ARTIFACT_SHA256
    assert manifest.change_id == "CHG-1042"
    assert manifest.requested_by == "catalin"
    assert manifest.health_check == HealthCheckSpec(
        attempts=5,
        required_consecutive=3,
        interval_seconds=0.1,
        timeout_seconds=2.0,
        max_error_rate=0.01,
        max_p99_latency_ms=500.0,
        rollout_timeout_seconds=30.0,
    )


def test_integer_thresholds_become_floats(load: Load) -> None:
    spec = load(valid_document()).health_check
    assert isinstance(spec.timeout_seconds, float)
    assert isinstance(spec.max_p99_latency_ms, float)
    assert isinstance(spec.rollout_timeout_seconds, float)


def test_health_check_is_optional(load: Load) -> None:
    assert load(without("health_check")).health_check == HealthCheckSpec()


def test_partial_health_check_keeps_defaults_for_the_rest(load: Load) -> None:
    document = valid_document()
    document["health_check"] = {"max_error_rate": 0.05}
    assert load(document).health_check == HealthCheckSpec(max_error_rate=0.05)


def test_partial_health_check_is_checked_against_defaults(load: Load) -> None:
    document = valid_document()
    document["health_check"] = {"attempts": 2}  # the default required_consecutive is 3
    errors = errors_for(load, document)
    assert errors == ["health_check.required_consecutive: 3 must not exceed attempts (2)"]


def test_missing_manifest_file_is_rejected(tmp_path: Path, artifact_dir: Path) -> None:
    with pytest.raises(ManifestError) as caught:
        load_manifest(tmp_path / "absent.json", artifact_dir)
    assert "cannot read" in caught.value.errors[0]


def test_non_utf8_manifest_is_rejected(manifest_path: Path, artifact_dir: Path) -> None:
    manifest_path.write_bytes(b'{"service": "\xff"}')
    with pytest.raises(ManifestError) as caught:
        load_manifest(manifest_path, artifact_dir)
    assert "cannot read" in caught.value.errors[0]


@pytest.mark.parametrize("content", ["", "{", "{'service': 'x'}", "[" * 200_000])
def test_malformed_json_is_rejected(manifest_path: Path, artifact_dir: Path, content: str) -> None:
    errors = raw_errors(manifest_path, artifact_dir, content)
    assert len(errors) == 1
    assert errors[0].startswith("manifest: invalid JSON")


@pytest.mark.parametrize("document", [[], ["service"], "orders-api", 42, None, True])
def test_non_object_is_rejected(load: Load, document: object) -> None:
    assert errors_for(load, document) == ["manifest: top level must be a JSON object"]


@pytest.mark.parametrize(
    "field",
    [
        "service",
        "environment",
        "version",
        "artifact",
        "artifact.path",
        "artifact.sha256",
        "change_id",
        "requested_by",
    ],
)
def test_missing_required_field_is_named(load: Load, field: str) -> None:
    assert errors_for(load, without(field)) == [f"{field}: required"]


def test_all_errors_are_reported_together(load: Load) -> None:
    document = {
        "service": "Orders_API",
        "environment": "qa",
        "version": "1.5",
        "verison": "1.5.0",
        "artifact": {"path": "../outside.bin", "sha256": "ABC"},
        "change_id": "1042",
        "health_check": {"attempts": 0, "max_error_rate": 2},
    }

    fields = [error.split(":")[0] for error in errors_for(load, document)]

    assert fields == [
        "manifest",  # unknown key
        "service",
        "environment",
        "version",
        "artifact.sha256",
        "artifact.path",
        "change_id",
        "requested_by",
        "health_check.attempts",
        "health_check.max_error_rate",
    ]


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        (None, "manifest: unknown key 'verison'"),
        ("artifact", "artifact: unknown key 'verison'"),
        ("health_check", "health_check: unknown key 'verison'"),
    ],
)
def test_unknown_keys_are_rejected(load: Load, section: str | None, expected: str) -> None:
    document = valid_document()
    (document[section] if section else document)["verison"] = "1.5.0"
    assert errors_for(load, document) == [expected]


def test_duplicate_keys_are_rejected(manifest_path: Path, artifact_dir: Path) -> None:
    content = json.dumps(valid_document()).replace(
        '"version": "1.5.0"', '"version": "9.9.9", "version": "1.5.0"'
    )
    errors = raw_errors(manifest_path, artifact_dir, content)
    assert errors == ["manifest: duplicate key 'version'"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service", 42),
        ("service", "o"),
        ("service", "1-orders"),
        ("service", "Orders-API"),
        ("service", "orders_api"),
        ("service", "orders/api"),
        ("service", "a" * 42),
        ("service", "orders-api\n"),
        ("environment", 42),
        ("environment", "qa"),
        ("environment", "PROD"),
        ("environment", "staging\n"),
        ("version", 1.5),
        ("version", "1.5"),
        ("version", "1.5.0.1"),
        ("version", "v1.5.0"),
        ("version", "1.5.0-rc1"),
        ("version", "01.5.0"),
        ("version", "1.5.0\n"),
        ("version", f"{NON_ASCII_ONE}.5.0"),
        ("change_id", 1042),
        ("change_id", "1042"),
        ("change_id", "CHG-42"),
        ("change_id", "chg-1042"),
        ("change_id", f"CHG-104{NON_ASCII_ONE}"),
        ("change_id", "CHG-1042\n"),
        ("requested_by", None),
        ("requested_by", ""),
        ("requested_by", "   "),
        ("artifact", None),
        ("artifact", "orders-api-1.5.0.bin"),
        ("artifact.sha256", 42),
        ("artifact.sha256", "abc123"),
        ("artifact.sha256", ARTIFACT_SHA256.upper()),
        ("artifact.sha256", ARTIFACT_SHA256 + "\n"),
        ("artifact.path", 42),
        ("artifact.path", ""),
        ("health_check", None),
        ("health_check", []),
    ],
)
def test_invalid_field_is_named(load: Load, field: str, value: object) -> None:
    errors = errors_for(load, with_value(field, value))
    assert len(errors) == 1
    assert errors[0].startswith(f"{field}: ")


@pytest.mark.parametrize("version", ["0.0.0", "1.5.0", "10.20.30"])
def test_strict_semver_is_accepted(load: Load, version: str) -> None:
    assert load(with_value("version", version)).version == version


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_known_environments_are_accepted(load: Load, environment: str) -> None:
    assert load(with_value("environment", environment)).environment == environment


@pytest.mark.parametrize("service", ["ab", "orders-api", "a" * 41])
def test_service_name_length_limits(load: Load, service: str) -> None:
    assert load(with_value("service", service)).service == service


def test_absent_artifact_is_rejected(load: Load) -> None:
    errors = errors_for(load, with_value("artifact.path", "absent.bin"))
    assert errors == ["artifact.path: 'absent.bin' not found in the artifact directory"]


def test_directory_is_not_an_artifact(load: Load, artifact_dir: Path) -> None:
    (artifact_dir / "nested").mkdir()
    errors = errors_for(load, with_value("artifact.path", "nested"))
    assert errors == ["artifact.path: 'nested' not found in the artifact directory"]


@pytest.mark.parametrize("path", ["../outside.bin", "../../etc/hosts", "nested/../../outside.bin"])
def test_traversal_is_rejected_even_when_the_target_exists(
    load: Load, artifact_dir: Path, path: str
) -> None:
    (artifact_dir.parent / "outside.bin").write_bytes(ARTIFACT_BYTES)
    (artifact_dir / "nested").mkdir()
    errors = errors_for(load, with_value("artifact.path", path))
    assert errors == [f"artifact.path: {path!r} escapes the artifact directory"]


def test_absolute_path_is_rejected_even_inside_the_directory(
    load: Load, artifact_dir: Path
) -> None:
    absolute = str((artifact_dir / ARTIFACT_NAME).resolve())
    errors = errors_for(load, with_value("artifact.path", absolute))
    assert len(errors) == 1
    assert "must be relative" in errors[0]


def test_symlink_escaping_the_directory_is_rejected(load: Load, artifact_dir: Path) -> None:
    outside = artifact_dir.parent / "outside.bin"
    outside.write_bytes(ARTIFACT_BYTES)
    (artifact_dir / "link.bin").symlink_to(outside)
    errors = errors_for(load, with_value("artifact.path", "link.bin"))
    assert errors == ["artifact.path: 'link.bin' escapes the artifact directory"]


def test_symlink_loop_is_rejected(load: Load, artifact_dir: Path) -> None:
    (artifact_dir / "loop.bin").symlink_to(artifact_dir / "loop.bin")
    errors = errors_for(load, with_value("artifact.path", "loop.bin"))
    assert len(errors) == 1
    assert errors[0].startswith("artifact.path: ")


def test_nul_byte_in_path_is_rejected(load: Load) -> None:
    errors = errors_for(load, with_value("artifact.path", "orders\x00.bin"))
    assert len(errors) == 1
    assert errors[0].startswith("artifact.path: ")


def test_path_stays_valid_when_it_resolves_inside_the_directory(load: Load) -> None:
    manifest = load(with_value("artifact.path", f"nested/../{ARTIFACT_NAME}"))
    assert manifest.artifact_path.name == ARTIFACT_NAME


def test_checksum_mismatch_is_rejected(load: Load) -> None:
    errors = errors_for(load, with_value("artifact.sha256", "0" * 64))
    assert errors == [f"artifact.sha256: mismatch, declared {'0' * 64}, file has {ARTIFACT_SHA256}"]


def test_artifact_changed_after_signing_is_rejected(load: Load, artifact_dir: Path) -> None:
    (artifact_dir / ARTIFACT_NAME).write_bytes(ARTIFACT_BYTES + b"tampered")
    errors = errors_for(load, valid_document())
    assert len(errors) == 1
    assert errors[0].startswith("artifact.sha256: mismatch")


def test_artifact_read_failure_is_rejected(
    load: Load, artifact_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("deployctl.manifest.hashlib.file_digest", deny)
    assert errors_for(load, valid_document()) == ["artifact.path: cannot read artifact: denied"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempts", 1),
        ("attempts", 20),
        ("required_consecutive", 1),
        ("required_consecutive", 5),
        ("interval_seconds", 0),
        ("interval_seconds", 60),
        ("timeout_seconds", 0.001),
        ("timeout_seconds", 30),
        ("max_error_rate", 0),
        ("max_error_rate", 1),
        ("max_p99_latency_ms", 0.001),
        ("rollout_timeout_seconds", 0),
        ("rollout_timeout_seconds", 3600),
    ],
)
def test_health_bounds_are_inclusive(load: Load, field: str, value: float) -> None:
    document = valid_document()
    document["health_check"]["required_consecutive"] = 1
    document["health_check"][field] = value
    assert getattr(load(document).health_check, field) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempts", 0),
        ("attempts", 21),
        ("attempts", -1),
        ("attempts", 5.0),
        ("attempts", "5"),
        ("attempts", True),
        ("attempts", None),
        ("required_consecutive", 0),
        ("required_consecutive", 6),
        ("required_consecutive", 3.0),
        ("required_consecutive", True),
        ("interval_seconds", -0.1),
        ("interval_seconds", 60.1),
        ("interval_seconds", "1"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 30.1),
        ("timeout_seconds", True),
        ("max_error_rate", -0.01),
        ("max_error_rate", 1.01),
        ("max_error_rate", None),
        ("max_p99_latency_ms", 0),
        ("max_p99_latency_ms", -1),
        ("max_error_rate", float("nan")),
        ("max_p99_latency_ms", float("nan")),
        ("max_p99_latency_ms", float("inf")),
        ("max_p99_latency_ms", 10**400),
        ("rollout_timeout_seconds", -1),
        ("rollout_timeout_seconds", 3600.1),
        ("rollout_timeout_seconds", True),
    ],
)
def test_health_bounds_reject(load: Load, field: str, value: object) -> None:
    errors = errors_for(load, with_value(f"health_check.{field}", value))
    assert len(errors) == 1
    assert errors[0].startswith(f"health_check.{field}: ")


def test_overflowing_float_literal_is_not_treated_as_a_threshold(
    manifest_path: Path, artifact_dir: Path
) -> None:
    content = json.dumps(valid_document()).replace(
        '"max_p99_latency_ms": 500', '"max_p99_latency_ms": 1e999'
    )
    assert raw_errors(manifest_path, artifact_dir, content) == [
        "health_check.max_p99_latency_ms: must be a finite number"
    ]


def test_required_consecutive_error_is_skipped_when_attempts_is_invalid(load: Load) -> None:
    errors = errors_for(load, with_value("health_check.attempts", "five"))
    assert errors == ["health_check.attempts: must be an integer"]


@pytest.mark.parametrize(
    "name",
    [
        "orders-api-1.4.2",
        "orders-api-1.5.0",
        "orders-api-1.5.1",
        "orders-api-1.6.0",
    ],
)
def test_example_manifest_is_valid(name: str) -> None:
    manifest = load_manifest(EXAMPLES / "manifests" / f"{name}.json", EXAMPLES / "artifacts")
    assert f"{manifest.service}-{manifest.version}" == name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("checksum-mismatch", "artifact.sha256: mismatch"),
        ("path-traversal", "artifact.path: '../../etc/hosts' escapes"),
        ("invalid-schema", "manifest: unknown key 'verison'"),
    ],
)
def test_example_manifest_is_rejected(name: str, expected: str) -> None:
    with pytest.raises(ManifestError) as caught:
        load_manifest(EXAMPLES / "manifests" / f"{name}.json", EXAMPLES / "artifacts")
    assert any(error.startswith(expected) for error in caught.value.errors)
