# Design write-up

Architecture diagrams and the demo commands are in [README.md](README.md);
this document covers the schema and the reasoning behind it.

## 1. Architecture

A **hexagonal architecture** — ports and adapters — with the dependency arrow
pointing inward and enforced by test. `domain/` imports the standard library and
nothing else; CI verifies this by importing every domain module into a virtual
environment with no packages installed, since the test suite runs where every
dependency is present and so proves nothing about it.

Six ports are declared, all `Protocol` rather than ABC, so an adapter never
imports a domain base class and cannot inherit behaviour from one.

**`Model` is a port; `Policy` deliberately is not.** A model is an external
dependency with several plausible implementations, and a substitutable fake is
what makes the decision loop testable without a network. A policy is a safety
rule: making it substitutable would allow a permissive implementation to be
wired in by accident. There is no second implementation worth having, and the
rules are already testable as a pure function over frozen values.

`DiscoverCapability` and `ReplayCapability` are the same five phases with one
substitution — observe, decide, check, act, verify. Replay reads its decision
from an artifact; discovery asks a model. Everything downstream of the decision
is shared code, which makes "both halves are held to identical rules" verifiable
rather than asserted.

Perception comes from the **accessibility tree, not the DOM**. Native handles
never leave the surface adapter: a `NodeRef` is opaque, scoped to one
observation, and the surface rejects a handle to a screen that has since been
replaced. The same choice underwrites the desktop case, since a desktop exposes
an accessibility tree too.

**Trade-off.** The accessibility tree sees less than the DOM — it cannot see a
control with no accessible name, and the target application has one: the sign-on
button appears in the handover record as `ctl00$cph$btnSignOn` because it has no
label. A DOM-based surface would see more and break more often. The narrower
view was taken deliberately and its cost is visible in the evidence.

## 2. Artifact schema

An artifact is a **contract plus a procedure**, because the two have different
audiences. The contract is what a calling agent reads — typed inputs with
validation patterns, typed outputs, effect class, approval state, provenance.
The steps are implementation the caller never sees.

```yaml
schema_version: "1.0"

contract:
  id: lookup_member_balance
  version: 1.0.0
  description: Look up a member by number and return their savings balance.
  effect: read_only          # read_only | mutating | irreversible
  approval: draft            # draft | approved
  provenance:
    source: discovered
    recorded_against: {app: riverside_cu_backoffice, release: "4.2.11", variant: base}
  inputs:
    - {name: member_id, type: string, pattern: "^[0-9]{6}$", required: true,
       sensitivity: personal}
  outputs:
    - {name: savings_balance, type: string, sensitivity: personal,
       transform: strip_whitespace}
    - {name: account_name, type: string, sensitivity: personal}

steps:
  - id: 01_type_member_number
    action: type
    value_template: "{{ inputs.member_id }}"
    target:
      intent: textbox 'Member Number'
      rationale: >
        Role and accessible name, both from the accessibility tree rather than
        markup, which is what survives a framework regenerating its ids.
      signals:
        - {kind: role_name, role: textbox, name: Member Number, confidence: high}
    checkpoint:
      detectors: [{kind: field_value_equals, target_ref: self,
                   value: "{{ inputs.member_id }}"}]

  - id: 04_read_savings_balance
    action: read
    reads_into: savings_balance
    target:
      intent: Value beside the 'Savings Balance' row header
      rationale: >
        The cell is named by its own contents, so it cannot be found by name
        without already knowing the answer. The stable fact is the adjacency.
      signals:
        - {kind: anchor, role: rowheader, name: Savings Balance,
           relation: next_sibling, confidence: high}
    checkpoint:
      detectors: [{kind: node_present, role: rowheader, name: Savings Balance}]

conditions:        # what a screen means when it is not the happy path
  - name: member_not_found
    outcome: business_outcome
    code: MEMBER_NOT_FOUND
    detail: No member exists with the supplied number.
    detectors: [{kind: text_present, text: No member matches}]

  - name: session_expired
    outcome: intervention_required
    code: AUTHENTICATION_REQUIRED
    resume_checkpoint: authenticated_session
    detectors:
      - {kind: text_present, text: Your session has ended}
      - {kind: node_present, role: textbox, name: Password}

success:
  detectors:
    - {kind: url_pattern_is, pattern: /members/{member_id}}
    - {kind: node_present, role: heading, name: Member Detail}
```

**Targets are ranked bundles of signals, not selectors**, ordered
`ROLE_NAME > LABEL > ANCHOR > GEOMETRY > WEB_CSS`, with the signal that actually
resolved recorded per step. A capability whose third step has begun resolving on
its second choice is degrading, and that appears in the run record before it
becomes a failure. The CSS kind is namespaced `web.css` because it has no
meaning on a desktop surface; naming it accurately keeps the portability claim
honest.

**ANCHOR exists because the schema could not otherwise express extraction.** A
value cell is named by its own contents — the balance cell's accessible name is
`4820.55` — so it cannot be located by name without already knowing the answer.
The stable property is its adjacency to the row header beside it.

**`rationale` is required on every target.** One line per step, and the
difference between an artifact a reviewer can approve and one they can only
accept.

**A secret cannot be declared as an interface value.** `Contract.__post_init__`
raises `UnsafeCapability` on a secret input or output — structural rather than
conventional, because a capability able to declare a password is one replay
would bind and type, which is what the handover design exists to prevent.

## 3. Determinism & error handling

Replay constructs no model and cannot reach one: `ReplayCapability` has no
`Model` in its dependency graph, so this is a property of the wiring rather than
a rule requiring discipline.

Each step observes **twice** — before acting and after. The second is
load-bearing: settling only before an action reported a member who does not
exist as a failed checkpoint rather than as the answer.

| Class | Meaning |
|---|---|
| **business outcome** | the run worked and the answer is no. `MEMBER_NOT_FOUND` is a result, not a crash. |
| **recoverable** | a known interstitial, dismissed and retried, bounded per step. |
| **hard failure** | stop and report which step, what was expected, what was observed. |
| **intervention required** | a person is needed. Also the default for a screen nothing describes. |

Precedence is by **severity, never declaration order**, so a screen matching
both `search_page_ready` and `member_not_found` yields the business outcome.

Two findings came from implementation rather than design.

**An unrecognised screen escalating means every happy-path screen also needs a
condition**, or the artifact halts on contact. The compiler emits those — but it
can only describe screens the discovery run encountered, and a run that reached
its goal never met a member who does not exist. `evidence/replay_not_found` and
`evidence/replay_not_found_reviewed` record the identical screen under the
compiled and reviewed artifacts: `hard_failure` against one, `business_outcome`
with a code against the other. That difference is what the `draft` approval
state marks.

**Equal-severity conditions disagreeing about a code are now refused.**
`resolve()` has always refused an ambiguous control rather than picking one;
`classify()` took the first in declaration order, which was acceptable until a
stable code began reaching the caller — at which point the contract would have
been decided by line position in a YAML file.

On UI drift: signal ranking plus `resolved_via_by_step` is the whole answer.
Drift appears as degradation before it appears as failure.

## 4. Heterogeneity & multi-tenant

The seam is `Surface`: six methods, no browser vocabulary. `observe()` returns
`Node`s carrying role, accessible name, value, bounds, enabled, visible,
destination. Nothing in the schema references a browser — a target says
"rowheader named Savings Balance, take the next sibling", which a desktop
accessibility API answers as readily as Chrome.

**Legacy web** is implemented; the surface reads markup only to answer the one
question the accessibility tree cannot — whether a control is a password field.
**Desktop** follows directly, since UIAutomation and AX expose the same four
properties; `supported_signal_kinds()` lets a surface declare `web.css`
unevaluable and `resolve()` skips it rather than failing. **Screenshot and
coordinates** is the weak case: GEOMETRY exists and is marked brittle wherever
emitted, and a surface synthesising `Node`s from OCR would be fragile in a way
the schema can describe but not remedy.

**Multi-tenant reuse.** Every artifact carries
`provenance.recorded_against: {app, release, variant}`, versioned separately
from the capability id — the shape a base-plus-override scheme requires. The
intended model is one artifact per vendor product at a base variant, with
per-tenant overrides replacing individual `TargetSpec`s rather than whole flows,
because what differs between two institutions running the same product is
typically a relabelled control, not a different procedure.

Drift detection is built. `resolved_via_by_step` records which signal fired per
step per run; aggregated across tenants, a step resolving on `role_name` for
ninety-nine institutions and `web.css` for one identifies a tenant that has
renamed a control, before its runs begin failing.

Override resolution, a registry keyed by product and version, and a base-to-
variant promotion path are designed but not built. Implementing them against one
application would have meant inventing the abstraction rather than deriving it.

## 5. Escalation & handoff

`Run` is a state machine: `RUNNING → PAUSED → HUMAN_CONTROL → RESUMING →
RUNNING`, plus terminals. `may_act` is true only in `RUNNING` under `AUTOMATION`
ownership, so an engine that failed to wait still could not act mid-handover.

"Stuck" is detected through the condition system — anything classified
`intervention_required`, plus ambiguous controls, exhausted recoveries and unmet
preconditions. Escalations are **bounded per reason**: a second escalation for
the same cause fails the run rather than asking twice, since an operator who
resolved it once and is asked again is looking at something asking will not fix.

Three properties make the transfer real. **The same session** — the operator
holds the same `Page` the surface holds, because a fresh window is a different
cookie and signing on there leaves the run as locked out as before. **Actions
are recorded, sanitised in the page** — the listener identifies a password field
and records that a value was entered without recording it, so no credential
enters the automation process; that survives a later debug statement, which
redaction on write does not. **Return is verified, not accepted** — the run
re-observes and checks the resume checkpoint, because an operator stating they
are finished is a claim about the operator, not the application.

The first live handover corrected an assumption: the **capability** restarts,
not the step. Signing back on returns an empty search page, so resuming at the
interrupted step submitted a blank search and reported the application broken.
Restart is conditional on effect class — read-only begins again; mutating
refuses and stays with the operator, because this layer cannot know whether an
earlier step has already taken effect.

The console is mocked as a terminal prompt, as permitted. The transfer, the
session, the recording and the verification are not.

## 6. Safety

| Layer | Enforcement |
|---|---|
| **action allowlist** | closed set of `ActionType`; an unimplemented action cannot be proposed or performed. |
| **route allowlist** | checked on a link's *destination* before the click, and on the screen the run is *standing on* before it proposes anything. |
| **denied controls** | by role and accessible name. Covers what routes cannot: a submit button has no href. |
| **secret controls** | `Node.secret`, set by the surface. Every action on a credential field refused, ahead of anything configurable. |
| **effect class** | `read_only` runs unattended; `mutating` needs approval; `irreversible` needs confirmation per invocation. |

Two layers were added only after a run exposed the gap. **`may_operate`**
followed a discovery run that typed a guessed user id into a sign-on form —
removing `/login` from the route allowlist would not have prevented it, because
typing navigates nowhere and no route rule was consulted; the page the run was
*standing on* was never evaluated. **`SECRET_CONTROL`** followed the same run:
the claim that the system never touches credentials held for capabilities, where
`Contract` refuses a secret input, and that was the claim under test. Discovery
is not a capability.

Redaction is driven by **declared sensitivity, not scanning**. A pattern like
`(?i)password` matches the field label while the value passes through, and a
scan publishes whatever it fails to recognise. An unclassified field is dropped.
A leak backstop catches a declared value copied into an unclassified one: a
personal value is masked in place, a secret takes the whole field with it, since
four characters of a password is not a redacted password.

**Limits.** The route allowlist cannot determine where a submit button leads.
`denied_controls` matches accessible names, so a relabelled control ceases to be
denied. An undeclared field is dropped rather than protected — safe, and it
costs evidence. And the model depends on the surface classifying controls
correctly: a password field the surface fails to identify is one the policy will
not defend.

## 7. Cuts

**Excluded deliberately.** The artifact registry and override resolution —
provenance carries the fields, nothing consumes them, and building it against
one application would mean inventing the abstraction. The operator console, a
terminal prompt as permitted. Desktop and screenshot surfaces — the seam is
exercised by two implementations, which is what demonstrates it is a seam.
`cua discover` as a subcommand, since discovery needs a signed-on session and a
subcommand would require a credential in a flag. Assisted LLM recovery on replay
failure, which is incompatible with the premise that the decision was made at
review time.

**Next, in order.**

1. **Attaching to a browser owned by someone else.** The handover works because
   the run owns a headed browser it can surrender; in production the session
   belongs to the operator's own browser and `PlaywrightSurface` would connect
   over CDP rather than launch. The one cut that changes a design rather than an
   implementation.
2. **The override scheme**, once a second application exists to derive it from.
3. **Multi-run stability** — replay N times, report flakiness per step. The data
   is recorded; nothing aggregates it.
4. **A confirmation surface for irreversible capabilities** richer than a
   terminal prompt. Policy layer and channel are both in place.

**One change on reflection.** The compiler classifies anything that types or
clicks as `mutating`, marking a read-only lookup as mutating and requiring
approval it should not need. Conservative in the defensible direction, but it
reduces the usefulness of the effect class; a better implementation would reason
about whether a step submits a form rather than whether it interacts with one.
