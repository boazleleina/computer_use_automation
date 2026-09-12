"""Replay the compiled capability twice, and keep both records.

    .venv/bin/python scripts/replay_demo.py

Once against a member who exists and once against one who does not. The second
is the more useful record: it is the difference between a system that reports
"no such member" as an answer and one that reports it as a crash, which is the
distinction the whole error taxonomy is built around.

No model is constructed here and none can be reached. That is the point of the
split, and it is why this runs without a key while scripts/discover.py does not.

A person signs the session on before either run, exactly as they would in
production. The script stands in for that person because a test cannot have one.
"""

import os
import sys
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path

import yaml
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

load_dotenv(find_dotenv(usecwd=True))

from cua.adapters.clocks import RealClock  # noqa: E402
from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink  # noqa: E402
from cua.adapters.playwright_surface import browser_session  # noqa: E402
from cua.app.replay import ReplayCapability  # noqa: E402
from cua.composition import load_settings, policy_from, redaction_from  # noqa: E402
from cua.domain.artifact import capability_from_document  # noqa: E402
from cua.domain.capability import Approval  # noqa: E402
from cua.domain.policy import Policy, Sensitivity  # noqa: E402

EVIDENCE = Path("evidence")

COMPILED = Path("evidence/discovery/capability.yaml")
REVIEWED = Path("tests/fixtures/member_lookup.handwritten.yaml")

# Four runs. The second is the parameterisation claim made as a record rather
# than a sentence; the last two are the reason for the other two.
#
# A compiled artifact only carries conditions for the screens the discovery run
# actually passed through, because a run that reached its goal never met a
# member who did not exist. Replayed against one who does not, it therefore
# reports a hard failure: the flow went somewhere it had no description for.
#
# The reviewed artifact has the condition a person added, and reports the same
# screen as a business outcome with a code the caller can act on. That
# difference is what review is for, and it is more convincing as two records
# side by side than as a paragraph.
RUNS = (
    ("replay_success", COMPILED, "100045", "compiled artifact, member exists"),
    (
        "replay_success_other_member",
        COMPILED,
        "100046",
        "the same artifact, a member the discovery run never saw",
    ),
    ("replay_not_found", COMPILED, "100099", "compiled artifact, member does not exist"),
    (
        "replay_not_found_reviewed",
        REVIEWED,
        "100099",
        "reviewed artifact, same screen, and a person had added the condition",
    ),
)

DECLARED = {
    field: Sensitivity.INTERNAL
    for field in (
        "event", "at", "capability", "version", "effect", "approval", "step", "action",
        "condition", "code", "outcome", "matched", "held", "intent", "via_signal",
        "signal_index", "allowed", "rule", "reason", "detail", "url_pattern",
        "page_title", "run_state", "expected", "observed", "into", "evidence_ref",
        "attempt", "resolved_via", "artifact_approval", "source",
    )
} | {"value": Sensitivity.PERSONAL}


def serve() -> tuple[str, object]:
    os.environ.setdefault("TARGET_APP_USER", "tmiller")
    if not os.environ.get("TARGET_APP_PASSWORD"):
        raise SystemExit("TARGET_APP_PASSWORD is not set. See .env.example.")

    from target_app.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def reset(base_url: str) -> None:
    """Clear any fault lever, so each run starts from a known application."""
    request = urllib.request.Request(
        f"{base_url}/_test/reset", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def main() -> int:
    base_url, server = serve()
    settings = load_settings()
    configured = policy_from(settings)
    policy = Policy(
        allowed_origins=(base_url,),  # the port is chosen at run time
        allowed_routes=configured.allowed_routes,
        allowed_actions=configured.allowed_actions,
        denied_controls=configured.denied_controls,
    )

    try:
        for run_id, artifact, member_id, description in RUNS:
            capability = capability_from_document(
                yaml.safe_load(artifact.read_text(encoding="utf-8"))
            )
            directory = EVIDENCE / run_id
            if directory.exists():
                for existing in directory.rglob("*"):
                    if existing.is_file():
                        existing.unlink()

            sink = FilesystemEvidenceSink(
                root=EVIDENCE,
                rules=redaction_from(settings),
                declared=DECLARED,
                known_values={
                    member_id: Sensitivity.PERSONAL,
                    os.environ["TARGET_APP_USER"]: Sensitivity.PERSONAL,
                },
                stream_name=f"{run_id}.jsonl",
            )

            # Proceeding as though a reviewer had approved this version. The
            # compiler marks a discovered artifact draft on purpose and a draft
            # does not run unattended, so a demonstration has to assert it —
            # but the record says so rather than implying somebody read it.
            sink.append(
                run_id,
                {
                    "event": "approval_assumed",
                    "artifact_approval": capability.contract.approval.value,
                    "source": "scripts/replay_demo.py",
                    "detail": (
                        "no review record was consulted; a reviewer did not "
                        "approve this version"
                    ),
                },
            )
            approved = replace(
                capability,
                contract=replace(capability.contract, approval=Approval.APPROVED),
            )

            reset(base_url)
            with browser_session(base_url, headless=True) as surface:
                surface.page.goto(f"{base_url}/login")
                surface.page.fill("#ctl00_cph_txtUser", os.environ["TARGET_APP_USER"])
                surface.page.fill("#ctl00_cph_txtPass", os.environ["TARGET_APP_PASSWORD"])
                surface.page.click("#ctl00_cph_btnSignOn")
                surface.page.wait_for_url(f"{base_url}/search")

                result = ReplayCapability(
                    surface=surface, policy=policy, clock=RealClock(), evidence=sink
                ).run(approved, {"member_id": member_id}, run_id=run_id)

            print(f"\n{run_id} — {description}")
            print(f"  artifact: {artifact}")
            print(f"  outcome : {result.outcome.value}")
            print(f"  code    : {result.code}")
            print(f"  detail  : {(result.detail or '').strip()}")
            if result.outputs:
                for name, value in result.outputs.items():
                    print(f"  {name} = {value}")
            print(f"  wrote {EVIDENCE / run_id}/{run_id}.jsonl")

            written = (EVIDENCE / run_id / f"{run_id}.jsonl").read_text(encoding="utf-8")
            print(f"  member number in the record: {member_id in written}")
            print(f"  records: {len(written.splitlines())}")
    finally:
        server.shutdown()  # type: ignore[attr-defined]

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
