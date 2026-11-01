import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from deployctl import __version__
from deployctl.models import ExitCode

ROOT = Path(__file__).parents[2]
BUILD = ROOT / "scripts" / "build_zipapp.py"
EXAMPLES = ROOT / "examples"
MANIFESTS = EXAMPLES / "manifests"
ARTIFACTS = EXAMPLES / "artifacts"
HEALTHY = EXAMPLES / "health" / "default.json"


def build(output: Path) -> Path:
    subprocess.run([sys.executable, str(BUILD), str(output)], check=True)  # noqa: S603 - our script
    return output


@pytest.fixture(scope="module")
def pyz(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("zipapp") / "deployctl.pyz")


def run_pyz(pyz: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # -S leaves out site-packages, so this also proves the archive needs only the stdlib.
    return subprocess.run(  # noqa: S603 - our own interpreter and arguments
        [sys.executable, "-S", str(pyz), *args], capture_output=True, text=True, check=False
    )


def validate(manifest: str) -> tuple[str, ...]:
    path = str(MANIFESTS / f"{manifest}.json")
    return ("validate", "--manifest", path, "--artifact-dir", str(ARTIFACTS))


def test_version(pyz: Path) -> None:
    result = run_pyz(pyz, "--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"deployctl {__version__}"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (validate("orders-api-1.4.2"), ExitCode.OK),
        (validate("invalid-schema"), ExitCode.REJECTED),
        ((), ExitCode.USAGE),
    ],
    ids=["valid", "invalid", "usage"],
)
def test_exit_code_propagates(pyz: Path, args: tuple[str, ...], expected: ExitCode) -> None:
    assert run_pyz(pyz, *args).returncode == expected


def test_deploy_exit_code_propagates(pyz: Path, tmp_path: Path) -> None:
    def deploy(manifest: str) -> int:
        command = ["deploy", "--manifest", str(MANIFESTS / f"{manifest}.json")]
        command += ["--artifact-dir", str(ARTIFACTS), "--health-profile", str(HEALTHY)]
        return run_pyz(pyz, *command, "--state-dir", str(tmp_path / "state")).returncode

    assert deploy("orders-api-1.4.2") == ExitCode.OK
    assert deploy("orders-api-1.5.0") == ExitCode.ROLLED_BACK


def test_build_is_reproducible(pyz: Path, tmp_path: Path) -> None:
    assert build(tmp_path / "again.pyz").read_bytes() == pyz.read_bytes()


def test_archive_carries_nothing_from_the_build_machine(pyz: Path) -> None:
    with zipfile.ZipFile(pyz) as archive:
        entries = archive.infolist()

    names = [entry.filename for entry in entries]
    assert names == ["__main__.py", *sorted(names[1:])]
    assert all(name.endswith(".py") for name in names)
    assert "deployctl/cli.py" in names
    assert {entry.date_time for entry in entries} == {(1980, 1, 1, 0, 0, 0)}
    assert {entry.create_system for entry in entries} == {3}
    assert {entry.external_attr >> 16 for entry in entries} == {0o644}
    assert {entry.compress_type for entry in entries} == {zipfile.ZIP_STORED}
