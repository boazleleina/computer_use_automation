"""Run records on disk, as JSON lines.

The redaction chokepoint. Every value that reaches durable storage passes
through append() here, which is what makes "no credential ever landed in
evidence" a thing one test can establish rather than a claim about every call
site in the engine. Callers hand over what happened and do not redact; if
redaction were their job it would be right in thirteen places and wrong in the
fourteenth, and the fourteenth is the leak.

JSON Lines rather than one JSON document. A run that dies half way through
still leaves a readable file, and a run in progress can be followed with `tail
-f`. A single array would have to be closed to parse, which means the records
worth reading most — the ones from the run that crashed — would be the ones
that cannot be read.

Layout, one directory per run:

    evidence/<run_id>/events.jsonl      the stream
    evidence/<run_id>/blobs/<name>      screenshots, dom dumps

attach() hands back `evidence://<run_id>/<name>` rather than a path, so nothing
upstream learns where this adapter puts things. It resolves to the blobs
directory above.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cua.domain.errors import EvidenceError
from cua.domain.policy import RedactionRules, Sensitivity, redact_event

STREAM = "events.jsonl"
BLOBS = "blobs"
REFERENCE_SCHEME = "evidence://"

# A run id and a blob name both become path segments. Without this a run called
# "../.." writes its stream over whatever is above the evidence directory, and
# a step id carried into a screenshot filename does the same. The artifact
# store had exactly this hole and it was not theoretical there either.
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# Dots are legal inside a segment — events.jsonl, submit_lookup.png — so the
# pattern above has to allow them, and that lets "." and ".." through as whole
# segments. Both name a directory rather than a file in it, and ".." names the
# one above the root. Spelt out because a character class cannot say it.
TRAVERSAL = frozenset({".", ".."})


@dataclass
class FilesystemEvidenceSink:
    """One evidence directory, redacting as it writes.

    `declared` is the sensitivity map from the capability being run, so this
    sink renders values the way that capability said they should be rendered. A
    sink built without one still works and drops almost everything, because
    unclassified fails closed — which is the behaviour I want when somebody
    wires this up and forgets.
    """

    root: Path
    rules: RedactionRules
    declared: Mapping[str, Sensitivity] = field(default_factory=dict)

    # Literals this run is known to handle, by class — the member number it was
    # asked about, say. Field-name rendering cannot catch those everywhere they
    # turn up, because most records do not carry them in a classified field: a
    # click records no value, so a rationale in that record naming the member
    # has nothing in the record to be checked against. The run knows them from
    # the moment it is given them, so it says so once here.
    known_values: Mapping[str, Sensitivity] = field(default_factory=dict)

    stream_name: str = STREAM

    _closed: bool = field(default=False, init=False)

    def append(self, run_id: str, event: Mapping[str, object]) -> None:
        """Write one record, redacted.

        Opened and closed per record rather than held open. Forty events in a
        discovery run is not a throughput problem, and a handle that is never
        flushed is how a run that crashed leaves an empty file.
        """
        self._require_open()
        redacted = redact_event(event, self.declared, self.rules, self.known_values)
        line = json.dumps(redacted, default=_unserialisable, sort_keys=False)

        path = self._run_dir(run_id) / _segment(self.stream_name, "stream name")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as error:
            raise EvidenceError(f"could not append to {path}: {error}") from error

    def attach(self, run_id: str, name: str, payload: bytes) -> str:
        """Store a blob and return a reference to it.

        Overwrites, unlike the artifact store. A second screenshot under the
        same name is the same step being retried, and keeping both would mean
        inventing a numbering scheme to say which was last.
        """
        self._require_open()
        safe = _segment(name, "attachment name")
        path = self._run_dir(run_id) / BLOBS / safe
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError as error:
            raise EvidenceError(f"could not write {path}: {error}") from error
        return f"{REFERENCE_SCHEME}{_segment(run_id, 'run id')}/{safe}"

    def close(self) -> None:
        """Nothing to flush: every record was already on disk when it returned.

        Kept because the port declares it, and because a sink that buffered
        would need it. Safe to call twice.
        """
        self._closed = True

    def _run_dir(self, run_id: str) -> Path:
        return self.root / _segment(run_id, "run id")

    def _require_open(self) -> None:
        if self._closed:
            raise EvidenceError("the evidence sink is closed")


def _segment(value: str, label: str) -> str:
    """One path segment, or a refusal.

    Checked rather than sanitised. Quietly rewriting "../escape" into something
    safe would put the record somewhere the caller did not ask for and leave
    nothing saying so.
    """
    if value in TRAVERSAL or not SAFE_SEGMENT.match(value):
        raise EvidenceError(
            f"{label} {value!r} is not a single path segment of letters, "
            "digits, dot, dash or underscore"
        )
    return value


def _unserialisable(value: object) -> str:
    """Last resort for a value json does not know.

    Reached only after redaction, so a field that survived to here was declared
    internal or personal and has already been rendered accordingly. Stringifying
    is better than raising: an evidence writer that throws takes down the run it
    was supposed to be describing.
    """
    return str(value)
