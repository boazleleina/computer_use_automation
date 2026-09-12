"""Watching a browser session while a person is driving it.

The other half of the handover. Automation stops looking when it cedes control,
and without this nobody could say what changed underneath it — which is exactly
the moment a record is worth most, because it is the one part of a run nobody
reviewed in advance.

Sanitised at the point of capture, in the page, before a value crosses back
into Python. A password typed during a handover is never in this process's
memory at all: the listener reads the field's type, sees `password`, and sends
the fact that something was typed without sending what. That is a stronger
claim than redacting afterwards, and it is the only version of the claim that
survives somebody adding a debug print later.

Scoped deliberately. This records which controls were touched and where the
session went, and not keystrokes. The distinction matters: the first is an
account of what happened to a member's record, the second is surveillance of an
employee doing their job, and only one of those is this system's business.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page

from cua.adapters.observation_shape import route_pattern
from cua.domain.intervention import REDACTED, HumanAction, HumanEvent

# Installed in the page once control has been ceded. It reports the accessible
# name of what was touched, never a selector, so a handover record reads in the
# same vocabulary as the run record beside it.
#
# The password branch is the whole point: the value never leaves the page.
LISTENER = """
(binding) => {
    if (window.__cuaWatching) { return; }
    window.__cuaWatching = true;

    const nameOf = (el) => {
        if (!el) { return null; }
        const byId = el.id && document.querySelector(`label[for="${el.id}"]`);
        const wrapping = el.closest && el.closest('label');
        const labelled = (byId || wrapping) && (byId || wrapping).textContent;
        // el.value is the last resort for naming an unlabelled control, and it
        // is never consulted for a password box. On a sign on form with no
        // label, aria-label or name, that fallback would have named the
        // control after the credential typed into it and sent it out as the
        // target — past every guard, because nothing downstream expects a
        // secret to arrive in that field.
        const secret = el.type === 'password';
        const fallback = secret ? '' : (el.name || el.value || el.textContent || '');
        const named = labelled || el.getAttribute('aria-label') || fallback;
        return (named || '').trim().slice(0, 120) || (secret ? 'password field' : null);
    };

    document.addEventListener('click', (event) => {
        const el = event.target.closest('a, button, input[type=submit], [role=button]')
                   || event.target;
        binding({ action: 'click', target: nameOf(el) });
    }, true);

    document.addEventListener('change', (event) => {
        const el = event.target;
        if (!el || !el.tagName || el.tagName !== 'INPUT') { return; }
        const secret = el.type === 'password';
        binding({
            action: secret ? 'sensitive_input' : 'input',
            target: nameOf(el),
            // A password's value is not sent. Not masked on the way out,
            // not truncated: never read.
            value: secret ? null : String(el.value || '').slice(0, 120),
        });
    }, true);
}
"""

BINDING = "__cuaRecord"


@dataclass
class BrowserActivity:
    """Records what a person does to one live page.

    Holds the same Page the surface holds, which is what makes the handover
    real rather than a gesture: the person is asked to fix the run's own
    session, and a fresh window would be a different session with a different
    cookie, leaving the automation as locked out as it was.
    """

    page: Page

    _events: list[HumanEvent] = field(default_factory=list, init=False)
    _watching: bool = field(default=False, init=False)

    def start(self) -> None:
        """Begin recording.

        Called after control has been ceded and never before: what the
        automation did is already in the run record, and counting it twice
        would make the handover look like it did more than it did.
        """
        if self._watching:
            return
        self._events = []
        self._watching = True

        self.page.on("framenavigated", self._navigated)
        # A page re-used across two handovers already has the binding, and
        # Playwright treats a second registration as an error rather than a
        # no-op. Being asked to watch twice is not a failure worth stopping a
        # handover for.
        with contextlib.suppress(Exception):
            self.page.expose_binding(BINDING, self._reported)
        # Both: once for the page as it stands, and once for every page the
        # person navigates to afterwards. Guarded like the other two: failing
        # to install the watcher costs a record, and raising here would cost
        # the handover itself, which is the more expensive of the two.
        with contextlib.suppress(Exception):
            self.page.add_init_script(
                f"() => window.{BINDING} && ({LISTENER})(window.{BINDING})"
            )
        self._install()

    def stop(self) -> list[HumanEvent]:
        """Stop recording and hand back what was seen, in order."""
        if self._watching:
            self.page.remove_listener("framenavigated", self._navigated)
            self._watching = False
        return list(self._events)

    # ---- the two sources ---------------------------------------------------

    def _reported(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        """One event from the page. `source` is Playwright's frame context.

        Dropped unless recording is open. The binding outlives a handover — it
        is installed once per page and cannot be uninstalled — so a callback
        that arrives after control came back would attribute the automation's
        own next click to the person who has already walked away.
        """
        if not self._watching:
            return
        action = HumanAction(str(payload.get("action", "click")))
        self._events.append(
            HumanEvent(
                action=action,
                target=_text(payload.get("target")),
                # The page sent no value for a password. Saying REDACTED here
                # records that something was typed, which is the fact worth
                # having; the alternative reads as though the field was skipped.
                value=REDACTED if action is HumanAction.SENSITIVE_INPUT else _text(
                    payload.get("value")
                ),
            )
        )

    def _navigated(self, frame: Any) -> None:
        """Where the session went. Routes, never urls, for the same reason
        Observation carries routes: an identifier must not enter a record."""
        if frame.parent_frame is not None:
            return
        route = route_pattern(frame.url)
        if self._events and self._events[-1].route == route:
            return
        self._events.append(HumanEvent(action=HumanAction.NAVIGATE, route=route))
        self._install()

    def _install(self) -> None:
        """Attach the listener to the page as it is now.

        add_init_script only fires on documents loaded after it is added, so
        the page already on screen when the handover starts needs this as well.
        Re-running it is harmless: the script guards on a flag.
        """
        # A page mid-navigation has no document to attach to. The init script
        # covers the one that arrives, so losing this attempt costs nothing.
        with contextlib.suppress(Exception):
            self.page.evaluate(f"({LISTENER})(window.{BINDING})")


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
