"""Where vendor failures stop being vendor failures.

Adapters are the only place allowed to know what a Playwright timeout or an SDK
rate-limit looks like. Each one catches its own library's exceptions and raises
a domain error instead, so the translation happens once per technology rather
than being repeated at every call site above it.

AdapterError exists for the narrow case where an adapter fails before it has
enough context to name a domain error — bad wiring, a missing binary, an
unreadable fixture. It is a configuration problem, not a run outcome, and it is
deliberately not a DomainError: the domain has nothing sensible to do with it.
"""


class AdapterError(Exception):
    """An adapter could not be constructed or could not start."""


class ConfigurationError(AdapterError):
    """Wiring is wrong: a missing credential, an unreachable target, a bad path."""
