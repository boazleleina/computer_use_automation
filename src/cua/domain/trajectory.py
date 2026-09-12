"""What a discovery run actually did.

Kept apart from the model's transcript on purpose. A transcript is what the
model said, including the proposals that were refused, the ones it withdrew and
the malformed output an adapter retried past. A trajectory is what the
application had done to it. Compiling the second into a capability is honest;
compiling the first would turn things the system declined to do into steps it
promises to repeat.

The separation is also what makes the evidence readable. A reviewer asking
"what did this run do to the member record" should not have to subtract the
model's false starts from its successes to find out.

Everything here is a record, not a decision. Nothing in this module knows how a
trajectory is produced or what becomes of it.
"""

from dataclasses import dataclass
from enum import StrEnum

from cua.domain.actions import ProposedAction
from cua.domain.observation import Node, Observation


class StopReason(StrEnum):
    """Why the loop ended. Exactly one of these is true of any finished run.

    GOAL_REACHED is the only one a capability may be compiled from. The rest
    describe a run that stopped, and a flow that stopped is not a flow that can
    be replayed — the interesting one being REPEATED_STATE, where the model was
    still proposing actions and the screen had stopped changing.
    """

    GOAL_REACHED = "goal_reached"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    REPEATED_STATE = "repeated_state"
    POLICY_REFUSED = "policy_refused"
    MODEL_STUCK = "model_stuck"


@dataclass(frozen=True)
class Budget:
    """What is left of the run, as the decider is told it.

    Paired with StopReason deliberately: these are the two bounds that end a
    run without anything going wrong — MAX_STEPS and TIMEOUT — expressed as
    what remains rather than as what was configured. A model told the limit is
    forty learns nothing; a model told it has two steps left can decide whether
    to spend them looking or to say it is stuck.

    A decider is free to ignore this. It is information, not a rule: the loop
    enforces the bounds itself, because a limit the model could talk itself
    past would not be a limit.
    """

    steps_remaining: int
    ms_remaining: int


@dataclass(frozen=True)
class ExecutedStep:
    """One action that was permitted, performed, and observed afterwards.

    A proposal that policy refused never becomes one of these. That is the
    point: the trajectory is the permitted subset, so "every step in this
    capability passed policy when it was recorded" is true by construction
    rather than by review.

    Both observations are kept whole. `before` is what the model was looking at
    when it chose, which is what a compiler needs to describe the control in
    terms that will find it again; `after` is what the action produced, which is
    the only honest source for a checkpoint. Recording a summary of either would
    mean deciding now what a later phase is allowed to ask.
    """

    proposal: ProposedAction
    before: Observation
    after: Observation

    # The control the ref resolved to. A NodeRef is opaque and belongs to one
    # observation, so it says nothing a month later; the node it named carries
    # the role and accessible name a target spec is built from.
    node: Node | None = None

    # What a read returned. Only ever set for a read step, and it is the reason
    # a capability can declare a typed output at all.
    read_value: str | None = None


@dataclass(frozen=True)
class Trajectory:
    """The executed path of one discovery run, and how it ended."""

    goal: str
    steps: tuple[ExecutedStep, ...]
    stopped_because: StopReason

    @property
    def succeeded(self) -> bool:
        """Whether this run reached the goal it was given.

        A separate question from whether anything happened. A run can execute
        nine permitted steps and still stop on max_steps, and compiling that
        would produce a capability that confidently performs most of a task.
        """
        return self.stopped_because is StopReason.GOAL_REACHED

    @property
    def reads(self) -> tuple[ExecutedStep, ...]:
        """The steps that extracted a value.

        Separated here rather than in the compiler because "what did this run
        learn" is a question about the trajectory, and the answer is the same
        whoever asks it.
        """
        return tuple(step for step in self.steps if step.read_value is not None)
