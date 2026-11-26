#!/usr/bin/env python3
"""Prepare a fresh self-hosted Flagsmith for Referee.

Creates (or reuses) a local admin user, an organisation, a project, an
environment and an organisation API key for the agent, connects the built-in
event warehouse, and writes the result to ``flagsmith.env``.

Safe to run again: existing things are reused. Standard library only.

    python3 scripts/bootstrap.py --out flagsmith.env
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Token " + token
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode("utf-8", "replace")[:300]


def results(data):
    return data.get("results", []) if isinstance(data, dict) else (data or [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("FLAGSMITH_URL", "http://localhost:8000"))
    parser.add_argument("--email", default=os.environ.get("FLAGSMITH_ADMIN_EMAIL", "referee@example.com"))
    parser.add_argument("--password", default=os.environ.get("FLAGSMITH_ADMIN_PASSWORD", "Referee-local-2026!"))
    parser.add_argument("--organisation", default="Referee Lab")
    parser.add_argument("--project", default="hack-night")
    parser.add_argument("--environment", default="Production")
    parser.add_argument("--out", default="flagsmith.env")
    parser.add_argument("--wait", type=int, default=180, help="seconds to wait for Flagsmith to come up")
    args = parser.parse_args()
    root = args.url.rstrip("/")
    base = root + "/api/v1"

    deadline = time.time() + args.wait
    while True:
        try:
            with urllib.request.urlopen(root + "/health", timeout=5) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        if time.time() > deadline:
            print("Flagsmith did not answer at %s within %ds." % (root, args.wait), file=sys.stderr)
            return 1
        time.sleep(2)

    status, data = call(base, "POST", "/auth/login/", {"email": args.email, "password": args.password})
    if status != 200:
        status, data = call(
            base, "POST", "/auth/users/",
            {"email": args.email, "password": args.password, "re_password": args.password,
             "first_name": "Local", "last_name": "Admin"},
        )
        if status != 201:
            print("Could not sign in or sign up as %s: %s" % (args.email, data), file=sys.stderr)
            return 1
        print("Created local admin %s" % args.email)
    token = data["key"]

    def ensure(list_path, create_path, name, body, label):
        status, found = call(base, "GET", list_path, token=token)
        for item in results(found):
            if item.get("name") == name:
                print("%s '%s' already exists" % (label, name))
                return item
        status, created = call(base, "POST", create_path, body, token=token)
        if status == 403 and len(results(found)) == 1:
            # Open-source Flagsmith's free plan allows one project per organisation.
            only = results(found)[0]
            print("%s '%s' could not be created (plan limit); using the existing '%s'" % (label, name, only.get("name")))
            return only
        if status not in (200, 201):
            raise SystemExit("Could not create %s '%s': %s" % (label.lower(), name, created))
        print("Created %s '%s'" % (label.lower(), name))
        return created

    organisation = ensure("/organisations/", "/organisations/", args.organisation, {"name": args.organisation}, "Organisation")
    project = ensure(
        "/projects/?organisation=%d" % organisation["id"], "/projects/", args.project,
        {"name": args.project, "organisation": organisation["id"]}, "Project",
    )
    environment = ensure(
        "/environments/?project=%d" % project["id"], "/environments/", args.environment,
        {"name": args.environment, "project": project["id"]}, "Environment",
    )
    env_key = environment["api_key"]

    status, connections = call(base, "GET", "/environments/%s/warehouse-connections/" % env_key, token=token)
    if status == 200 and not connections:
        status, created = call(
            base, "POST", "/environments/%s/warehouse-connections/" % env_key,
            {"warehouse_type": "postgres", "config": {"source": "flagsmith_database"}}, token=token,
        )
        print("Connected the built-in event warehouse" if status == 201 else "Warehouse not connected: %s" % created)
    elif status != 200:
        print("Experimentation does not seem to be enabled on this Flagsmith: %s" % connections, file=sys.stderr)

    api_key = ""
    if os.path.isfile(args.out):
        for line in open(args.out, encoding="utf-8"):
            if line.startswith("FLAGSMITH_API_KEY=") and env_key in open(args.out, encoding="utf-8").read():
                api_key = line.split("=", 1)[1].strip()
    if api_key:
        probe = urllib.request.Request(
            base + "/environments/%s/" % env_key, headers={"Authorization": "Api-Key " + api_key}
        )
        try:
            urllib.request.urlopen(probe, timeout=10).read()
            print("Reusing the agent API key in %s" % args.out)
        except (urllib.error.URLError, OSError):
            api_key = ""
    if not api_key:
        status, created = call(
            base, "POST", "/organisations/%d/master-api-keys/" % organisation["id"],
            {"name": "referee-agent", "organisation": organisation["id"], "is_admin": True}, token=token,
        )
        if status != 201:
            print("Could not create an API key for the agent: %s" % created, file=sys.stderr)
            return 1
        api_key = created["key"]
        print("Created organisation API key 'referee-agent' for the agent")

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(
            "# Written by scripts/bootstrap.py. Local development values; do not commit.\n"
            "FLAGSMITH_API_URL=%s\n"
            "FLAGSMITH_API_KEY=%s\n"
            "FLAGSMITH_ENVIRONMENT_KEY=%s\n"
            "FLAGSMITH_PROJECT_ID=%d\n"
            "FLAGSMITH_DASHBOARD_URL=%s\n" % (base, api_key, env_key, project["id"], root)
        )
    try:
        os.chmod(args.out, 0o600)
    except OSError:
        pass
    print("")
    print("Wrote %s" % args.out)
    print("Flagsmith dashboard: %s  (sign in as %s)" % (root, args.email))
    print("The person who approves rollouts signs in with that same account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
