"""Where capability artifacts live.

An artifact is the product: a typed, versioned, human-reviewable procedure.
Kept separate from EvidenceSink because the two have opposite lifetimes. An
artifact is edited, reviewed, approved, and executed thousands of times; a run
record is written once and never changed.

The port deals in documents, not in a parsed Capability. Parsing and schema
validation belong to the domain, so a store implementation cannot accidentally
become the thing that decides whether a capability is valid.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol


class ArtifactStore(Protocol):
    """Read and write versioned capability documents."""

    def save(self, name: str, version: int, document: Mapping[str, object]) -> str:
        """Persist a document and return an identifier for it.

        Versions are explicit and never overwritten: a capability that has been
        approved must stay byte-identical to what was approved.
        """
        ...

    def load(self, name: str, version: int) -> Mapping[str, object]:
        """Read one version back.

        Raises ArtifactNotFound when the version does not exist, rather than
        returning None, so a missing artifact cannot be mistaken for an empty
        one.
        """
        ...

    def list_versions(self, name: str) -> Sequence[int]:
        """Known versions of a capability, ascending."""
        ...
