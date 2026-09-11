"""Computer-use automation: discover a capability once, then replay it.

Layer rules, enforced by tests/unit/test_layering.py:

    domain/          the standard library only. No I/O, no SDKs, no validation
                     library. Business rules and the vocabulary they use.
    ports/           imports domain only. Signatures, no behaviour.
    app/             imports domain and ports only. Use cases.
    adapters/        imports domain, ports, and whatever technology they wrap.
    composition.py   the only module permitted to import adapters, so every
                     other module is written against ports.
    cli.py           argument parsing and exit codes. No orchestration.

The dependency arrow points inward at every layer, which is what lets the same
capability replay against a live browser and a JSON fixture without changing a
line, and what keeps a model out of the replay path by construction rather than
by convention.
"""
