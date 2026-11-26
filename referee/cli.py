"""The ``referee`` command."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, config, decisions, ops, report, spec
from .api import FlagsmithError

PLAN_TEMPLATE = {
    "name": "Signup form length",
    "hypothesis": "A three-field signup form gets more completed signups than the six-field form, because every extra field is a reason to leave.",
    "flag": "signup_form",
    "control": {"value": "six_fields", "description": "The current form"},
    "variants": [
        {"key": "three_fields", "value": "three_fields", "share": 50, "description": "Email, password, name"}
    ],
    "rollout_percentage": 100,
    "metrics": [
        {"name": "Signup completed", "event": "signup_completed", "goal": "increase", "aggregation": "occurrence"},
        {"name": "Signup error shown", "event": "signup_error", "goal": "not_increase", "aggregation": "occurrence"},
    ],
    "assumptions": {
        "baseline_rate": 0.2,
        "minimum_lift": 0.2,
        "daily_identities": 800,
        "power": 0.8,
        "deadline": None,
    },
}


def _one_line(text: Any) -> str:
    """Free text for one row of output: no line breaks, modest length."""
    return " ".join(str(text).split())[:120]


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _load_plan_file(path: str) -> Any:
    if Path(path).is_dir():
        raise ops.OpError("%s is a directory; give the plan file itself." % path)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ops.OpError("No plan file at %s. Create one with `referee plan --new %s`." % (path, path))
    except ValueError as error:
        raise ops.OpError("%s is not valid JSON: %s" % (path, error))


# ------------------------------------------------------------------ commands


def cmd_doctor(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.doctor(settings)
    if args.json:
        _print_json(result)
        return 0 if result["ok"] else 1
    shown = result["settings"]
    print("Flagsmith API:  %s" % shown["api_url"])
    print("API key:        %s" % (shown["api_key"] or "(not set)"))
    print("Environment:    %s" % (shown["environment_key"] or "(not set)"))
    print("Settings from:  %s" % shown["source"])
    if "flagsmith" in result:
        f = result["flagsmith"]
        print("Flagsmith:      version %s, %s, experimentation %s" % (
            f.get("version") or "unknown",
            "self-hosted" if f.get("self_hosted") else "cloud" if f.get("version") else "kind unknown",
            "on" if f.get("experimentation_enabled") else "off" if f.get("experimentation_enabled") is False else "unknown",
        ))
    if "environment" in result:
        print("Environment:    '%s' in project %s" % (result["environment"]["name"], result["environment"]["project"]))
    if "warehouse" in result:
        warehouse = result["warehouse"]
        print("Warehouse:      %s" % ("%s (%s)" % (warehouse["name"], warehouse["status"]) if warehouse else "none yet (`referee apply` connects the built-in one)"))
    if "experiments" in result:
        print("Experiments:    %d" % result["experiments"])
    for problem in result["problems"]:
        print("PROBLEM: %s" % problem)
    print("OK: Referee can work with this Flagsmith." if result["ok"] else "Not ready.")
    return 0 if result["ok"] else 1


def cmd_plan(args: argparse.Namespace, settings: config.Settings) -> int:
    if args.new:
        path = Path(args.new)
        if path.exists() and not args.force:
            raise ops.OpError("%s already exists (use --force to overwrite)." % path)
        path.write_text(json.dumps(PLAN_TEMPLATE, indent=2) + "\n", encoding="utf-8")
        print("Wrote a plan template to %s. Edit it, then run `referee plan %s`." % (path, path))
        return 0
    if not args.plan:
        raise ops.OpError("Give a plan file, or use --new FILE to create a template.")
    result = ops.check_plan(_load_plan_file(args.plan))
    if args.json:
        _print_json(result)
        return 0 if result["valid"] else 1
    if not result["valid"]:
        print("The plan has %d problem(s):" % len(result["problems"]))
        for problem in result["problems"]:
            print("  - %s" % problem)
        return 1
    print(report.plan_text(spec.parse(result["plan"])))
    return 0


def cmd_apply(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.apply_plan(settings, _load_plan_file(args.plan), dry_run=args.dry_run)
    if args.json:
        _print_json(result)
        return 0
    print("Dry run: nothing was changed." if result["dry_run"] else "Applied to Flagsmith:")
    for action in result["actions"]:
        print("  - %s" % action)
    if result.get("next"):
        print(result["next"])
    return 0


def cmd_preflight(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.run_preflight(settings, args.experiment, args.code, args.probe)
    if args.json:
        _print_json(result)
    else:
        print(report.preflight_text(result))
    return 0 if result["ready"] else 1


def cmd_start(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.start(settings, args.experiment, args.code, force=args.force)
    if args.json:
        _print_json(result)
        return 0 if result.get("started") or result.get("already_running") else 1
    print(report.preflight_text(result["preflight"]))
    print("")
    if result.get("already_running"):
        print("The experiment is already running.%s" % (
            " Its live rollout was at 0%% and has been raised back to the planned %g%%, so it is serving again."
            % result.get("rollout_percentage", 0) if result.get("rollout_restored") else ""
        ))
        return 0
    if not result["started"]:
        print(result["reason"])
        return 1
    print("Started '%s' at %s, serving %g%% of identities." % (
        result["experiment"]["name"], result["experiment"]["started_at"], result.get("rollout_percentage", 0),
    ))
    if not result.get("code_checked"):
        print("Note: the code was not checked (no --code PATH), so the instrumentation is unverified.")
    if result.get("forced"):
        print("Note: started with --force although preflight failed.")
    return 0


def cmd_pause(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.pause(settings, args.experiment)
    if args.json:
        _print_json(result)
    else:
        print("'%s' is now %s%s" % (
            result["name"], result["status"],
            "." if result.get("serving") else ", and nobody is being served a variant; everyone gets the flag's normal value.",
        ))
        if result.get("warning"):
            print("WARNING: %s" % result["warning"])
    return 0


def cmd_list(args: argparse.Namespace, settings: config.Settings) -> int:
    experiments = ops.list_experiments(settings)
    if args.json:
        _print_json(experiments)
        return 0
    if not experiments:
        print("No experiments in this environment yet. Start with `referee plan --new plan.json`.")
        return 0
    rows = [
        [str(e["id"]), e["status"], e["flag"] or "", e["name"], e["pending_decision"] or ""]
        for e in experiments
    ]
    for line in report._table(["id", "status", "flag", "name", "awaiting approval"], rows):
        print(line)
    return 0


def cmd_scorecard(args: argparse.Namespace, settings: config.Settings) -> int:
    card = ops.get_scorecard(settings, args.experiment, refresh=not args.no_refresh)
    if args.json:
        _print_json(card)
    elif args.markdown:
        print(report.scorecard_markdown(card))
    else:
        print(report.scorecard_text(card))
        if card.get("pending_decision"):
            print("\nAwaiting approval: %s (`referee approve %s`)." % (card["pending_decision"], card["pending_decision"]))
        elif card["verdict"].get("actionable"):
            print("\nTo act on this: `referee propose %s`, then a person approves it." % card["experiment"]["id"])
    return 0


def cmd_watch(args: argparse.Namespace, settings: config.Settings) -> int:
    last_call = None
    try:
        while True:
            card = ops.get_scorecard(settings, args.experiment)
            verdict = card["verdict"]
            progress = card["progress"]
            line = "%s  %-12s %s" % (time.strftime("%H:%M:%S"), verdict["call"], verdict["headline"])
            if progress.get("fraction") is not None:
                line += "  [%d/%d per version]" % (progress["smallest_arm"], progress["needed_per_arm"])
            print(line, flush=True)
            if verdict["call"] != last_call and last_call is not None:
                print(report.scorecard_text(card), flush=True)
            last_call = verdict["call"]
            if verdict["call"] in ("ship_variant", "keep_control", "investigate", "completed", "not_started") and not args.forever:
                if last_call is not None:
                    print("")
                    print(report.scorecard_text(card))
                return 0
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        target = getattr(error, "filename", None)
        print("referee: %s%s." % (error.strerror or error, ": %s" % target if target else ""), file=sys.stderr)
        return 1
    except RecursionError:
        print("referee: that file is nested too deeply to be a plan.", file=sys.stderr)
        return 1


def cmd_propose(args: argparse.Namespace, settings: config.Settings) -> int:
    decision = ops.propose(settings, args.experiment, proposed_by=_one_line(args.by), note=args.note)
    if args.json:
        _print_json(decisions.summary(decision))
        return 0
    print(report.decision_text(decision))
    print("")
    print("Nothing has changed in Flagsmith. A person approves with: referee approve %s" % decision["id"])
    return 0


def cmd_decisions(args: argparse.Namespace, settings: config.Settings) -> int:
    found = ops.list_decisions(settings)
    if args.json:
        _print_json(found)
        return 0
    if not found:
        print("No decisions recorded yet.")
        return 0
    rows = [[d["id"], d["status"], str(d["experiment_id"]), d["headline"], d["proposed_at"]] for d in found]
    for line in report._table(["decision", "status", "experiment", "proposal", "proposed at"], rows):
        print(line)
    return 0


def cmd_show(args: argparse.Namespace, settings: config.Settings) -> int:
    decision = ops.get_decision(settings, args.decision)
    if args.json:
        _print_json(decision)
        return 0
    print(report.decision_text(decision))
    print("")
    print("Evidence when proposed:")
    print(report.scorecard_text(decision["scorecard"]))
    return 0


def _read_tty(prompt: str) -> str:
    """Read a line from the person at the terminal, never from a pipe."""
    # Opened separately for writing and reading: a terminal is not seekable,
    # so a single read-write text stream cannot be used on it.
    with open("/dev/tty", "w") as out:
        out.write(prompt)
        out.flush()
    with open("/dev/tty", "r") as tty:
        return tty.readline().strip()


def cmd_approve(args: argparse.Namespace, settings: config.Settings) -> int:
    # Approval is the one step that must be a person. It needs a terminal and a
    # Flagsmith login; there is deliberately no flag to pass a token.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "Approval has to be done by a person at a terminal (or in the dashboard: `referee serve`).\n"
            "This command will not run without one.",
            file=sys.stderr,
        )
        return 2
    decision = ops.get_decision(settings, args.decision)
    print(report.decision_text(decision))
    if decision["status"] != "pending":
        decisions.Ledger(settings.state_dir).note(
            "refused", decision["id"], "referee", {"reason": "decision is already %s" % decision["status"], "method": "cli"}
        )
        return 1
    print("")
    print("Approving ends the experiment in Flagsmith and serves the decided version to everyone.")
    print("Sign in with your Flagsmith account; the change is made as you.")
    email = _read_tty("Flagsmith email: ")
    password = getpass.getpass("Flagsmith password: ")
    try:
        token = settings.client().login(email, password)
    except FlagsmithError as error:
        ops.note_refused_sign_in(settings, args.decision, email, "cli")
        print("Sign-in failed: %s" % error, file=sys.stderr)
        return 1
    typed = _read_tty("Type the flag name '%s' to confirm: " % decision["flag"])
    if typed != decision["flag"]:
        decisions.Ledger(settings.state_dir).note(
            "approval_refused", decision["id"], _one_line(email), {"reason": "flag name confirmation did not match", "method": "cli"}
        )
        print("That does not match; nothing was changed.")
        return 1
    applied = ops.approve(settings, args.decision, token, note=args.note, method="cli")
    print("")
    print(report.decision_text(applied))
    print("Done. Flagsmith reports: %s" % json.dumps(applied.get("applied")))
    return 0


def cmd_reject(args: argparse.Namespace, settings: config.Settings) -> int:
    by = _one_line(args.by or os.environ.get("USER") or "someone")
    decision = ops.reject(settings, args.decision, by, args.reason)
    if args.json:
        _print_json(decisions.summary(decision))
    else:
        print(report.decision_text(decision))
    return 0


def cmd_cleanup(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.cleanup(settings, args.experiment, args.code, args.repo_url, args.revision)
    if args.json:
        _print_json(result)
        return 0
    experiment = result["experiment"]
    print("Cleanup: %s (flag '%s', %s)" % (experiment["name"], experiment["flag"], experiment["status"]))
    print("  %s" % result["advice"])
    print("")
    if not result["references"] and result.get("ready_to_clean"):
        print("  The flag is no longer mentioned in %d source files. Safe to archive it in Flagsmith." % result["files_scanned"])
    elif not result["references"]:
        print("  The flag is not mentioned in the %d source files scanned." % result["files_scanned"])
    else:
        print("  The flag is still read in %d place(s):" % len(result["references"]))
        for reference in result["references"]:
            print("    %s:%d  %s" % (reference["file"], reference["line"], reference["text"]))
    for value, references in (result.get("loser_code") or {}).items():
        print("  Code for the losing version '%s':" % value)
        for reference in references:
            print("    %s:%d  %s" % (reference["file"], reference["line"], reference["text"]))
    if "uploaded_to_flagsmith" in result:
        print("  Uploaded %d code reference(s) to Flagsmith." % result["uploaded_to_flagsmith"])
    if "upload_error" in result:
        print("  Could not upload code references: %s" % result["upload_error"])
    return 0


def cmd_stale(args: argparse.Namespace, settings: config.Settings) -> int:
    result = ops.stale_flags(settings, args.code)
    if args.json:
        _print_json(result)
        return 0
    if not result["stale"]:
        print("No stale experiment flags in %s (%d finished experiments checked)." % (result["code_root"], result["finished_experiments"]))
        return 0
    for item in result["stale"]:
        print("'%s': experiment '%s' ended %s (%s) but the code still reads the flag:" % (
            item["flag"], item["experiment"], item["ended_at"], item["rollout_decision"],
        ))
        for reference in item["references"]:
            print("    %s:%d  %s" % (reference["file"], reference["line"], reference["text"]))
    return 0


def cmd_ledger(args: argparse.Namespace, settings: config.Settings) -> int:
    ledger = decisions.Ledger(settings.state_dir)
    check = ledger.verify()
    if args.json:
        _print_json({"verify": check, "entries": ledger.entries()})
        return 0 if check["ok"] else 1
    for index, entry in enumerate(ledger.entries(), start=1):
        if entry.get("damaged"):
            print("%3d  (unreadable line)" % index)
            continue
        print("%3s  %s  %-22s %-10s %s  %s" % (
            entry.get("seq", "?"), entry.get("at", ""), entry.get("event", ""), entry.get("decision", ""),
            _one_line(entry.get("actor", "")), str(entry.get("hash", ""))[:12],
        ))
    if check["ok"]:
        print("Ledger intact: %d entries, hash chain verified." % check["entries"])
        return 0
    print("LEDGER TAMPERED: %s." % check["reason"])
    return 1


def cmd_mcp(args: argparse.Namespace, settings: config.Settings) -> int:
    from . import mcp_server

    return mcp_server.serve(settings)


def cmd_serve(args: argparse.Namespace, settings: config.Settings) -> int:
    from . import web

    return web.serve(settings, host=args.host, port=args.port, allowed_hosts=args.allow_host)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="referee",
        description="Plan, check, watch and call Flagsmith experiments. A person approves every rollout.",
    )
    parser.add_argument("--version", action="version", version="referee " + __version__)
    parser.add_argument("-C", "--directory", default=None, help="run as if started in this directory")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")
        return command

    add("doctor", cmd_doctor, "Check the connection to Flagsmith.")

    p = add("plan", cmd_plan, "Validate a plan and estimate how long it needs.")
    p.add_argument("plan", nargs="?", help="plan file (JSON)")
    p.add_argument("--new", metavar="FILE", help="write a plan template to FILE")
    p.add_argument("--force", action="store_true", help="overwrite an existing file with --new")

    p = add("apply", cmd_apply, "Create the flag, variants, metrics and experiment in Flagsmith. Starts nothing.")
    p.add_argument("plan", help="plan file (JSON)")
    p.add_argument("--dry-run", action="store_true", help="show what would be created")

    p = add("preflight", cmd_preflight, "Check an experiment and its code before it starts.")
    p.add_argument("experiment", help="experiment id, name or flag name")
    p.add_argument("--code", metavar="PATH", help="source tree to check for instrumentation")
    p.add_argument("--probe", type=int, default=0, metavar="N", help="once running: evaluate N made-up identities to check assignment")

    p = add("start", cmd_start, "Start an experiment if preflight passes.")
    p.add_argument("experiment")
    p.add_argument("--code", metavar="PATH")
    p.add_argument("--force", action="store_true", help="start even though preflight failed")

    p = add("pause", cmd_pause, "Pause a running experiment.")
    p.add_argument("experiment")

    add("list", cmd_list, "List experiments in this environment.")

    p = add("scorecard", cmd_scorecard, "Show the numbers, the checks and the call for an experiment.")
    p.add_argument("experiment")
    p.add_argument("--markdown", action="store_true", help="print Markdown for a pull request or chat")
    p.add_argument("--no-refresh", action="store_true", help="do not ask Flagsmith to recompute results")

    p = add("watch", cmd_watch, "Re-check an experiment until there is a call.")
    p.add_argument("experiment")
    p.add_argument("--interval", type=int, default=120, help="seconds between checks (default 120)")
    p.add_argument("--forever", action="store_true", help="keep watching after a call")

    p = add("propose", cmd_propose, "Record a rollout proposal for a person to approve.")
    p.add_argument("experiment")
    p.add_argument("--by", default=os.environ.get("USER") or "cli", help="who is proposing")
    p.add_argument("--note", default="", help="a note for the approver")

    add("decisions", cmd_decisions, "List rollout decisions.")

    p = add("show", cmd_show, "Show one decision with the evidence it rests on.")
    p.add_argument("decision")

    p = add("approve", cmd_approve, "Approve a decision as yourself and apply it in Flagsmith (interactive only).")
    p.add_argument("decision")
    p.add_argument("--note", default="")

    p = add("reject", cmd_reject, "Reject a pending decision.")
    p.add_argument("decision")
    p.add_argument("--reason", default="")
    p.add_argument("--by", default="")

    p = add("cleanup", cmd_cleanup, "After an experiment: list the flag code left to remove.")
    p.add_argument("experiment")
    p.add_argument("--code", metavar="PATH", default=".")
    p.add_argument("--repo-url", default="", help="also upload code references to Flagsmith for this repository URL")
    p.add_argument("--revision", default="", help="commit the references were scanned at")

    p = add("stale", cmd_stale, "Find flags of finished experiments that the code still reads.")
    p.add_argument("--code", metavar="PATH", default=".")

    add("ledger", cmd_ledger, "Print the decision ledger and verify its hash chain.")

    p = add("mcp", cmd_mcp, "Run the MCP server on stdin/stdout for a coding agent.")

    p = add("serve", cmd_serve, "Run the local dashboard where a person reviews and approves decisions.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME", help="extra host name the dashboard may be reached by (localhost is always allowed)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        if args.directory and not Path(args.directory).is_dir():
            raise ops.OpError("There is no directory at %s." % args.directory)
        settings = config.load(Path(args.directory) if args.directory else None)
        return int(args.func(args, settings) or 0)
    except ops.OpError as error:
        print("referee: %s" % error, file=sys.stderr)
        return 1
    except FlagsmithError as error:
        print("referee: %s" % error, file=sys.stderr)
        return 1
    except ValueError as error:
        print("referee: %s" % error, file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # a bug: say so plainly instead of a traceback
        if os.environ.get("REFEREE_DEBUG"):
            raise
        print(
            "referee: unexpected error (%s: %s). Set REFEREE_DEBUG=1 to see the traceback."
            % (type(error).__name__, error),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
