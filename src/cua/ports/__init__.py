"""The interfaces the domain declares, phrased in domain types.

Six ports, each justified by an external side effect, a meaningful substitution
boundary, or a test seam that materially changes what can be tested:

    Surface          perceive and act on a screen
    Model            propose the next step during discovery
    ArtifactStore    read and write capability artifacts
    EvidenceSink     append run records; the single redaction chokepoint
    OperatorChannel  hand control to a human and take it back
    Clock            time and deadlines, so tests never sleep

Signatures only. No behaviour, no defaults, no I/O.

This package imports from domain and from the standard library, and from nothing
else. In particular it must not name a driver or an SDK type in a signature: a
vendor type in a port is a vendor dependency in every module that calls it.

ReplayCapability depends on every port except Model. Replay therefore cannot
reach a model even by mistake, because there is nothing wired in for it to
reach.
"""
