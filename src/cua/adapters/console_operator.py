"""A human operator at a terminal, holding a browser window.

The mocked half of the handover, and the brief's scope note allows exactly
this: the operator console is stubbed, the control transfer is real. What is
mocked here is the delivery — a person reads a prompt in a terminal rather than
picking a ticket off a queue. What is not mocked is anything that matters: the
session handed over is the run's own live session, the automation genuinely
stops while somebody else drives it, and what they did comes back recorded.

Swapping this for an HTTP callback or a queue consumer changes this file and
nothing else. That is the point of it being an adapter, and it is the reason
the request arrives as an assembled value rather than as a rendered message.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

from cua.domain.intervention import Handover, HumanEvent, InterventionRequest
from cua.ports.operator import HumanActivity

RULE = "─" * 72

PROMPT = "Take the browser, do what is needed, then press Enter to hand it back: "


@dataclass
class ConsoleOperator:
    """Prints the request, waits for a person, reports what they did.

    `activity` is optional so this can be driven without a browser — the
    handover logic is the same whether or not anything is watching, and a test
    that only cares about the control transfer should not need a page.
    """

    activity: HumanActivity | None = None
    out: TextIO = field(default=sys.stderr)
    ask: Callable[[str], str] = field(default=input)

    _released: list[HumanEvent] = field(default_factory=list, init=False)

    def request_intervention(self, request: InterventionRequest) -> None:
        """Put the request in front of a person.

        Written to stderr rather than stdout so a run whose output is being
        piped somewhere still shows this to whoever is sitting there. An
        intervention request that scrolled past in a redirected file would be a
        request nobody answered.
        """
        summary = request.summary()
        print(f"\n{RULE}", file=self.out)
        print("  THIS RUN NEEDS A PERSON", file=self.out)
        print(RULE, file=self.out)
        print(f"  capability   {summary['capability']}", file=self.out)
        print(f"  goal         {summary['goal']}", file=self.out)
        print(f"  stopped at   {summary['step'] or 'before the first step'}", file=self.out)
        print(f"  because      {summary['reason']}", file=self.out)
        print(f"  detail       {summary['detail']}", file=self.out)
        print(f"  screen       {summary['page_title']} at {summary['url_pattern']}", file=self.out)
        if summary["screenshot"]:
            print(f"  screenshot   {summary['screenshot']}", file=self.out)
        print(RULE, file=self.out)
        print(
            f"  Before this run can continue: {summary['resume_checkpoint']}",
            file=self.out,
        )
        print(
            "  It is checked when you hand back, so finishing early stops the run\n"
            "  rather than letting it act on a screen it did not expect.",
            file=self.out,
        )
        print(f"{RULE}\n", file=self.out)

        if self.activity is not None:
            # Started only now. The automation's own actions are already in the
            # run record, and recording them again would attribute them to the
            # person who has just picked this up.
            self.activity.start()

    def await_release(self, run_id: str) -> Handover:
        """Block until the person says they are finished.

        Saying they are finished is all this establishes. Whether the run may
        continue is a different question, answered by re-observing and checking
        the resume checkpoint, and it is the caller's to ask.
        """
        try:
            self.ask(PROMPT)
        except EOFError:
            # No terminal attached. Treated as an immediate hand back rather
            # than a crash: the checkpoint will refuse on the way out, which is
            # the honest outcome for a handover nobody was there to perform.
            print("  (no terminal; handing straight back)", file=self.out)

        events: list[HumanEvent] = []
        if self.activity is not None:
            events = list(self.activity.stop())

        print(f"  control returned after {len(events)} recorded action(s)\n", file=self.out)
        self._released = events
        return Handover(events=tuple(events))
