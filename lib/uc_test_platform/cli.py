"""The uc-test-platform command.

Each subcommand hands its arguments to one module: `windows` and `linux`
run a repository's tests and end-to-end scenario on a clean instance,
and `cut` runs the network cut's own commands, such as its self-test.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import List, Optional

from . import __version__

COMMANDS = {
    "windows": ("uc_test_platform.windows",
                "Run on a Windows virtual machine reached over SSH."),
    "linux": ("uc_test_platform.linux",
              "Run in an Incus container, reached with incus exec."),
    "cut": ("uc_test_platform.incus_acl",
            "The network cut: selftest, killtest, recover, undo, hold."),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uc-test-platform",
        description="Verification runs and end-to-end scenarios on clean "
                    "test instances (uc-automa 0031).",
        epilog="Run 'uc-test-platform COMMAND --help' for a command's options.")
    parser.add_argument("--version", action="version",
                        version=f"uc-test-platform {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (_, summary) in COMMANDS.items():
        commands.add_parser(name, help=summary, add_help=False)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        module = importlib.import_module(COMMANDS[argv[0]][0])
        return module.main(argv[1:])
    build_parser().parse_args(argv)
    return 0
