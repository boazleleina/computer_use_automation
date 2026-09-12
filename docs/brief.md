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
| 3.1 | Goal-driven LLM loop: goal + target in, observe/decide/act against a live surface, stop on max steps / timeout / dead end | `app/discover.py`, `adapters/claude_model.py` | done; five stopping conditions, one real run per scenario in `evidence/` |
| 3.2 | Typed, versioned, serialisable artifact: ordered steps, how each target is identified + why, typed inputs, typed outputs, checkpoint | `domain/capability.py`, `domain/artifact.py`, `domain/compiler.py` | done; now emitted by the compiler, not only hand-written |
| 3.3 | Deterministic replay, no model: stable targeting, verify checkpoint, return outputs, separate business outcome / recoverable / hard failure | `app/replay.py`, `domain/outcomes.py`, `domain/resolution.py` | done, proven on two surfaces |
| 3.4 | Allowlist of routes and action types; risky vs reversible handled conservatively; never persist secrets or raw PII | `domain/policy.py` | done; three layers — action allowlist, `may_operate` on the screen, `SECRET_CONTROL` on the control |
| 3.5 | Evidence: structured log of what it did and why, plus a richer signal on failure | `adapters/fs_evidence_sink.py` | log done and redacting; **screenshot on failure still only a port call** |
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

Done, three times over, all with claude-sonnet-5 against the live Flask app:

| run | outcome | what it shows |
|---|---|---|
| `discovery` | `goal_reached`, 5 steps | the artifact beside it replays unmodified, and for a member the run never saw |
| `not_found` | `repeated_state`, 3 steps | the loop stops when the screen stops moving |
| `signed_out` | `policy_refused`, 1 step | the credential guard firing, with the model's own rationale admitting it was guessing |

The two that fail earn their place more than the one that works. A guardrail
firing is a better record than a model happening to fail, which is what the
same scenario produced before the rule existed.

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
