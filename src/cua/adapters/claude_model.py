"""The model, behind the Model port.

Tool use rather than free text, and three tools rather than one with a mode
field. The schema is the validation: a response that is not a call to act,
complete or stuck is not a thing this adapter can return, so "never parse prose
as a control instruction" is enforced by the API rather than by a parser here
that has to be trusted.

The action vocabulary in the schema is generated from ActionType, so the set
the model may choose from and the set Policy checks against cannot drift. An
action nobody implements cannot be proposed, and it is refused again downstream
anyway, which is the belt and the braces.

Controls are addressed by their index in the observation the model was shown.
Indices rather than the application's own ids because the application's ids are
generated and meaningless, and rather than NodeRef values because those are
internal plumbing the model has no reason to see or reproduce.

Malformed output is retried here, bounded. The port promises callers a
well-formed proposal or a ModelError, so a caller that had to sanity check what
it received would make that promise worthless.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic, AnthropicError
from anthropic.types import MessageParam, ToolChoiceAnyParam, ToolParam

from cua.domain.actions import ActionType, ProposalKind, ProposedAction
from cua.domain.errors import ModelError
from cua.domain.observation import Observation

# Retries for output that does not fit the schema. Two, because a model that
# has produced garbage twice against the same screen is not one attempt away
# from producing something useful, and each attempt costs a call.
MALFORMED_RETRIES = 2

# Actions the model may propose. Generated from the enum so this cannot drift
# from what Policy enforces. READ is included: extracting a value is a step in
# the flow, not something that happens after it.
ACTION_VALUES = [action.value for action in ActionType]

SYSTEM = """\
You operate a legacy bank back-office application by looking at its \
accessibility tree and acting on it, the way a member services operator would.

You will be given a goal, the controls currently on screen, and what you have \
done so far. Call exactly one tool each turn.

Rules that matter:
- Address a control by the index shown beside it. Never invent an index.
- One action per turn. Look at the result before deciding the next one.
- A control's value is shown after it. If a field already holds what you were \
going to type, it is done.
- Call complete only when the goal has actually been achieved on screen, not \
when the next step is obvious.
- Call stuck rather than guessing. A wrong click in this application is worse \
than a stopped run.
- Give a one sentence reason every time. Someone reads these months later to \
understand what the run did.
"""

ACT_TOOL: ToolParam = {
    "name": "act",
    "description": "Perform one action on one control.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": "One sentence on why this action, now.",
            },
            "action": {"type": "string", "enum": ACTION_VALUES},
            "target": {
                "type": "integer",
                "description": (
                    "Index of the control, from the list on screen. Omit only for navigate."
                ),
            },
            "value": {
                "type": "string",
                "description": "Text to type, option to select, or route to navigate to.",
            },
        },
        "required": ["rationale", "action"],
    },
}

COMPLETE_TOOL: ToolParam = {
    "name": "complete",
    "description": "The goal has been achieved on screen. Stop.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {"type": "string", "description": "What on screen shows it is done."}
        },
        "required": ["rationale"],
    },
}

STUCK_TOOL: ToolParam = {
    "name": "stuck",
    "description": "The goal cannot be achieved safely from here. Stop.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {"type": "string", "description": "What is blocking progress."}
        },
        "required": ["rationale"],
    },
}

TOOLS: list[ToolParam] = [ACT_TOOL, COMPLETE_TOOL, STUCK_TOOL]

# Forces a tool call, so a reply in prose is not a shape this adapter can
# receive rather than one it has to detect and reject.
ANY_TOOL: ToolChoiceAnyParam = {"type": "any"}

KIND_BY_TOOL = {
    "act": ProposalKind.ACT,
    "complete": ProposalKind.COMPLETE,
    "stuck": ProposalKind.STUCK,
}


@dataclass
class ClaudeModel:
    """One model, one call per proposal."""

    client: Anthropic
    model: str = "claude-sonnet-5"
    max_tokens: int = 4096

    # No temperature. The SDK does not take one, and chasing determinism at the
    # sampling knob would be the wrong fix anyway: discovery is allowed to be
    # non-deterministic, which is precisely why its output is an artifact a
    # person reviews rather than a decision made fresh on every invocation.
    # Determinism is replay's property, and replay has no model in it.

    # Kept so a transcript can be written alongside the trajectory. The raw
    # exchange is the model's own record and belongs apart from what the
    # application had done to it.
    transcript: list[dict[str, Any]] = field(default_factory=list, init=False)

    def propose(
        self,
        goal: str,
        observation: Observation,
        history: Sequence[ProposedAction],
    ) -> ProposedAction:
        """One validated proposal, or a ModelError after retrying.

        `history` arrives from the caller rather than being accumulated here,
        so the same call with the same inputs asks the same question. An
        adapter holding its own conversation state could not be replayed and
        could not be tested twice.
        """
        prompt = _prompt(goal, observation, history)
        complaint: str | None = None

        for attempt in range(MALFORMED_RETRIES + 1):
            block = self._call(prompt, complaint, attempt)
            try:
                return _proposal_from(block, observation)
            except ModelError as error:
                complaint = str(error)
                self.transcript.append(
                    {"attempt": attempt, "rejected": complaint, "input": block.get("input")}
                )

        raise ModelError(
            f"the model produced output that does not fit the schema "
            f"{MALFORMED_RETRIES + 1} times; last complaint: {complaint}"
        )

    def _call(self, prompt: str, complaint: str | None, attempt: int) -> dict[str, Any]:
        """One request, returning the tool call it made.

        A response with no tool call is itself malformed. tool_choice forces
        one, so this is the API failing its own contract rather than the model
        being creative, and it is treated the same way: retried, then given up
        on.
        """
        content = prompt if complaint is None else f"{prompt}\n\nPrevious attempt: {complaint}"
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM,
                tools=TOOLS,
                tool_choice=ANY_TOOL,
                messages=_messages(content),
            )
        except AnthropicError as error:
            raise ModelError(f"the model call failed: {error}") from error

        self.transcript.append(
            {
                "attempt": attempt,
                "request": content,
                "stop_reason": response.stop_reason,
                "content": [block.model_dump() for block in response.content],
            }
        )

        for block in response.content:
            if block.type == "tool_use":
                return {"name": block.name, "input": block.input}
        raise ModelError("the response contained no tool call")


def _proposal_from(block: Mapping[str, Any], observation: Observation) -> ProposedAction:
    """Turn one tool call into a proposal, or say why it is not one.

    Everything the schema cannot express is checked here: that the index names
    a control on the screen the model was shown, and that an action which needs
    a control was given one. The schema can require a field; it cannot know
    how many controls are on the page.
    """
    name = str(block.get("name"))
    kind = KIND_BY_TOOL.get(name)
    if kind is None:
        raise ModelError(f"unknown tool {name!r}")

    payload = block.get("input")
    if not isinstance(payload, Mapping):
        raise ModelError(f"tool {name!r} was called with {type(payload).__name__}, not an object")

    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        raise ModelError(f"tool {name!r} was called without a rationale")

    if kind is not ProposalKind.ACT:
        return ProposedAction(kind=kind, rationale=rationale)

    try:
        action_type = ActionType(str(payload.get("action")))
    except ValueError as error:
        raise ModelError(f"{payload.get('action')!r} is not an action") from error

    value = payload.get("value")
    if action_type is ActionType.NAVIGATE:
        if not value:
            raise ModelError("navigate was proposed with no route")
        return ProposedAction(
            kind=kind, rationale=rationale, action_type=action_type, value=str(value)
        )

    ref = _ref_at(payload.get("target"), observation)
    return ProposedAction(
        kind=kind,
        rationale=rationale,
        action_type=action_type,
        node_ref=ref,
        value=None if value is None else str(value),
    )


def _ref_at(target: object, observation: Observation) -> Any:
    """The ref for an index, or a complaint about the index.

    Out of range is the common failure and the one worth a clear message: the
    model has read the list, counted wrong, and will usually get it right when
    told the range.
    """
    if not isinstance(target, int) or isinstance(target, bool):
        raise ModelError(f"target {target!r} is not a control index")
    if not 0 <= target < len(observation.nodes):
        raise ModelError(
            f"there is no control {target} on this screen; "
            f"the indices run 0 to {len(observation.nodes) - 1}"
        )
    return observation.nodes[target].ref


def _prompt(goal: str, observation: Observation, history: Sequence[ProposedAction]) -> str:
    """What the model is shown. Structured, not chatty.

    The screen is rendered as a numbered list rather than as markup. Markup is
    what this system exists to avoid depending on, and handing the model raw
    HTML would put the brittleness back one layer up.
    """
    lines = [
        f"Goal: {goal}",
        "",
        f"Screen: {observation.page_title} at {observation.url_pattern}",
        "",
        "Controls:",
    ]
    for index, node in enumerate(observation.nodes):
        parts = [f"  [{index}] {node.role}"]
        if node.name:
            parts.append(f'"{node.name}"')
        if node.text:
            parts.append(f"= {node.text!r}")
        if not node.enabled:
            parts.append("(disabled)")
        if node.destination:
            parts.append(f"-> {node.destination}")
        lines.append(" ".join(parts))

    if history:
        lines += ["", "Already done:"]
        lines += [f"  {_summary(step)}" for step in history]
    else:
        lines += ["", "Nothing has been done yet."]

    return "\n".join(lines)


def _summary(step: ProposedAction) -> str:
    if step.action_type is None:
        return f"{step.kind.value}: {step.rationale}"
    value = f" {step.value!r}" if step.value else ""
    return f"{step.action_type.value}{value}: {step.rationale}"


def _messages(content: str) -> list[MessageParam]:
    """One user turn. The conversation is rebuilt each call, never accumulated,
    so the same inputs ask the same question."""
    return [{"role": "user", "content": content}]


def anthropic_client(api_key: str) -> Anthropic:
    """The SDK client, built in one place.

    Here rather than in composition so that nothing outside this module needs
    to import the SDK to wire up a model.
    """
    return Anthropic(api_key=api_key)
