"""Watching a browser session while a person is driving it.

The other half of the handover. Automation stops looking when it cedes control,
and without this nobody could say what changed underneath it — which is exactly
the moment a record is worth most, because it is the one part of a run nobody
reviewed in advance.

Sanitised at the point of capture, in the page, before a value crosses back
into Python. A password typed during a handover is never in this process's
memory at all: the listener reads the field's type, sees `password`, and stores
the fact that something was typed without storing what. That is a stronger
claim than redacting afterwards, and it is the only version of the claim that
survives somebody adding a debug print later.

The page accumulates and this reads it back at the end. The obvious design — a
Playwright binding calling into Python as things happen — does not work for the
case it exists for: while a console operator blocks on a keypress, nothing is
pumping Playwright's event loop, so no callback is ever delivered. A real
handover recorded nothing while the scripted test recorded five actions,
because the test's "person" drives through Playwright on the same thread. What
a person does has to survive a blocked process, so it goes in sessionStorage
and is collected when control comes back.

Scoped deliberately. This records which controls were touched and where the
session went, and not keystrokes. The distinction matters: the first is an
account of what happened to a member's record, the second is surveillance of an
employee doing their job, and only one of those is this system's business.
"""

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page

from cua.adapters.observation_shape import route_pattern
from cua.domain.intervention import REDACTED, HumanAction, HumanEvent

# Where the page keeps what it has seen. sessionStorage rather than a variable,
# because a person signing on navigates and a variable goes with the document.
# Same origin, same tab, survives the reloads a sign on involves.
STORE = "__cua_handover"

# Installed on the page, and on every page the person navigates to afterwards.
# It names a control the way the rest of the system names one — accessible
# name, never a selector — so a handover record reads beside a run record
# without translation.
LISTENER = """
() => {
    if (window.__cuaWatching || window.top !== window) { return; }
    window.__cuaWatching = true;

    const KEY = '__cua_handover';
    const remember = (entry) => {
        try {
            const seen = JSON.parse(sessionStorage.getItem(KEY) || '[]');
            seen.push(entry);
            sessionStorage.setItem(KEY, JSON.stringify(seen.slice(-200)));
        } catch (e) { /* storage disabled: the record is lost, the run is not */ }
    };

    const nameOf = (el) => {
        if (!el) { return null; }
        const byId = el.id && document.querySelector(`label[for="${el.id}"]`);
        const wrapping = el.closest && el.closest('label');
        const labelled = (byId || wrapping) && (byId || wrapping).textContent;
        // el.value is the last resort for naming an unlabelled control, and it
        // is never consulted for a password box. On a sign on form with no
        // label, aria-label or name, that fallback would have named the control
        // after the credential typed into it and stored it as the target — past
        // every guard, because nothing downstream expects a secret in that
        // field.
        const secret = el.type === 'password';
        const fallback = secret ? '' : (el.name || el.value || el.textContent || '');
        const named = labelled || el.getAttribute('aria-label') || fallback;
        return (named || '').trim().slice(0, 120) || (secret ? 'password field' : null);
    };

    remember({ action: 'navigate', route: location.pathname });

    document.addEventListener('click', (event) => {
        const el = event.target.closest('a, button, input[type=submit], [role=button]')
                   || event.target;
        remember({ action: 'click', target: nameOf(el) });
    }, true);

    document.addEventListener('change', (event) => {
        const el = event.target;
        if (!el || !el.tagName || el.tagName !== 'INPUT') { return; }
        const secret = el.type === 'password';
        remember({
            action: secret ? 'sensitive_input' : 'input',
            target: nameOf(el),
            // A password's value is not stored. Not masked on the way out,
            // not truncated: never read.
            value: secret ? null : String(el.value || '').slice(0, 120),
        });
    }, true);
}
"""


@dataclass
class BrowserActivity:
    """Records what a person does to one live page.

    Holds the same Page the surface holds, which is what makes the handover
    real rather than a gesture: the person is asked to fix the run's own
    session, and a fresh window would be a different session with a different
    cookie, leaving the automation as locked out as it was.
    """

    page: Page

    _watching: bool = field(default=False, init=False)

    def start(self) -> None:
        """Begin recording.

        Called after control has been ceded and never before: what the
        automation did is already in the run record, and counting it twice
        would make the handover look like it did more than it did.
        """
        if self._watching:
            return
        self._watching = True

        # Cleared first, so a second handover in one run does not inherit the
        # actions of the first.
        self._forget()

        # Both: the page as it stands, and every page the person navigates to
        # afterwards. Guarded because failing to install the watcher costs a
        # record, and raising here would cost the handover itself.
        with contextlib.suppress(Exception):
            # Self-invoking. add_init_script runs what it is given, and given a
            # bare arrow expression it defines a function and stops — which is
            # why nothing survived a navigation: only the direct install below
            # was ever doing anything, and it covers one document.
            self.page.add_init_script(f"({LISTENER})()")
        self._install()

    def stop(self) -> list[HumanEvent]:
        """Collect what the page saw, in order, and stop recording.

        Read at the end rather than streamed, because nothing is pumping
        Playwright while a person is being waited on: anything depending on a
        callback arriving during the handover would arrive never.
        """
        self._watching = False
        raw: list[Any] = []
        with contextlib.suppress(Exception):
            stored = self.page.evaluate(f"() => sessionStorage.getItem('{STORE}')")
            if stored:
                raw = json.loads(str(stored))
        events = [_event(item) for item in raw if isinstance(item, dict)]

        # Where they left the session. The init script records a page as it
        # loads, so the last navigation of a handover is only seen if another
        # page follows it — and the one that matters most is usually the last.
        with contextlib.suppress(Exception):
            landed = route_pattern(self.page.url)
            if not events or events[-1].route != landed:
                events.append(HumanEvent(action=HumanAction.NAVIGATE, route=landed))

        self._forget()
        return events

    def _install(self) -> None:
        """Attach the listener to the page as it is now.

        add_init_script only fires on documents loaded after it is added, so
        the page already on screen when the handover starts needs this too.
        Re-running it is harmless: the script guards on a flag.
        """
        # A page mid-navigation has no document to attach to. The init script
        # covers the one that arrives, so losing this attempt costs nothing.
        with contextlib.suppress(Exception):
            self.page.evaluate(f"({LISTENER})()")

    def _forget(self) -> None:
        with contextlib.suppress(Exception):
            self.page.evaluate(f"() => sessionStorage.removeItem('{STORE}')")


def _event(item: dict[str, Any]) -> HumanEvent:
    """One stored record as a domain event.

    A sensitive input arrives with no value, because the page never read one.
    REDACTED is written here rather than left empty: that something was typed
    is worth recording, and an empty field reads as a step somebody skipped.
    """
    action = HumanAction(str(item.get("action", "click")))
    route = item.get("route")
    return HumanEvent(
        action=action,
        target=_text(item.get("target")),
        value=REDACTED if action is HumanAction.SENSITIVE_INPUT else _text(item.get("value")),
        route=route_pattern(str(route)) if route else None,
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
