"""Executing a capability. No model anywhere in this file.

The sequence is fixed and short: observe, classify, resolve, check policy, act,
verify. Every judgement in it is borrowed from the domain — classify decides
what a screen means, resolve decides which control, Policy decides whether an
action is permitted, Run decides who owns the session. What is left here is
order, which is the only thing an orchestrator should contain.

Each step observes before it does anything. Whatever was resolved last time
belongs to a screen that may since have been replaced, and a handle to a
replaced screen is dead — the surface enforces that rather than trusting this
loop to remember.

Every step writes what it did as it goes, rather than one summary at the end.
A record that says only how a run finished cannot answer which target started
resolving on a weaker signal, and that question is the whole reason signals are
ranked.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from cua.domain.actions import ActionType, Effect
from cua.domain.capability import Capability, SignalKind, Step, TargetSpec
from cua.domain.conditions import Condition, Detector, DetectorKind, RecoveryAction
from cua.domain.errors import MalformedArtifact
from cua.domain.intervention import InterventionRequest
from cua.domain.observation import NodeRef, Observation
from cua.domain.outcomes import Classification, Outcome, Result, classify
from cua.domain.policy import Denied, Policy, PolicyRule
from cua.domain.resolution import Ambiguous, Resolved, resolve
from cua.domain.run import EscalationReason, Run
from cua.ports.clock import Clock
from cua.ports.evidence import EvidenceSink
from cua.ports.operator import OperatorChannel
from cua.ports.surface import Surface

PLACEHOLDER = re.compile(r"\{\{\s*inputs\.([a-zA-Z0-9_]+)\s*\}\}")

# Anything shaped like a placeholder, whether or not it is a valid one. A
# template is checked against both: `{{ inputs.member-id }}` matches this and
# not PLACEHOLDER, and without the wider pattern it would survive substitution
# untouched and be typed into the application exactly as written.
PLACEHOLDER_SHAPED = re.compile(r"\{\{[^}]*\}\}")


class _Restart:
    """A person fixed the run, and it has to begin again rather than carry on.

    Resuming where the escalation happened would work against a screen somebody
    else has since changed. Signing back on returns an empty search page, so a
    run that picked up at "verify the checkpoint" would judge the result of a
    search that no longer exists and report the application broken.

    Whether beginning again is safe is the caller's judgement, not this value's:
    it says the world moved, and _start_over decides what may be done about it.
    """


RESTART = _Restart()

# A detector may say `target_ref: self`, meaning the control this step acted on.
SELF_REF = "self"

# Outcomes that end a run where they are found. A business outcome is the
# answer, a hard failure is reportable, and a screen nobody described is
# somebody's to look at.
TERMINAL_OUTCOMES = frozenset(
    {Outcome.BUSINESS_OUTCOME, Outcome.HARD_FAILURE, Outcome.INTERVENTION_REQUIRED}
)


@dataclass
class ReplayCapability:
    """Runs one capability against one surface.

    There is no Model here and no way to reach one. A procedure reviewed once
    executes the same way every time, and nothing decides anything new while it
    runs.
    """

    surface: Surface
    policy: Policy
    clock: Clock
    evidence: EvidenceSink | None = None
    operator: OperatorChannel | None = None

    def run(self, capability: Capability, inputs: Mapping[str, str], run_id: str) -> Result:
        """Execute a capability, or refuse before anything has been done.

        Inputs are bound before the risk gate, and the order matters now that
        an irreversible capability asks a person to confirm the invocation.
        Confirmation is supposed to be about this member rather than about the
        procedure, and an unbound goal reads "look up member {{ inputs.member_id
        }}" — which is the procedure, and is the thing approval already covered.
        Binding touches nothing: it validates against the contract and fills in
        a template, so doing it first costs no risk and makes the request
        answerable.
        """
        contract = capability.contract
        state = _RunState(capability=capability, run_id=run_id, bound={}, run=None)
        state.bound = _bind_inputs(capability, inputs)

        gate = self.policy.may_run_unattended(contract.effect, approved=contract.approved)
        if isinstance(gate, Denied) and not self._confirmed(state, gate):
            return self._refused_before_starting(state, gate)
        state.run = Run.start(
            run_id=run_id, capability=contract.name, version=contract.version
        )
        self._emit(
            state,
            "run_started",
            capability=contract.name,
            version=contract.version,
            effect=contract.effect.value,
            approval=contract.approval.value,
        )

        unmet = self._check_preconditions(state)
        if unmet is not None:
            return unmet

        while True:
            restarted = False
            for step in capability.steps[state.active.step_index :]:
                finished = self._run_step(state, step)
                if finished is RESTART:
                    restarted = True
                    break
                if finished is not None:
                    return finished  # type: ignore[return-value]
                state.run = state.active.advance()

            if not restarted:
                finished = self._finish(state)
                if finished is not RESTART and not isinstance(finished, _Restart):
                    return finished
                # The last screen was rescued too. Same question as any other
                # rescue: may this capability begin again?

            refused = self._start_over(state)
            if refused is not None:
                return refused

    def _start_over(self, state: "_RunState") -> Result | None:
        """Begin the capability again after a person rescued it.

        The reason a run stopped is usually also a reason its earlier steps no
        longer hold: signing back on returns an empty search page, so carrying
        on from the interrupted step would submit a search nobody filled in.

        Only for a capability that changes nothing. A mutating flow that was
        rescued half way through might already have submitted something, and
        this layer cannot tell whether repeating it would do the work twice —
        so it refuses and leaves that to a person, which is where the run
        already is. Being wrong in this direction costs a stopped run; being
        wrong in the other costs a duplicate transaction.
        """
        effect = state.capability.contract.effect
        if effect is not Effect.READ_ONLY:
            state.run = state.active.fail("a mutating capability cannot restart itself")
            return self._result(
                state,
                Outcome.INTERVENTION_REQUIRED,
                detail=(
                    f"a person rescued this run, but a {effect.value} capability cannot "
                    "safely begin again: an earlier step may already have taken effect"
                ),
            )

        state.run = state.active.start_over()
        self._emit(state, "restarted", reason="a person rescued the run")
        return None

    def _confirmed(self, state: "_RunState", gate: Denied) -> bool:
        """Ask a person to confirm an irreversible invocation, if one is there.

        Only for CONFIRMATION_REQUIRED, and the distinction is the point. A
        capability that was never approved is refused and stays refused: nobody
        at a terminal can substitute for the review it skipped. An irreversible
        one that was approved is a different matter — the procedure has been
        read, and what is missing is somebody saying yes to this invocation, on
        this member, now.

        Unattended and irreversible therefore means refused, which is the
        conservative reading and the one worth defending: an operator channel
        that is absent is not the same as an operator who agreed.
        """
        if gate.rule is not PolicyRule.CONFIRMATION_REQUIRED:
            return False
        if self.operator is None or not state.capability.contract.approved:
            return False

        contract = state.capability.contract
        request = InterventionRequest(
            run_id=state.run_id,
            capability=contract.name,
            version=contract.version,
            # The bound goal, so the person is confirming an invocation and
            # not a procedure. Unbound it says "member {{ inputs.member_id }}",
            # which is exactly the thing approval already signed off on.
            goal=_fill(contract.goal, state.bound) or contract.goal,
            reason=EscalationReason.APPROVAL_REQUIRED,
            detail=gate.reason,
            resume_checkpoint="confirmed_by_operator",
        )
        self._emit(state, "confirmation_requested", **request.summary())
        self.operator.request_intervention(request)
        handover = self.operator.await_release(state.run_id)
        self._emit(state, "confirmed", actions=len(handover.events))
        return True

    # ---- guards before the first step -------------------------------------

    def _refused_before_starting(self, state: "_RunState", gate: Denied) -> Result:
        """No Run is started at all.

        A capability refused unattended did not execute and did not partially
        execute, and a run record for it would imply otherwise.
        """
        contract = state.capability.contract
        result = Result(
            run_id=state.run_id,
            capability=contract.name,
            version=contract.version,
            outcome=Outcome.HARD_FAILURE,
            detail=gate.reason,
            expected="a capability approved for unattended replay",
            observed=gate.rule.value,
        )
        self._emit(state, "run_refused", reason=gate.reason, rule=gate.rule.value)
        return result

    def _check_preconditions(self, state: "_RunState") -> Result | None:
        """Look once before step one, and stop if the ground is not what the
        capability said it needed.

        A precondition names a checkpoint. A condition that declares the same
        name as its resume_checkpoint is, by construction, the thing that makes
        that checkpoint untrue: session_expired resumes on authenticated_session,
        so session_expired holding means authenticated_session does not.

        Checking here means a run started against an already expired session
        escalates at step zero rather than three steps in, through the same path
        a mid-run expiry takes.
        """
        if not state.capability.contract.preconditions:
            return None

        observation, classification = self._observe_and_classify(state)
        condition = state.condition()
        if condition is None or condition.resume_checkpoint is None:
            return None
        if condition.resume_checkpoint not in state.capability.contract.preconditions:
            return None

        reason = _reason_for(condition)
        state.run = state.active.escalate(
            reason, resume_checkpoint=condition.resume_checkpoint
        )
        self._emit(
            state,
            "precondition_unmet",
            precondition=condition.resume_checkpoint,
            condition=classification.condition_name,
        )
        return self._escalate(state, None, observation, reason)

    # ---- the phases of one step -------------------------------------------

    def _run_step(self, state: "_RunState", step: Step) -> "Result | None | _Restart":
        """One step, or the Result that ends the run inside it.

        A step that was rescued by a person hands RESTART back to run(), which
        begins the whole capability again through _start_over. Not the step:
        the reason a run stopped is usually also a reason its earlier steps no
        longer hold. The bound on that is the escalation
        budget: a second escalation for the same reason fails the run instead
        of asking again.
        """
        return self._attempt_step(state, step)

    def _attempt_step(self, state: "_RunState", step: Step) -> "Result | None | _Restart":
        self._emit(state, "step_started", step=step.id, action=step.action_type.value)

        settled = self._settle(state, step)
        if isinstance(settled, (Result, _Restart)):
            return settled
        observation = settled

        subject: NodeRef | None = None
        if step.target is not None:
            located = self._locate(state, step, step.target, observation)
            if isinstance(located, Result):
                return located
            subject = located

        refused = self._check_policy(state, step, subject, observation)
        if refused is not None:
            return refused

        self._perform(state, step, subject)

        # Settle again before judging the step. What the application says about
        # the screen it just produced outranks what this step was hoping for: a
        # member that does not exist is an answer, and reporting it as "the
        # checkpoint failed" would turn a true result into a crash.
        after = self._settle(state, step)
        if isinstance(after, (Result, _Restart)):
            # A rescue here is the interesting one. The action happened, then
            # the session died, then somebody signed back on — which resets the
            # search and leaves nothing for this step's checkpoint to find. The
            # step begins again rather than judging a screen that was rebuilt
            # underneath it.
            return after
        return self._checkpoint(state, step, after)

    def _observe_and_classify(self, state: "_RunState") -> tuple[Observation, Classification]:
        """Look at the screen and decide what it means. Changes nothing."""
        observation = self.surface.observe()
        classification = classify(observation, state.capability.conditions)
        state.classification = classification
        return observation, classification

    def _attempt_recovery(self, state: "_RunState", observation: Observation) -> bool:
        """Clear something that is merely in the way. True if a try was spent.

        False when the condition declares no recovery or the artifact's bound is
        gone, which is the caller's signal to ask for a person instead.
        """
        condition = state.condition()
        recovery = condition.recovery if condition else None
        if recovery is None or state.active.recovery_count() >= recovery.max_attempts:
            return False

        if recovery.action is RecoveryAction.WAIT:
            self.clock.sleep(recovery.wait_ms)
        elif recovery.target is not None:
            cleared = resolve(recovery.target, observation, self._kinds())
            if isinstance(cleared, Resolved):
                self.surface.act(ActionType.CLICK, cleared.ref)

        state.run = state.active.recover()
        self._emit(
            state,
            "recovered",
            condition=state.classification.condition_name if state.classification else None,
            action=recovery.action.value,
            attempt=state.active.recovery_count(),
        )
        return True

    def _settle(
        self, state: "_RunState", step: Step | None
    ) -> Observation | Result | _Restart:
        """Observe until the screen is one the run can work on.

        A recoverable condition is cleared and looked at again, up to the bound
        the artifact declared. Exhausting that bound asks for a person: waiting
        has demonstrably stopped working, and waiting again will not fix it.
        """
        while True:
            observation, classification = self._observe_and_classify(state)
            self._emit(
                state,
                "classified",
                step=step.id if step else None,
                condition=classification.condition_name,
                outcome=classification.outcome.value,
                matched=list(classification.matched),
            )

            if classification.outcome is not Outcome.RECOVERABLE:
                if classification.outcome in TERMINAL_OUTCOMES:
                    stopped = self._stop(state, step, observation, classification)
                    if stopped is not None:
                        return stopped
                    return RESTART
                return observation

            if not self._attempt_recovery(state, observation):
                state.run = state.active.escalate(
                    EscalationReason.RECOVERY_EXHAUSTED, resume_checkpoint="step_precondition"
                )
                escalated = self._escalate(
                    state, step, observation, EscalationReason.RECOVERY_EXHAUSTED
                )
                if escalated is not None:
                    return escalated
                return RESTART

    def _locate(
        self, state: "_RunState", step: Step, target: TargetSpec, observation: Observation
    ) -> NodeRef | Result:
        # A control can be named after the data it carries. The link to a
        # member is named with that member's number, so the spec describing it
        # has to be bound to this run before it can match anything.
        target = _fill_target(target, state.bound)
        resolution = resolve(target, observation, self._kinds())

        if isinstance(resolution, Resolved):
            state.resolved_via[step.id] = resolution.via_signal
            self._emit(
                state,
                "resolved",
                step=step.id,
                intent=target.intent,
                via_signal=resolution.via_signal.value,
                signal_index=resolution.signal_index,
                skipped=[kind.value for kind in resolution.skipped],
            )
            return resolution.ref

        if isinstance(resolution, Ambiguous):
            state.run = state.active.escalate(
                EscalationReason.AMBIGUOUS_CONTROL, resume_checkpoint="target_unique"
            )
            return self._result(
                state,
                Outcome.INTERVENTION_REQUIRED,
                detail=f"{resolution.count} controls match {target.intent!r}",
                step=step,
                expected=f"exactly one control matching {target.intent!r}",
                observed=f"{resolution.count} matched on {resolution.at_signal.value}",
                capture=True,
            )

        state.run = state.active.escalate(
            EscalationReason.TARGET_NOT_FOUND, resume_checkpoint="target_present"
        )
        return self._result(
            state,
            Outcome.INTERVENTION_REQUIRED,
            detail=f"no control matches {target.intent!r}",
            step=step,
            expected=target.intent,
            observed=f"{observation.page_title} at {observation.url_pattern}",
            capture=True,
        )

    def _check_policy(
        self, state: "_RunState", step: Step, subject: NodeRef | None, observation: Observation
    ) -> Result | None:
        """Ask before acting, never after.

        A refusal recorded afterwards is an account of something that already
        happened. The surface is not touched unless this returns None.
        """
        node = observation.node(subject) if subject is not None else None
        route = _fill(step.value, state.bound) if step.action_type is ActionType.NAVIGATE else None
        verdict = self.policy.evaluate(step.action_type, node=node, route=route)

        self._emit(
            state,
            "policy_check",
            step=step.id,
            action=step.action_type.value,
            allowed=not isinstance(verdict, Denied),
            rule=verdict.rule.value if isinstance(verdict, Denied) else None,
        )

        if isinstance(verdict, Denied):
            state.run = state.active.fail(verdict.reason)
            return self._result(
                state,
                Outcome.HARD_FAILURE,
                detail=verdict.reason,
                step=step,
                expected="an action the policy permits",
                observed=verdict.rule.value,
            )
        return None

    def _perform(self, state: "_RunState", step: Step, subject: NodeRef | None) -> None:
        if step.action_type is ActionType.READ:
            if subject is None or step.reads_into is None:
                raise MalformedArtifact(f"step {step.id!r} reads but names no target or output")
            value = self.surface.read(subject)
            state.outputs[step.reads_into] = _transform(state.capability, step.reads_into, value)
            self._emit(state, "read", step=step.id, into=step.reads_into, value=value)
            return

        filled = _fill(step.value, state.bound)
        self.surface.act(step.action_type, subject, filled)
        # The raw value goes out. Redaction belongs to the sink, which is the
        # one place every value on its way to disk passes through.
        self._emit(state, "acted", step=step.id, action=step.action_type.value, value=filled)

    def _checkpoint(self, state: "_RunState", step: Step, after: Observation) -> Result | None:
        """A step that silently did nothing must not look like one that worked.

        Only reached once the screen has settled and no condition explains it,
        so a failure here means the application went somewhere nobody described.
        """
        if not step.checkpoint:
            return None

        subject = self._subject_for(state, step, after)

        for detector in step.checkpoint:
            bound = _fill_detector(detector, state.bound)
            if not bound.holds(after, subject):
                self._emit(
                    state, "checkpoint", step=step.id, held=False, expected=_describe(bound)
                )
                state.run = state.active.fail(f"checkpoint failed after {step.id}")
                return self._result(
                    state,
                    Outcome.HARD_FAILURE,
                    detail=f"{step.id} did not leave the application where it was expected",
                    step=step,
                    expected=_describe(bound),
                    observed=f"{after.page_title} at {after.url_pattern}",
                    capture=True,
                )

        self._emit(state, "checkpoint", step=step.id, held=True)
        return None

    def _subject_for(self, state: "_RunState", step: Step, after: Observation) -> NodeRef | None:
        """Re-resolve the step's own target against the screen being checked.

        `target_ref: self` means the control this step acted on. The ref from
        before the action points at a screen that has been replaced, so the
        control is found again rather than remembered.
        """
        if not any(d.target_ref == SELF_REF for d in step.checkpoint) or step.target is None:
            return None
        found = resolve(_fill_target(step.target, state.bound), after, self._kinds())
        return found.ref if isinstance(found, Resolved) else None

    # ---- endings -----------------------------------------------------------

    def _stop(
        self,
        state: "_RunState",
        step: Step | None,
        observation: Observation,
        classification: Classification,
    ) -> Result | None:
        """End the run on what the screen says, or hand it to a person.

        None means a person took it, fixed it, and handed it back with the
        resume checkpoint holding. The caller looks again rather than carrying
        on from a screen nobody has re-examined.

        The classification is passed in rather than read back off the state: the
        caller has just computed it, and taking it as an argument removes the
        question of whether it could be missing.
        """
        if classification.outcome is not Outcome.INTERVENTION_REQUIRED:
            state.run = state.active.fail(classification.detail)
            return self._result(
                state,
                classification.outcome,
                detail=classification.detail,
                step=step,
                capture=classification.outcome is Outcome.HARD_FAILURE,
            )

        condition = state.condition()
        reason = _reason_for(condition)
        state.run = state.active.escalate(
            reason,
            resume_checkpoint=(condition.resume_checkpoint if condition else None)
            or "step_precondition",
        )
        return self._escalate(state, step, observation, reason)

    def _escalate(
        self,
        state: "_RunState",
        step: Step | None,
        observation: Observation,
        reason: EscalationReason,
    ) -> Result | None:
        """Ask for a person. Continue only if one takes it and puts it right.

        With no operator wired in, this stops — which is the correct behaviour
        for an unattended deployment and is what every replay did before there
        was anywhere to escalate to.
        """
        reference = self._capture(state, step)

        if self.operator is None or not state.active.awaiting_operator:
            return self._result(
                state,
                Outcome.INTERVENTION_REQUIRED,
                detail=state.detail(),
                step=step,
                evidence_ref=reference,
            )

        request = self._request(state, step, observation, reference, reason)
        self._emit(state, "intervention_requested", **request.summary())

        # The screen that caused the decision travels with the request, not a
        # fresh look at the world. On a live browser those differ: a page still
        # settling would show the operator something the classifier never saw.
        self.operator.request_intervention(request)

        state.run = state.active.hand_over()
        self._emit(state, "handed_over", owner=state.active.owner.value)

        handover = self.operator.await_release(state.run_id)
        for record in handover.records():
            self._emit(state, "human_action", **record)

        state.run = state.active.return_control()
        self._emit(state, "control_returned", actions=len(handover.events))

        return self._resume(state, step, request, reference)

    def _resume(
        self,
        state: "_RunState",
        step: Step | None,
        request: InterventionRequest,
        reference: str | None,
    ) -> Result | None:
        """Look again, and continue only if the checkpoint holds.

        Re-observed rather than trusted. A person saying they are finished is a
        claim about them, not about the application: they may have signed on
        and gone somewhere else, or fixed a different thing, or changed nothing
        at all. Everything the run resolved before pausing belongs to a screen
        that no longer exists, so the step runs again from observation.
        """
        observation = self.surface.observe()
        classification = classify(observation, state.capability.conditions)
        holds = _checkpoint_holds(request.resume_checkpoint, state, classification)

        self._emit(
            state,
            "resume_checked",
            checkpoint=request.resume_checkpoint,
            holds=holds,
            url_pattern=observation.url_pattern,
            condition=classification.condition_name,
        )

        state.run = state.active.resume(checkpoint_holds=holds)
        if not holds:
            return self._result(
                state,
                Outcome.INTERVENTION_REQUIRED,
                detail=(
                    f"control came back but {request.resume_checkpoint!r} still does not hold"
                ),
                step=step,
                evidence_ref=reference,
            )
        return None

    def _request(
        self,
        state: "_RunState",
        step: Step | None,
        observation: Observation,
        reference: str | None,
        reason: EscalationReason,
    ) -> InterventionRequest:
        """Everything a person needs, assembled where it is still knowable.

        Here rather than in the operator, because by the time a request reaches
        a channel the engine has moved on and the answers would have to be
        reconstructed. Reconstruction is wrong exactly when it matters.
        """
        contract = state.capability.contract
        return InterventionRequest(
            run_id=state.run_id,
            capability=contract.name,
            version=contract.version,
            goal=contract.goal,
            reason=reason,
            detail=state.detail() or "a person is needed",
            resume_checkpoint=state.active.resume_checkpoint or "step_precondition",
            step_id=step.id if step else None,
            observation=observation,
            screenshot_ref=reference,
        )

    def _finish(self, state: "_RunState") -> "Result | _Restart":
        """Every step ran. The capability's own success check decides the rest.

        Settled like every other read of the screen. The last screen is exactly
        where a session expires, and looking without settling would report that
        as "the success condition did not hold" rather than as the expiry it is.
        """
        settled = self._settle(state, None)
        if isinstance(settled, Result):
            return settled
        if isinstance(settled, _Restart):
            # Handed back to the run loop rather than recursing here. Recursing
            # would skip _start_over, which is where the effect guard lives and
            # the only thing deciding whether beginning again is safe at all —
            # and nothing would have bounded the recursion either.
            return settled

        for detector in state.capability.success:
            bound = _fill_detector(detector, state.bound)
            if not bound.holds(settled):
                state.run = state.active.fail("the success condition did not hold")
                return self._result(
                    state,
                    Outcome.HARD_FAILURE,
                    detail="every step ran and the capability did not end where it should",
                    expected=_describe(bound),
                    observed=f"{settled.page_title} at {settled.url_pattern}",
                    capture=True,
                )

        state.run = state.active.succeed()
        return self._result(state, Outcome.SUCCESS, detail="the capability completed")

    # ---- reporting ---------------------------------------------------------

    def _capture(self, state: "_RunState", step: Step | None) -> str | None:
        """Keep a picture of the screen that ended the run.

        A structured result says what went wrong. An image says what it looked
        like, which is what someone who was not there actually needs.
        """
        if self.evidence is None:
            return None
        name = f"{step.id if step else 'run'}-{state.run_id}.png"
        return self.evidence.attach(state.run_id, name, self.surface.screenshot())

    def _result(
        self,
        state: "_RunState",
        outcome: Outcome,
        detail: str,
        step: Step | None = None,
        expected: str | None = None,
        observed: str | None = None,
        evidence_ref: str | None = None,
        capture: bool = False,
    ) -> Result:
        reference = evidence_ref or (self._capture(state, step) if capture else None)
        result = Result(
            run_id=state.run_id,
            capability=state.capability.contract.name,
            version=state.capability.contract.version,
            outcome=outcome,
            detail=detail,
            condition_name=(
                state.classification.condition_name if state.classification else None
            ),
            code=state.classification.code if state.classification else None,
            outputs=dict(state.outputs),
            step_id=step.id if step is not None else None,
            expected=expected,
            observed=observed,
            evidence_ref=reference,
            resolved_via_by_step=dict(state.resolved_via),
        )
        self._emit(
            state,
            "run_finished",
            outcome=result.outcome.value,
            condition=result.condition_name,
            code=result.code,
            detail=result.detail,
            step=result.step_id,
            expected=result.expected,
            observed=result.observed,
            evidence_ref=result.evidence_ref,
            run_state=state.run.state.value if state.run else None,
        )
        return result

    def _emit(self, state: "_RunState", event: str, **fields: object) -> None:
        """One line of the run's own account of itself."""
        if self.evidence is None:
            return
        self.evidence.append(
            state.run_id, {"event": event, "at": self.clock.now().isoformat(), **fields}
        )

    def _kinds(self) -> frozenset[SignalKind]:
        return self.surface.supported_signal_kinds()


@dataclass
class _RunState:
    """Everything one run accumulates, kept out of the engine's signatures.

    Mutable, while everything it holds is frozen. `run` is advanced by _settle,
    _locate, _check_policy, _checkpoint and _finish; no other method moves it.
    """

    capability: Capability
    run_id: str
    bound: Mapping[str, str]
    # None only between the approval gate and Run.start. A refused capability
    # never produces a run, so there is nothing to record about one.
    run: Run | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    resolved_via: dict[str, SignalKind] = field(default_factory=dict)
    # None until the first screen has been looked at. A sentinel typed as a
    # success would be a small lie that a later result could repeat.
    classification: Classification | None = None

    @property
    def active(self) -> Run:
        """The run, once it has started. Every caller is past the gate."""
        if self.run is None:
            raise MalformedArtifact("the run has not started")
        return self.run

    def detail(self) -> str:
        return self.classification.detail if self.classification else ""

    def condition(self) -> Condition | None:
        if self.classification is None or self.classification.condition_name is None:
            return None
        name = self.classification.condition_name
        return next((c for c in self.capability.conditions if c.name == name), None)


def _reason_for(condition: Condition | None) -> EscalationReason:
    """Which bound this escalation counts against.

    Per reason, so a session that aged out and an ambiguous control later in the
    same run each get their own turn.
    """
    if condition is not None and condition.code == "AUTHENTICATION_REQUIRED":
        return EscalationReason.AUTHENTICATION_REQUIRED
    return EscalationReason.UNKNOWN_STATE


def _bind_inputs(capability: Capability, inputs: Mapping[str, str]) -> dict[str, str]:
    """Check inputs against the contract before anything runs.

    A missing required input, or one that does not look like what the contract
    said it would, is caught here rather than typed into a live application and
    rejected by a form three screens later.
    """
    bound: dict[str, str] = {}
    for spec in capability.contract.inputs:
        if spec.name not in inputs:
            if spec.required:
                raise MalformedArtifact(f"input {spec.name!r} is required and was not supplied")
            # Declared and omitted. Bound to nothing rather than left unbound,
            # so a template mentioning it fills with nothing instead of being
            # mistaken for a name the contract never declared.
            bound[spec.name] = ""
            continue
        value = str(inputs[spec.name])
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            raise MalformedArtifact(
                f"input {spec.name!r} does not match the declared pattern {spec.pattern!r}"
            )
        bound[spec.name] = value
    return bound


def _fill(template: str | None, bound: Mapping[str, str]) -> str | None:
    """Replace {{ inputs.name }} with the value supplied for this run.

    A name the contract never declared is refused rather than left as it was.
    Returning the template unchanged would type the literal text
    "{{ inputs.member_od }}" into a live application and write it to evidence,
    and a typo in an artifact should not become input to a bank.
    """
    if template is None:
        return None

    malformed = sorted(
        found.group(0)
        for found in PLACEHOLDER_SHAPED.finditer(template)
        if not PLACEHOLDER.fullmatch(found.group(0))
    )
    if malformed:
        raise MalformedArtifact(
            f"template contains placeholder(s) {malformed} that are not "
            "of the form {{ inputs.name }}"
        )

    unknown = sorted({m.group(1) for m in PLACEHOLDER.finditer(template)} - set(bound))
    if unknown:
        raise MalformedArtifact(
            f"template refers to input(s) {unknown} that the contract does not declare"
        )
    return PLACEHOLDER.sub(lambda match: bound[match.group(1)], template)


def _fill_target(target: TargetSpec, bound: Mapping[str, str]) -> TargetSpec:
    """A spec with its placeholders bound, so it describes this run's controls.

    Only the name is filled. A role is a kind of control and never varies with
    the data, and a relation describes structure; binding either would mean the
    artifact was describing something other than the application.
    """
    # PLACEHOLDER_SHAPED, not PLACEHOLDER. A malformed placeholder in a signal
    # name does not match the strict pattern, so guarding with it here returned
    # early and handed the literal braces to resolve() as a control name —
    # skipping the very validation _fill was given to do.
    if not any(PLACEHOLDER_SHAPED.search(signal.name or "") for signal in target.signals):
        return target
    return replace(
        target,
        signals=tuple(
            replace(signal, name=_fill(signal.name, bound)) for signal in target.signals
        ),
    )


def _fill_detector(detector: Detector, bound: Mapping[str, str]) -> Detector:
    """A detector with its placeholders bound, so it compares real values.

    Name and text as well as value: a control can be named after the data it
    carries, and the link to a member is named with that member's number.

    url_pattern is left alone. A route keeps its placeholder, because
    identifiers must not enter an Observation and therefore never appear in one
    to compare against.
    """
    return replace(
        detector,
        value=_fill(detector.value, bound),
        name=_fill(detector.name, bound),
        text=_fill(detector.text, bound),
    )


def _checkpoint_holds(
    checkpoint: str, state: "_RunState", classification: Classification
) -> bool:
    """Whether the named checkpoint is true of the screen in front of us.

    Read the same way a precondition is read, and for the same reason: a
    condition that names a checkpoint as the thing it resumes on is, by
    construction, the thing that makes that checkpoint false. session_expired
    resumes on authenticated_session, so session_expired matching means
    authenticated_session does not hold.

    A screen that matches no such condition passes. That is deliberate and it
    is the weaker half of this test: it establishes that nothing known to be
    wrong is on screen, not that everything required is. A capability wanting
    more than that says so with a step checkpoint, which runs next anyway.
    """
    for condition in state.capability.conditions:
        if condition.name not in classification.matched:
            continue
        if condition.resume_checkpoint == checkpoint:
            return False
    return True


def _describe(detector: Detector) -> str:
    """What this detector was looking for, in words a person can act on."""
    if detector.kind is DetectorKind.NODE_PRESENT:
        return f"{detector.role} named {detector.name!r}"
    if detector.kind is DetectorKind.URL_PATTERN:
        return f"route {detector.url_pattern!r}"
    if detector.kind is DetectorKind.FIELD_VALUE_EQUALS:
        return f"the field to contain {detector.value!r}"
    if detector.kind is DetectorKind.TEXT_PRESENT:
        return f"the text {detector.text!r}"
    if detector.kind is DetectorKind.TEXT_ABSENT:
        return f"the text {detector.text!r} to be gone"
    return f"page title {detector.title!r}"


def _transform(capability: Capability, output_name: str, value: str) -> str:
    spec = next((o for o in capability.contract.outputs if o.name == output_name), None)
    if spec is not None and spec.transform == "strip_whitespace":
        return value.strip()
    return value
