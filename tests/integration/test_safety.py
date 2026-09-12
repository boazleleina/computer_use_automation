"""The safety claims, asserted rather than described.

Three things this system says about itself, each of which would be a paragraph
in a design document and is instead a test that fails when it stops being true:

    a declared value never reaches the evidence stream
    an action outside the allowlist is refused before the surface is touched
    nothing shipped in this repository carries a fixture identifier

The third one greps. That is not elegant and it is the point: the claim is
about files on disk, so the test reads files on disk. A structural check would
prove the paths I thought of are clean.
"""

import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from werkzeug.serving import make_server

from cua.adapters.clocks import FakeClock, RealClock
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink
from cua.adapters.playwright_surface import browser_session
from cua.adapters.scripted_surface import ScriptedSurface
from cua.app.replay import ReplayCapability
from cua.domain.actions import ActionType, Effect
from cua.domain.artifact import capability_from_document
from cua.domain.outcomes import Outcome
from cua.domain.policy import (
    DeniedControl,
    Policy,
    PolicyRule,
    RedactionRules,
    Rendering,
    Sensitivity,
)
from tests.integration.test_replay_scripted import ARTIFACT, MEMBER_ID

REPOSITORY = Path(__file__).resolve().parents[2]

# The fixtures this repository uses. Every one is invented, and none of them
# should survive into a file that gets committed.
FIXTURE_IDENTIFIERS = ("100045", "100046", "100047", "100099")

# Where a leak would actually matter: run records and stored capabilities, read
# by somebody other than me and produced by machinery rather than by hand.
#
# Deliberately not tests/fixtures/observations. Those are captured screens of a
# member detail page, and a recording of that page contains that member's
# number because that is what the page says — scrubbing it would leave a
# fixture that no longer describes the application it was taken from, and every
# resolution test would then be passing against a screen that does not exist.
# The fixtures are invented data by construction; the point of this check is
# that nothing *derived* from them keeps a concrete value.
SHIPPED = ("evidence", "capabilities")

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)


@pytest.fixture(scope="module")
def target_app() -> Iterator[str]:
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    os.environ.setdefault("TARGET_APP_PASSWORD", "not-a-real-password")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def capability():
    return capability_from_document(yaml.safe_load(Path(ARTIFACT).read_text(encoding="utf-8")))


# ---- a declared value never reaches evidence --------------------------------


def test_a_personal_input_never_reaches_the_evidence_stream(
    capability, target_app: str, tmp_path
):
    """End to end, against the real browser, reading the file back off disk.

    Not a unit test of redact_event — that exists and proves the function. This
    proves the wiring: a member number goes in as an input, the run types it,
    reads a screen full of it, writes forty odd records, and the number is not
    in the file afterwards.
    """
    sink = FilesystemEvidenceSink(
        root=tmp_path,
        rules=RULES,
        declared={
            "event": Sensitivity.INTERNAL,
            "step": Sensitivity.INTERNAL,
            "action": Sensitivity.INTERNAL,
            "url_pattern": Sensitivity.INTERNAL,
            "value": Sensitivity.PERSONAL,
        },
        known_values={MEMBER_ID: Sensitivity.PERSONAL},
    )
    policy = Policy(
        allowed_origins=(target_app,),
        allowed_routes=("/login", "/search", "/members/{member_id}"),
        allowed_actions=frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.READ}),
        denied_controls=(DeniedControl(role="link", name="Sign Off"),),
    )

    with browser_session(target_app, headless=True) as surface:
        surface.page.goto(f"{target_app}/login")
        surface.page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
        surface.page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
        surface.page.click("#ctl00_cph_btnSignOn")
        surface.page.wait_for_url(f"{target_app}/search")
        result = ReplayCapability(
            surface=surface, policy=policy, clock=RealClock(), evidence=sink
        ).run(capability, {"member_id": MEMBER_ID}, run_id="pii")

    assert result.outcome is Outcome.SUCCESS  # it really did the whole flow

    written = (tmp_path / "pii" / "events.jsonl").read_text(encoding="utf-8")
    assert MEMBER_ID not in written
    assert os.environ["TARGET_APP_PASSWORD"] not in written

    # And the record is still worth having: masked, not emptied.
    assert "****0045" in written


# ---- an action outside the allowlist is refused before it happens -----------


def test_an_action_outside_the_allowlist_never_reaches_the_surface(capability, search_page):
    """Refused before execution, and the proof is that nothing was executed.

    A surface that recorded the action and a policy that refused it afterwards
    would produce the same Result and a different application. The assertion
    that separates them is `acted == []`.
    """
    surface = ScriptedSurface([search_page])
    policy = Policy(
        allowed_origins=("http://127.0.0.1:5000",),
        allowed_routes=("/search",),
        # type is what the first step needs, and it is not here.
        allowed_actions=frozenset({ActionType.CLICK, ActionType.READ}),
        denied_controls=(),
    )

    result = ReplayCapability(surface=surface, policy=policy, clock=FakeClock()).run(
        capability, {"member_id": MEMBER_ID}, run_id="refused"
    )

    assert result.outcome is Outcome.HARD_FAILURE
    assert surface.acted == []


def test_a_denied_control_is_never_touched_either(capability, search_page):
    """The other layer, proven the same way."""
    surface = ScriptedSurface([search_page])
    policy = Policy(
        allowed_origins=("http://127.0.0.1:5000",),
        allowed_routes=("/search",),
        allowed_actions=frozenset(ActionType),
        denied_controls=(DeniedControl(role="textbox", name="Member Number"),),
    )

    result = ReplayCapability(surface=surface, policy=policy, clock=FakeClock()).run(
        capability, {"member_id": MEMBER_ID}, run_id="denied"
    )

    assert result.outcome is Outcome.HARD_FAILURE
    assert surface.acted == []


def test_an_irreversible_capability_is_not_waved_through_by_approval(capability, search_page):
    """Approval is not confirmation, and unattended is not confirmed.

    With no operator channel there is nobody to confirm anything, so the run is
    refused before it starts — an absent operator is not an operator who agreed.
    """
    from dataclasses import replace

    from cua.domain.capability import Approval

    dangerous = replace(
        capability,
        contract=replace(
            capability.contract, effect=Effect.IRREVERSIBLE, approval=Approval.APPROVED
        ),
    )
    surface = ScriptedSurface([search_page])

    result = ReplayCapability(
        surface=surface,
        policy=Policy(
            allowed_origins=("http://127.0.0.1:5000",),
            allowed_routes=("/search",),
            allowed_actions=frozenset(ActionType),
            denied_controls=(),
        ),
        clock=FakeClock(),
    ).run(dangerous, {"member_id": MEMBER_ID}, run_id="irreversible")

    assert result.outcome is Outcome.HARD_FAILURE
    assert result.observed == PolicyRule.CONFIRMATION_REQUIRED.value
    assert surface.acted == []


# ---- nothing shipped carries a fixture identifier ---------------------------


def shipped_files() -> list[Path]:
    """Everything under the shipped directories that git is tracking."""
    tracked = subprocess.run(
        ["git", "ls-files", *SHIPPED],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    return [REPOSITORY / line for line in tracked.stdout.splitlines() if line]


def test_no_shipped_file_carries_a_fixture_identifier():
    """The claim, checked the way somebody reviewing this would check it.

    A grep, because the claim is about files on disk. Walking the objects that
    produced them would prove the paths I thought of are clean, and the ones
    that have leaked so far — a step id, a page title inside a condition, a
    model prompt — were all paths nobody had thought of.
    """
    offenders: dict[str, list[str]] = {}
    for path in shipped_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = [value for value in FIXTURE_IDENTIFIERS if value in text]
        if found:
            offenders[str(path.relative_to(REPOSITORY))] = found

    assert not offenders, f"fixture identifiers found in shipped files: {offenders}"


def test_no_shipped_file_carries_the_operator_password():
    """The obvious one, and the one the whole handover design is built around."""
    password = os.environ.get("TARGET_APP_PASSWORD") or "not-a-real-password"

    for path in shipped_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert password not in text, f"{path} carries the operator password"


def test_the_capability_artifacts_are_parameterised_not_recorded():
    """Every committed artifact, not only the one a test happens to load."""
    pattern = re.compile("|".join(FIXTURE_IDENTIFIERS))

    for path in REPOSITORY.glob("**/*.yaml"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or "contract" not in document:
            continue
        serialised = json.dumps(document)
        assert not pattern.search(serialised), f"{path} hard codes a fixture identifier"


def test_two_conditions_that_disagree_about_a_screen_are_refused(search_page):
    """Severity decides between outcomes; it cannot decide between codes.

    Two conditions of equal severity reach the same outcome by two routes,
    which is harmless while they agree on the code — the only part of this a
    caller branches on. When they disagree, taking the higher one by list order
    publishes a contract decided by where somebody put a line in a YAML file.

    resolve() refuses an ambiguous control rather than picking one. This used
    to pick one.
    """
    from cua.domain.conditions import Condition, Detector, DetectorKind
    from cua.domain.outcomes import classify

    def describing(name: str, code: str) -> Condition:
        return Condition(
            name=name,
            outcome=Outcome.BUSINESS_OUTCOME,
            detectors=(Detector(kind=DetectorKind.URL_PATTERN, url_pattern="/search"),),
            detail=f"{name} says so",
            code=code,
        )

    verdict = classify(
        search_page, (describing("first", "ONE"), describing("second", "TWO"))
    )

    assert verdict.outcome is Outcome.INTERVENTION_REQUIRED
    assert verdict.condition_name is None
    assert verdict.code is None
    assert "first" in verdict.detail and "second" in verdict.detail


def test_two_conditions_that_agree_are_not_ambiguous(search_page):
    """The tie that does not matter. Same outcome, same code, different label:
    a caller cannot tell the difference, so there is nothing to refuse."""
    from cua.domain.conditions import Condition, Detector, DetectorKind
    from cua.domain.outcomes import classify

    def describing(name: str) -> Condition:
        return Condition(
            name=name,
            outcome=Outcome.SUCCESS,
            detectors=(Detector(kind=DetectorKind.URL_PATTERN, url_pattern="/search"),),
            detail=f"{name} says so",
        )

    verdict = classify(search_page, (describing("first"), describing("second")))

    assert verdict.outcome is Outcome.SUCCESS
    assert verdict.condition_name == "first"


def test_an_operator_confirms_an_invocation_not_a_procedure(capability, search_page):
    """What the person is shown decides whether confirmation means anything.

    Unbound, the request reads "look up member {{ inputs.member_id }}" — the
    procedure, which is what approval already covered. Bound, it names the
    member somebody is about to do something irreversible to.
    """
    from dataclasses import replace

    from cua.domain.capability import Approval
    from cua.domain.intervention import Handover, InterventionRequest

    seen: list[InterventionRequest] = []

    class Confirming:
        def request_intervention(self, request: InterventionRequest) -> None:
            seen.append(request)

        def await_release(self, run_id: str) -> Handover:
            return Handover()

    dangerous = replace(
        capability,
        contract=replace(
            capability.contract,
            effect=Effect.IRREVERSIBLE,
            approval=Approval.APPROVED,
            goal="Close the sub-account for member {{ inputs.member_id }}.",
        ),
    )

    ReplayCapability(
        surface=ScriptedSurface([search_page]),
        policy=Policy(
            allowed_origins=("http://127.0.0.1:5000",),
            allowed_routes=("/search",),
            allowed_actions=frozenset(ActionType),
            denied_controls=(),
        ),
        clock=FakeClock(),
        operator=Confirming(),
    ).run(dangerous, {"member_id": MEMBER_ID}, run_id="confirm")

    assert seen, "an irreversible capability must ask before it starts"
    assert MEMBER_ID in seen[0].goal
    assert "{{" not in seen[0].goal
