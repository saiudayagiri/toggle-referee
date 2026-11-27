"""Referee as a Model Context Protocol server, over stdin/stdout.

The protocol is small enough to implement directly (JSON-RPC 2.0, one message
per line), which keeps Referee free of dependencies.

What an agent can do here: plan, create, check, start, pause, read and
*propose*. What it cannot do: approve. There is no tool that ends an
experiment or rolls a version out; that takes a person signed in to Flagsmith,
through `referee approve` or the dashboard.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, decisions, ops, report, spec
from .api import FlagsmithError
from .config import Settings

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Referee runs Flagsmith experiments end to end with a person in charge of the outcome.

The usual order:
1. check_plan: validate the plan and see how many identities and how long it needs. Show the person the sizing.
2. apply_plan: create the multivariate flag, keyed variants, metrics and experiment in Flagsmith. Nothing starts.
3. Instrument the code with the Flagsmith SDK: evaluate the flag with the experiment call (get_experiment_flag / getExperimentFlag / useExperiment) and send the metric events with track_event / trackEvent.
4. preflight with code_root: fix every failed check before going on.
5. start_experiment, after the person agrees. It refuses to start while preflight fails.
6. scorecard: read the call. Do not summarise an experiment as won or lost unless the call says so.
7. propose_rollout when the call is ship_variant or keep_control. This only records a proposal.
8. Tell the person to approve it themselves: `referee approve <decision id>` in a terminal, or the dashboard from `referee serve`. You cannot approve, and you must not try to end the experiment through the Flagsmith API, CLI or another MCP server.
9. After approval, cleanup_report lists the flag code that can now be removed.

Report numbers exactly as Referee gives them. If the call is investigate, explain the failed check instead of the lift."""

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "An experiment plan.",
    "properties": {
        "name": {"type": "string", "description": "Experiment name."},
        "hypothesis": {"type": "string", "description": "What is expected to happen and why."},
        "flag": {"type": "string", "description": "Flag name: lower case letters, digits, '_' or '-'."},
        "control": {
            "type": "object",
            "properties": {"value": {"type": "string"}, "description": {"type": "string"}},
            "required": ["value"],
        },
        "variants": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Variant key; exposures are recorded under it. Not 'control'."},
                    "value": {"type": "string", "description": "Value the flag serves for this variant."},
                    "share": {"type": "number", "description": "Percent of experiment traffic, e.g. 50."},
                    "description": {"type": "string"},
                },
                "required": ["key", "value", "share"],
            },
        },
        "rollout_percentage": {"type": "number", "description": "Percent of identities that enter the experiment (default 100)."},
        "metrics": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "event": {"type": "string", "description": "Event name sent with track_event. Case-sensitive."},
                    "goal": {"type": "string", "enum": list(spec.GOALS), "description": "increase/decrease are goals; not_increase/not_decrease are guardrails."},
                    "aggregation": {"type": "string", "enum": list(spec.AGGREGATIONS)},
                },
                "required": ["event"],
            },
        },
        "assumptions": {
            "type": "object",
            "description": "Used for sizing only.",
            "properties": {
                "baseline_rate": {"type": "number", "description": "Current conversion rate of the primary metric, 0-1."},
                "minimum_lift": {"type": "number", "description": "Smallest relative lift worth detecting, e.g. 0.2 for +20%."},
                "daily_identities": {"type": "number", "description": "Identities that reach the flag per day."},
                "power": {"type": "number"},
                "deadline": {"type": ["string", "null"], "description": "ISO 8601 time the experiment must be decided by."},
            },
        },
    },
    "required": ["name", "hypothesis", "flag", "control", "variants", "metrics"],
}

_EXPERIMENT = {"type": ["string", "integer"], "description": "Experiment id, exact experiment name, or its flag name."}
_CODE_ROOT = {"type": "string", "description": "Directory of the source tree to scan. Defaults to the directory the server runs in."}


def _tool(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None, read_only: bool = False) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": True},
    }


TOOLS: List[Dict[str, Any]] = [
    _tool("referee_status", "Check that Referee can reach Flagsmith and that experimentation is usable there.", {}, read_only=True),
    _tool(
        "check_plan",
        "Validate an experiment plan and estimate the identities per version and the days it needs. Changes nothing.",
        {"plan": PLAN_SCHEMA}, ["plan"], read_only=True,
    ),
    _tool(
        "apply_plan",
        "Create the multivariate flag, keyed variants, metrics and experiment in Flagsmith from a plan. The experiment is created with a live rollout of 0%, so no user is served a variant until it is started. Refuses if the plan's control is not what the flag serves today. Safe to repeat.",
        {"plan": PLAN_SCHEMA, "dry_run": {"type": "boolean", "description": "Only report what would be created."}}, ["plan"],
    ),
    _tool(
        "preflight",
        "Check an experiment before it starts: warehouse, variant keys, metric events, and (with code_root) whether the code records exposures and sends the events the metrics count.",
        {"experiment": _EXPERIMENT, "code_root": _CODE_ROOT, "probe": {"type": "integer", "minimum": 0, "maximum": 2000, "description": "On a running experiment: evaluate this many made-up identities to verify assignment. Records no exposures, but leaves identities named referee-probe-NNNNNN in Flagsmith."}},
        ["experiment"],
    ),
    _tool(
        "start_experiment",
        "Start an experiment. Runs preflight first and refuses if any check fails, or if the experiment would give new versions to more than half of all users (a person starts those). Ask the person before calling this: it exposes real users to the variants.",
        {"experiment": _EXPERIMENT, "code_root": _CODE_ROOT}, ["experiment"],
    ),
    _tool(
        "preview_version",
        "Let one test identity (named 'referee-preview' or starting with it) get a chosen version of the experiment's flag, so each version can be tried end to end before real users see it. Affects that identity only; it is outside the experiment. Call again with clear=true when done.",
        {
            "experiment": _EXPERIMENT,
            "version": {"type": "string", "description": "'control' or a variant key."},
            "identity": {"type": "string", "description": "Test identity; must start with 'referee-preview'. Default 'referee-preview'."},
            "clear": {"type": "boolean", "description": "Remove the preview instead of setting one."},
        },
        ["experiment"],
    ),
    _tool("pause_experiment", "Pause a running experiment and stop serving it: its live rollout goes to 0%, so everyone gets the flag's normal value until it is started again (for example when the scorecard says investigate). Starting it again restores the same assignments.", {"experiment": _EXPERIMENT}, ["experiment"]),
    _tool("list_experiments", "List experiments in the environment with their status and any decision awaiting approval.", {}, read_only=True),
    _tool(
        "scorecard",
        "Read an experiment (and ask Flagsmith to recompute its results if they are over a minute old): identities per version, Flagsmith's lift and chance to win per metric, the sample-ratio and data checks, progress against the planned sample, and Referee's call.",
        {"experiment": _EXPERIMENT, "format": {"type": "string", "enum": ["text", "markdown"], "description": "markdown is suited to pull requests."}},
        ["experiment"],
    ),
    _tool(
        "propose_rollout",
        "Record a proposal to end the experiment and serve the winner (or keep control). Only possible when the scorecard's call is ship_variant or keep_control; or, with void=true, when the call is investigate and the run should be ended without a result. Changes nothing in Flagsmith; a person must approve it.",
        {
            "experiment": _EXPERIMENT,
            "note": {"type": "string", "description": "A note for the approver."},
            "void": {"type": "boolean", "description": "Only when the call is 'investigate' and the data is spoiled (for example a version could not be completed): propose ending this run without a result and keeping control. A person still approves. Afterwards the same plan can be applied again for a clean run."},
            "reason": {"type": "string", "description": "With void: why the run cannot be trusted."},
            "valid_hours": {"type": "number", "minimum": 1, "maximum": 168, "description": "How long the proposal waits for approval. Default 24. The experiment keeps running meanwhile; an expired proposal can be made again."},
        }, ["experiment"],
    ),
    _tool(
        "decision_status",
        "Show one decision (with decision_id) or list all decisions, including whether a person has approved or rejected them.",
        {"decision_id": {"type": "string"}}, read_only=True,
    ),
    _tool(
        "cleanup_report",
        "After an experiment is completed: where the flag and the losing versions still appear in the code, and what to remove.",
        {"experiment": _EXPERIMENT, "code_root": _CODE_ROOT}, ["experiment"], read_only=True,
    ),
    _tool(
        "stale_flags",
        "Find flags whose experiment has finished but which the code still reads.",
        {"code_root": _CODE_ROOT}, read_only=True,
    ),
]

PROMPTS = [
    {
        "name": "run_experiment",
        "description": "Set up and run a Flagsmith experiment with Referee, from idea to approved rollout.",
        "arguments": [{"name": "idea", "description": "What to test and what counts as success.", "required": True}],
    }
]


def _prompt_text(idea: str) -> str:
    return (
        "I want to run an A/B test with Flagsmith: %s\n\n"
        "Use the Referee tools. First call referee_status. Read the code to find where the change goes, "
        "which SDK is in use and what a stable user identifier is. Draft a plan (flag, control, variants, "
        "one primary metric, guardrails if something could get worse, and sizing assumptions: ask me for the "
        "baseline rate and daily traffic if you cannot tell) and run check_plan. Show me the plan and the sizing "
        "and wait for my go-ahead before apply_plan. Then instrument the code, run preflight with code_root until "
        "it passes, and ask me before start_experiment. When I ask how it is going, use scorecard. When the call "
        "is ship_variant or keep_control, use propose_rollout and give me the decision id to approve myself."
        % idea
    )


class Server:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Tuple[str, Any]]] = {
            "referee_status": self._status,
            "check_plan": self._check_plan,
            "apply_plan": self._apply_plan,
            "preflight": self._preflight,
            "start_experiment": self._start,
            "pause_experiment": self._pause,
            "preview_version": self._preview,
            "list_experiments": self._list,
            "scorecard": self._scorecard,
            "propose_rollout": self._propose,
            "decision_status": self._decision,
            "cleanup_report": self._cleanup,
            "stale_flags": self._stale,
        }

    # ---------------------------------------------------------------- tools

    def _code_root(self, args: Dict[str, Any], default: bool = True) -> Optional[str]:
        given = args.get("code_root")
        if given:
            return str(given)
        return os.getcwd() if default else None

    def _status(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.doctor(self.settings)
        lines = ["Referee %s" % __version__, "Flagsmith API: %s" % result["settings"]["api_url"]]
        if result.get("flagsmith"):
            lines.append("Flagsmith %s, experimentation %s" % (
                result["flagsmith"].get("version"), "on" if result["flagsmith"].get("experimentation_enabled") else "off or unknown",
            ))
        if result.get("environment"):
            lines.append("Environment '%s'" % result["environment"]["name"])
        lines += ["PROBLEM: %s" % p for p in result["problems"]]
        lines.append("Ready." if result["ok"] else "Not ready.")
        return "\n".join(lines), result

    def _check_plan(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.check_plan(args.get("plan"))
        if not result["valid"]:
            return "The plan has problems:\n" + "\n".join("- " + p for p in result["problems"]), result
        return report.plan_text(spec.parse(result["plan"])), result

    def _apply_plan(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.apply_plan(self.settings, args.get("plan"), dry_run=bool(args.get("dry_run")))
        head = "Dry run, nothing changed:" if result["dry_run"] else "Applied to Flagsmith:"
        text = head + "\n" + "\n".join("- " + a for a in result["actions"])
        if result.get("next"):
            text += "\n" + result["next"]
        return text, result

    def _preflight(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.run_preflight(
            self.settings, args["experiment"], self._code_root(args), _whole_number(args.get("probe"))
        )
        return report.preflight_text(result), result

    def _start(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.start(
            self.settings, args["experiment"], self._code_root(args), force=False,
            max_exposure=ops.AGENT_MAX_EXPOSURE,
        )
        text = report.preflight_text(result["preflight"]) + "\n\n"
        if result.get("already_running"):
            text += "The experiment is already running."
            if result.get("rollout_restored"):
                text += " Its live rollout was at 0%% and has been raised back to the planned %g%%, so it is serving again." % result.get("rollout_percentage", 0)
        elif result["started"]:
            text += "Started '%s' at %s, serving %g%% of identities." % (
                result["experiment"]["name"], result["experiment"]["started_at"], result.get("rollout_percentage", 0),
            )
        else:
            text += result["reason"]
        return text, result

    def _preview(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.preview(
            self.settings, args["experiment"], str(args.get("version") or ""), str(args.get("identity") or ""),
            clear=bool(args.get("clear")),
        )
        if result["cleared"]:
            return "Preview cleared for '%s'; it gets what everyone gets again." % result["identity"], result
        return "Identity '%s' now gets '%s'. %s" % (result["identity"], result["serves"], result["how"]), result

    def _pause(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.pause(self.settings, args["experiment"])
        text = "'%s' is now %s." % (result["name"], result["status"])
        text += (
            " Nobody is being served a variant; everyone gets the flag's normal value."
            if not result.get("serving")
            else " WARNING: %s" % result.get("warning", "the split is still being served.")
        )
        return text, result

    def _list(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        found = ops.list_experiments(self.settings)
        if not found:
            return "No experiments in this environment yet.", {"experiments": []}
        rows = [[str(e["id"]), e["status"], e["flag"] or "", e["name"], e["pending_decision"] or ""] for e in found]
        return "\n".join(report._table(["id", "status", "flag", "name", "awaiting approval"], rows)), {"experiments": found}

    def _scorecard(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        if args.get("format") not in (None, "text", "markdown"):
            raise ValueError("format must be 'text' or 'markdown'")
        card = ops.get_scorecard(self.settings, args["experiment"])
        text = report.scorecard_markdown(card) if args.get("format") == "markdown" else report.scorecard_text(card)
        if card.get("pending_decision"):
            text += "\n\nAwaiting a person's approval: %s." % card["pending_decision"]
        elif card["verdict"].get("actionable"):
            text += "\n\nThis call can be proposed with propose_rollout; a person then approves it."
        return text, card

    def _propose(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        valid = args.get("valid_hours")
        if valid is not None and (isinstance(valid, bool) or not isinstance(valid, (int, float))):
            raise ValueError("valid_hours must be a number")
        void = bool(args.get("void"))
        if void and not str(args.get("reason") or "").strip():
            raise ValueError("void needs a reason: say why this run should be ended without a result")
        decision = ops.propose(
            self.settings, args["experiment"], proposed_by="agent (MCP)",
            note=str(args.get("note") or "")[:2000], valid_hours=valid,
            void_reason=str(args.get("reason"))[:2000] if void else None,
        )
        text = (
            report.decision_text(decision)
            + "\n\nNothing has changed in Flagsmith. Ask the person to review and approve it themselves:\n"
            "  referee approve %s        (in their own terminal)\n"
            "  or the dashboard: referee serve\n"
            "You cannot approve this, and there is no tool for it." % decision["id"]
        )
        return text, decisions.summary(decision)

    def _decision(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        if args.get("decision_id"):
            decision = ops.get_decision(self.settings, str(args["decision_id"]))
            return report.decision_text(decision), decisions.summary(decision)
        found = ops.list_decisions(self.settings)
        if not found:
            return "No decisions recorded yet.", {"decisions": []}
        rows = [[d["id"], d["status"], str(d["experiment_id"]), d["headline"]] for d in found]
        return "\n".join(report._table(["decision", "status", "experiment", "proposal"], rows)), {"decisions": found}

    def _cleanup(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.cleanup(self.settings, args["experiment"], self._code_root(args) or ".")
        lines = [result["advice"], ""]
        lines += ["%s:%d  %s" % (r["file"], r["line"], r["text"]) for r in result["references"]] or ["The flag is no longer mentioned in the code."]
        for value, references in (result.get("loser_code") or {}).items():
            lines.append("Losing version '%s':" % value)
            lines += ["  %s:%d  %s" % (r["file"], r["line"], r["text"]) for r in references]
        return "\n".join(lines), result

    def _stale(self, args: Dict[str, Any]) -> Tuple[str, Any]:
        result = ops.stale_flags(self.settings, self._code_root(args) or ".")
        if not result["stale"]:
            return "No stale experiment flags found.", result
        lines = []
        for item in result["stale"]:
            lines.append("'%s' (experiment '%s' ended %s):" % (item["flag"], item["experiment"], item["ended_at"]))
            lines += ["  %s:%d  %s" % (r["file"], r["line"], r["text"]) for r in item["references"]]
        return "\n".join(lines), result

    # ------------------------------------------------------------- protocol

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Answer one JSON-RPC message. Returns ``None`` for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
        method = message["method"]
        params = message.get("params")
        is_notification = "id" not in message
        request_id = message.get("id")
        if is_notification:
            return None
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid request: method must be a string")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params: expected an object")
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return _result(request_id, {
                "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": "flagsmith-referee", "title": "Referee for Flagsmith experiments", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})
        if method == "prompts/list":
            return _result(request_id, {"prompts": PROMPTS})
        if method == "prompts/get":
            if params.get("name") != "run_experiment":
                return _error(request_id, -32602, "Unknown prompt")
            arguments = params.get("arguments")
            idea = str((arguments.get("idea") if isinstance(arguments, dict) else None) or "(describe the test)")[:2000]
            return _result(request_id, {
                "description": PROMPTS[0]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": _prompt_text(idea)}}],
            })
        if method in ("resources/list", "resources/templates/list"):
            return _result(request_id, {"resources": []} if method == "resources/list" else {"resourceTemplates": []})
        if method == "tools/call":
            arguments = params.get("arguments")
            return _result(request_id, self.call_tool(params.get("name"), {} if arguments is None else arguments))
        return _error(request_id, -32601, "Method not found: %s" % method)

    def call_tool(self, name: Any, arguments: Any) -> Dict[str, Any]:
        handler = self.handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return _tool_error("There is no tool called '%s'." % str(name)[:80])
        if not isinstance(arguments, dict):
            return _tool_error("Tool arguments must be an object.")
        schema = next(t for t in TOOLS if t["name"] == name)["inputSchema"]
        missing = [key for key in schema["required"] if arguments.get(key) in (None, "")]
        if missing:
            return _tool_error("Missing required argument(s): %s." % ", ".join(missing))
        unknown = [key for key in arguments if key not in schema["properties"]]
        if unknown:
            return _tool_error("Unknown argument(s): %s." % ", ".join(unknown))
        try:
            text, data = handler(arguments)
        except (ops.OpError, FlagsmithError, ValueError) as error:
            return _tool_error(str(error))
        except Exception as error:  # a bug here must not take the session down
            return _tool_error("Referee hit an unexpected error: %s: %s" % (type(error).__name__, error))
        structured = data if isinstance(data, dict) else {"result": data}
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": json.loads(json.dumps(structured, default=str)),
            "isError": False,
        }


def _result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _whole_number(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    raise ValueError("probe must be a whole number")


def _safely(server: "Server", message: Any) -> Optional[Dict[str, Any]]:
    """Answer one message; a bug must cost one error reply, not the session."""
    try:
        return server.handle(message)
    except Exception as error:
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, -32603, "Internal error: %s" % type(error).__name__)


def serve(settings: Settings, stdin: Any = None, stdout: Any = None) -> int:
    """Read JSON-RPC messages line by line until stdin closes."""
    # Read bytes and decode leniently: a client sending invalid UTF-8 should
    # get a parse error for that line, not end the session.
    stdin = stdin or getattr(sys.stdin, "buffer", sys.stdin)
    stdout = stdout or sys.stdout
    server = Server(settings)
    for line in stdin:
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                stdout.write(json.dumps(_error(None, -32700, "Parse error: not valid UTF-8")) + "\n")
                stdout.flush()
                continue
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            response: Any = _error(None, -32700, "Parse error")
        else:
            if isinstance(message, list) and not message:
                response = _error(None, -32600, "Invalid request: empty batch")
            elif isinstance(message, list):
                answers = [_safely(server, item) for item in message[:50]]
                response = [a for a in answers if a is not None] or None
            else:
                response = _safely(server, message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0
