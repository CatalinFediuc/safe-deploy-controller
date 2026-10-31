import argparse
import sys
from typing import NoReturn

from deployctl import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deployctl", description="Safe deployment controller")
    parser.add_argument("--version", action="version", version=f"deployctl {__version__}")
    parser.parse_args(argv)
    return 0


def run() -> NoReturn:  # pragma: no cover - thin shim, exercised by `make smoke`
    sys.exit(main())
