import os
from pathlib import Path

import pytest

from deployctl.fileio import write_atomic


def test_write_creates_missing_directories(tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "data.json"
    write_atomic(path, "first")
    assert path.read_text(encoding="utf-8") == "first"


def test_write_replaces_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    write_atomic(path, "a much longer first version")
    write_atomic(path, "second")
    assert path.read_text(encoding="utf-8") == "second"


def test_leftover_tmp_file_is_overwritten_and_removed(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    tmp = tmp_path / "data.json.tmp"
    tmp.write_text("half a wri", encoding="utf-8")
    write_atomic(path, "complete")
    assert path.read_text(encoding="utf-8") == "complete"
    assert not tmp.exists()


def test_failed_replace_keeps_the_old_content_and_removes_the_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.json"
    write_atomic(path, "old")

    def fail(*_: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk went away"):
        write_atomic(path, "new")

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [path]


def test_new_content_is_durable_before_it_replaces_the_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor: int) -> None:
        calls.append("fsync")
        real_fsync(descriptor)

    def replace(source: Path, target: Path) -> None:
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    write_atomic(tmp_path / "data.json", "content")
    # The second fsync is the directory, which makes the rename itself durable.
    assert calls == ["fsync", "replace", "fsync"]
