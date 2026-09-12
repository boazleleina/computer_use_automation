# Recorded runs

Seven runs, each produced by the commands in the [README](../README.md) rather
than written for this directory: three model-driven discovery runs, three
replays, and one handover performed by a person.

The runs that fail are included deliberately. A run reaching its goal
demonstrates that the machinery works; a run that stops demonstrates what the
machinery does when something is wrong, which is the question that determines
whether this is safe to point at a production system.

| run | outcome | what it shows |
|---|---|---|
| `discovery` | `goal_reached` | a model found the flow; the artifact is beside it |
| `not_found` | `repeated_state` | the loop stops when the screen stops changing |
| `signed_out` | `policy_refused` | a guardrail firing before a model call is spent |
| `replay_success` | `success` | the compiled artifact executing with no model |
| `replay_not_found` | `hard_failure` | what an unreviewed artifact does with a screen it has never seen |
| `replay_not_found_reviewed` | `business_outcome` | the same screen, after a person added the condition |
| `replay_handoff` | `success` | a run rescued mid-flight by a human |

## discovery — a model found the flow

`claude-sonnet-5`, five steps, `goal_reached`.

```
discovery_run.jsonl   what the run did: proposals, rationales, policy checks,
                      actions, observations — redacted on the way to disk
transcript.json       the raw model exchange, kept apart from the above
capability.yaml       the compiled artifact
```

The artifact beside it is the deliverable. It replays unmodified, including
against a member the discovery run never saw; the [README](../README.md#quick-start)
gives the command.

In `capability.yaml`, `{{ inputs.member_id }}` appears wherever the member
number stood — including in the accessible name of the control that is clicked,
since the result row is named after the member. That substitution is what makes
the file a capability rather than a recording.

In `transcript.json`, each prompt ends with the remaining budget
(`Remaining: 40 actions, 899 seconds`) and the history renders a read with its
result (`read 'Savings Balance' -> '4820.55'`). The model is told what it has
spent and what its reads returned. An earlier version told it neither, and it
read the same cell five times before completing.

## not_found — the screen stopped changing

`repeated_state`, three steps. Asked for a member who does not exist, the model
searched, read the "No member matches" notice, and clicked Find again. The
second click changed nothing, so the run stopped rather than spending its budget
on a screen that had stopped moving.

## signed_out — the guardrail fired

`policy_refused`, **nought steps**, and an empty `transcript.json` — the record
that no request was ever sent.

Started from a session nobody signed on, the run observed `/login`, asked whether
it may operate that screen, and was told no. The model was never consulted about
a page it has no business on.

Before the route rule existed, the same scenario produced a different record:
the model located the sign-on form, guessed `operator` / `password`, and entered
them, with the rationale *"Enter a plausible password to proceed with sign-on."*
`may_operate` and `SECRET_CONTROL` were added in response, and this file is the
result.

## replay_success, replay_not_found, replay_not_found_reviewed

Three replays, produced by `scripts/replay_demo.py`. No model is constructed in
any of them and none could be reached.

Each record opens with `approval_assumed`. A discovered artifact is marked
`draft`, a draft does not run unattended, and this demonstration has no artifact
store holding a real approval — so the script asserts one. The record names the
artifact's actual state and states that no review was consulted, rather than
reporting an approval nobody gave.

The first runs the compiled artifact against a member who exists and returns the
balance and the account name.

The second and third should be read together. Both encounter the same screen —
a search returning "No member matches" — and report it differently:

| | artifact | outcome | code |
|---|---|---|---|
| `replay_not_found` | compiled by discovery | `hard_failure` | — |
| `replay_not_found_reviewed` | reviewed by a person | `business_outcome` | `MEMBER_NOT_FOUND` |

The compiled artifact carries conditions only for the screens the discovery run
actually passed through, and a run that reached its goal never met a member who
did not exist. So it has no description for that screen and reports honestly
that the flow went somewhere it did not recognise: *"02_click_find did not leave
the application where it was expected."*

The reviewed artifact has the condition somebody added, and returns an answer
the caller can act on rather than an error it has to interpret.

That difference is what the approval state is for. A discovered artifact is
marked `draft` because it is a correct recording of one path and nothing more,
and this pair is what the gap looks like in practice.

## replay_handoff — a person rescued a run

A replay started on a healthy session, the session was expired underneath it,
and the run stopped and asked for help. A person signed on in the same browser
window and handed it back; the run verified the session was live and completed.

```
intervention_requested  reason=authentication_required  step=submit_lookup
handed_over             owner=human
human_action            navigate         /search
human_action            click            "User Id"
human_action            input            "User Id"   ****ller
human_action            sensitive_input  "Password"  [REDACTED]
human_action            click            ctl00$cph$btnSignOn
human_action            navigate         /search
control_returned        actions=7
resume_checked          authenticated_session  holds=true
restarted               "a person rescued the run"
run_finished            success
```

Two details in that record are worth noting.

**The clicks into each field.** A script calling `fill()` does not produce
those; they indicate the input was typed by a person.

**There is no navigation to `/login`,** because none occurred. The expired
session renders the sign-on form at the route of the page it replaced, returning
HTTP 200. That behaviour is what the artifact's `session_expired` condition is
written around, and this record confirms it independently.

The password does not appear in the file. It is not masked on write — it is
never read: the page listener identifies `type=password` and reports that a
value was entered without reporting the value.

## What is not here

No member number, operator password or operator user id appears in any file in
this directory. The claim is enforced rather than stated:
[`test_safety.py`](../tests/integration/test_safety.py) searches every tracked
file under `evidence/` for the fixture identifiers and fails if one is found.

The recorded screens under `tests/fixtures/observations/` do contain member
numbers, deliberately. They are captured accessibility trees of a member detail
page, and a recording of that page contains that member's number because that is
what the page displays. Removing it would leave fixtures that no longer describe
the application they were taken from.
