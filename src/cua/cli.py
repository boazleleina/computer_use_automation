"""Command line entry point. Both subcommands are stubs for now.

Parses arguments and turns domain errors into exit codes. It holds no
orchestration: the sequence of a run belongs in app/, so that the same use case
can be driven by a test, an HTTP handler, or this file without duplication.

Two subcommands, mirroring the two halves of the system:

    discover   run the model loop once against a live UI and emit an artifact
    replay     execute an existing artifact with inputs, with no model involved
"""

import argparse
import sys
from collections.abc import Sequence

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Separate from main so tests can assert the interface without running it.
    """
    parser = argparse.ArgumentParser(
        prog="cua",
        description="Turn legacy UI flows into callable, replayable capabilities.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="configuration file (default: config.yaml)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover = subcommands.add_parser(
        "discover",
        help="run discovery against a live UI and emit a capability artifact",
    )
    discover.add_argument("--goal", required=True, help="what the capability should accomplish")
    discover.add_argument("--name", required=True, help="capability name to emit")

    replay = subcommands.add_parser(
        "replay",
        help="execute a stored capability; no model is constructed",
    )
    replay.add_argument("--name", required=True, help="capability name")
    replay.add_argument(
        "--version",
        default="1.0.0",
        metavar="SEMVER",
        help="capability version (default: 1.0.0)",
    )
    replay.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="capability input; repeatable",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than calling sys.exit,
    so it can be invoked from a test."""
    args = build_parser().parse_args(argv)
    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
