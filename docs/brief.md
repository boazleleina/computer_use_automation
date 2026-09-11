# What this has to be when it is finished

The assignment, condensed to the things I will be marked on, with where each
one currently lives. Kept in the repo rather than in my head because the phase
plan is mine and the requirements are theirs, and when those two disagree the
requirements win.

Their through-line, which is also mine:

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

Their warning, worth writing down because it is the one I designed around:

> "no such member" is a legitimate answer the caller needs, not a crash.
> Conflating the two is the most common design mistake here.

## Section 3 — what is required

| # | Requirement | Where it is | State |
|---|---|---|---|
| 3.1 | Goal-driven LLM loop: goal + target in, observe/decide/act against a live surface, stop on max steps / timeout / dead end | `app/discover.py` | **not built** — Phase 5 |
| 3.2 | Typed, versioned, serialisable artifact: ordered steps, how each target is identified + why, typed inputs, typed outputs, checkpoint | `domain/capability.py`, `domain/artifact.py`, `tests/fixtures/member_lookup.handwritten.yaml` | done, hand-written; discovery must emit this same shape |
| 3.3 | Deterministic replay, no model: stable targeting, verify checkpoint, return outputs, separate business outcome / recoverable / hard failure | `app/replay.py`, `domain/outcomes.py`, `domain/resolution.py` | done, proven on two surfaces |
| 3.4 | Allowlist of routes and action types; risky vs reversible handled conservatively; never persist secrets or raw PII | `domain/policy.py` | done |
| 3.5 | Evidence: structured log of what it did and why, plus a richer signal on failure | `ports/evidence.py`, `_emit` / `_capture` in replay | port + call sites done; **no adapter that writes to disk** |
| 3.6 | Escalation and handoff: detect stuck, route an intervention request with context, human takes over **the same live session**, control handed back, record what they did | `domain/run.py`, `ports/operator.py` | state machine done and tested; **engine never calls `hand_over`/`resume`**, no operator adapter, browser ownership unresolved |
| 3.7 | Design (not build) for heterogeneous surfaces and multi-tenant reuse | `REPORT.md` §4 | **not written** |

## Deliverables — exact paths, they said so

- [x] public repo
- [ ] `/README.md` — setup, how to run without live services, and a **demo path**: the exact commands to run discovery on a goal then replay the artifact. Current README still says "Phase 0: nothing executes yet", which is now false.
- [ ] `/REPORT.md` — 1–3 pages, seven headings, their words exactly:
      1. Architecture 2. Artifact schema 3. Determinism & error handling
      4. Heterogeneity & multi-tenant 5. Escalation & handoff 6. Safety 7. Cuts
- [ ] `/evidence/` — the example artifact, a discovery run log, a replay run log,
      and at least one replay that hits an exceptional state

## The one thing that is not my call

> the discovery run has to be real. At least one genuine LLM-driven run against a
> live surface, with the evidence in /evidence/ to show it happened.

Everything else may be stubbed at a clean seam if I say so and say why. That one
may not. It is the only hard external dependency in the project: a model key.

## How they weight it

System design, then correctness of the core loop, then robustness and error
handling, then human-in-the-loop, then generalisation, then safety, then code
quality, then the write-up. Breadth is explicitly not rewarded; a thin-but-real
version of every requirement beats a polished subset.

So the cut line is: nothing in Section 3 gets skipped entirely. Depth gets cut,
capabilities do not.

## Stretch, pick at most one or two

Approval gating (`draft → approved`) is already half-built — `Contract.approval`
exists and `may_run_unattended` enforces it. Canonicalisation is also already
there: `route_pattern()` turns `/members/100045` into `/members/{member_id}`,
which is the parameterisation their example asks for. Both are close enough to
free that they should be claimed in the report rather than built again.
