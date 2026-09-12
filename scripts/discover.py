"""Run discovery against the live application with a real model, and keep it.

The one command in this project that costs money and needs a network. It is a
script rather than a CLI subcommand because the composition root is not written
yet; when it is, this file is what moves into it.

    export ANTHROPIC_API_KEY=...
    .venv/bin/python scripts/discover.py

What it leaves behind, under evidence/<run_id>/:

    discovery_run.jsonl what the run did: proposals, rationales, policy checks,
                        actions and observations, redacted on the way out
    transcript.json     the raw model exchange, kept apart from the above
    capability.yaml     the compiled artifact

This script signs the session on before the loop starts, standing in for the
operator who would do it in production. The credential is read from the
environment here and never enters the system proper: no capability can declare
one, because Contract refuses to be constructed with a secret input, and the
discovery loop is handed a session that is already signed on.
"""

import argparse
import json
import os
import sys
import threading
from pathlib import Path

import yaml
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

# Before anything reads the environment. target_app loads .env too, but it is
# imported inside serve(), which runs after the key check below — so without
# this the key sits in .env and the script reports it missing.
load_dotenv(find_dotenv(usecwd=True))

from cua.adapters.claude_model import ClaudeModel, anthropic_client  # noqa: E402
from cua.adapters.clocks import RealClock  # noqa: E402
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink  # noqa: E402
from cua.adapters.playwright_surface import browser_session  # noqa: E402
from cua.app.discover import DiscoverCapability  # noqa: E402
from cua.domain.actions import ActionType  # noqa: E402
from cua.domain.artifact import capability_to_document  # noqa: E402
from cua.domain.compiler import compile_capability  # noqa: E402
from cua.domain.policy import (  # noqa: E402
    DeniedControl,
    Policy,
    RedactionRules,
    Rendering,
    Sensitivity,
    mask,
)

DEFAULT_MEMBER = "100045"
RUN_ID = "discovery"


def goal_for(member_id: str) -> str:
    return (
        f"Look up member {member_id} and read their current savings balance "
        "and the name on the account."
    )

EVIDENCE = Path("evidence")

# What the run is allowed to do. The same rules replay runs under, because a
# discovery run permitted to go somewhere replay is not would compile a
# capability that cannot execute.
# /login is deliberately absent. A discovery run operates a session it was
# handed and never creates one, so the sign on page is not a screen it has any
# business on — and a run that finds itself there has been bounced, which is
# worth stopping for rather than working around. sign_on() is unaffected: it
# drives the browser directly and never consults policy, which is the point of
# it standing outside the system.
ROUTES = ("/search", "/members/{member_id}")
ACTIONS = frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ})

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)

# What the evidence writer knows about the fields the loop emits. Anything not
# named here is unclassified and therefore dropped, which is the direction to
# fail in: forgetting to classify a field costs evidence, not a member's data.
DECLARED = {
    "event": Sensitivity.INTERNAL,
    "at": Sensitivity.INTERNAL,
    "goal": Sensitivity.INTERNAL,
    "step": Sensitivity.INTERNAL,
    "kind": Sensitivity.INTERNAL,
    "rationale": Sensitivity.INTERNAL,
    "action": Sensitivity.INTERNAL,
    "allowed": Sensitivity.INTERNAL,
    "rule": Sensitivity.INTERNAL,
    "reason": Sensitivity.INTERNAL,
    "detail": Sensitivity.INTERNAL,
    "url_pattern": Sensitivity.INTERNAL,
    "page_title": Sensitivity.INTERNAL,
    "nodes": Sensitivity.INTERNAL,
    "target_role": Sensitivity.INTERNAL,
    "max_steps": Sensitivity.INTERNAL,
    "run_ms": Sensitivity.INTERNAL,
    "stopped_because": Sensitivity.INTERNAL,
    "succeeded": Sensitivity.INTERNAL,
    "steps": Sensitivity.INTERNAL,
    # A typed value is the member number often enough to treat it as one.
    "value": Sensitivity.PERSONAL,
    # A control name is the application's own vocabulary — "Member Number",
    # "Find" — so recording it is how the log stays readable. The one case
    # where it is member data is the result row named after the member, and
    # the leak backstop drops that field without blinding every other name.
    "target_name": Sensitivity.INTERNAL,
}


def settings(path: Path) -> dict[str, object]:
    """The model block out of config.yaml.

    Read here rather than defaulted in the adapter so there is one answer to
    "which model ran", and it is in the file that is committed next to the
    evidence. When composition.py exists this is the sort of thing it does for
    every adapter; until then the script reads the one block it needs.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = document.get("model") if isinstance(document, dict) else None
    if not isinstance(model, dict) or not model.get("name"):
        raise SystemExit(f"{path} has no model.name; nothing says which model to run")
    return model


def serve() -> tuple[str, object]:
    """The target application, on a free port, for the length of this run."""
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    if not os.environ.get("TARGET_APP_PASSWORD"):
        raise SystemExit("TARGET_APP_PASSWORD is not set. See .env.example.")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def sign_on(surface: object, base_url: str) -> None:
    page = surface.page  # type: ignore[attr-defined]
    page.goto(f"{base_url}/login")
    page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
    page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
    page.click("#ctl00_cph_btnSignOn")
    page.wait_for_url(f"{base_url}/search")


def options(argv: list[str] | None = None) -> argparse.Namespace:
    """What varies between runs.

    A discovery run is defined by where it starts and what it is asked for, so
    both are arguments. Hard coding them meant one scenario could ever be
    recorded, and the scenarios worth recording are the ones that do not go to
    plan: a member who does not exist, a session nobody signed on.
    """
    parser = argparse.ArgumentParser(
        prog="discover",
        description="Run discovery against the target application with a real model.",
    )
    parser.add_argument(
        "--member",
        default=DEFAULT_MEMBER,
        help=f"member number to look up (default: {DEFAULT_MEMBER}). "
        "100099 does not exist; 100047 is restricted.",
    )
    parser.add_argument("--goal", default=None, help="override the goal text entirely")
    parser.add_argument(
        "--signed-out",
        action="store_true",
        help="skip sign on, so the run starts at a session nobody authenticated",
    )
    parser.add_argument(
        "--run-id",
        default=RUN_ID,
        help=f"names the directory under evidence/ (default: {RUN_ID})",
    )
    parser.add_argument(
        "--headless", action="store_true", help="do not open a browser window"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = options(argv)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Discovery needs a model; replay does "
            "not, which is why only this path asks for one."
        )

    base_url, server = serve()
    member_id = str(args.member)
    sink = FilesystemEvidenceSink(
        root=EVIDENCE,
        rules=RULES,
        declared=DECLARED,
        known_values={member_id: Sensitivity.PERSONAL},
        # Named for what the run was, not for what the sink writes. A replay
        # stream sitting beside this one would be events.jsonl, and telling the
        # two apart by directory alone is a thing somebody gets wrong once.
        stream_name="discovery_run.jsonl",
    )
    chosen = settings(Path(os.environ.get("CUA_CONFIG", "config.yaml")))
    model = ClaudeModel(
        client=anthropic_client(key),
        model=str(chosen["name"]),
        max_tokens=int(chosen.get("max_tokens", 4096)),  # type: ignore[call-overload]
    )
    print(f"model: {model.model}")

    goal = str(args.goal) if args.goal else goal_for(member_id)
    print(f"goal : {goal}")
    print(f"start: {'signed out' if args.signed_out else 'signed on'}")

    try:
        with browser_session(
            base_url, headless=bool(args.headless), slow_mo_ms=250
        ) as surface:
            if not args.signed_out:
                sign_on(surface, base_url)
            else:
                # Straight to the search route without signing on. The
                # application bounces it to /login, which is the screen the run
                # then has to make sense of.
                surface.page.goto(f"{base_url}/search")
            engine = DiscoverCapability(
                surface=surface,
                model=model,
                policy=Policy(
                    allowed_origins=(base_url,),
                    allowed_routes=ROUTES,
                    allowed_actions=ACTIONS,
                    denied_controls=(DeniedControl(role="link", name="Sign Off"),),
                ),
                clock=RealClock(),
                evidence=sink,
            )
            trajectory = engine.run(goal, run_id=str(args.run_id))
    finally:
        server.shutdown()  # type: ignore[attr-defined]

    # The model's own record, kept apart from what the application had done to
    # it. One is what was said; the other is what happened.
    #
    # Masked before it is written. Every prompt in here contains the screen as
    # the model saw it, which means the member number typed into the field and
    # the result row named after it. The events stream gets that treatment from
    # the sink; writing this one straight past the sink would put in the
    # deliverable exactly the data the sink exists to keep out of it.
    (EVIDENCE / str(args.run_id)).mkdir(parents=True, exist_ok=True)
    (EVIDENCE / RUN_ID / "transcript.json").write_text(
        _masked(json.dumps(model.transcript, indent=2, default=str), {"member_id": member_id}),
        encoding="utf-8",
    )

    print(f"stopped because: {trajectory.stopped_because.value}")
    print(f"steps executed : {len(trajectory.steps)}")
    for step in trajectory.steps:
        action = step.proposal.action_type
        print(f"  {action.value if action else '-':8} {step.proposal.rationale}")

    if not trajectory.succeeded:
        print("\nThe run did not reach the goal, so there is nothing to compile.")
        return 1

    capability = compile_capability(
        trajectory,
        name="lookup_member_balance",
        version="1.0.0",
        inputs={"member_id": member_id},
        app="riverside_cu_backoffice",
        release="4.2.11",
    )
    document = capability_to_document(capability)
    (EVIDENCE / RUN_ID / "capability.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    print(f"\nwrote {EVIDENCE / str(args.run_id)}/")
    return 0


def _masked(text: str, inputs: dict[str, str]) -> str:
    """Replace declared personal literals with the same mask the sink applies.

    The sink renders by field name, which cannot work on a blob of free text
    where the member number sits inside a prompt. What is known here is which
    literals were personal, because this script chose them — so the same
    knowledge that drives redaction drives this.
    """
    for literal in inputs.values():
        text = text.replace(literal, mask(literal))
    return text


if __name__ == "__main__":
    raise SystemExit(main())
