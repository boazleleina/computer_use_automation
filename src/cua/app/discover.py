"""Driving an application toward a goal with a model in the loop.

The other half of replay, and deliberately the same shape: observe, decide,
check, act, verify. Only the decider changes. Replay reads its decision out of
an artifact; this asks a model. Everything downstream of the decision — policy,
the surface, the evidence stream — is identical, which is what makes the claim
"discovery and replay are held to the same rules" checkable rather than stated.

This loop never parses prose. The Model port hands back a validated
ProposedAction or raises, so there is no point in this file where text becomes
an instruction. What is left here is whether a well-formed proposal makes sense
against the screen in front of it, which the adapter cannot know.

A proposal that policy refuses does not become part of the trajectory. It is
written to evidence — a refusal is one of the more interesting things a run can
record — but the trajectory is the permitted subset, so a capability compiled
from it cannot contain a step that was not allowed when it was found.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from cua.domain.actions import ActionType, ProposalKind, ProposedAction
from cua.domain.errors import ModelError, SurfaceError
from cua.domain.observation import Node, Observation
from cua.domain.policy import Denied, Policy
from cua.domain.trajectory import Budget, ExecutedStep, StopReason, Trajectory
from cua.ports.clock import Clock
from cua.ports.evidence import EvidenceSink
from cua.ports.model import Model
from cua.ports.surface import Surface

# How much of the run the model is shown. The whole history would grow the
# prompt without bound and bury the recent steps, which are the ones that
# explain the screen it is looking at.
HISTORY_DEPTH = 8


@dataclass(frozen=True)
class DiscoveryLimits:
    """Bounds on the loop, not parameters of the model.

    They hold whichever Model implementation is wired in, a recorded one
    included, which is the point: a run that cannot end is a worse failure than
    one that ends early, and that must not depend on the model behaving.

    A policy refusal is not on this list because it is not optional. A model
    that proposed a denied action will propose it again against the same
    screen, so continuing means either the same refusal forever or acting on
    the second guess, and neither is a thing to make configurable.
    """

    max_steps: int = 40
    run_ms: int = 900_000
    stop_on_repeated_state: bool = True


@dataclass
class DiscoverCapability:
    """Run one goal against one surface, with a model deciding.

    Returns a Trajectory whatever happens. A run that stopped is still a record
    of what it did, and throwing that away because it did not reach the goal
    would discard the evidence that explains why.
    """

    surface: Surface
    model: Model
    policy: Policy
    clock: Clock
    evidence: EvidenceSink | None = None
    limits: DiscoveryLimits = field(default_factory=DiscoveryLimits)

    def run(self, goal: str, run_id: str) -> Trajectory:
        deadline = self.clock.monotonic_ms() + self.limits.run_ms
        steps: list[ExecutedStep] = []

        self._emit(
            run_id,
            "discovery_started",
            goal=goal,
            max_steps=self.limits.max_steps,
            run_ms=self.limits.run_ms,
        )

        observation = self.surface.observe()
        self._emit_observed(run_id, observation)

        while True:
            bound = self._bound_reached(len(steps), deadline)
            if bound is not None:
                return self._finish(run_id, goal, steps, bound)

            outcome = self._one_step(
                run_id, goal, observation, steps, self._budget(len(steps), deadline)
            )
            if isinstance(outcome, StopReason):
                return self._finish(run_id, goal, steps, outcome)

            steps.append(outcome)
            if self._went_nowhere(outcome):
                return self._finish(run_id, goal, steps, StopReason.REPEATED_STATE)

            observation = outcome.after

    # ---- one turn of the loop ----------------------------------------------

    def _one_step(
        self,
        run_id: str,
        goal: str,
        observation: Observation,
        history: Sequence[ExecutedStep],
        budget: Budget,
    ) -> ExecutedStep | StopReason:
        """Ask, check, act. Either a step happened or the run is over."""
        proposal = self._propose(goal, observation, history, budget)
        if isinstance(proposal, StopReason):
            return proposal

        # Role and name, not the ref. A NodeRef belongs to one observation and
        # says nothing in a later run, so a transcript recorded with refs alone
        # could not be replayed against a fresh session — which is exactly what
        # RecordedModel and the offline suite need to do.
        named = observation.node(proposal.node_ref) if proposal.node_ref else None
        self._emit(
            run_id,
            "proposed",
            step=len(history),
            kind=proposal.kind.value,
            rationale=proposal.rationale,
            action=proposal.action_type.value if proposal.action_type else None,
            value=proposal.value,
            target_role=named.role if named else None,
            target_name=named.name if named else None,
        )

        if proposal.kind is ProposalKind.COMPLETE:
            return StopReason.GOAL_REACHED
        if proposal.kind is ProposalKind.STUCK:
            return StopReason.MODEL_STUCK

        return self._perform(run_id, proposal, observation)

    def _propose(
        self,
        goal: str,
        observation: Observation,
        history: Sequence[ExecutedStep],
        budget: Budget,
    ) -> ProposedAction | StopReason:
        """Ask the model, or stop because asking failed.

        A ModelError has already survived whatever retry the adapter does, so
        by the time it reaches here the model has failed to produce a usable
        proposal more than once. Retrying again from this layer would be
        guessing that the third attempt differs from the second.
        """
        try:
            return self.model.propose(goal, observation, history[-HISTORY_DEPTH:], budget)
        except ModelError:
            return StopReason.MODEL_STUCK

    # ---- the checks --------------------------------------------------------

    def _budget(self, taken: int, deadline: int) -> Budget:
        """What is left, as the decider is told it."""
        return Budget(
            steps_remaining=max(0, self.limits.max_steps - taken),
            ms_remaining=max(0, deadline - self.clock.monotonic_ms()),
        )

    def _bound_reached(self, taken: int, deadline: int) -> StopReason | None:
        """Bounds checked before asking, not after acting.

        Before, so a run that has already spent its budget does not pay for one
        more model call to be told it is over.
        """
        if taken >= self.limits.max_steps:
            return StopReason.MAX_STEPS
        if self.clock.monotonic_ms() >= deadline:
            return StopReason.TIMEOUT
        return None

    def _went_nowhere(self, executed: ExecutedStep) -> bool:
        """Whether the action left the screen exactly as it found it.

        Consecutive rather than global. An action that changes nothing is a
        mistake, so this is safe to stop on; a screen seen twice at any point in
        a run is not, because a flow that legitimately returns to a search page
        to look up a second member would trip it. max_steps bounds the cyclic
        case without inventing a rule about which cycles are wrong.

        A read is exempt, and it is not an exception so much as the definition:
        reading a value off the screen is supposed to leave the screen alone.
        Without this, any capability that extracts more than nothing stops on
        its first read and reports itself stuck — which is every capability
        worth having.
        """
        if not self.limits.stop_on_repeated_state:
            return False
        if executed.proposal.action_type is ActionType.READ:
            return False
        return _fingerprint(executed.before) == _fingerprint(executed.after)

    # ---- doing it ----------------------------------------------------------

    def _perform(
        self, run_id: str, proposal: ProposedAction, observation: Observation
    ) -> ExecutedStep | StopReason:
        """Check a well-formed proposal against this screen, then carry it out.

        The adapter proved the proposal is shaped like a proposal. Whether the
        control it names is on the screen the model was shown, and whether
        policy permits touching it, are questions only this layer can answer.
        """
        action_type = proposal.action_type
        if action_type is None:
            self._emit(run_id, "proposal_rejected", detail="an act proposal named no action")
            return StopReason.MODEL_STUCK

        if action_type is ActionType.NAVIGATE:
            node = None
        else:
            found = self._control(run_id, proposal, observation)
            if found is None:
                return StopReason.MODEL_STUCK
            node = found

        route = proposal.value if action_type is ActionType.NAVIGATE else None
        verdict = self.policy.evaluate(action_type, node, route=route)
        self._emit(
            run_id,
            "policy_check",
            action=action_type.value,
            allowed=not isinstance(verdict, Denied),
            rule=verdict.rule.value if isinstance(verdict, Denied) else None,
            reason=verdict.reason if isinstance(verdict, Denied) else None,
        )
        if isinstance(verdict, Denied):
            return StopReason.POLICY_REFUSED

        return self._act(run_id, action_type, proposal, observation, node)

    def _control(
        self, run_id: str, proposal: ProposedAction, observation: Observation
    ) -> Node | None:
        """The control this proposal names, if it is on the screen it was shown.

        None means the run is over. Nothing here can repair a ref that names
        nothing, and acting on the nearest similar control is how automation
        clicks the wrong button.
        """
        if proposal.node_ref is None:
            self._emit(run_id, "proposal_rejected", detail="no control was named")
            return None
        node = observation.node(proposal.node_ref)
        if node is None:
            self._emit(
                run_id,
                "proposal_rejected",
                detail=f"ref {proposal.node_ref.value!r} is not on the screen it was given",
            )
            return None
        return node

    def _act(
        self,
        run_id: str,
        action_type: ActionType,
        proposal: ProposedAction,
        before: Observation,
        node: Node | None,
    ) -> ExecutedStep | StopReason:
        """Touch the application, then look at what that did."""
        try:
            read_value = self._carry_out(run_id, action_type, proposal)
        except SurfaceError as error:
            self._emit(run_id, "surface_failed", action=action_type.value, detail=str(error))
            return StopReason.MODEL_STUCK

        after = self.surface.observe()
        self._emit_observed(run_id, after)
        return ExecutedStep(
            proposal=proposal, before=before, after=after, node=node, read_value=read_value
        )

    def _carry_out(
        self, run_id: str, action_type: ActionType, proposal: ProposedAction
    ) -> str | None:
        """The one call to the surface, and what it gave back.

        A read is not an act. It returns a value and changes nothing, which is
        why the surface separates them and why only this branch can produce the
        value a typed output is later declared from.
        """
        if action_type is not ActionType.READ:
            self.surface.act(action_type, proposal.node_ref, proposal.value)
            self._emit(run_id, "acted", action=action_type.value, value=proposal.value)
            return None

        if proposal.node_ref is None:  # pragma: no cover - _control refused it already
            raise SurfaceError("read named no control")
        value = self.surface.read(proposal.node_ref)
        self._emit(run_id, "read", value=value)
        return value

    # ---- reporting ---------------------------------------------------------

    def _finish(
        self, run_id: str, goal: str, steps: list[ExecutedStep], why: StopReason
    ) -> Trajectory:
        trajectory = Trajectory(goal=goal, steps=tuple(steps), stopped_because=why)
        self._emit(
            run_id,
            "discovery_finished",
            stopped_because=why.value,
            succeeded=trajectory.succeeded,
            steps=len(steps),
        )
        return trajectory

    def _emit_observed(self, run_id: str, observation: Observation) -> None:
        self._emit(
            run_id,
            "observed",
            url_pattern=observation.url_pattern,
            page_title=observation.page_title,
            nodes=len(observation.nodes),
        )

    def _emit(self, run_id: str, event: str, **fields: object) -> None:
        """One record. Redaction happens in the sink, never here.

        A run with no sink still runs. Evidence is how a run is reviewed, not
        how it works, and a discovery run in a scratch session should not need
        a writable directory.
        """
        if self.evidence is None:
            return
        self.evidence.append(
            run_id, {"event": event, "at": self.clock.now().isoformat(), **fields}
        )


def _fingerprint(observation: Observation) -> tuple[object, ...]:
    """What makes one screen the same screen as another.

    Route, title, and every control with the value it is carrying. The values
    matter: typing into a field changes nothing else about the page, and
    without them the step that fills the search box would look like a step that
    did nothing at all.
    """
    return (
        observation.url_pattern,
        observation.page_title,
        tuple((n.role, n.name, n.text) for n in observation.nodes),
    )
