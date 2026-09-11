"""Pure domain layer.

The vocabulary of the system and the rules that operate on it: what a screen
looks like, what may be done to it, what counts as a legitimate outcome. No I/O,
no waiting, no network.

Dependency rule: the standard library only. Not a driver, not an SDK, not a
validation library. That is stricter than it needs to be for correctness, and it
is deliberate. A domain with no third party dependency can be tested with
nothing installed, which is what keeps the suite running in under a second.

Enforced mechanically by tests/unit/test_layering.py.
"""
