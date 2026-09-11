"""Scratch tool. Captures an Observation-shaped JSON from the live target app.

Not PlaywrightSurface, and not part of the package. It lives outside src/ so the
layering test never sees it and nothing here gets mistaken for the real adapter.
Its output is a starting point to curate by hand, not a fixture to trust.

    .venv/bin/python scripts/dump_observation.py OUT.json --goto /search
    .venv/bin/python scripts/dump_observation.py OUT.json --goto /search \
        --fill 'ctl00$cph$txtMemberNo=100099' --click Find
    .venv/bin/python scripts/dump_observation.py OUT.json --goto /members/100045 --expire

Signs on first, because every page worth capturing is behind a session.
Delete this file once the four fixtures exist.
"""

import argparse
import json
import os
import re
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from playwright.sync_api import Page, sync_playwright

load_dotenv(find_dotenv(usecwd=True))

BASE = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5000")
USER = os.environ.get("TARGET_APP_USER", "tmiller")
PASSWORD = os.environ.get("TARGET_APP_PASSWORD", "")

# page.accessibility was removed in Playwright 1.5x, so the tree comes over CDP.
# Chromium only, which is fine for a scratch tool that gets deleted.
SKIP_ROLES = {
    "generic",
    "none",
    "InlineTextBox",
    "GenericContainer",
    "RootWebArea",
    "LineBreak",
    "",
    # Always duplicates the name of the cell that contains it.
    "StaticText",
    # Chrome's marker for a table used as layout rather than data. Nested
    # layout tables are how this application is built, so these are pure noise;
    # anything worth keeping in one also appears as a real cell.
    "LayoutTable",
    "LayoutTableRow",
    "LayoutTableCell",
}

# Structural nodes with no accessible name identify nothing. Form controls are
# kept regardless, because an unnamed control is itself worth seeing.
INTERACTIVE_ROLES = {"button", "textbox", "combobox", "checkbox", "radio", "link", "listbox"}

ZERO_BOUNDS = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}


def route_pattern(url: str) -> str:
    """Normalise a URL to a route. Identifiers must not enter an Observation."""
    path = re.sub(r"^https?://[^/]+", "", url) or "/"
    path = re.sub(r"/\d{4,}", "/{member_id}", path)
    return path.split("?")[0]


def sign_on(page: Page) -> None:
    page.goto(f"{BASE}/login")
    page.fill("#ctl00_cph_txtUser", USER)
    page.fill("#ctl00_cph_txtPass", PASSWORD)
    page.click("#ctl00_cph_btnSignOn")


def link_destinations(page: Page) -> dict[str, str]:
    """Accessible name -> route, for anchors only.

    Correlating the accessibility tree back to DOM elements properly belongs in
    the real adapter. Matching on visible text is good enough for a fixture that
    gets read by hand, and a repeated name is reported rather than guessed at.
    """
    seen: dict[str, str] = {}
    repeated: set[str] = set()
    for anchor in page.query_selector_all("a[href]"):
        name = (anchor.inner_text() or "").strip()
        href = anchor.get_attribute("href") or ""
        if not name:
            continue
        if name in seen and seen[name] != route_pattern(href):
            repeated.add(name)
        seen[name] = route_pattern(href)
    for name in repeated:
        print(f"  ! link name {name!r} points at more than one route; destination left null")
        seen.pop(name, None)
    return seen


def box_of(cdp: Any, backend_node_id: int | None) -> dict[str, float]:
    """Real geometry for one node, or zeros when the node has no box."""
    if not backend_node_id:
        return dict(ZERO_BOUNDS)
    try:
        model = cdp.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})["model"]
        border = model["border"]
        return {
            "x": float(border[0]),
            "y": float(border[1]),
            "width": float(model["width"]),
            "height": float(model["height"]),
        }
    except Exception:
        return dict(ZERO_BOUNDS)


def capture(page: Page, out: Path, keep_duplicates: bool = False) -> None:
    destinations = link_destinations(page)

    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("DOM.getDocument")
    tree = cdp.send("Accessibility.getFullAXTree")

    observation_id = f"obs_{uuid.uuid4().hex[:8]}"
    nodes: list[dict[str, Any]] = []

    # The flat list arrives in document order, so index order is reading order.
    for entry in tree.get("nodes", []):
        if entry.get("ignored"):
            continue
        role = (entry.get("role") or {}).get("value", "")
        if role in SKIP_ROLES:
            continue
        name = (entry.get("name") or {}).get("value") or None
        if name is None and role not in INTERACTIVE_ROLES:
            continue
        value = (entry.get("value") or {}).get("value") or None
        properties = {
            prop["name"]: (prop.get("value") or {}).get("value")
            for prop in entry.get("properties", [])
        }
        bounds = box_of(cdp, entry.get("backendDOMNodeId"))
        nodes.append(
            {
                "ref": {"observation_id": observation_id, "value": f"main:{len(nodes)}"},
                "role": role,
                "name": name,
                "text": value,
                "frame_id": "main",
                "bounds": bounds,
                "enabled": not bool(properties.get("disabled", False)),
                "visible": bounds["width"] > 0 and bounds["height"] > 0,
                "destination": destinations.get(name or "") if role == "link" else None,
            }
        )

    if not keep_duplicates:
        # A panel head is <td class="panelhead"><h2>…</h2></td>, so the cell and
        # the heading arrive with the same accessible name. The heading is the
        # one worth targeting; the cell carries no extra information.
        heading_names = {n["name"] for n in nodes if n["role"] == "heading" and n["name"]}
        kept = [n for n in nodes if not (n["role"] == "cell" and n["name"] in heading_names)]
        dropped = len(nodes) - len(kept)
        nodes = [dict(n, ref={"observation_id": observation_id, "value": f"main:{i}"})
                 for i, n in enumerate(kept)]
        if dropped:
            print(f"    dropped {dropped} cells duplicating a heading")

    document = {
        "observation_id": observation_id,
        "nodes": nodes,
        "url_pattern": route_pattern(page.url),
        "page_title": page.title(),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2) + "\n")

    roles = sorted({str(n["role"]) for n in nodes})
    print(f"  {out}  nodes={len(nodes)}  url_pattern={document['url_pattern']!r}")
    print(f"    title : {document['page_title']}")
    print(f"    roles : {', '.join(roles)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--goto", required=True, metavar="PATH")
    parser.add_argument("--fill", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--click", metavar="BUTTON_VALUE")
    parser.add_argument("--expire", action="store_true", help="force session expiry first")
    parser.add_argument("--no-login", action="store_true")
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="keep cells whose name duplicates a heading",
    )
    args = parser.parse_args()

    if not PASSWORD and not args.no_login:
        raise SystemExit("TARGET_APP_PASSWORD is not set. See .env.example.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        if not args.no_login:
            sign_on(page)

        page.goto(f"{BASE}{args.goto}")

        for pair in args.fill:
            name, _, value = pair.partition("=")
            page.fill(f'[name="{name}"]', value)
        if args.click:
            page.click(f'input[value="{args.click}"]')

        if args.expire:
            # Out of band, exactly as a harness would arm it. Nothing the page
            # renders reveals that this happened.
            urllib.request.urlopen(
                urllib.request.Request(f"{BASE}/_test/expire-session", data=b"", method="POST")
            ).read()
            page.goto(f"{BASE}{args.goto}")

        capture(page, args.out, keep_duplicates=args.keep_duplicates)
        browser.close()


if __name__ == "__main__":
    main()
