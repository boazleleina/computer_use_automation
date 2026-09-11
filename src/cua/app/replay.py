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
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from cua.domain.actions import ActionType
from cua.domain.capability import Capability, SignalKind, Step, TargetSpec
from cua.domain.conditions import Condition, Detector, DetectorKind, RecoveryAction
from cua.domain.errors import MalformedArtifact
from cua.domain.observation import NodeRef, Observation
from cua.domain.outcomes import Classification, Outcome, Result, classify
from cua.domain.policy import Denied, Policy
from cua.domain.resolution import Ambiguous, Resolved, resolve
from cua.domain.run import EscalationReason, Run
from cua.ports.clock import Clock
from cua.ports.evidence import EvidenceSink
from cua.ports.operator import OperatorChannel
from cua.ports.surface import Surface

PLACEHOLDER = re.compile(r"\{\{\s*inputs\.([a-zA-Z0-9_]+)\s*\}\}")

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
        state = _RunState(
            capability=capability,
            bound=_bind_inputs(capability, inputs),
            run=Run.start(
                run_id=run_id,
                capability=capability.contract.name,
                version=capability.contract.version,
            ),
        )

        for step in capability.steps:
            finished = self._run_step(state, step)
            if finished is not None:
                return finished
            state.run = state.run.advance()

        return self._finish(state)

    def _run_step(self, state: "_RunState", step: Step) -> Result | None:
        """One step, or the Result that ends the run inside it."""
        settled = self._settle(state, step)
        if isinstance(settled, Result):
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
        if isinstance(after, Result):
            return after
        return self._checkpoint(state, step, after)

    def _settle(self, state: "_RunState", step: Step) -> Observation | Result:
        """Observe and classify until the screen is one this step can work on.

        A recoverable condition is cleared and looked at again, up to the bound
        the artifact declared. Exhausting that bound asks for a person: waiting
        has demonstrably stopped working, and waiting again will not fix it.
        """
        while True:
            observation = self.surface.observe()
            state.classification = classify(observation, state.capability.conditions)
            condition = state.condition()

            if state.classification.outcome is not Outcome.RECOVERABLE:
                if state.classification.outcome in TERMINAL_OUTCOMES:
                    return self._stop(state, step)
                return observation

            recovery = condition.recovery if condition else None
            if recovery is None or state.run.recovery_count() >= recovery.max_attempts:
                state.run = state.run.escalate(
                    EscalationReason.RECOVERY_EXHAUSTED, resume_checkpoint="step_precondition"
                )
                return self._escalate(state, step)

            if recovery.action is RecoveryAction.WAIT:
                self.clock.sleep(recovery.wait_ms)
            elif recovery.target is not None:
                cleared = resolve(recovery.target, observation, self._kinds())
                if isinstance(cleared, Resolved):
                    self.surface.act(ActionType.CLICK, cleared.ref)
            state.run = state.run.recover()

    def _locate(
        self, state: "_RunState", step: Step, target: TargetSpec, observation: Observation
    ) -> NodeRef | Result:
        resolution = resolve(target, observation, self._kinds())

        if isinstance(resolution, Resolved):
            state.resolved_via = resolution.via_signal
            return resolution.ref

        if isinstance(resolution, Ambiguous):
            state.run = state.run.escalate(
                EscalationReason.AMBIGUOUS_CONTROL, resume_checkpoint="target_unique"
            )
            return self._result(
                state,
                Outcome.INTERVENTION_REQUIRED,
                detail=f"{resolution.count} controls match {target.intent!r}",
                step=step,
                expected=f"exactly one control matching {target.intent!r}",
                observed=f"{resolution.count} matched on {resolution.at_signal.value}",
            )

        state.run = state.run.escalate(
            EscalationReason.TARGET_NOT_FOUND, resume_checkpoint="target_present"
        )
        return self._result(
            state,
            Outcome.INTERVENTION_REQUIRED,
            detail=f"no control matches {target.intent!r}",
            step=step,
            expected=target.intent,
            observed=f"{observation.page_title} at {observation.url_pattern}",
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

        if isinstance(verdict, Denied):
            state.run = state.run.fail(verdict.reason)
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
            return
        self.surface.act(step.action_type, subject, _fill(step.value, state.bound))

    def _checkpoint(self, state: "_RunState", step: Step, after: Observation) -> Result | None:
        """A step that silently did nothing must not look like one that worked.

        Only reached once the screen has settled and no condition explains it,
        so a failure here means the application went somewhere nobody described.
        """
        if not step.checkpoint:
            return None

        subject = self._subject_for(step, after)

        for detector in step.checkpoint:
            bound = _fill_detector(detector, state.bound)
            if not bound.holds(after, subject):
                state.run = state.run.fail(f"checkpoint failed after {step.id}")
                return self._result(
                    state,
                    Outcome.HARD_FAILURE,
                    detail=f"{step.id} did not leave the application where it was expected",
                    step=step,
                    expected=_describe(bound),
                    observed=f"{after.page_title} at {after.url_pattern}",
                )
        return None

    def _subject_for(self, step: Step, after: Observation) -> NodeRef | None:
        """Re-resolve the step's own target against the screen being checked.

        `target_ref: self` means the control this step acted on. The ref from
        before the action points at a screen that has been replaced, so the
        control is found again rather than remembered.
        """
        if not any(d.target_ref == SELF_REF for d in step.checkpoint) or step.target is None:
            return None
        found = resolve(step.target, after, self._kinds())
        return found.ref if isinstance(found, Resolved) else None

    def _stop(self, state: "_RunState", step: Step) -> Result:
        classification = state.classification
        if classification.outcome is not Outcome.INTERVENTION_REQUIRED:
            state.run = state.run.fail(classification.detail)
            return self._result(
                state, classification.outcome, detail=classification.detail, step=step
            )

        condition = state.condition()
        state.run = state.run.escalate(
            _reason_for(condition),
            resume_checkpoint=(condition.resume_checkpoint if condition else None)
            or "step_precondition",
        )
        return self._escalate(state, step)

    def _escalate(self, state: "_RunState", step: Step) -> Result:
        """Ask for a person, and stop. The automation issues nothing further."""
        if self.operator is not None and state.run.state.value == "paused":
            self.operator.request_intervention(
                run_id=state.run.run_id,
                reason=state.classification.detail or "a person is needed",
                observation=self.surface.observe(),
            )
        return self._result(
            state,
            Outcome.INTERVENTION_REQUIRED,
            detail=state.classification.detail,
            step=step,
        )

    def _finish(self, state: "_RunState") -> Result:
        """Every step ran. The capability's own success check decides the rest."""
        final = self.surface.observe()
        for detector in state.capability.success:
            bound = _fill_detector(detector, state.bound)
            if not bound.holds(final):
                state.run = state.run.fail("the success condition did not hold")
                return self._result(
                    state,
                    Outcome.HARD_FAILURE,
                    detail="every step ran and the capability did not end where it should",
                    expected=_describe(bound),
                    observed=f"{final.page_title} at {final.url_pattern}",
                )
        state.run = state.run.succeed()
        return self._result(state, Outcome.SUCCESS, detail="the capability completed")

    def _result(
        self,
        state: "_RunState",
        outcome: Outcome,
        detail: str,
        step: Step | None = None,
        expected: str | None = None,
        observed: str | None = None,
    ) -> Result:
        result = Result(
            run_id=state.run.run_id,
            capability=state.run.capability,
            version=state.run.version,
            outcome=outcome,
            detail=detail,
            condition_name=state.classification.condition_name,
            outputs=dict(state.outputs),
            step_id=step.id if step is not None else None,
            expected=expected,
            observed=observed,
            resolved_via=state.resolved_via,
        )
        self._record(state, result)
        return result

    def _record(self, state: "_RunState", result: Result) -> None:
        if self.evidence is None:
            return
        self.evidence.append(
            result.run_id,
            {
                "capability": result.capability,
                "version": result.version,
                "outcome": result.outcome.value,
                "condition": result.condition_name,
                "detail": result.detail,
                "step": result.step_id,
                "expected": result.expected,
                "observed": result.observed,
                "resolved_via": result.resolved_via.value if result.resolved_via else None,
                "run_state": state.run.state.value,
                "at": self.clock.now().isoformat(),
            },
        )

    def _kinds(self) -> frozenset[SignalKind]:
        return self.surface.supported_signal_kinds()


# Before the first screen has been looked at there is nothing to report.
_NOTHING_SEEN_YET = Classification(outcome=Outcome.SUCCESS, condition_name=None, detail="")


@dataclass
class _RunState:
    """Everything one run accumulates, kept out of the engine's signatures."""

    capability: Capability
    bound: Mapping[str, str]
    run: Run
    outputs: dict[str, str] = field(default_factory=dict)
    classification: Classification = field(default_factory=lambda: _NOTHING_SEEN_YET)
    resolved_via: SignalKind | None = None

    def condition(self) -> Condition | None:
        name = self.classification.condition_name
        if name is None:
            return None
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
            continue
        value = str(inputs[spec.name])
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            raise MalformedArtifact(
                f"input {spec.name!r} does not match the declared pattern {spec.pattern!r}"
            )
        bound[spec.name] = value
    return bound


def _fill(template: str | None, bound: Mapping[str, str]) -> str | None:
    """Replace {{ inputs.name }} with the value supplied for this run."""
    if template is None:
        return None
    return PLACEHOLDER.sub(lambda match: bound.get(match.group(1), match.group(0)), template)


def _fill_detector(detector: Detector, bound: Mapping[str, str]) -> Detector:
    """A detector with its placeholders bound, so it compares real values.

    url_pattern is left alone: a route pattern keeps its placeholder, because
    identifiers must not enter an Observation and therefore never appear in one
    to compare against.
    """
    return replace(detector, value=_fill(detector.value, bound))


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
