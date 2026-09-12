"""The handover, end to end, with a real person in the middle.

    .venv/bin/python scripts/handoff.py

A replay starts on a signed on session and is walked into an expired one part
way through. It stops, writes an intervention request, and gives you the
browser. You sign on in that same window and press Enter. The run looks again,
checks that the thing which stopped it is no longer true, and finishes the
capability from where it paused.

What it leaves behind, under evidence/replay_handoff/:

    replay_handoff.jsonl   the whole sequence, redacted on the way out
    blobs/                 the screen as it was when the run stopped

The session expiry is forced rather than waited for — the fixture application
has a lever for it, and sitting through a real fifteen minute timeout would
prove the same thing more slowly.

Nothing here is a special path through the engine. It is ReplayCapability with
an operator wired in, running the same artifact the other tests run.
"""

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

import yaml
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

load_dotenv(find_dotenv(usecwd=True))

from cua.adapters.browser_activity import BrowserActivity  # noqa: E402
from cua.adapters.clocks import RealClock  # noqa: E402
from cua.adapters.console_operator import ConsoleOperator  # noqa: E402
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink  # noqa: E402
from cua.adapters.playwright_surface import browser_session  # noqa: E402
from cua.app.replay import ReplayCapability  # noqa: E402
from cua.domain.actions import ActionType  # noqa: E402
from cua.domain.artifact import capability_from_document  # noqa: E402
from cua.domain.policy import (  # noqa: E402
    DeniedControl,
    Policy,
    RedactionRules,
    Rendering,
    Sensitivity,
)

MEMBER_ID = "100045"
RUN_ID = "replay_handoff"
ARTIFACT = Path("tests/fixtures/member_lookup.handwritten.yaml")
EVIDENCE = Path("evidence")

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)

# Everything the engine and the handover emit. Unlisted fields are dropped, so
# this list failing to keep up costs evidence rather than a member's data.
DECLARED = {
    field: Sensitivity.INTERNAL
    for field in (
        "event", "at", "capability", "version", "effect", "approval", "step", "action",
        "condition", "outcome", "matched", "held", "intent", "via_signal", "signal_index",
        "allowed", "rule", "reason", "detail", "url_pattern", "page_title", "run_id",
        "goal", "resume_checkpoint", "screenshot", "owner", "actions", "checkpoint",
        "holds", "into", "run_state", "expected", "observed", "attempt", "route",
    )
} | {
    # What a person typed, and what the run typed. Both are member data until
    # something says otherwise, and a password is neither: the page never sends
    # it, so by the time it reaches here the value is already REDACTED.
    "value": Sensitivity.PERSONAL,
    # A control's name is the application's own vocabulary — "User Id",
    # "Sign On" — and masking every one of them turned the handover record
    # into noise. The leak backstop still drops one that carries a member.
    "target": Sensitivity.INTERNAL,
}


def serve() -> tuple[str, object]:
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    if not os.environ.get("TARGET_APP_PASSWORD"):
        raise SystemExit("TARGET_APP_PASSWORD is not set. See .env.example.")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def arm(base_url: str, lever: str) -> None:
    """Pull a server side lever. Nothing in the browser reveals it was pulled."""
    request = urllib.request.Request(
        f"{base_url}/_test/{lever}", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def sign_on(page: object, base_url: str) -> None:
    """The operator signs on before the run starts, as they would in production."""
    page.goto(f"{base_url}/login")  # type: ignore[attr-defined]
    page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])  # type: ignore[attr-defined]
    page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])  # type: ignore[attr-defined]
    page.click("#ctl00_cph_btnSignOn")  # type: ignore[attr-defined]
    page.wait_for_url(f"{base_url}/search")  # type: ignore[attr-defined]


def main() -> int:
    base_url, server = serve()
    capability = capability_from_document(yaml.safe_load(ARTIFACT.read_text(encoding="utf-8")))
    sink = FilesystemEvidenceSink(
        root=EVIDENCE,
        rules=RULES,
        declared=DECLARED,
        known_values={MEMBER_ID: Sensitivity.PERSONAL},
        stream_name=f"{RUN_ID}.jsonl",
    )

    try:
        with browser_session(base_url, headless=False, slow_mo_ms=300) as surface:
            sign_on(surface.page, base_url)

            # The operator holds the same page the surface holds. Not a fresh
            # window: a new one would be a different session with a different
            # cookie, and signing on there would leave the run as locked out as
            # it was.
            operator = ConsoleOperator(activity=BrowserActivity(page=surface.page))

            # Armed after sign on, so the run starts healthy and dies part way
            # through, which is the case worth demonstrating.
            arm(base_url, "expire-session")

            engine = ReplayCapability(
                surface=surface,
                policy=Policy(
                    allowed_origins=(base_url,),
                    allowed_routes=("/login", "/search", "/members/{member_id}"),
                    allowed_actions=frozenset(
                        {ActionType.CLICK, ActionType.TYPE, ActionType.READ}
                    ),
                    denied_controls=(DeniedControl(role="link", name="Sign Off"),),
                ),
                clock=RealClock(),
                evidence=sink,
                operator=operator,
            )
            result = engine.run(capability, {"member_id": MEMBER_ID}, run_id=RUN_ID)
    finally:
        server.shutdown()  # type: ignore[attr-defined]

    print(f"\noutcome : {result.outcome.value}")
    print(f"outputs : {result.outputs}")
    print(f"detail  : {(result.detail or '').strip()}")
    print(f"\nwrote {EVIDENCE / RUN_ID}/{RUN_ID}.jsonl")

    stream = (EVIDENCE / RUN_ID / f"{RUN_ID}.jsonl")
    if stream.exists():
        password = os.environ["TARGET_APP_PASSWORD"]
        text = stream.read_text(encoding="utf-8")
        print(f"password in the record: {password in text}")
        print("sequence:")
        for line in text.splitlines():
            print(f"  {json.loads(line)['event']}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
