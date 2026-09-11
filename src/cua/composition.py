"""The composition root. Not wired up yet.

The one module permitted to import from adapters. It reads configuration and
constructs the six port implementations, choosing PlaywrightSurface or
ScriptedSurface, ClaudeModel or RecordedModel, and hands them to a use case.

Everything else in the system is written against ports and therefore cannot
know which implementation it received. That is what lets the same capability
run against a live browser and a JSON fixture without changing a line.

Kept as a single module on purpose. A dependency injection container would hide
the wiring, and the wiring is the first thing to check when a run behaves
differently against two surfaces.

This is the only layer that uses pydantic. config.yaml and .env are untrusted
text until something validates them, and that validation belongs here rather
than in the domain: a settings error is a wiring problem, caught once at
startup, not a business rule.
"""
