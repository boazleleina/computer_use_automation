"""Run discovery against the live application with a real model, and keep it.

The one command in this project that costs money and needs a network. It is a
script rather than a CLI subcommand because the composition root is not written
yet; when it is, this file is what moves into it.

    export ANTHROPIC_API_KEY=...
    .venv/bin/python scripts/discover.py

What it leaves behind, under evidence/<run_id>/:

    events.jsonl        what the run did: proposals, rationales, policy checks,
                        actions and observations, redacted on the way out
    transcript.json     the raw model exchange, kept apart from the above
    capability.yaml     the compiled artifact

This script signs the session on before the loop starts, standing in for the
operator who would do it in production. The credential is read from the
environment here and never enters the system proper: no capability can declare
one, because Contract refuses to be constructed with a secret input, and the
discovery loop is handed a session that is already signed on.
"""

import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import yaml
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cua.adapters.claude_model import ClaudeModel, anthropic_client  # noqa: E402
from cua.adapters.clocks import RealClock  # noqa: E402
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink  # noqa: E402
from cua.adapters.playwright_surface import browser_session  # noqa: E402
from cua.app.discover import DiscoverCapability  # noqa: E402
from cua.domain.actions import ActionType  # noqa: E402
from cua.domain.compiler import compile_capability  # noqa: E402
from cua.domain.policy import (  # noqa: E402
    DeniedControl,
    Policy,
    RedactionRules,
    Rendering,
    Sensitivity,
    mask,
)

MEMBER_ID = "100045"
GOAL = (
    f"Look up member {MEMBER_ID} and read their current savings balance "
    "and the name on the account."
)
RUN_ID = "discovery"

EVIDENCE = Path("evidence")

# What the run is allowed to do. The same rules replay runs under, because a
# discovery run permitted to go somewhere replay is not would compile a
# capability that cannot execute.
ROUTES = ("/login", "/search", "/members/{member_id}")
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
    # The two that carry member data. A typed member number and the name of a
    # control named after one are both the member, so both are masked.
    "value": Sensitivity.PERSONAL,
    "target_name": Sensitivity.PERSONAL,
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


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Discovery needs a model; replay does "
            "not, which is why only this path asks for one."
        )

    base_url, server = serve()
    sink = FilesystemEvidenceSink(root=EVIDENCE, rules=RULES, declared=DECLARED)
    chosen = settings(Path(os.environ.get("CUA_CONFIG", "config.yaml")))
    model = ClaudeModel(
        client=anthropic_client(key),
        model=str(chosen["name"]),
        max_tokens=int(chosen.get("max_tokens", 4096)),  # type: ignore[call-overload]
    )
    print(f"model: {model.model}")

    try:
        with browser_session(base_url, headless=False, slow_mo_ms=250) as surface:
            sign_on(surface, base_url)
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
            trajectory = engine.run(GOAL, run_id=RUN_ID)
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
    (EVIDENCE / RUN_ID).mkdir(parents=True, exist_ok=True)
    (EVIDENCE / RUN_ID / "transcript.json").write_text(
        _masked(json.dumps(model.transcript, indent=2, default=str), {"member_id": MEMBER_ID}),
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
        inputs={"member_id": MEMBER_ID},
        app="riverside_cu_backoffice",
        release="4.2.11",
    )
    document = _document(capability)
    (EVIDENCE / RUN_ID / "capability.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    print(f"\nwrote {EVIDENCE / RUN_ID}/")
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


def _document(capability: object) -> dict[str, object]:
    """The capability as a plain document, in the vocabulary the loader reads.

    asdict rather than a hand written serialiser, with the enums flattened.
    Writing a second serialiser by hand is how the thing that is saved drifts
    from the thing that was compiled.
    """
    plain = _plain(asdict(capability))  # type: ignore[call-overload]
    assert isinstance(plain, dict)
    return plain


def _plain(value: object) -> object:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    return value


if __name__ == "__main__":
    raise SystemExit(main())
