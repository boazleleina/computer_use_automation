"""Command line entry point.

Parses arguments, wires nothing, and turns domain errors into exit codes. The
sequence of a run belongs in app/, so the same use case can be driven by a
test, an HTTP handler or this file without duplication; the construction of
adapters belongs in composition.py. What is left here is the two things a
command line is actually for — reading arguments and choosing an exit status.

Two subcommands, mirroring the two halves of the system:

    discover   run the model loop once against a live UI and emit an artifact
    replay     execute an existing artifact with inputs, with no model involved

`replay` is the production path and needs no key, no network and no model. That
it can be run by somebody who has never set ANTHROPIC_API_KEY is the whole
point of the split, and is worth checking rather than assuming.

Exit codes are for scripts. A business outcome is not a failure of the system —
"no such member" is an answer — so it gets a code of its own rather than being
folded into either success or error.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from cua.domain.capability import Approval, Capability
from cua.domain.errors import DomainError
from cua.domain.outcomes import Outcome, Result
from cua.domain.policy import Sensitivity

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# Distinct from both. A caller scripting this needs to tell "the run worked and
# the answer is no" apart from "the run did not work", and an exit status is
# the only thing a shell can see.
EXIT_BUSINESS_OUTCOME = 3
EXIT_INTERVENTION = 4


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
    replay.add_argument("--artifact", required=True, metavar="PATH", help="the capability file")
    replay.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="capability input; repeatable",
    )
    replay.add_argument("--run-id", default="cli", help="names the evidence directory")
    replay.add_argument(
        "--approve",
        action="store_true",
        help="treat the artifact as approved for this run, as a reviewer would",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than calling sys.exit,
    so it can be invoked from a test."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "replay":
            return _replay(args)
        return _discover(args)
    except DomainError as error:
        # Everything this system defined, and only that. An unexpected
        # exception keeps its traceback, because a bug should look like one.
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return EXIT_ERROR


def _replay(args: argparse.Namespace) -> int:
    """Execute a stored capability. Constructs no model and cannot reach one."""
    import yaml

    from cua.app.replay import ReplayCapability
    from cua.composition import (
        clock,
        evidence_sink,
        load_settings,
        policy_from,
        surface_session,
    )
    from cua.domain.artifact import capability_from_document

    settings = load_settings(Path(args.config))
    document = yaml.safe_load(Path(args.artifact).read_text(encoding="utf-8"))
    capability = capability_from_document(document)

    inputs = _inputs(args.input)
    if args.approve:
        capability = _approved(capability)

    sink = evidence_sink(
        settings,
        declared=_declared(capability),
        # What this run is handling, so a value that turns up somewhere nobody
        # classified is still caught on the way to disk.
        known_values=dict.fromkeys(inputs.values(), Sensitivity.PERSONAL),
        stream_name=f"{args.run_id}.jsonl",
    )

    with surface_session(settings) as surface:
        result = ReplayCapability(
            surface=surface, policy=policy_from(settings), clock=clock(), evidence=sink
        ).run(capability, inputs, run_id=args.run_id)

    _report(result)
    return _status(result.outcome)


def _discover(_: argparse.Namespace) -> int:
    """Not wired to this entry point.

    A discovery run needs a signed on session, and signing on is a person's
    job — so the runnable version lives in scripts/discover.py, where that
    person is standing in front of a browser rather than behind a process.
    Wiring it here would mean either putting a credential in a flag or
    pretending the session appears by itself.
    """
    print(
        "discover is run from scripts/discover.py, which signs a session on first.\n"
        "  ANTHROPIC_API_KEY=... .venv/bin/python scripts/discover.py --help",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _inputs(pairs: Sequence[str]) -> dict[str, str]:
    """KEY=VALUE arguments as a mapping, or a usage error naming the offender."""
    inputs: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise SystemExit(f"--input expects KEY=VALUE, got {pair!r}")
        inputs[key] = value
    return inputs


def _approved(capability: Capability) -> Capability:
    """Stand in for a reviewer, and say so.

    A flag rather than an edit to the file, because approval belongs to a
    version of an artifact and flipping it in place would make the approved
    thing and the reviewed thing two different documents. For a demonstration
    this is the honest shortcut; in production the store holds the approval.
    """
    from dataclasses import replace

    return replace(
        capability, contract=replace(capability.contract, approval=Approval.APPROVED)
    )


def _declared(capability: Capability) -> dict[str, Sensitivity]:
    """What the artifact says about its own values, plus the run record's own
    vocabulary. Anything unlisted is dropped, which is the direction to fail in.
    """
    declared: dict[str, Sensitivity] = {
        field: Sensitivity.INTERNAL
        for field in (
            "event", "at", "capability", "version", "effect", "approval", "step",
            "action", "condition", "code", "outcome", "matched", "held", "intent",
            "via_signal", "signal_index", "allowed", "rule", "reason", "detail",
            "url_pattern", "page_title", "run_state", "expected", "observed",
            "into", "evidence_ref", "attempt",
        )
    }
    # Separately, because a tuple built from both loses the common type and
    # leaves the field being read off `object`.
    contract = capability.contract
    for given in contract.inputs:
        declared[given.name] = given.sensitivity
    for returned in contract.outputs:
        declared[returned.name] = returned.sensitivity
    declared["value"] = Sensitivity.PERSONAL
    return declared


def _report(result: Result) -> None:
    print(f"outcome : {result.outcome.value}")
    print(f"code    : {result.code}")
    print(f"detail  : {(result.detail or '').strip()}")
    if result.outputs:
        print("outputs :")
        for name, value in result.outputs.items():
            print(f"  {name} = {value}")


def _status(outcome: Outcome) -> int:
    if outcome is Outcome.SUCCESS:
        return EXIT_OK
    if outcome is Outcome.BUSINESS_OUTCOME:
        return EXIT_BUSINESS_OUTCOME
    if outcome is Outcome.INTERVENTION_REQUIRED:
        return EXIT_INTERVENTION
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
