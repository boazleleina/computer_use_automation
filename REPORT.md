# Design write-up

## 1. Architecture

The system uses a **hexagonal architecture** — ports and adapters — with the
dependency arrow pointing inward, enforced by test. The `domain/` package
imports the standard library and nothing else. CI verifies this by importing
every domain module into a virtual environment with no packages installed;
running the test suite would prove nothing, since the suite runs in an
environment where every dependency is present.

Six ports are declared: `Surface`, `Model`, `ArtifactStore`, `EvidenceSink`,
`OperatorChannel`, `Clock`. All are `Protocol` rather than ABC, so an adapter
never imports a domain base class and cannot inherit behaviour from one.

**`Model` is a port; `Policy` deliberately is not.** A model is an external
dependency with several plausible implementations, and a substitutable fake is
what makes the decision loop testable without a network. A policy is a safety
rule. Making it substitutable would allow a permissive implementation to be
wired in by accident, and there is no second implementation worth having. The
rules are already testable as a pure function over frozen values.

The two use cases in `app/` — `DiscoverCapability` and `ReplayCapability` —
are the same five phases with a single substitution: observe, decide, check,
act, verify. Replay reads its decision from an artifact; discovery asks a model.
Everything downstream of the decision is shared code, which makes the claim that
both halves are held to identical rules verifiable rather than asserted.

Perception is taken from the **accessibility tree rather than the DOM**:
`Accessibility.getFullAXTree` for structure, `DOM.getBoxModel` for geometry.
Native handles never leave the surface adapter. A `NodeRef` is opaque and scoped
to a single observation, and the surface rejects a handle to a screen that has
since been replaced rather than relying on the calling loop to remember. The
same choice underwrites the desktop case, since a desktop exposes an
accessibility tree as well.

**Trade-off.** The accessibility tree sees less than the DOM. It cannot see a
control with no accessible name, and the target application contains one: the
sign-on button appears in the handover record as `ctl00$cph$btnSignOn` because
it carries no label. A DOM-based surface would see more and break more often.
The narrower view was taken deliberately, and its cost is visible in the
evidence.

## 2. Artifact schema

An artifact is a **contract plus a procedure**. The two have different
audiences: the contract is what a calling agent reads — typed inputs with
validation patterns, typed outputs, an effect class, an approval state and
provenance — while the steps are implementation the caller never sees.

```yaml
contract:
  id: lookup_member_balance
  version: 1.0.0
  effect: read_only          # read_only | mutating | irreversible
  approval: draft            # draft | approved
  inputs:  [{name: member_id, type: string, pattern: "^[0-9]{6}$",
             sensitivity: personal}]
  outputs: [{name: savings_balance, sensitivity: personal}, ...]

steps:
  - id: 04_read_savings_balance
    action: read
    reads_into: savings_balance
    target:
      intent: Value beside the 'Savings Balance' row header
      rationale: >
        The cell is named by its own contents, so it cannot be found by name
        without already knowing the answer...
      signals:
        - {kind: anchor, role: rowheader, name: Savings Balance,
           relation: next_sibling, confidence: high}
    checkpoint:
      detectors: [{kind: node_present, role: rowheader, name: Savings Balance}]

conditions:  # what a screen means, when it is not the happy path
  - {name: member_not_found, outcome: business_outcome, code: MEMBER_NOT_FOUND, ...}
```

Four decisions shaped it.

**Targets are ranked bundles of signals, not selectors.** The order is
`ROLE_NAME > LABEL > ANCHOR > GEOMETRY > WEB_CSS`, and the signal that actually
resolved is recorded per step. A capability whose third step has begun resolving
on its second-choice signal is degrading, and that appears in the run record
before it becomes a failure. The CSS kind is namespaced `web.css` rather than
`css` because it has no meaning on a desktop surface; naming it accurately is
what keeps the portability claim honest.

**ANCHOR exists because the schema could not otherwise express extraction.** A
value cell is named by its own contents — the balance cell's accessible name is
`4820.55` — so it cannot be located by name without already knowing the answer.
The stable property is its adjacency to the row header beside it.

**`rationale` is required on every target.** It costs one line per step and is
the difference between an artifact a reviewer can approve and one they can only
accept.

**A secret cannot be declared as an interface value.** `Contract.__post_init__`
raises `UnsafeCapability` on a secret input or output. This is structural rather
than conventional: a capability able to declare a password is one that replay
would bind and type, which is precisely what the handover design exists to
prevent.

## 3. Determinism & error handling

Replay constructs no model and cannot reach one. `ReplayCapability` has no
`Model` in its dependency graph, so "no model in the decision loop" is a
property of the wiring rather than a rule requiring discipline to maintain.

Each step runs: observe, classify, resolve, check policy, act, **observe
again**. The second observation is load-bearing. An earlier version settled only
before acting, and consequently reported a member who does not exist as a failed
checkpoint rather than as the answer.

Three outcome classes are kept distinct:

| Class | Meaning |
|---|---|
| **business outcome** | the run worked and the answer is no. `MEMBER_NOT_FOUND` is a result, not a crash. |
| **recoverable** | a known interstitial, dismissed and retried, bounded per step. |
| **hard failure** | stop and report which step, what was expected, what was observed. |

`intervention_required` covers anything requiring a person, and `success`
completes the set. Precedence is resolved **by severity, never by declaration
order**, so a screen matching both `search_page_ready` and `member_not_found`
yields the business outcome.

Two findings emerged from implementation rather than design.

**An unrecognised screen is `intervention_required`.** This is the correct
default, and it carries a consequence that is easy to miss: every screen the
happy path legitimately traverses also requires a condition, or the artifact
halts on contact. The compiler emits these, since one that did not would produce
artifacts that appear complete and execute nothing.

The compiler can only emit conditions for screens the discovery run actually
encountered, and a run that reached its goal never met a member who does not
exist. A compiled artifact therefore reports that screen as a hard failure,
while the reviewed artifact reports a business outcome with a code.
`evidence/replay_not_found` and `evidence/replay_not_found_reviewed` record the
identical situation under both, and the difference between them is what the
`draft` approval state marks.

**Equal-severity conditions that disagree about a code are now refused.**
`resolve()` has always refused an ambiguous control rather than selecting one.
`classify()` took the first in declaration order, which was acceptable until a
stable code began reaching the caller — at which point the contract would have
been determined by line position in a YAML file.

On UI drift specifically: signal ranking combined with `resolved_via_by_step` is
the complete answer. Drift manifests as degradation before it manifests as
failure.

## 4. Heterogeneity & multi-tenant

The seam is `Surface`: six methods, no browser vocabulary. `observe()` returns
an `Observation` of `Node`s carrying role, accessible name, value, bounds,
enabled, visible and destination. Nothing in the artifact schema references a
browser — a target specifies "rowheader named Savings Balance, take the next
sibling", which a desktop accessibility API answers as readily as Chrome.

Extension paths:

- **Legacy web application** — implemented. The target application is hostile in
  the ways described in the brief, and the surface reads markup only to answer
  one question the accessibility tree cannot: whether a control is a password
  field.
- **Desktop application** — UIAutomation on Windows and AX on macOS both expose
  role, name, value and bounds. `supported_signal_kinds()` allows a surface to
  declare that `web.css` is not evaluable, and `resolve()` skips an unsupported
  signal rather than failing on it.
- **Screenshot and coordinates** — the weakest case. GEOMETRY exists as a signal
  kind and is marked brittle wherever it is emitted. A surface without an
  accessibility tree would need to synthesise `Node`s from OCR, and a capability
  recorded against it would be fragile in a way the schema can describe but not
  remedy.

**Multi-tenant reuse.** Every artifact carries
`provenance.recorded_against: {app, release, variant}`, versioned separately
from the capability identifier — the shape a base-plus-override scheme requires.
The intended model is one artifact per vendor product at a base variant, with
per-tenant overrides replacing individual `TargetSpec`s rather than whole flows,
because what differs between two institutions running the same product is
typically a relabelled control rather than a different procedure.

Drift detection is implemented. `resolved_via_by_step` records which signal
fired for each step of each run. Aggregated across tenants, a step resolving on
`role_name` for ninety-nine institutions and on `web.css` for one identifies a
tenant that has renamed a control, before that tenant's runs begin to fail.

Override resolution, a registry keyed by product and version, and a promotion
path from base to variant are designed but not built. Implementing them against
a single application would have meant inventing the abstraction rather than
deriving it from two.

## 5. Escalation & handoff

A `Run` is a state machine: `RUNNING → PAUSED → HUMAN_CONTROL → RESUMING →
RUNNING`, plus terminals. `may_act` is true only in `RUNNING` under
`AUTOMATION` ownership, so an engine that failed to wait still could not act
during a handover.

Detection of "stuck" runs through the condition system: anything classified
`intervention_required` escalates, as do ambiguous controls, exhausted
recoveries and unmet preconditions. Escalations are **bounded per reason** — a
second escalation for the same cause fails the run rather than asking twice,
since an operator who resolved it once and is asked again is looking at
something that asking will not resolve.

Three properties make the control transfer real rather than nominal.

**The same session.** The operator holds the same Playwright `Page` the surface
holds. A fresh window would be a different session with a different cookie, and
signing on there would leave the run exactly as locked out as before.

**Human actions are recorded, sanitised in the page.** The listener reads the
field type, identifies `password`, and records that a value was entered without
recording the value. A credential never enters the automation process's memory.
This property survives a later debug statement; redaction on write does not.

**Return is verified, not accepted.** On hand-back the run re-observes and
checks the resume checkpoint. An operator stating they are finished is a claim
about the operator, not about the application, and one who presses Enter without
resolving the problem stops the run rather than resuming it.

The first live handover corrected a design assumption: the **capability**
restarts, not the step. Signing back on returns an empty search page, so
resuming at the interrupted step submitted a blank search and reported the
application broken. Restart is conditional on effect class — a read-only flow
begins again, a mutating one refuses and remains with the operator, because this
layer cannot determine whether an earlier step has already taken effect.

The operator console is mocked as a terminal prompt, as the brief permits. The
control transfer, the session, the recording and the verification are not.
Replacing the prompt with a queue consumer changes one adapter.

## 6. Safety

The guardrails are layered, and each is independently load-bearing:

| Layer | Enforcement |
|---|---|
| **action allowlist** | a closed set of `ActionType`; an unimplemented action cannot be proposed or performed. |
| **route allowlist** | checked on a link's *destination* before the click, and on the screen the run is *standing on* before it proposes anything. |
| **denied controls** | by role and accessible name. Covers what routes cannot: a submit button has no href. |
| **secret controls** | `Node.secret`, set by the surface. Every action on a credential field is refused, ahead of the denied list and anything configurable. |
| **effect class** | `read_only` runs unattended; `mutating` requires approval; `irreversible` requires confirmation of the invocation, each time. |

Two layers were added only after a run exposed the gap.

**`may_operate`** followed a discovery run that typed a guessed user identifier
into a sign-on form. Removing `/login` from the route allowlist would not have
prevented it: typing into a field navigates nowhere, so no route rule was
consulted. The page the run was *standing on* was never evaluated.

**`SECRET_CONTROL`** followed the same run. The claim that the system never
touches credentials held for capabilities, where `Contract` refuses a secret
input, and that was the claim under test. Discovery is not a capability.

Redaction is driven by **declared sensitivity, not scanning**. A pattern such as
`(?i)password` matches the field label while the value entered into it passes
through unmodified, and a scan publishes whatever it fails to recognise. An
unclassified field is dropped. A leak backstop catches a declared value copied
into an unclassified field: a personal value is masked in place, while a secret
takes the whole field with it, since four characters of a password is not a
redacted password.

**Limits.** The route allowlist cannot determine where a submit button leads.
`denied_controls` matches accessible names, so a relabelled control ceases to be
denied. Redaction protects declared values; an undeclared field is dropped
rather than protected, which is safe but costs evidence. The model as a whole
depends on the surface classifying controls correctly — a password field the
surface fails to identify is one the policy will not defend.

## 7. Cuts

**Excluded deliberately:**

- **Artifact registry and override resolution.** Provenance carries the fields a
  base-plus-variant scheme needs; nothing consumes them yet. Building it against
  one application would have meant inventing the abstraction rather than
  deriving it.
- **Operator console.** A terminal prompt, as the brief permits. The transfer
  beneath it is real.
- **Desktop and screenshot surfaces.** The seam is exercised by two
  implementations — live browser and recorded fixtures — which is what
  demonstrates it is a seam.
- **`cua discover` as a subcommand.** Discovery requires a signed-on session and
  signing on is an operator's responsibility, so it remains a script run in
  front of a browser. A subcommand would require either a credential in a flag
  or the assumption that a session appears unaided.
- **Assisted LLM recovery on replay failure.** Attractive, and incompatible with
  the premise: the argument for this architecture is that the decision was made
  at review time.

**Next, in priority order:**

1. **Attaching to a browser owned by someone else.** The handover currently
   works because the run owns a headed browser it can surrender. In production
   the session belongs to an operator's own browser, and `PlaywrightSurface`
   would need to connect over CDP rather than launch. This is the one cut that
   changes a design rather than an implementation.
2. **The override scheme**, once a second application exists to derive it from.
3. **Multi-run stability** — replay N times, report a flakiness signal per step.
   The data is already recorded; nothing aggregates it.
4. **A confirmation surface for irreversible capabilities** richer than a
   terminal prompt. The policy layer and the channel are both in place.

**One change to make on reflection.** The compiler classifies anything that
types or clicks as `mutating`, which marks a read-only lookup as mutating and
means the shipped artifact requires approval it should not need. The direction
is conservative and therefore defensible, but it reduces the usefulness of the
effect class. A better implementation would reason about whether a step submits
a form rather than whether it interacts with one.
