"""What may this run touch?

Pure computation over declared rules. Not a port, and deliberately so: making
the safety guard substitutable would mean a fake policy could be wired in by
accident and permit everything. There is no second implementation anyone wants,
and the rules are already trivially testable as a function.

Two independent layers, both always applied:

    scope       allowed origins and routes. Where a run may go.
    landmines   denied controls. What a run may never touch, whatever its
                effect class and wherever it leads.

They are layers, not modes. A route allowlist cannot guard a control whose
destination is unknowable before the click — a submit button has no href — and a
denial list cannot enumerate every page a run should stay off. Each covers the
other's blind spot, so a config that offered a choice between them would be
offering a choice of which hole to leave open.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, assert_never

from cua.domain.actions import ActionType, Effect
from cua.domain.observation import Node


class PolicyRule(StrEnum):
    """Which rule refused. An enum rather than prose so a refusal is countable
    in evidence, not only readable."""

    ACTION_NOT_ALLOWED = "action_not_allowed"
    CONTROL_DENIED = "control_denied"
    ROUTE_NOT_ALLOWED = "route_not_allowed"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    APPROVAL_REQUIRED = "approval_required"
    SECRET_CONTROL = "secret_control"


class Sensitivity(StrEnum):
    """What a value is, declared on a capability's inputs and outputs.

    Redaction is driven by this, not by scanning recorded text. A pattern like
    "(?i)password" matches the field label while the value typed into it goes
    straight through, and a scan's default is to publish whatever it does not
    recognise. A declared type can be checked once and fails closed.

    SECRET    credentials, tokens, cookies. Never recorded in any form.
    PERSONAL  member numbers, names, balances. Masked.
    INTERNAL  routes, control names, timings. Recorded as they are.
    """

    SECRET = "secret"
    PERSONAL = "personal"
    INTERNAL = "internal"


class Rendering(StrEnum):
    """What happens to a value on its way to evidence.

    DROP    the field survives, the value does not. Removing the key entirely
            would make the record read as though nothing was ever entered.
    MASK    enough remains to tell two runs apart, not enough to identify a
            person.
    RECORD  written as it is.
    """

    DROP = "drop"
    MASK = "mask"
    RECORD = "record"


@dataclass(frozen=True)
class RedactionRules:
    """How each declared class is rendered. Comes from config.evidence.redaction.

    `unclassified` is the property a text scan cannot have. A scan publishes
    whatever it fails to recognise; this drops it, so forgetting to classify a
    field costs evidence rather than a member's data.
    """

    secret: Rendering = Rendering.DROP
    personal: Rendering = Rendering.MASK
    internal: Rendering = Rendering.RECORD
    unclassified: Rendering = Rendering.DROP

    def rendering_for(self, sensitivity: Sensitivity | None) -> Rendering:
        if sensitivity is None:
            return self.unclassified
        if sensitivity is Sensitivity.SECRET:
            return self.secret
        if sensitivity is Sensitivity.PERSONAL:
            return self.personal
        if sensitivity is Sensitivity.INTERNAL:
            return self.internal
        assert_never(sensitivity)


MASK_KEEP_LAST = 4
MASK_PREFIX = "*" * MASK_KEEP_LAST


def mask(value: str) -> str:
    """Keep the last four characters, hide the rest.

    A value of four characters or fewer is hidden completely: masking it to its
    last four would not be masking at all.
    """
    if len(value) <= MASK_KEEP_LAST:
        return MASK_PREFIX
    return MASK_PREFIX + value[-MASK_KEEP_LAST:]


# A shorter value would match half the record by coincidence. Six digits of
# member number and any real credential are comfortably above this.
MIN_LEAK_LENGTH = 4


def redact_event(
    event: Mapping[str, object],
    declared: Mapping[str, Sensitivity],
    rules: RedactionRules,
    known: Mapping[str, Sensitivity] | None = None,
) -> dict[str, object]:
    """Render one evidence record according to what the capability declared.

    Recurses into mappings and sequences, because a record is not flat and a
    value hidden one level down still reaches disk. Returns a new structure: a
    caller must not be able to tell whether its record was redacted by looking
    at the object it still holds.

    Rendering keys off the field name, which is correct for the value in that
    field and blind to the same value copied into another one — a member number
    spliced into a url, an error message quoting what was typed. So a second
    pass goes over what was rendered looking for values the record itself
    declared sensitive. That is not pattern matching: the exact strings are
    known, because the capability handed them over in this same record.

    What the second pass does depends on what leaked, and the two cases are not
    the same question:

    A personal value is masked where it sits, so "search for member 100045"
    becomes "search for member ****0045". The rule is that the raw value never
    reaches disk, and that holds exactly — there is nothing left to recover.
    Dropping the whole field would satisfy the rule too, and would also throw
    away the sentence, which is the only reason the field was recorded.

    A secret takes the field with it. Masking a credential in place would
    publish its last four characters, and four characters of a password is not
    a redacted password. There is no version of a leaked secret worth keeping.

    The first line of defence is upstream — url_pattern is a route by
    construction and never carries an identifier. This catches the case where
    something else did.
    """
    rendered = {key: _render(key, value, declared, rules) for key, value in event.items()}

    # What this record declared, plus what the run declared. The second half
    # matters more than it looks: a record is only self-describing when it
    # happens to carry the value in a classified field, and most do not. A
    # click records no value at all, so a rationale in that record mentioning
    # the member number had nothing to be compared against and went to disk
    # raw. The run knows its own identifiers from the moment it is given them.
    leaked = dict(known or {})
    leaked |= _sensitive_values(event, declared)
    if not leaked:
        return rendered
    return {key: _contain(value, leaked) for key, value in rendered.items()}


def _sensitive_values(
    value: object,
    declared: Mapping[str, Sensitivity],
    key: str = "",
) -> dict[str, Sensitivity]:
    """Every raw string in the record whose field was declared secret or personal.

    Carries the class along with the string, because what to do about a leak
    depends on which kind of value leaked.
    """
    if isinstance(value, Mapping):
        found: dict[str, Sensitivity] = {}
        for k, v in value.items():
            found |= _sensitive_values(v, declared, k)
        return found
    if isinstance(value, (list, tuple)):
        found = {}
        for item in value:
            found |= _sensitive_values(item, declared, key)
        return found

    sensitivity = declared.get(key)
    if sensitivity in (Sensitivity.SECRET, Sensitivity.PERSONAL):
        text = str(value)
        if len(text) >= MIN_LEAK_LENGTH:
            return {text: sensitivity}
    return {}


def _contain(value: object, leaked: Mapping[str, Sensitivity]) -> object:
    """Mask a leaked personal value in place; drop a field carrying a secret."""
    if isinstance(value, Mapping):
        return {k: _contain(v, leaked) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_contain(item, leaked) for item in value]
    if not isinstance(value, str):
        return value

    contained = value
    for raw, sensitivity in leaked.items():
        if raw not in contained:
            continue
        if sensitivity is Sensitivity.SECRET:
            return None
        contained = contained.replace(raw, mask(raw))
    return contained


def _render(
    key: str,
    value: object,
    declared: Mapping[str, Sensitivity],
    rules: RedactionRules,
) -> object:
    if isinstance(value, Mapping):
        return {k: _render(k, v, declared, rules) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render(key, item, declared, rules) for item in value]

    rendering = rules.rendering_for(declared.get(key))
    if rendering is Rendering.DROP:
        return None
    if rendering is Rendering.MASK:
        # Nothing to hide is not the same as something hidden. A click carries
        # no value, and masking the absence of one wrote "****" into the record,
        # which reads as a value that was entered and withheld. The whole point
        # of dropping to null rather than removing the key is that the record
        # must not imply something happened; inventing a mask breaks that in
        # the other direction.
        if value is None:
            return None
        return mask(str(value))
    if rendering is Rendering.RECORD:
        return value
    assert_never(rendering)


@dataclass(frozen=True)
class DeniedControl:
    """A control the automation must never act on.

    Matched by role and accessible name, never by selector: the domain holds no
    selectors, so a CSS list here could not be evaluated at all.
    """

    role: str
    name: str


@dataclass(frozen=True)
class Allowed:
    """The action may proceed. Carries nothing, so there is nothing to misuse."""


@dataclass(frozen=True)
class Denied:
    """The action is refused, and says why.

    The reason is required. A denial with no stated cause cannot be written into
    evidence as an account of why a run stopped.
    """

    rule: PolicyRule
    reason: str


Verdict: TypeAlias = Allowed | Denied


@dataclass(frozen=True)
class Policy:
    """The rules a run was approved under.

    Constructed once from config at the composition root and passed inward as
    plain domain data. Nothing here reads a file or an environment variable.
    """

    allowed_origins: tuple[str, ...]
    allowed_routes: tuple[str, ...]
    allowed_actions: frozenset[ActionType]
    denied_controls: tuple[DeniedControl, ...]

    def evaluate(
        self,
        action_type: ActionType,
        node: Node | None = None,
        route: str | None = None,
    ) -> Verdict:
        """Decide whether one proposed action may run.

        Checked cheapest and most general first, then most specific. The order
        matters only for which rule gets reported: any one of them refusing is
        a refusal, and reporting the most specific rule that fired is what makes
        the evidence useful rather than merely correct.
        """
        if action_type not in self.allowed_actions:
            return Denied(
                rule=PolicyRule.ACTION_NOT_ALLOWED,
                reason=f"action {action_type.value!r} is not in the allowlist",
            )

        if action_type is ActionType.NAVIGATE and route is None:
            # Navigation with nowhere to go skips every guard this method has:
            # there is no control to match against the denied list, no
            # destination to check, and no route to compare. A check that
            # cannot be performed is not a check that passed.
            return Denied(
                rule=PolicyRule.ROUTE_NOT_ALLOWED,
                reason="navigate was given no route, so there is nothing to check it against",
            )

        if node is not None and node.secret:
            # Before the denied-control list and before anything else about the
            # control, because this is the one refusal that must not depend on
            # somebody having remembered to configure it. A password field is a
            # password field on every screen of every tenant.
            #
            # Every action, not only type. Reading one returns whatever is in
            # it, and clicking one is at best pointless. There is no action on a
            # credential field that this system has business performing: a
            # person signs on, and the automation is handed a session.
            return Denied(
                rule=PolicyRule.SECRET_CONTROL,
                reason=(
                    f"{node.name or node.role!r} is a credential field; "
                    "the session must be signed on before a run starts"
                ),
            )

        if node is not None:
            denial = self._check_control(node)
            if denial is not None:
                return denial

            if node.destination is not None:
                denial = self._check_route(node.destination, following=node.name)
                if denial is not None:
                    return denial

        if route is not None:
            denial = self._check_route(route, following=None)
            if denial is not None:
                return denial

        return Allowed()

    def may_operate(self, route: str) -> Verdict:
        """Whether the run may touch the screen it is currently standing on.

        A different question from evaluate(), which asks where an action would
        take the run. Both are needed and neither implies the other: the route
        allowlist governed destinations and navigate targets, so a run that
        arrived somewhere unlisted — redirected, bounced, timed out — could
        operate that screen freely, because no individual action was going
        anywhere new.

        That gap is how a discovery run came to be typing a guessed user id
        into a sign on form. Nothing it proposed had a destination, so nothing
        was checked, and the page it was standing on was never anybody's
        question.

        Asked once per screen rather than once per action, because the answer
        cannot differ between two actions on the same page.
        """
        if route not in self.allowed_routes:
            return Denied(
                rule=PolicyRule.ROUTE_NOT_ALLOWED,
                reason=f"the run is on {route!r}, which is not in the allowlist",
            )
        return Allowed()

    def may_run_unattended(self, effect: Effect, approved: bool) -> Verdict:
        """Whether a capability of this risk class may run with nobody watching.

        A separate question from whether any individual action is allowed, and
        asked once per run rather than once per step. A read only capability
        needs no approval: the worst it can do is read the wrong number, and
        that surfaces as a wrong answer rather than a wrong account.

        Anything that writes needs an approval recorded against the artifact.
        The point of writing a procedure down is that somebody signs it off
        before it executes thousands of times, and a mutating capability that
        never got that signature has skipped the only review it was going to
        get.
        """
        if effect is Effect.READ_ONLY:
            return Allowed()
        if approved:
            return Allowed()
        return Denied(
            rule=PolicyRule.APPROVAL_REQUIRED,
            reason=f"a {effect.value} capability needs an approval before it runs unattended",
        )

    def _check_control(self, node: Node) -> Denied | None:
        """Landmines. Applies whatever the control's effect class is.

        Signing off changes no member record, so it is read_only by data effect,
        and it still ends a run that cannot recover — replay may not type a
        password. Effect class and continuity risk are separate axes, and this
        is the one that guards the second.
        """
        for denied in self.denied_controls:
            if node.role == denied.role and node.name == denied.name:
                return Denied(
                    rule=PolicyRule.CONTROL_DENIED,
                    reason=f"control {denied.name!r} ({denied.role}) is on the denied list",
                )
        return None

    def _check_route(self, target: str, following: str | None) -> Denied | None:
        """Scope. Evaluated against a route, before the run is standing on it."""
        where = f" via {following!r}" if following else ""

        if target.startswith(("http://", "https://")):
            if not any(target.startswith(origin) for origin in self.allowed_origins):
                return Denied(
                    rule=PolicyRule.ORIGIN_NOT_ALLOWED,
                    reason=f"origin of {target!r} is not allowed{where}",
                )
            target = _path_of(target)

        if target not in self.allowed_routes:
            return Denied(
                rule=PolicyRule.ROUTE_NOT_ALLOWED,
                reason=f"route {target!r} is not in the allowlist{where}",
            )
        return None


def _path_of(url: str) -> str:
    """The path portion of an absolute url, without parsing a query string.

    urllib is avoided on purpose: it is standard library, so it would be legal
    here, but a route is domain vocabulary and splitting a string the domain
    already understands keeps this readable to someone auditing the rules.
    """
    without_scheme = url.split("://", 1)[1]
    slash = without_scheme.find("/")
    if slash == -1:
        return "/"
    return without_scheme[slash:].split("?")[0]
