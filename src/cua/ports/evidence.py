"""Append-only proof of what a run did.

The single redaction chokepoint. Every value that leaves the system for durable
storage passes through one implementation of this port, which is what makes
"no credentials in evidence" a property that can be tested in one place instead
of audited at every call site.

Callers therefore do not redact. If redaction were the caller's job it would be
correct in most call sites and wrong in one, and the wrong one is the leak.
"""

from collections.abc import Mapping
from typing import Protocol


class EvidenceSink(Protocol):
    """Record what happened, redacting as it writes."""

    def append(self, run_id: str, event: Mapping[str, object]) -> None:
        """Append one record to the run's evidence stream.

        Implementations redact sensitive values before writing. The event is a
        plain mapping rather than a closed type because the record shape grows
        across phases, and evidence is read by humans and tools rather than
        being parsed back into domain objects.
        """
        ...

    def attach(self, run_id: str, name: str, payload: bytes) -> str:
        """Store a binary artifact — a screenshot, a trace — and return a
        reference that can be embedded in a later event.

        Returning a reference rather than a path keeps filesystem layout out of
        every other layer.
        """
        ...

    def close(self) -> None:
        """Flush and release. Must be safe to call twice."""
        ...
