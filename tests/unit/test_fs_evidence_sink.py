"""Evidence on disk.

The assertions that matter here read the file back rather than the return
value. Redaction that works on the way out of redact_event and not on the way
into the file is not redaction, and the only way to tell those apart is to open
what landed.
"""

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from cua.adapters.fs_evidence_sink import FilesystemEvidenceSink
from cua.domain.errors import EvidenceError
from cua.domain.policy import RedactionRules, Rendering, Sensitivity
from cua.ports.evidence import EvidenceSink

RULES = RedactionRules(
    secret=Rendering.DROP,
    personal=Rendering.MASK,
    internal=Rendering.RECORD,
    unclassified=Rendering.DROP,
)

DECLARED = {
    "member_id": Sensitivity.PERSONAL,
    "operator_password": Sensitivity.SECRET,
    "step": Sensitivity.INTERNAL,
    "event": Sensitivity.INTERNAL,
    "url_pattern": Sensitivity.INTERNAL,
}

MEMBER_NUMBER = "100045"
PASSWORD = "pa55w0rd-do-not-log"


@pytest.fixture
def sink(tmp_path):
    return FilesystemEvidenceSink(root=tmp_path, rules=RULES, declared=DECLARED)


def lines(tmp_path: Path, run_id: str = "run_1") -> list[dict[str, Any]]:
    """The stream as parsed records, one per line."""
    path = tmp_path / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_it_satisfies_the_port(sink):
    """Typed slot. mypy proves the structural conformance here, not pytest."""
    wired: EvidenceSink = sink
    assert wired is sink


# ---- the stream ------------------------------------------------------------


def test_each_record_is_one_line(sink, tmp_path):
    """A reviewer follows a live run with tail -f, and a run that dies half way
    through still leaves a file that parses."""
    sink.append("run_1", {"event": "run_started"})
    sink.append("run_1", {"event": "step_started", "step": "enter_member_number"})
    sink.append("run_1", {"event": "run_finished"})

    assert [record["event"] for record in lines(tmp_path)] == [
        "run_started",
        "step_started",
        "run_finished",
    ]


def test_a_truncated_stream_still_reads(sink, tmp_path):
    """The property a single JSON array does not have.

    Written as an array, the records worth reading most — the ones from the run
    that crashed before closing the bracket — are the ones that cannot be read.
    """
    sink.append("run_1", {"event": "run_started"})
    sink.append("run_1", {"event": "step_started"})

    path = tmp_path / "run_1" / "events.jsonl"
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: len(whole) // 2], encoding="utf-8")

    survived = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            survived.append(json.loads(line))

    assert survived == [{"event": "run_started"}]


def test_two_runs_do_not_share_a_stream(sink, tmp_path):
    sink.append("run_1", {"event": "run_started"})
    sink.append("run_2", {"event": "run_started"})

    assert len(lines(tmp_path, "run_1")) == 1
    assert len(lines(tmp_path, "run_2")) == 1


# ---- redaction, read back off the disk --------------------------------------


def test_a_secret_never_reaches_the_file(sink, tmp_path):
    sink.append("run_1", {"event": "acted", "operator_password": PASSWORD})

    assert PASSWORD not in (tmp_path / "run_1" / "events.jsonl").read_text(encoding="utf-8")
    assert lines(tmp_path)[0]["operator_password"] is None


def test_personal_data_is_masked_in_the_file(sink, tmp_path):
    sink.append("run_1", {"event": "acted", "member_id": MEMBER_NUMBER})

    assert lines(tmp_path)[0]["member_id"] == "****0045"
    assert MEMBER_NUMBER not in (tmp_path / "run_1" / "events.jsonl").read_text(
        encoding="utf-8"
    )


def test_a_nested_value_is_redacted_too(sink, tmp_path):
    """A record is not flat. A member number one level down still reaches disk."""
    sink.append(
        "run_1",
        {
            "event": "run_started",
            "inputs": {"member_id": MEMBER_NUMBER, "operator_password": PASSWORD},
        },
    )

    written = (tmp_path / "run_1" / "events.jsonl").read_text(encoding="utf-8")
    assert MEMBER_NUMBER not in written
    assert PASSWORD not in written


def test_an_undeclared_field_fails_closed(sink, tmp_path):
    """Forgetting to classify a field costs evidence, not a member's data."""
    sink.append("run_1", {"event": "acted", "something_nobody_declared": MEMBER_NUMBER})

    assert lines(tmp_path)[0]["something_nobody_declared"] is None


def test_a_sink_with_no_declarations_drops_almost_everything(tmp_path):
    """The behaviour I want when somebody wires this up and forgets.

    Every field is unclassified, and unclassified drops, so a misconfigured
    sink writes a useless record rather than a dangerous one.
    """
    forgetful = FilesystemEvidenceSink(root=tmp_path, rules=RULES)
    forgetful.append("run_1", {"event": "acted", "member_id": MEMBER_NUMBER})

    assert lines(tmp_path)[0] == {"event": None, "member_id": None}


# ---- attachments ------------------------------------------------------------


def test_an_attachment_is_written_and_referenced(sink, tmp_path):
    reference = sink.attach("run_1", "submit_lookup.png", b"\x89PNG-not-really")

    assert reference == "evidence://run_1/submit_lookup.png"
    assert (tmp_path / "run_1" / "blobs" / "submit_lookup.png").read_bytes() == (
        b"\x89PNG-not-really"
    )


def test_a_reference_carries_no_filesystem_layout(sink):
    """Nothing upstream should learn where this adapter puts things."""
    reference = sink.attach("run_1", "shot.png", b"x")

    assert "blobs" not in reference
    assert reference.startswith("evidence://")


def test_reattaching_the_same_name_overwrites(sink, tmp_path):
    """A second screenshot under one name is the same step retried. Keeping
    both would mean inventing a numbering scheme to say which was last."""
    sink.attach("run_1", "shot.png", b"first")
    sink.attach("run_1", "shot.png", b"second")

    assert (tmp_path / "run_1" / "blobs" / "shot.png").read_bytes() == b"second"


# ---- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["../escaped", "run/1", "..", "", "run 1"])
def test_a_run_id_that_is_not_one_segment_is_refused(sink, run_id):
    """The artifact store had this hole and it was not theoretical there.

    Checked rather than sanitised: quietly rewriting the name would put the
    record somewhere the caller did not ask for and leave nothing saying so.
    """
    with pytest.raises(EvidenceError):
        sink.append(run_id, {"event": "run_started"})


@pytest.mark.parametrize("name", ["../escaped.png", "blobs/../../x", "", "a b.png"])
def test_an_attachment_name_that_is_not_one_segment_is_refused(sink, name):
    """Blob names are built from step ids, which come from artifact files."""
    with pytest.raises(EvidenceError):
        sink.attach("run_1", name, b"x")


def test_nothing_is_written_outside_the_root(sink, tmp_path):
    """The assertion behind the two above."""
    with pytest.raises(EvidenceError):
        sink.append("../escaped", {"event": "run_started"})

    assert list(tmp_path.parent.glob("escaped*")) == []


def test_a_closed_sink_refuses_to_write(sink):
    with pytest.raises(EvidenceError):
        sink.close()
        sink.append("run_1", {"event": "run_started"})


def test_close_is_safe_to_call_twice(sink):
    sink.close()
    sink.close()


# ---- durability -------------------------------------------------------------


def test_a_record_is_on_disk_when_append_returns(sink, tmp_path):
    """No held handle, so a run that dies leaves what it already wrote.

    Read without closing the sink, which is the whole point.
    """
    sink.append("run_1", {"event": "run_started"})

    assert lines(tmp_path) == [{"event": "run_started"}]


def test_a_value_json_cannot_encode_does_not_take_the_run_down(sink, tmp_path):
    """An evidence writer that throws destroys the run it was describing."""

    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    sink.append("run_1", {"event": "acted", "url_pattern": Opaque()})

    assert lines(tmp_path)[0]["url_pattern"] == "opaque"
