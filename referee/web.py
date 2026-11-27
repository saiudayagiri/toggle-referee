"""The local dashboard: where a person reads the scorecard and signs off.

A deliberately small server built on the standard library. It shows every
experiment's scorecard and the decisions awaiting approval. Approving asks for
the person's Flagsmith email and password, exchanges them for that person's
token, and ends the experiment with it. The password is used for that one
request and is never stored or logged.

It binds to 127.0.0.1 by default and is meant to run on the approver's own
machine.
"""

from __future__ import annotations

import html
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, decisions, ops, report
from .api import FlagsmithError
from .config import Settings

CSS = """
:root{--bg:#f3f0e8;--panel:#fffdf7;--ink:#1c1a15;--muted:#6a6455;--line:#d8d1c0;--accent:#1f5f4a;
--good:#1f6b43;--bad:#a3341f;--warn:#8a5a00;--chip:#ebe6d8;--bar:#cfc7b3}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15140f;--panel:#1e1c16;--ink:#ece7da;
--muted:#a39c8a;--line:#3a362b;--accent:#6fc7a4;--good:#74c995;--bad:#f08a72;--warn:#e0b25a;--chip:#2a271f;--bar:#4a4537}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
header b{font-size:17px;letter-spacing:.02em}header span{color:var(--muted);font-size:13px}
main{max-width:980px;margin:0 auto;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
h1{font-size:22px;margin:0 0 4px;text-wrap:balance}h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:18px 0 8px}
p{margin:6px 0}.muted{color:var(--muted)}.small{font-size:13px}
.call{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
.badge{font-weight:700;font-size:12px;letter-spacing:.08em;padding:4px 9px;border-radius:999px;background:var(--chip);white-space:nowrap}
.badge.ship_variant,.badge.completed,.badge.applied,.badge.pass{background:var(--good);color:var(--panel)}
.badge.investigate,.badge.fail,.badge.rejected,.badge.stale{background:var(--bad);color:var(--panel)}
.badge.keep_control,.badge.warn,.badge.pending{background:var(--warn);color:var(--panel)}
.headline{font-size:18px;font-weight:600}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}td.num,th.num{text-align:right}
.bar{height:8px;background:var(--chip);border-radius:4px;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:var(--accent)}
.ci{position:relative;height:14px;min-width:160px}.ci .axis{position:absolute;left:0;right:0;top:6px;height:2px;background:var(--bar)}
.ci .zero{position:absolute;top:0;bottom:0;width:1px;background:var(--muted)}.ci .range{position:absolute;top:4px;height:6px;border-radius:3px;background:var(--accent);opacity:.55}
.ci .dot{position:absolute;top:2px;width:10px;height:10px;margin-left:-5px;border-radius:50%;background:var(--accent)}
.state-winning,.state-held{color:var(--good);font-weight:600}.state-losing,.state-worse{color:var(--bad);font-weight:600}.state-leans{color:var(--warn);font-weight:600}
form.approve{display:grid;gap:8px;align-content:start}
.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px;margin-top:12px}
label{font-size:13px;color:var(--muted)}input[type=text],input[type=email],input[type=password]{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
button{padding:9px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:var(--panel);font:inherit;font-weight:600;cursor:pointer}
button.secondary{background:transparent;color:var(--ink);border-color:var(--line)}
.notice{border-left:4px solid var(--accent);padding:8px 12px;background:var(--chip);border-radius:0 6px 6px 0;margin:0 0 16px}
.notice.error{border-color:var(--bad)}code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def pct(value: Optional[float], digits: int = 1) -> str:
    return "–" if value is None else report._pct(value, digits)


def signed(value: Optional[float]) -> str:
    return "–" if value is None else "%+.1f%%" % (value * 100)


def page(title: str, body: str, refresh: int = 0) -> str:
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">%s"
        "<title>%s</title><style>%s</style></head><body>"
        "<header><b><a href=\"/\" style=\"color:inherit;text-decoration:none\">Referee</a></b>"
        "<span>Flagsmith experiments, called fairly. A person signs every rollout.</span></header>"
        "<main>%s</main></body></html>" % (meta, esc(title), CSS, body)
    )


def _interval(inference: Optional[Dict[str, Any]], scale: float) -> str:
    """A small drawing of the credible interval around zero."""
    if not inference:
        return ""
    def place(value: float) -> float:
        return max(0.0, min(100.0, 50.0 + 50.0 * value / scale))
    low, high, mid = place(inference["ci_low"]), place(inference["ci_high"]), place(inference["lift"])
    return (
        '<div class="ci" title="95%% interval %s to %s"><span class="axis"></span>'
        '<span class="zero" style="left:50%%"></span>'
        '<span class="range" style="left:%.1f%%;width:%.1f%%"></span>'
        '<span class="dot" style="left:%.1f%%"></span></div>'
        % (signed(inference["ci_low"]), signed(inference["ci_high"]), low, max(1.0, high - low), mid)
    )


def scorecard_html(card: Dict[str, Any]) -> str:
    experiment = card["experiment"]
    verdict = card["verdict"]
    parts: List[str] = []
    parts.append(
        '<div class="row"><h1>%s</h1><span class="badge">%s</span></div>'
        '<p class="muted">Flag <code>%s</code> · experiment %s%s</p>'
        % (
            esc(experiment["name"]), esc(experiment["status"]), esc(experiment["flag"]), esc(experiment["id"]),
            " · results as of %s" % esc(card["freshness"]["as_of"]) if card["freshness"].get("as_of") else "",
        )
    )
    if experiment.get("hypothesis"):
        parts.append("<p>%s</p>" % esc(experiment["hypothesis"]))
    parts.append(
        '<div class="call"><span class="badge %s">%s</span><span class="headline">%s</span></div><ul>%s</ul>'
        % (
            esc(verdict["call"]), esc(report.CALL_LABELS.get(verdict["call"], verdict["call"])), esc(verdict["headline"]),
            "".join("<li>%s</li>" % esc(reason) for reason in verdict["reasons"]),
        )
    )
    if card["freshness"].get("note"):
        parts.append('<p class="muted small">%s</p>' % esc(card["freshness"]["note"]))

    progress = card["progress"]
    if progress.get("needed_per_arm"):
        reached = progress["smallest_arm"] >= progress["needed_per_arm"]
        parts.append(
            '<h2>Progress</h2><div class="bar"><i style="width:%.0f%%"></i></div>'
            '<p class="small muted">%s</p>'
            % (
                (progress["fraction"] or 0) * 100,
                "Planned sample reached: %d identities in the smallest version (planned %d)"
                % (progress["smallest_arm"], progress["needed_per_arm"])
                if reached
                else "%d of %d identities in the smallest version (%s)%s" % (
                    progress["smallest_arm"], progress["needed_per_arm"], pct(progress["fraction"], 0),
                    ", about %s to go" % esc(_duration(progress["eta_hours"])) if progress.get("eta_hours") else "",
                ),
            )
        )

    rows = "".join(
        "<tr><td>%s</td><td><code>%s</code></td><td class=num>%d</td><td class=num>%s</td><td class=num>%s</td></tr>"
        % (
            esc(arm["key"]), esc(arm["value"]), arm["identities"],
            pct(arm["expected_share"], 0) if arm["expected_share"] is not None else "–",
            pct(arm["observed_share"]) if arm["observed_share"] is not None else "–",
        )
        for arm in card["arms"]
    )
    parts.append(
        '<h2>Versions</h2><div class="scroll"><table><tr><th>Version</th><th>Serves</th><th class=num>Identities</th>'
        "<th class=num>Planned</th><th class=num>Observed</th></tr>%s</table></div>" % rows
    )

    for metric in card["metrics"]:
        spans = [
            abs(v)
            for arm in metric["arms"].values()
            for v in ((arm.get("inference") or {}).get("ci_low"), (arm.get("inference") or {}).get("ci_high"))
            if v is not None
        ]
        scale = max(spans + [0.1]) * 1.1
        rows = ""
        for arm in card["arms"]:
            numbers = metric["arms"].get(arm["key"], {})
            inference = numbers.get("inference")
            value = numbers.get("value")
            if metric["aggregation"] == "occurrence":
                shown = "–" if value is None else "%s <span class=muted>(%d/%d)</span>" % (pct(value), numbers.get("sum", 0), numbers.get("identities", 0))
            else:
                shown = "–" if value is None else "%.3f" % value
            if arm["key"] == "control":
                rows += "<tr><td>control</td><td class=num>%s</td><td class=num muted>baseline</td><td></td><td></td><td></td></tr>" % shown
            else:
                state = numbers.get("state", "")
                rows += (
                    "<tr><td>%s</td><td class=num>%s</td><td class=num>%s</td><td>%s</td><td class=num>%s</td><td class=\"state-%s\">%s</td></tr>"
                    % (
                        esc(arm["key"]), shown, signed((inference or {}).get("lift")), _interval(inference, scale),
                        esc(report._chance(numbers).replace("-", "–") if numbers.get("chance_good") is None else report._chance(numbers)), esc(state.split(" ")[0]), esc(state),
                    )
                )
        parts.append(
            '<h2>%s <span style="text-transform:none;letter-spacing:0">· %s, should %s</span></h2>'
            '<div class="scroll"><table><tr><th>Version</th><th class=num>Value</th><th class=num>Lift</th>'
            "<th>95%% interval</th><th class=num>%s</th><th>State</th></tr>%s</table></div>"
            % (esc(metric["name"]), esc(metric["role"]), esc(metric["goal"].replace("_", " ")), esc(report._chance_title(metric).capitalize()), rows)
        )

    if card["checks"]:
        items = "".join(
            '<li><span class="badge %s">%s</span> <b>%s</b><br><span class="muted">%s</span></li>'
            % (esc(c["status"]), esc(c["status"].upper()), esc(c["title"]), esc(c["detail"]))
            for c in card["checks"]
        )
        parts.append('<h2>Checks</h2><ul style="list-style:none;padding:0">%s</ul>' % items)
    parts.append('<p class="muted small">Evidence fingerprint <code>%s</code></p>' % esc(card["evidence_hash"][:16]))
    return "".join(parts)


def _duration(hours: float) -> str:
    from .scorecard import duration

    return duration(hours)


LOCAL_NAMES = ("localhost", "127.0.0.1", "[::1]")
# A host name or bracketed IPv6 address with an optional port, and nothing else.
_HOST = re.compile(r"^(\[[0-9a-f:]+\]|[a-z0-9.\-]+)(?::\d{1,5})?$")


def _is_id(text: str) -> bool:
    return text.isascii() and text.isdigit() and len(text) < 12


class App:
    def __init__(self, settings: Settings, allowed_hosts: Optional[List[str]] = None):
        self.settings = settings
        # Names this dashboard may be addressed by. Checking the Host header
        # stops a web page on another origin from reaching it through DNS
        # rebinding and reading the form token.
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])] + list(LOCAL_NAMES)
        # One secret per server run, required on every form post, so another
        # site open in the same browser cannot submit the approval form.
        self.form_token = secrets.token_urlsafe(24)

    # ---------------------------------------------------------------- pages

    def home(self, notice: str = "", error: bool = False) -> str:
        body = self._notice(notice, error)
        pending = [d for d in ops.list_decisions(self.settings) if d["status"] == "pending"]
        if pending:
            rows = "".join(
                '<li><a href="/decision/%s"><b>%s</b></a> · %s · proposed by %s at %s</li>'
                % (esc(d["id"]), esc(d["headline"]), esc(d["experiment_name"]), esc(d["proposed_by"]), esc(d["proposed_at"]))
                for d in pending
            )
            body += '<div class="card"><h2 style="margin-top:0">Awaiting your approval</h2><ul>%s</ul></div>' % rows
        try:
            experiments = ops.list_experiments(self.settings)
        except (ops.OpError, FlagsmithError) as failure:
            return page("Referee", body + '<div class="notice error">%s</div>' % esc(failure))
        if not experiments:
            body += '<div class="card"><h1>No experiments yet</h1><p>Create one with <code>referee apply plan.json</code>, or ask your coding agent to plan one.</p></div>'
        rows = "".join(
            '<tr><td><a href="/experiment/%d">%s</a></td><td><code>%s</code></td><td><span class="badge">%s</span></td><td>%s</td></tr>'
            % (
                e["id"], esc(e["name"]), esc(e["flag"]), esc(e["status"]),
                '<a href="/decision/%s">%s</a>' % (esc(e["pending_decision"]), esc(e["pending_decision"])) if e["pending_decision"] else "",
            )
            for e in experiments
        )
        if rows:
            body += (
                '<div class="card"><h2 style="margin-top:0">Experiments</h2><div class="scroll"><table>'
                "<tr><th>Name</th><th>Flag</th><th>Status</th><th>Decision</th></tr>%s</table></div></div>" % rows
            )
        history = [d for d in ops.list_decisions(self.settings) if d["status"] != "pending"][:10]
        if history:
            rows = "".join(
                '<tr><td><a href="/decision/%s">%s</a></td><td><span class="badge %s">%s</span></td><td>%s</td><td>%s</td></tr>'
                % (
                    esc(d["id"]), esc(d["id"]), esc(d["status"]), esc(d["status"]), esc(d["headline"]),
                    esc((d.get("approval") or {}).get("by") or (d.get("rejection") or {}).get("by") or ""),
                )
                for d in history
            )
            ledger = decisions.Ledger(self.settings.state_dir).verify()
            body += (
                '<div class="card"><h2 style="margin-top:0">Decision history</h2><div class="scroll"><table>'
                "<tr><th>Decision</th><th>Status</th><th>Proposal</th><th>By</th></tr>%s</table></div>"
                '<p class="small muted">Ledger: %s</p></div>'
                % (rows, "%d entries, hash chain intact" % ledger["entries"] if ledger["ok"] else "TAMPERED: " + esc(ledger["reason"]))
            )
        return page("Referee", body, refresh=60)

    def experiment(self, experiment_id: int) -> str:
        card = ops.get_scorecard(self.settings, experiment_id)
        body = '<div class="card">%s' % scorecard_html(card)
        if card.get("pending_decision"):
            body += '<p><a href="/decision/%s"><b>A decision is awaiting approval: %s</b></a></p>' % (
                esc(card["pending_decision"]), esc(card["pending_decision"]),
            )
        elif card["verdict"].get("actionable"):
            body += (
                '<form method="post" action="/experiment/%d/propose"><input type="hidden" name="form_token" value="%s">'
                "<button>Propose this rollout</button> <span class=\"muted small\">Records a proposal; nothing changes until someone approves it.</span></form>"
                % (experiment_id, esc(self.form_token))
            )
        body += "</div>"
        return page(card["experiment"]["name"], body, refresh=60 if card["experiment"]["status"] == "running" else 0)

    def decision(self, decision_id: str, notice: str = "", error: bool = False) -> str:
        decision = ops.get_decision(self.settings, decision_id)
        status = decisions.summary(decision)["status"]  # shows an overdue pending decision as expired
        body = self._notice(notice, error)
        body += (
            '<div class="card"><div class="row"><h1>%s</h1><span class="badge %s">%s</span></div>'
            '<p class="muted">Decision %s · proposed by %s at %s</p>'
            % (esc(decision["headline"]), esc(status), esc(status), esc(decision["id"]), esc(decision["proposed_by"]), esc(decision["proposed_at"]))
        )
        if decision.get("note"):
            body += "<p>Note from the proposer: %s</p>" % esc(decision["note"])
        approval = decision.get("approval")
        if approval:
            applied = decision.get("applied") or {}
            body += (
                "<p><b>Approved by %s</b> at %s, through the %s.</p>"
                '<p class="muted small">Flagsmith ended the experiment at %s with the decision <code>%s</code>. '
                "The results relied on were computed at %s%s.</p>"
                % (
                    esc(approval["by"]), esc(approval["at"]), esc("terminal" if approval["method"] == "cli" else approval["method"]),
                    esc(applied.get("ended_at")), esc(applied.get("rollout_decision")), esc(approval.get("results_as_of")),
                    "; the numbers had moved since the proposal, and the call was the same" if approval.get("evidence_changed_since_proposal") else "",
                )
            )
            if approval.get("note"):
                body += "<p>Note from the approver: %s</p>" % esc(approval["note"])
        if decision.get("rejection"):
            body += "<p><b>Rejected by %s</b> at %s. %s</p>" % (
                esc(decision["rejection"]["by"]), esc(decision["rejection"]["at"]), esc(decision["rejection"].get("reason")),
            )
        if decision.get("stale_reason"):
            body += "<p><b>No longer valid.</b> %s</p>" % esc(decision["stale_reason"])
        if status == "expired":
            body += "<p><b>Expired</b> at %s without a decision. Ask for a new proposal so it rests on current data.</p>" % esc(decision["expires_at"])
        body += "</div>"

        # Evidence first, the decision after it: nobody should meet the
        # approve button before the numbers it rests on.
        body += '<div class="card"><h2 style="margin-top:0">Evidence when proposed</h2>%s</div>' % scorecard_html(decision["scorecard"])
        if status == "pending":
            effect = (
                "serve <code>%s</code> to everyone" % esc(decision["variant_key"])
                if decision["action"] == "ship_variant"
                else "serve the control version to everyone, with no winner recorded: this run's data is set aside, "
                "and the same plan can be applied again for a clean run"
                if decision["action"] == "void"
                else "serve the control version to everyone"
            )
            body += (
                '<div class="card"><h2 style="margin-top:0">Your decision</h2>'
                "<p>Approving ends the experiment in Flagsmith and makes the flag <code>%s</code> %s. "
                "It is done with your Flagsmith account, so the audit log names you. Referee re-checks Flagsmith's "
                "latest results first and refuses if the call has changed.</p>"
                '<div class="choices">'
                '<form class="approve" method="post" action="/decision/%s/approve" autocomplete="off">'
                '<input type="hidden" name="form_token" value="%s">'
                '<label>Flagsmith email<input type="email" name="email" required></label>'
                '<label>Flagsmith password<input type="password" name="password" required></label>'
                '<label>Note (optional)<input type="text" name="note"></label>'
                "<button>Approve and roll out</button></form>"
                '<form class="approve" method="post" action="/decision/%s/reject">'
                '<input type="hidden" name="form_token" value="%s">'
                '<label>Your name<input type="text" name="by" required></label>'
                '<label>Reason for rejecting<input type="text" name="reason"></label>'
                '<button class="secondary">Reject</button></form>'
                "</div>"
                '<p class="small muted">This proposal expires %s.</p></div>'
                % (esc(decision["flag"]), effect, esc(decision["id"]), esc(self.form_token), esc(decision["id"]), esc(self.form_token), esc(decision["expires_at"]))
            )
        return page("Decision " + decision["id"], body)

    @staticmethod
    def _notice(text: str, error: bool) -> str:
        return '<div class="notice%s">%s</div>' % (" error" if error else "", esc(text)) if text else ""

    # -------------------------------------------------------------- actions

    def host_allowed(self, header: Optional[str]) -> bool:
        if not header:
            return False
        found = _HOST.match(header.strip().lower())
        return bool(found) and found.group(1) in self.allowed_hosts

    def approve(self, decision_id: str, form: Dict[str, str]) -> str:
        try:
            token = self.settings.client().login(form.get("email", ""), form.get("password", ""))
        except FlagsmithError as failure:
            ops.note_refused_sign_in(self.settings, decision_id, form.get("email", ""), "dashboard")
            message = "Sign-in to Flagsmith failed. Check the email and password." if failure.status in (400, 401, 403) else str(failure)
            return self.decision(decision_id, message, error=True)
        try:
            applied = ops.approve(self.settings, decision_id, token, note=form.get("note", ""), method="dashboard")
        except ops.OpError as failure:
            return self.decision(decision_id, str(failure), error=True)
        return self.decision(decision_id, "Approved by %s. Flagsmith has ended the experiment and rolled out the decision." % applied["approval"]["by"])


def make_handler(app: App) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Referee/" + __version__

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _guarded(self, render: Any) -> None:
            try:
                self._send(200, render())
            except ops.OpError as failure:
                self._send(404, page("Referee", '<div class="notice error">%s</div><p><a href="/">Back</a></p>' % esc(failure)))
            except FlagsmithError as failure:
                self._send(404 if failure.status == 404 else 502, page("Referee", '<div class="notice error">%s</div><p><a href="/">Back</a></p>' % esc(failure)))
            except Exception as failure:  # a bug must still produce a page
                self._send(500, page("Referee", '<div class="notice error">Referee hit an unexpected error (%s). Nothing was changed.</div><p><a href="/">Back</a></p>' % esc(type(failure).__name__)))

        def _refuse_foreign(self, posting: bool) -> bool:
            """True (and a 403 sent) when the request does not come from this page."""
            if not app.host_allowed(self.headers.get("Host")):
                self._send(403, "This dashboard only answers requests addressed to it directly.\n", "text/plain")
                return True
            if posting:
                origin = self.headers.get("Origin")
                if origin and not app.host_allowed(urlparse(origin).netloc):
                    self._send(403, "Cross-site requests are not accepted.\n", "text/plain")
                    return True
            return False

        def do_GET(self) -> None:
            if self._refuse_foreign(posting=False):
                return
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            if path == "/":
                self._guarded(app.home)
            elif path == "/healthz":
                self._send(200, "ok", "text/plain")
            elif len(parts) == 2 and parts[0] == "experiment" and _is_id(parts[1]):
                self._guarded(lambda: app.experiment(int(parts[1])))
            elif len(parts) == 2 and parts[0] == "decision":
                self._guarded(lambda: app.decision(parts[1]))
            elif len(parts) == 3 and parts[0] == "api" and parts[1] == "scorecard" and _is_id(parts[2]):
                try:
                    self._send(200, json.dumps(ops.get_scorecard(app.settings, int(parts[2])), default=str), "application/json")
                except (ops.OpError, FlagsmithError) as failure:
                    self._send(404, json.dumps({"error": str(failure)}), "application/json")
            else:
                self._send(404, page("Not found", '<div class="card"><h1>Not found</h1><p><a href="/">Back</a></p></div>'))

        def do_POST(self) -> None:
            if self._refuse_foreign(posting=True):
                return
            path = urlparse(self.path).path.rstrip("/")
            parts = path.strip("/").split("/")
            try:
                length = max(0, min(int(self.headers.get("Content-Length") or 0), 20000))
            except ValueError:
                length = 0
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            if not secrets.compare_digest(form.get("form_token", ""), app.form_token):
                self._send(403, page("Referee", '<div class="notice error">This form is out of date. Reload the page and try again.</div>'))
                return
            if len(parts) == 3 and parts[0] == "decision" and parts[2] == "approve":
                self._guarded(lambda: app.approve(parts[1], form))
            elif len(parts) == 3 and parts[0] == "decision" and parts[2] == "reject":
                def run() -> str:
                    try:
                        ops.reject(app.settings, parts[1], form.get("by", "someone"), form.get("reason", ""))
                    except ops.OpError as failure:
                        return app.decision(parts[1], str(failure), error=True)
                    return app.decision(parts[1], "Rejected.")
                self._guarded(run)
            elif len(parts) == 3 and parts[0] == "experiment" and _is_id(parts[1]) and parts[2] == "propose":
                def run() -> str:
                    try:
                        decision = ops.propose(app.settings, int(parts[1]), proposed_by="dashboard")
                    except ops.OpError as failure:
                        return app.home(str(failure), error=True)
                    return app.decision(decision["id"], "Proposal recorded. Review the evidence, then approve or reject.")
                self._guarded(run)
            else:
                self._send(404, page("Not found", '<div class="card"><h1>Not found</h1></div>'))

        def log_message(self, *args: Any) -> None:
            return

    return Handler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8787, allowed_hosts: Optional[List[str]] = None) -> int:
    missing = settings.missing()
    if missing:
        print("referee: set %s first." % " and ".join(missing))
        return 1
    app = App(settings, allowed_hosts)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print("Referee dashboard: http://%s:%d" % (host, port), flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Note: listening beyond this machine. Approval still needs a Flagsmith login, but use this on a trusted network only.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
