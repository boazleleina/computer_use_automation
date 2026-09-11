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


class OperatorError(DomainError):
    """Control could not be handed to a human, or was never returned."""


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

    def __init__(self, name: str, version: int) -> None:
        super().__init__(f"no artifact {name!r} at version {version}")
        self.name = name
        self.version = version


class PolicyDenied(DomainError):
    """A proposed action was refused.

    The reason is required. A denial with no stated cause cannot be written
    into evidence as an account of why a run stopped.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TimeoutExceeded(DomainError):
    """A budgeted operation ran past its deadline.

    Carries the budget as a number rather than only in the message, because the
    value is written to evidence and read back by tooling.
    """

    def __init__(self, what: str, budget_ms: int) -> None:
        super().__init__(f"{what} exceeded {budget_ms}ms")
        self.what = what
        self.budget_ms = budget_ms
