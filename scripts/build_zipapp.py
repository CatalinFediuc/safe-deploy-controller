"""Build the single-file deployctl.pyz; the same source always gives the same bytes.

`python -m zipapp` can't do this: it stamps the __main__.py it generates with the current time.
Entries are stored, not deflated, so the bytes don't depend on which zlib Python was built with.
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src"
DEFAULT_OUTPUT = ROOT / "dist" / "deployctl.pyz"

SHEBANG = b"#!/usr/bin/env python3\n"
# cli.run, not cli.main: run() passes the exit code to sys.exit.
MAIN = b"from deployctl.cli import run\n\nrun()\n"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
UNIX = 3
FILE_MODE = 0o644


def entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.create_system = UNIX
    info.external_attr = FILE_MODE << 16
    return info


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = sorted((SOURCE / "deployctl").rglob("*.py"))
    with output.open("wb") as file:
        file.write(SHEBANG)
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr(entry("__main__.py"), MAIN)
            for path in sources:
                archive.writestr(entry(path.relative_to(SOURCE).as_posix()), path.read_bytes())
    output.chmod(0o755)


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT)
