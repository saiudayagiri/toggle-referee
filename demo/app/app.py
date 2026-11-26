"""Hack Night signup: the sample app Referee's demo runs an experiment on.

One page, two versions of the signup form, chosen by the Flagsmith flag
``signup_form``. The official Flagsmith Python SDK does three things here:

* ``get_experiment_flag`` picks the version for a visitor and records that
  they saw it (the exposure),
* ``track_event("signup_completed")`` records the success the experiment is
  judged on,
* ``track_event("signup_error")`` records a guardrail: the short form must not
  produce more failed submissions.
"""

import html
import os
import uuid
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from flagsmith import Flagsmith
from flagsmith.analytics import EventProcessorConfig
from flagsmith.models import DefaultFlag

API_URL = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1").rstrip("/") + "/"
# The SDK appends "v1/events" to the events URL itself, so on self-hosted
# Flagsmith it must end in /api/ (not /api/v1/), or every event gets a 405.
EVENTS_URL = os.environ.get("FLAGSMITH_EVENTS_URL") or (
    API_URL[: -len("v1/")] if API_URL.endswith("/api/v1/") else API_URL
)
ENVIRONMENT_KEY = os.environ["FLAGSMITH_ENVIRONMENT_KEY"]
PORT = int(os.environ.get("PORT", "8080"))

flagsmith = Flagsmith(
    environment_key=ENVIRONMENT_KEY,
    api_url=API_URL,
    enable_events=True,
    event_processor_config=EventProcessorConfig(
        events_api_url=EVENTS_URL,
        flush_interval_seconds=float(os.environ.get("FLUSH_SECONDS", "2")),
    ),
    # Before the flag exists in Flagsmith (or if Flagsmith is unreachable) the
    # page still works and shows the current form.
    default_flag_handler=lambda name: DefaultFlag(enabled=False, value=None),
)

FIELDS = {
    "six_fields": [
        ("name", "Full name", "text"),
        ("email", "Email", "email"),
        ("password", "Password", "password"),
        ("college", "College", "text"),
        ("phone", "Phone number", "tel"),
        ("github", "GitHub username", "text"),
    ],
    "three_fields": [
        ("name", "Full name", "text"),
        ("email", "Email", "email"),
        ("password", "Password", "password"),
    ],
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hack Night signup</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;background:#f4f1ea;color:#1d1b16;margin:0}}
 main{{max-width:26rem;margin:3rem auto;padding:1.5rem;background:#fffdf8;border:1px solid #d9d2c3;border-radius:10px}}
 h1{{font-size:1.5rem;margin:0 0 .25rem}} p{{margin:.25rem 0 1rem;color:#5b564a}}
 label{{display:block;margin:.75rem 0 .2rem;font-size:.9rem}}
 input{{width:100%;box-sizing:border-box;padding:.55rem;border:1px solid #b9b19f;border-radius:6px;font:inherit}}
 button{{margin-top:1.1rem;width:100%;padding:.7rem;border:0;border-radius:6px;background:#1f5f4a;color:#fff;font:inherit;cursor:pointer}}
 .error{{background:#fbe6e1;border:1px solid #d98c7c;padding:.5rem .7rem;border-radius:6px}}
 small{{color:#8a8372}}
</style></head><body><main>{body}</main></body></html>"""


def form_version(visitor: str) -> str:
    """Which form this visitor gets. Also records the exposure in Flagsmith."""
    flag = flagsmith.get_experiment_flag("signup_form", identifier=visitor)
    if flag.enabled and flag.value == "three_fields":
        return "three_fields"
    return "six_fields"


def render_form(version: str, error: str = "") -> str:
    rows = []
    for name, label, kind in FIELDS[version]:
        rows.append(
            '<label for="{0}">{1}</label><input id="{0}" name="{0}" type="{2}" required>'.format(name, label, kind)
        )
    return PAGE.format(
        body=(
            "<h1>Join Hack Night</h1><p>Saturday, 6 pm. Bring a laptop and an idea.</p>"
            + ('<p class="error">%s</p>' % html.escape(error) if error else "")
            + '<form method="post" action="/signup" data-version="%s">%s<button>Save my seat</button></form>'
            % (version, "".join(rows))
            + "<small>Form version: %s</small>" % version
        )
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "HackNight/1.0"

    def visitor(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "visitor" in jar:
            return jar["visitor"].value, False
        return "visitor-" + uuid.uuid4().hex[:16], True

    def reply(self, status: int, body: str, visitor: str = "", new: bool = False) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if new:
            self.send_header("Set-Cookie", "visitor=%s; Path=/; HttpOnly; SameSite=Lax" % visitor)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.reply(200, "ok")
            return
        if self.path != "/":
            self.reply(404, PAGE.format(body="<h1>Not found</h1>"))
            return
        visitor, new = self.visitor()
        self.reply(200, render_form(form_version(visitor)), visitor, new)

    def do_POST(self) -> None:
        if self.path != "/signup":
            self.reply(404, PAGE.format(body="<h1>Not found</h1>"))
            return
        visitor, new = self.visitor()
        length = min(int(self.headers.get("Content-Length") or 0), 20000)
        form = {k: v[0].strip() for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
        version = form_version(visitor)
        missing = [label for name, label, _ in FIELDS[version] if not form.get(name)]
        problem = ""
        if missing:
            problem = "Please fill in: %s." % ", ".join(missing)
        elif "@" not in form.get("email", ""):
            problem = "That email address does not look right."
        elif len(form.get("password", "")) < 8:
            problem = "The password needs at least 8 characters."
        if problem:
            flagsmith.track_event("signup_error", identifier=visitor)
            self.reply(400, render_form(version, problem), visitor, new)
            return
        flagsmith.track_event("signup_completed", identifier=visitor)
        self.reply(
            200,
            PAGE.format(body="<h1>You are in, %s.</h1><p>See you on Saturday.</p>" % html.escape(form["name"])),
            visitor,
            new,
        )

    def log_message(self, *args) -> None:
        return


if __name__ == "__main__":
    print("Hack Night signup on http://0.0.0.0:%d (flags from %s, events to %s)" % (PORT, API_URL, EVENTS_URL), flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    finally:
        flagsmith.close()
