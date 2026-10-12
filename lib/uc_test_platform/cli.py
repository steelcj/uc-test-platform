"""The uc-test-platform command."""

from __future__ import annotations

import argparse
from typing import List, Optional

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uc-test-platform",
        description="Verification runs and end-to-end scenarios on clean "
                    "test instances (uc-automa 0031).")
    parser.add_argument("--version", action="version",
                        version=f"uc-test-platform {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    build_parser().parse_args(argv)
    return 0
