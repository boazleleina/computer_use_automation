"""A run that meets an expired session, is rescued, and finishes.

The whole control transfer, against the real browser and the real application:
the session dies part way through, the engine stops and asks for a person, a
person signs on in that same window, hands it back, and the run verifies the
thing that stopped it is no longer true before carrying on.

The person here is scripted. What they do is what a person would do — type into
the sign on form of the run's own live page — and the point of scripting it is
that the transfer, not the typing, is what these tests are about. The
interactive version is scripts/handoff.py, and it is the same engine with a
terminal prompt in place of this fake.

What would falsify the claim: if the run could continue without the checkpoint
holding, if it could act while the human held it, or if the credential the
human typed appeared anywhere in what was recorded.
"""

import json
import os
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from werkzeug.serving import make_server

from cua.adapters.browser_activity import BrowserActivity
from cua.adapters.clocks import RealClock
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink
from cua.adapters.playwright_surface import PlaywrightSurface, browser_session
from cua.app.replay import ReplayCapability
from cua.domain.actions import ActionType
from cua.domain.artifact import capability_from_document
from cua.domain.intervention import REDACTED, Handover, HumanAction, InterventionRequest
from cua.domain.outcomes import Outcome, Result
from cua.domain.policy import (
    DeniedControl,
    Policy,
    RedactionRules,
    Rendering,
    Sensitivity,
)
from cua.domain.run import EscalationReason, RunState
from tests.integration.test_replay_scripted import ARTIFACT, MEMBER_ID

pytest.importorskip("playwright", reason="the live surface needs a browser")

HEADLESS = os.environ.get("CUA_HEADLESS", "1") != "0"
# Only a floor. A real .env wins, which is why the leak assertions read the
# password back out of the environment rather than trusting this.
PASSWORD = "not-a-real-password"

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)
DECLARED = {
    "event": Sensitivity.INTERNAL,
    "step": Sensitivity.INTERNAL,
    "reason": Sensitivity.INTERNAL,
    "checkpoint": Sensitivity.INTERNAL,
    "holds": Sensitivity.INTERNAL,
    "action": Sensitivity.INTERNAL,
    "url_pattern": Sensitivity.INTERNAL,
    "condition": Sensitivity.INTERNAL,
    "outcome": Sensitivity.INTERNAL,
    "matched": Sensitivity.INTERNAL,
    "route": Sensitivity.INTERNAL,
    "value": Sensitivity.PERSONAL,
    "target": Sensitivity.INTERNAL,
}


@pytest.fixture(scope="module")
def target_app() -> Iterator[str]:
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    os.environ.setdefault("TARGET_APP_PASSWORD", PASSWORD)

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def arm(base_url: str, lever: str) -> None:
    request = urllib.request.Request(
        f"{base_url}/_test/{lever}", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def sign_on(surface: PlaywrightSurface, base_url: str) -> None:
    surface.page.goto(f"{base_url}/login")
    surface.page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
    surface.page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
    surface.page.click("#ctl00_cph_btnSignOn")
    surface.page.wait_for_url(f"{base_url}/search")


class ScriptedOperator:
    """A person who signs the session back on, or does not.

    `rescue=False` is the operator who says they are finished without having
    fixed anything, which is the case the resume checkpoint exists for.
    """

    def __init__(
        self, surface: PlaywrightSurface, base_url: str, *, rescue: bool = True
    ) -> None:
        self.surface = surface
        self.base_url = base_url
        self.rescue = rescue
        self.activity = BrowserActivity(page=surface.page)
        self.requests: list[InterventionRequest] = []

    def request_intervention(self, request: InterventionRequest) -> None:
        self.requests.append(request)
        self.activity.start()

    def await_release(self, run_id: str) -> Handover:
        if self.rescue:
            # In this window, on this session. A fresh page would sign on a
            # different cookie and leave the run exactly as stuck.
            page = self.surface.page
            page.goto(f"{self.base_url}/login")
            page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
            page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
            page.click("#ctl00_cph_btnSignOn")
            page.wait_for_url(f"{self.base_url}/search")
        return Handover(events=tuple(self.activity.stop()))


def replay_with(
    target_app: str, operator_rescues: bool, tmp_path: Path
) -> tuple[Result, ScriptedOperator, list[dict[str, object]]]:
    """One run, with the session expired part way through."""
    capability = capability_from_document(
        yaml.safe_load(Path(ARTIFACT).read_text(encoding="utf-8"))
    )
    sink = FilesystemEvidenceSink(
        root=tmp_path, rules=RULES, declared=DECLARED,
        known_values={MEMBER_ID: Sensitivity.PERSONAL}, stream_name="handoff.jsonl",
    )
    arm(target_app, "reset")

    with browser_session(target_app, headless=HEADLESS) as surface:
        sign_on(surface, target_app)
        operator = ScriptedOperator(surface, target_app, rescue=operator_rescues)
        arm(target_app, "expire-session")

        engine = ReplayCapability(
            surface=surface,
            policy=Policy(
                allowed_origins=(target_app,),
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
        result = engine.run(capability, {"member_id": MEMBER_ID}, run_id="handoff")

    stream = tmp_path / "handoff" / "handoff.jsonl"
    events = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    return result, operator, events


@pytest.fixture(scope="module")
def rescued(target_app: str, tmp_path_factory):
    return replay_with(target_app, True, tmp_path_factory.mktemp("rescued"))


# ---- the gate ---------------------------------------------------------------


def test_the_run_completes_after_a_person_signs_back_on(rescued):
    """The whole point. A session died, somebody fixed it, the run finished."""
    result, _, _ = rescued

    assert result.outcome is Outcome.SUCCESS
    assert result.outputs == {
        "savings_balance": "4820.55",
        "account_name": "Test Member One",
    }


def test_the_sequence_is_all_there(rescued):
    """Read in order, the record is the story of the handover."""
    _, _, events = rescued
    names = [e["event"] for e in events]

    for expected in (
        "intervention_requested",
        "handed_over",
        "human_action",
        "control_returned",
        "resume_checked",
    ):
        assert expected in names, expected

    assert names.index("intervention_requested") < names.index("handed_over")
    assert names.index("handed_over") < names.index("control_returned")
    assert names.index("control_returned") < names.index("resume_checked")
    assert names[-1] == "run_finished"


def test_the_request_carries_enough_to_act_on(rescued):
    """A person picking this up should not have to go and find anything."""
    _, operator, _ = rescued
    request = operator.requests[0]

    assert request.reason is EscalationReason.AUTHENTICATION_REQUIRED
    assert request.capability == "lookup_member_balance"
    assert request.resume_checkpoint == "authenticated_session"
    assert request.goal
    assert request.observation is not None
    summary = request.summary()
    assert summary["url_pattern"]
    assert summary["page_title"]


def test_what_the_person_did_was_recorded(rescued):
    """Automation stops looking during a handover. Without this nobody could
    say what changed underneath it."""
    _, _, events = rescued
    human = [e for e in events if e["event"] == "human_action"]

    assert human
    assert any(e["action"] == HumanAction.SENSITIVE_INPUT.value for e in human)
    assert any(e["action"] == HumanAction.NAVIGATE.value for e in human)


def test_the_password_is_nowhere_in_the_record(rescued):
    """The claim the whole design rests on, checked against the file.

    Never masked on the way out — never read. The page sends the fact that a
    password field changed and does not send what was typed into it.

    Asserted against the credential that was actually typed rather than against
    the module constant. The fixture sets the password with setdefault, so a
    real one in .env wins — and this test was searching the record for a string
    nobody had entered, which it was never going to find.
    """
    _, _, events = rescued
    written = json.dumps(events)
    typed_password = os.environ["TARGET_APP_PASSWORD"]

    assert typed_password not in written

    # No assertion on a fragment of it. The last four characters of a password
    # are ordinary English often enough — this one ends "word", and the control
    # is called "Password" — so a substring check would fail on a record that
    # leaked nothing. What matters is that the value never appears, and that
    # the field it was typed into reports as withheld rather than as empty.
    typed = [
        e
        for e in events
        if e["event"] == "human_action" and e["action"] == HumanAction.SENSITIVE_INPUT.value
    ]
    assert typed
    assert all(e["value"] == REDACTED for e in typed)


def test_the_checkpoint_was_verified_rather_than_assumed(rescued):
    _, _, events = rescued
    checked = next(e for e in events if e["event"] == "resume_checked")

    assert checked["checkpoint"] == "authenticated_session"
    assert checked["holds"] is True


# ---- the operator who did not fix it ----------------------------------------


def test_a_handover_that_fixed_nothing_does_not_let_the_run_continue(
    target_app: str, tmp_path
):
    """"I am finished" is a claim about the person, not about the application.

    A run that resumed on that alone would act on a screen nobody checked,
    which is the failure the resume checkpoint exists to prevent.
    """
    result, operator, events = replay_with(target_app, False, tmp_path)

    assert result.outcome is Outcome.INTERVENTION_REQUIRED
    assert "authenticated_session" in (result.detail or "")

    checked = next(e for e in events if e["event"] == "resume_checked")
    assert checked["holds"] is False
    assert operator.requests  # it did ask


def test_the_automation_cannot_act_while_a_person_holds_the_run():
    """Enforced by the state model rather than by the engine remembering.

    An engine that forgot to wait still could not act: may_act is false in
    HUMAN_CONTROL and false again in RESUMING, so control is not handed back
    by the human saying so — it is handed back by the checkpoint holding.
    """
    from cua.domain.run import Run

    run = Run.start(run_id="r", capability="c", version="1.0.0")
    assert run.may_act

    paused = run.escalate(
        EscalationReason.AUTHENTICATION_REQUIRED, resume_checkpoint="authenticated_session"
    )
    assert not paused.may_act

    held = paused.hand_over()
    assert held.state is RunState.HUMAN_CONTROL
    assert not held.may_act

    returning = held.return_control()
    assert returning.state is RunState.RESUMING
    assert not returning.may_act

    assert returning.resume(checkpoint_holds=True).may_act
    assert not returning.resume(checkpoint_holds=False).may_act


def test_an_unlabelled_password_field_is_not_named_after_its_own_value(target_app: str):
    """The hole that was invisible because this application has labels.

    The page side helper names a control by its label, then its aria-label,
    then falls back to what is in it. On a sign on form with none of the first
    three, that fallback named the control after the credential typed into it
    and sent it out as the target — past every guard downstream, because
    nothing there expects a secret to arrive in that field.
    """
    secret = "hunter2-would-have-leaked"

    with browser_session(target_app, headless=HEADLESS) as surface:
        activity = BrowserActivity(page=surface.page)
        surface.page.goto(f"{target_app}/login")
        # No label, no aria-label, no name: only a value.
        surface.page.evaluate(
            """() => {
                const box = document.createElement('input');
                box.type = 'password';
                box.id = 'bare';
                document.body.appendChild(box);
            }"""
        )
        activity.start()
        surface.page.fill("#bare", secret)
        surface.page.click("body")
        events = activity.stop()

    typed = [e for e in events if e.action is HumanAction.SENSITIVE_INPUT]

    assert typed, "the change should still be recorded"
    for event in typed:
        assert event.value == REDACTED
        assert secret not in (event.target or "")
        assert secret not in (event.value or "")


def test_what_a_person_did_survives_a_blocked_process(target_app: str):
    """The case the console operator is, and the one the design first missed.

    A binding calling back into Python records nothing while the process is
    blocked on a keypress, because nothing is pumping Playwright — so a real
    handover recorded zero actions while this suite recorded five, and the
    difference was that the suite's person drives through Playwright on the
    same thread.

    The page accumulates and is read at the end, which is why the sleep below
    changes nothing.
    """
    import time

    secret = "hunter2-would-have-leaked"

    with browser_session(target_app, headless=HEADLESS) as surface:
        activity = BrowserActivity(page=surface.page)
        surface.page.goto(f"{target_app}/login")
        activity.start()
        surface.page.fill("#ctl00_cph_txtUser", "tmiller")
        surface.page.fill("#ctl00_cph_txtPass", secret)
        surface.page.click("#ctl00_cph_btnSignOn")
        time.sleep(1.0)  # nothing is pumping Playwright here, as on input()
        events = activity.stop()

    kinds = [e.action for e in events]
    assert HumanAction.INPUT in kinds
    assert HumanAction.SENSITIVE_INPUT in kinds
    assert HumanAction.CLICK in kinds
    assert secret not in json.dumps([e.__dict__ for e in events], default=str)
