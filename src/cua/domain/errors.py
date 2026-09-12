"""Domain error taxonomy.

Every failure the domain can express, and nothing else. An adapter catches a
vendor exception and raises one of these instead, so a use case only ever
handles errors it can reason about and never learns which driver was used.

These are exceptions, not data. Outcomes that the business considers legitimate
— a member not found, a validation rejection — are classified results, not
errors, and live in the outcome taxonomy. Confusing the two is how a system ends
up reporting "system error" when the honest answer was "no such member".
"""


class DomainError(Exception):
    """Root of the taxonomy.

    Lets the CLI catch every failure this system defined, and only those, so
    an unexpected exception still produces a traceback instead of being
    swallowed as if it were expected.
    """


class SurfaceError(DomainError):
    """The screen could not be read, or an action could not be performed."""


class ModelError(DomainError):
    """The model failed, or produced output that is not a valid proposal."""


class EvidenceError(DomainError):
    """A run record or attachment could not be written."""



class MalformedArtifact(DomainError):
    """An artifact document does not describe a capability.

    An authoring mistake: an action nobody implements, a detector kind that does
    not exist, a step with no id. It fails here, at load, rather than part way
    through a run against a live application.
    """


class UnsafeCapability(DomainError):
    """A capability that parses, would run, and must not exist.

    Distinct from a malformed artifact. An artifact with a step reading into an
    output nobody declared is inconsistent, and it fails loudly the first time
    anyone runs it. This is the other kind: well formed, internally coherent,
    and describing something the system has structurally decided not to do.
    """


class InvalidTransition(DomainError):
    """A run was asked to move to a state it cannot reach from where it is.

    Always a bug in the caller rather than something the application did, which
    is why it raises instead of becoming an outcome.
    """


class ArtifactNotFound(DomainError):
    """No stored capability matches the requested name and version."""

    def __init__(self, name: str, version: str) -> None:
        super().__init__(f"no artifact {name!r} at version {version}")
        self.name = name
        self.version = version

