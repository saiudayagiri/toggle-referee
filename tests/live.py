#!/usr/bin/env python3
"""Run real experiments against a live Flagsmith and check Referee end to end.

Needs the demo stack (./demo/up.sh) or any Flagsmith that the settings in
flagsmith.env point at, plus the login of a person who may approve:

    python3 tests/live.py
    REFEREE_APPROVER_EMAIL=... REFEREE_APPROVER_PASSWORD=... python3 tests/live.py

Each run creates its own flags (named live_<random>_...), so it can be repeated.
Events are sent straight to Flagsmith's SDK events endpoint, the same one the
SDKs use, which keeps the scenarios exact and fast.

Scenarios:
  winner        a variant clearly wins        -> ship_variant, approved, flag serves it
  no_difference planned sample, no difference -> keep_control, approved, flag serves control
  broken_split  50/50 plan delivered 60/40    -> investigate, proposal refused
  wrong_key     exposures sent under the value, not the variant key -> investigate
  misspelt      the code sends ..._Done, the metric counts ..._done -> preflight fails, start refused
"""

import os
import random
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from referee import config, decisions, ops, spec  # noqa: E402
from referee.api import now_ms  # noqa: E402

RUN = "live_" + secrets.token_hex(3)
FAILURES = []


def check(label, condition, detail=""):
    print("  %s %s%s" % ("ok  " if condition else "FAIL", label, (" -- " + str(detail)) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def plan(name, **overrides):
    data = {
        "name": "%s %s" % (RUN, name),
        "hypothesis": "Live test scenario '%s'." % name,
        "flag": "%s_%s" % (RUN, name),
        "control": {"value": "old"},
        "variants": [{"key": "new", "value": "new_value", "share": 50}],
        "metrics": [{"name": "%s %s done" % (RUN, name), "event": "%s_%s_done" % (RUN, name), "goal": "increase"}],
        "assumptions": {"baseline_rate": 0.2, "minimum_lift": 0.3, "daily_identities": 5000},
    }
    data.update(overrides)
    return data


def send(client, flag, event, arms, exposure_value=None):
    """``arms`` maps a variant key to (identities, conversions)."""
    now = now_ms()
    events = []
    for key, (identities, conversions) in arms.items():
        for index in range(identities):
            who = "%s-%s-%s-%d" % (RUN, flag, key, index)
            events.append({
                "event": "$flag_exposure", "feature_name": flag, "identifier": who,
                "timestamp": now, "value": (exposure_value or {}).get(key, key),
            })
            if index < conversions:
                events.append({"event": event, "identifier": who, "timestamp": now + 1000})
    random.shuffle(events)
    accepted = 0
    for start in range(0, len(events), 2000):
        answer = client.send_events(events[start:start + 2000])
        accepted += answer.get("accepted", 0)
        if answer.get("rejected"):
            raise SystemExit("Flagsmith rejected events: %s" % answer["rejected"][:3])
    return accepted


def values_served(client, flag, count=40, prefix="probe"):
    """What Flagsmith serves for this flag to a batch of identities, as a set of values."""
    values = set()
    for index in range(count):
        flags = client.identity_flags("%s-%s-%s-%d" % (RUN, flag, prefix, index))["flags"]
        values.add(next(f for f in flags if f["feature"]["name"] == flag)["feature_state_value"])
    return values


def set_up(settings, data):
    applied = ops.apply_plan(settings, data)
    experiment_id = applied["experiment_id"]
    started = ops.start(settings, experiment_id)
    if not started.get("started"):
        raise SystemExit("Could not start %s: %s" % (data["name"], started.get("reason")))
    time.sleep(1.5)  # events must be stamped after the experiment's start
    return experiment_id


def scorecard(settings, experiment_id):
    time.sleep(2)
    card = ops.get_scorecard(settings, experiment_id)
    wanted = sum(1 for arm in card["arms"] if arm["identities"])
    if not wanted:  # the first compute can race the insert; one retry after the task runs
        time.sleep(6)
        card = ops.get_scorecard(settings, experiment_id, refresh=False)
    return card


def clean_up(client):
    """Delete the flags (and with them the experiments) and metrics this run made."""
    removed = 0
    try:
        for feature in client.features():
            if feature["name"].startswith(RUN + "_"):
                client.request("DELETE", "/projects/%d/features/%d/" % (client.project_id, feature["id"]))
                removed += 1
        for metric in client.metrics():
            if metric["name"].startswith(RUN + " "):
                client.request("DELETE", client._env("experiment-metrics/%d/" % metric["id"]))
    except Exception as error:  # cleaning up must not hide the test result
        print("  (could not remove everything: %s)" % error)
    return removed


def main():
    base = config.load(Path(__file__).resolve().parent.parent)
    if base.missing():
        raise SystemExit("Set up Flagsmith first (./demo/up.sh), or provide %s." % ", ".join(base.missing()))
    email = os.environ.get("REFEREE_APPROVER_EMAIL", "referee@example.com")
    password = os.environ.get("REFEREE_APPROVER_PASSWORD", "Referee-local-2026!")
    with tempfile.TemporaryDirectory() as folder:
        settings = config.Settings(
            api_url=base.api_url, api_key=base.api_key, environment_key=base.environment_key,
            project_id=base.project_id, state_dir=Path(folder),
        )
        client = settings.client()
        approver_token = client.login(email, password)
        needed = spec.parse(plan("x")).sizing()["per_arm"]
        print("Run %s against %s (planned sample %d per version)" % (RUN, settings.api_url, needed))

        print("serving")
        data = plan("serving")
        applied = ops.apply_plan(settings, data)
        experiment_id = applied["experiment_id"]
        check("after apply nobody is served the variant", values_served(client, data["flag"]) == {"old"}, values_served(client, data["flag"]))
        check("the planned rollout is recorded with the experiment",
              ops.planned_rollout(client.experiment(experiment_id)) == 100.0 and ops.live_rollout(client.experiment(experiment_id)) == 0)
        ops.start(settings, experiment_id)
        before = {i: client.identity_flags("%s-keep-%d" % (RUN, i))["flags"] for i in range(20)}
        check("after start both versions are served", values_served(client, data["flag"]) == {"old", "new_value"}, values_served(client, data["flag"]))
        paused = ops.pause(settings, experiment_id)
        check("after pause nobody is served the variant", not paused["serving"] and values_served(client, data["flag"]) == {"old"}, paused)
        ops.start(settings, experiment_id)
        after = {i: client.identity_flags("%s-keep-%d" % (RUN, i))["flags"] for i in range(20)}
        same = all(
            [f["feature_state_value"] for f in before[i] if f["feature"]["name"] == data["flag"]]
            == [f["feature_state_value"] for f in after[i] if f["feature"]["name"] == data["flag"]]
            for i in range(20)
        )
        check("after resuming, the same people get the same versions", same)
        ops.pause(settings, experiment_id)

        print("winner")
        data = plan("winner")
        RUN_FIRST = RUN
        experiment_id = set_up(settings, data)
        send(client, data["flag"], data["metrics"][0]["event"],
             {"control": (needed + 20, int(needed * 0.2)), "new": (needed + 10, int(needed * 0.3))})
        card = scorecard(settings, experiment_id)
        check("call is ship_variant", card["verdict"]["call"] == "ship_variant", card["verdict"])
        check("sample ratio checked and clean", card["sample_ratio"]["checked"] and not card["sample_ratio"]["alarm"])
        check("Flagsmith's own srm value is absent (why Referee computes one)", card["sample_ratio"]["flagsmith_p_value"] is None)
        decision = ops.propose(settings, experiment_id, proposed_by="live test")
        check("proposal is pending", decision["status"] == "pending")
        check("experiment still running after proposal", client.experiment(experiment_id)["status"] == "running")
        try:
            ops.approve(settings, decision["id"], settings.api_key)
            check("agent API key cannot approve", False)
        except ops.OpError:
            check("agent API key cannot approve", True)
        check("still running after the refused approval", client.experiment(experiment_id)["status"] == "running")
        applied = ops.approve(settings, decision["id"], approver_token, note="live test")
        check("approved by the person", applied["approval"]["by"] == email, applied.get("approval"))
        finished = client.experiment(experiment_id)
        check("Flagsmith completed the experiment with the variant", finished["status"] == "completed" and finished["rollout_decision"] == "variant")
        flags = client.identity_flags("%s-someone-new" % RUN)["flags"]
        served = next(f for f in flags if f["feature"]["name"] == data["flag"])
        check("a new identity is now served the winner", served["feature_state_value"] == "new_value", served["feature_state_value"])
        check("completed scorecard is not actionable", not ops.get_scorecard(settings, experiment_id)["verdict"]["actionable"])

        print("no_difference")
        data = plan("nodiff")
        experiment_id = set_up(settings, data)
        send(client, data["flag"], data["metrics"][0]["event"],
             {"control": (needed + 5, int(needed * 0.2)), "new": (needed + 15, int(needed * 0.2) + 3)})
        card = scorecard(settings, experiment_id)
        check("call is keep_control", card["verdict"]["call"] == "keep_control", card["verdict"])
        decision = ops.propose(settings, experiment_id, proposed_by="live test")
        ops.approve(settings, decision["id"], approver_token)
        finished = client.experiment(experiment_id)
        check("Flagsmith completed the experiment with control", finished["rollout_decision"] == "control", finished["rollout_decision"])
        flags = client.identity_flags("%s-someone-else" % RUN)["flags"]
        served = next(f for f in flags if f["feature"]["name"] == data["flag"])
        check("a new identity is served control", served["feature_state_value"] == "old", served["feature_state_value"])

        print("broken_split")
        data = plan("srm")
        experiment_id = set_up(settings, data)
        send(client, data["flag"], data["metrics"][0]["event"], {"control": (900, 180), "new": (600, 190)})
        card = scorecard(settings, experiment_id)
        check("sample ratio alarm", card["sample_ratio"]["alarm"], card["sample_ratio"])
        check("call is investigate", card["verdict"]["call"] == "investigate", card["verdict"]["call"])
        try:
            ops.propose(settings, experiment_id)
            check("proposal refused", False)
        except ops.OpError:
            check("proposal refused", True)
        ops.pause(settings, experiment_id)
        check("paused", client.experiment(experiment_id)["status"] == "paused")

        print("wrong_key")
        data = plan("wrongkey")
        experiment_id = set_up(settings, data)
        send(client, data["flag"], data["metrics"][0]["event"], {"control": (300, 60), "new": (300, 90)},
             exposure_value={"new": "new_value"})
        card = scorecard(settings, experiment_id)
        check("stray version detected", "unexpected_variant" in [c["id"] for c in card["checks"]], [c["id"] for c in card["checks"]])
        check("call is investigate", card["verdict"]["call"] == "investigate", card["verdict"]["call"])
        ops.pause(settings, experiment_id)

        print("misspelt")
        data = plan("misspelt")
        applied = ops.apply_plan(settings, data)
        event = data["metrics"][0]["event"]
        wrong = event.replace("_done", "_Done")
        with tempfile.TemporaryDirectory() as code:
            Path(code, "shop.py").write_text(
                'flag = flagsmith.get_experiment_flag("%s", identifier=user)\n'
                'if flag.value == "new_value":\n    show_new()\n'
                'flagsmith.track_event("%s", identifier=user)\n' % (data["flag"], wrong)
            )
            result = ops.run_preflight(settings, applied["experiment_id"], code)
            failed = [c for c in result["checks"] if c["status"] == "fail"]
            check("preflight fails on the event name", not result["ready"] and any(wrong in c["detail"] for c in failed), failed)
            refused = ops.start(settings, applied["experiment_id"], code)
            check("start is refused", not refused["started"])
            check("experiment was not started", client.experiment(applied["experiment_id"])["status"] == "created")
            Path(code, "shop.py").write_text(Path(code, "shop.py").read_text().replace(wrong, event))
            check("preflight passes once the name is fixed", ops.run_preflight(settings, applied["experiment_id"], code)["ready"])
        client.delete_experiment(applied["experiment_id"])

        # The winner's flag now serves the variant to everyone, so "control"
        # there is no longer what users get. Setting up the same test again
        # must be refused before anything is created.
        rerun = plan("winner")
        rerun["name"] = "%s winner again" % RUN
        rerun["flag"] = "%s_winner" % RUN_FIRST
        try:
            ops.apply_plan(settings, rerun)
            check("re-running on a flag that already serves the winner is refused", False)
        except ops.OpError as error:
            check("re-running on a flag that already serves the winner is refused", "currently serves" in str(error), error)
        check("the refusal created no experiment", not any(e["name"] == rerun["name"] for e in client.experiments()))

        swapped = plan("swap")
        swapped["control"], swapped["variants"] = {"value": "new_value"}, [{"key": "legacy", "value": "old", "share": 10}]
        ops.apply_plan(settings, plan("swap"), dry_run=False)  # the flag now exists, serving "old"
        client.delete_experiment(next(e["id"] for e in client.experiments() if e["name"] == plan("swap")["name"]))
        try:
            ops.apply_plan(settings, swapped)
            check("a plan that calls the new value 'control' is refused", False)
        except ops.OpError as error:
            check("a plan that calls the new value 'control' is refused", "Control has to be what users get now" in str(error), error)

        print("paused")
        data = plan("paused")
        experiment_id = set_up(settings, data)
        send(client, data["flag"], data["metrics"][0]["event"],
             {"control": (needed + 20, int(needed * 0.2)), "new": (needed + 10, int(needed * 0.3))})
        card = scorecard(settings, experiment_id)
        decision = ops.propose(settings, experiment_id, proposed_by="live test")
        ops.pause(settings, experiment_id)
        applied = ops.approve(settings, decision["id"], approver_token)
        check("a paused experiment's decision can still be approved", applied["status"] == "applied", applied.get("status"))
        check("and its winner is then served to everyone", values_served(client, data["flag"], prefix="after") == {"new_value"})

        ledger = decisions.Ledger(settings.state_dir)
        verified = ledger.verify()
        events = [entry["event"] for entry in ledger.entries()]
        check("ledger intact", verified["ok"], verified)
        check(
            "ledger records every proposal, the refused approval and every approval",
            events.count("proposed") == 3 and events.count("approval_refused") == 1 and events.count("approved_and_applied") == 3,
            events,
        )

    removed = clean_up(base.client())
    print("")
    print("Removed the %d flags this run created." % removed)
    if FAILURES:
        print("%d check(s) FAILED: %s" % (len(FAILURES), "; ".join(FAILURES)))
        return 1
    print("All live checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
