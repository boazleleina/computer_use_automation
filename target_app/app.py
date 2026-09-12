"""A legacy credit union back-office, simulated.

Server-rendered Jinja, no JavaScript framework, no database. The point is not
that it is a good application; the point is that it behaves like the kind of
system that has no API and cannot be changed — nested table layout, generated
control ids, a session that ages out mid-task, and more than one control with
the same visible label.

Session expiry deliberately renders the login page inline with HTTP 200 and the
words "Your session has ended". A legacy application does not answer 401; it
hands back a login screen where the page you asked for should have been. An
automation that trusts the status code will believe it succeeded, which is
precisely the trap worth reproducing.
"""

import os
import time
from typing import Any, Final

from flask import Flask, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from werkzeug.wrappers.response import Response

from target_app import auth, fixtures, test_control

SESSION_TTL_SECONDS: Final = float(os.environ.get("TARGET_APP_SESSION_TTL", "900"))

# Endpoints reachable without a session.
PUBLIC_ENDPOINTS: Final = frozenset({"login", "static"})

SESSION_ENDED_NOTICE: Final = "Your session has ended"

# Deliberately different wording from expiry. Both land on the login page, and a
# classifier that could not tell them apart would report a clean sign off as an
# intervention, or worse, treat a real expiry as something the run chose to do.
SIGNED_OFF_NOTICE: Final = "You have signed off"


def _is_authenticated() -> bool:
    return bool(session.get("operator"))


def _session_expired(forced: bool) -> bool:
    """Whether this request should be treated as having an aged out session.

    `forced` is passed in rather than read here, so that consuming a fault is
    something only before_request does. A predicate that quietly disarms a lever
    would give a different answer the second time it was called.
    """
    if forced:
        return True
    issued_at = session.get("issued_at")
    if not isinstance(issued_at, (int, float)):
        # Belt and braces. Sign on writes operator and issued_at together, and
        # the caller has already established the operator is present, so this
        # is unreachable in normal flow. If it ever did fire it would show
        # "Your session has ended" to someone who never had one.
        return True
    return (time.time() - float(issued_at)) > SESSION_TTL_SECONDS


def _render_login(notice: str | None = None, error: str | None = None) -> str:
    return render_template("login.html", notice=notice, error=error)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("TARGET_APP_SECRET", "legacy-fixture-secret")
    app.register_blueprint(test_control.blueprint)

    @app.before_request
    def apply_faults_and_check_session() -> ResponseReturnValue | None:
        """The single place faults fire and sessions are judged.

        Runs before every view except the out of band control blueprint, which
        must stay reachable while a fault is armed.
        """
        endpoint = request.endpoint or ""
        if endpoint.startswith("_test.") or endpoint == "static":
            return None

        if test_control.consume(test_control.FAULT_SLOW, request.path):
            time.sleep(test_control.SLOW_FAULT_SECONDS)

        # Only on GET: an interstitial served in place of a POST would discard
        # the submitted form, which is not how the real system behaves. The arm
        # survives an intervening POST rather than being spent on it.
        if request.method == "GET" and test_control.consume(
            test_control.FAULT_INTERSTITIAL, request.path
        ):
            return render_template("interstitial.html", continue_path=request.path)

        if endpoint in PUBLIC_ENDPOINTS:
            return None

        if not _is_authenticated():
            return redirect(url_for("login"))

        # Consumed here, not inside the predicate, so every lever this request
        # spends is visible in one place.
        forced_expiry = test_control.consume_forced_expiry()
        if _session_expired(forced=forced_expiry):
            session.clear()
            return _render_login(notice=SESSION_ENDED_NOTICE)

        return None

    @app.get("/")
    def home() -> Response:
        return redirect(url_for("search"))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> ResponseReturnValue:
        if request.method == "GET":
            return _render_login()

        username = request.form.get("ctl00$cph$txtUser", "").strip()
        password = request.form.get("ctl00$cph$txtPass", "")
        if not auth.credentials_valid(username, password):
            return _render_login(error="Sign on failed. Check your user id and password.")

        session.clear()
        session["operator"] = username
        session["issued_at"] = time.time()
        return redirect(url_for("search"))

    @app.get("/logout")
    def logout() -> ResponseReturnValue:
        """End the session deliberately.

        A GET link rather than a posted form, because that is what a terminal of
        this vintage does. It also makes Sign Off a one-click destructive
        control reachable from every page, which is exactly the kind of thing
        policy's denied_selectors exists to keep an automation away from.
        """
        session.clear()
        return _render_login(notice=SIGNED_OFF_NOTICE)

    @app.route("/search", methods=["GET", "POST"])
    def search() -> ResponseReturnValue:
        """The dashboard. Two panels, each with a button labelled Search."""
        if request.method == "GET":
            return render_template("dashboard.html", operator=session.get("operator"))

        # Three panels post to this one endpoint, as a single-form legacy page
        # does. Which button was pressed is visible only in the control name.
        if "ctl00$cph$btnActivitySearch" in request.form:
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                activity_notice="No activity in the selected date range.",
                activity_from=request.form.get("ctl00$cph$txtActivityFrom", ""),
                activity_to=request.form.get("ctl00$cph$txtActivityTo", ""),
            )

        if "ctl00$cph$btnDocSearch" in request.form:
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                doc_notice="No documents matched the reference supplied.",
                doc_ref=request.form.get("ctl00$cph$txtDocRef", ""),
            )

        raw = request.form.get("ctl00$cph$txtMemberNo", "").strip()
        if not raw:
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                error="Member number is required.",
                member_no=raw,
            )
        if not raw.isdigit():
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                error="Member number must be numeric.",
                member_no=raw,
            )

        member = fixtures.get_member(raw)
        if member is None:
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                notice="No member matches",
                member_no=raw,
            )

        return render_template(
            "dashboard.html",
            operator=session.get("operator"),
            result=member,
            member_no=raw,
        )

    @app.get("/members/<number>")
    def member_detail(number: str) -> ResponseReturnValue:
        member = fixtures.get_member(number)
        if member is None:
            return render_template(
                "dashboard.html",
                operator=session.get("operator"),
                notice="No member matches",
                member_no=number,
            )
        if member["status"] == fixtures.STATUS_RESTRICTED:
            return render_template("member_denied.html", number=number)
        return render_template("member_detail.html", member=member)

    @app.route("/members/<number>/subaccount/new", methods=["GET", "POST"])
    def sub_account_new(number: str) -> ResponseReturnValue:
        member = _member_or_none(number)
        if member is None:
            return redirect(url_for("search"))
        if member["status"] != fixtures.STATUS_ACTIVE:
            return render_template("member_denied.html", number=number)

        if request.method == "GET":
            return render_template(
                "sub_account_new.html",
                member=member,
                types=fixtures.SUB_ACCOUNT_TYPES,
                purposes=fixtures.SUB_ACCOUNT_PURPOSES,
                form={},
            )

        form = {
            "account_type": request.form.get("ctl00$cph$ddlAccountType", ""),
            "nickname": request.form.get("ctl00$cph$txtNickname", "").strip(),
            "deposit": request.form.get("ctl00$cph$txtOpeningDeposit", "").strip(),
            "purpose": request.form.get("ctl00$cph$ddlPurpose", ""),
        }
        error = _validate_sub_account(form)
        if error is not None:
            return render_template(
                "sub_account_new.html",
                member=member,
                types=fixtures.SUB_ACCOUNT_TYPES,
                purposes=fixtures.SUB_ACCOUNT_PURPOSES,
                form=form,
                error=error,
            )

        session["pending_subaccount"] = form
        return redirect(url_for("sub_account_review", number=number))

    @app.route("/members/<number>/subaccount/review", methods=["GET", "POST"])
    def sub_account_review(number: str) -> ResponseReturnValue:
        member = _member_or_none(number)
        pending = session.get("pending_subaccount")
        if member is None or not isinstance(pending, dict):
            return redirect(url_for("search"))

        if request.method == "POST":
            return redirect(url_for("sub_account_confirm", number=number))

        return render_template(
            "sub_account_review.html",
            member=member,
            pending=pending,
            type_label=dict(fixtures.SUB_ACCOUNT_TYPES).get(pending["account_type"], ""),
            purpose_label=dict(fixtures.SUB_ACCOUNT_PURPOSES).get(pending["purpose"], ""),
        )

    @app.route("/members/<number>/subaccount/confirm", methods=["GET", "POST"])
    def sub_account_confirm(number: str) -> ResponseReturnValue:
        member = _member_or_none(number)
        if member is None:
            return redirect(url_for("search"))

        pending = session.pop("pending_subaccount", None)
        if isinstance(pending, dict):
            session["last_subaccount"] = fixtures.add_sub_account(
                number,
                str(pending["account_type"]),
                str(pending["nickname"]),
                str(pending["deposit"]),
            )

        # A refresh of the confirmation page must not open a second sub-account,
        # so the suffix is read back from the session rather than recreated.
        suffix = session.get("last_subaccount")
        if not isinstance(suffix, str):
            return redirect(url_for("member_detail", number=number))

        return render_template("sub_account_confirm.html", member=member, suffix=suffix)

    return app


def _member_or_none(number: str) -> dict[str, Any] | None:
    return fixtures.get_member(number)


def _validate_sub_account(form: dict[str, str]) -> str | None:
    """Field level validation. Returns the message a teller would see."""
    if not form["account_type"]:
        return "Select an account type."
    if not form["nickname"]:
        return "Account nickname is required."
    if len(form["nickname"]) > 20:
        return "Account nickname must be 20 characters or fewer."
    deposit = form["deposit"]
    if not deposit:
        return "Opening deposit is required."
    try:
        amount = float(deposit)
    except ValueError:
        return "Opening deposit must be a number."
    if amount < 0:
        return "Opening deposit must be a number."
    return None


app = create_app()


if __name__ == "__main__":
    # 5055 rather than Flask's 5000, which macOS reserves for AirPlay Receiver
    # and which therefore fails on every developer Mac out of the box.
    app.run(host="127.0.0.1", port=int(os.environ.get("TARGET_APP_PORT", "5055")), debug=False)
