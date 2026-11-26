"""Regression tests for defects found in review. Each test names the failure it guards against."""

import copy
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from referee import api, codescan, config, decisions, mcp_server, ops, provision, scorecard, spec, stats, web
from tests import helpers as h
from tests.test_core import FakeFlagsmith, winning_card


def judge(arms, errors=None, plan=True, experiment=None, **kwargs):
    return scorecard.judge(
        experiment or h.experiment(**kwargs), h.results(arms, errors), h.exposures(),
        spec.parse(h.PLAN) if plan else None, now=h.NOW,
    )


NEEDED = spec.parse(h.PLAN).sizing()["per_arm"]
WIN = {"control": (NEEDED, int(NEEDED * 0.20)), "three_fields": (NEEDED, int(NEEDED * 0.27))}


class GuardrailTests(unittest.TestCase):
    def test_guardrail_without_flagsmith_inference_still_blocks(self):
        # Control has 2 errors, so Flagsmith reports no inference for the guardrail.
        card = judge(WIN, errors={"control": (NEEDED, 2), "three_fields": (NEEDED, 300)})
        guardrail = card["metrics"][1]["arms"]["three_fields"]
        self.assertIsNone(guardrail["inference"])
        self.assertEqual(guardrail["chance_source"], "difference")
        self.assertEqual(card["verdict"]["call"], "keep_control")
        self.assertNotIn("No guardrail metric got reliably worse", " ".join(card["verdict"]["reasons"]))

    def test_guardrail_that_cannot_be_judged_is_said_so(self):
        card = judge(WIN, errors={"control": (NEEDED, 0), "three_fields": (NEEDED, 3)})
        self.assertEqual(card["verdict"]["call"], "ship_variant")
        text = " ".join(card["verdict"]["reasons"])
        self.assertIn("could not be judged", text)
        self.assertNotIn("No guardrail metric got reliably worse", text)

    def test_guardrail_with_no_events_at_all_does_not_block(self):
        card = judge(WIN, errors={"control": (NEEDED, 0), "three_fields": (NEEDED, 0)})
        self.assertEqual(card["verdict"]["call"], "ship_variant")

    def test_harm_threshold_does_not_weaken_with_more_variants(self):
        experiment = h.experiment(variants=(("b", 33.0), ("c", 33.0)))
        arms = {"control": (2000, 300), "b": (2000, 420), "c": (2000, 305)}
        errors = {"control": (2000, 60), "b": (2000, 84), "c": (2000, 60)}
        card = scorecard.judge(experiment, h.results(arms, errors), h.exposures(), None, now=h.NOW)
        chance = card["metrics"][1]["arms"]["b"]["chance_good"]
        self.assertTrue(0.025 < chance <= 0.05, chance)  # between the old, weaker bar and the fixed one
        self.assertEqual(card["thresholds"]["harm"], 0.05)
        self.assertNotEqual(card["verdict"]["variant_key"], "b")

    def test_only_guardrails_is_not_judged_as_a_win(self):
        experiment = h.experiment()
        experiment["metrics"] = [experiment["metrics"][1]]
        card = scorecard.judge(
            experiment, h.results(WIN, errors={"control": (NEEDED, 200), "three_fields": (NEEDED, 90)}),
            h.exposures(), None, now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "investigate")


class DirectionTests(unittest.TestCase):
    def results_with_raw_chance(self, arms, errors):
        """Flagsmith's payload when the guardrail metric is marked 'higher is better'."""
        data = h.results(arms, errors)
        control = errors["control"]
        data["payload"]["metrics"][1]["inference"] = {
            key: h.inference(control[0], control[1], n, s, lower_is_better=False)
            for key, (n, s) in errors.items() if key != "control"
        }
        return data

    def test_guardrail_on_an_up_metric_is_read_the_right_way_round(self):
        experiment = h.experiment()
        experiment["metrics"][1]["direction"] = "up"  # created outside Referee
        errors = {"control": (NEEDED, 40), "three_fields": (NEEDED, 300)}
        card = scorecard.judge(experiment, self.results_with_raw_chance(WIN, errors), h.exposures(), spec.parse(h.PLAN), now=h.NOW)
        raw = card["metrics"][1]["arms"]["three_fields"]["inference"]["chance_to_win"]
        self.assertGreater(raw, 0.99)  # Flagsmith: "it went up", which here is bad
        self.assertLess(card["metrics"][1]["arms"]["three_fields"]["chance_good"], 0.01)
        self.assertEqual(card["verdict"]["call"], "keep_control")
        self.assertIn("direction:102", [c["id"] for c in card["checks"]])

    def test_decrease_goal_on_an_up_metric_does_not_ship_a_rise(self):
        experiment = h.experiment()
        experiment["metrics"][0]["expected_direction"] = "decrease"  # metric direction stays "up"
        card = scorecard.judge(experiment, h.results(WIN), h.exposures(), None, now=h.NOW)
        self.assertNotEqual(card["verdict"]["call"], "ship_variant")
        self.assertLess(card["metrics"][0]["arms"]["three_fields"]["chance_good"], 0.05)

    def test_decrease_goal_ships_a_real_fall(self):
        experiment = h.experiment()
        experiment["metrics"][0].update(expected_direction="decrease", direction="down")
        arms = {"control": (NEEDED, int(NEEDED * 0.27)), "three_fields": (NEEDED, int(NEEDED * 0.18))}
        data = h.results(arms)
        data["payload"]["metrics"][0]["inference"] = {
            "three_fields": h.inference(NEEDED, arms["control"][1], NEEDED, arms["three_fields"][1], lower_is_better=True)
        }
        card = scorecard.judge(experiment, data, h.exposures(), spec.parse(h.PLAN), now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "ship_variant")


class PeekingTests(unittest.TestCase):
    EARLY = {"control": (120, 20), "three_fields": (120, 36)}

    def test_early_lead_without_a_plan_is_not_called(self):
        card = judge(self.EARLY, plan=False, started_hours_ago=1.0)
        chance = card["metrics"][0]["arms"]["three_fields"]["chance_good"]
        self.assertTrue(0.95 <= chance < 0.999, chance)
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertFalse(card["verdict"]["actionable"])

    def test_lead_without_a_plan_is_called_after_a_week_of_data(self):
        card = scorecard.judge(
            h.experiment(started_hours_ago=8 * 24.0), h.results(self.EARLY, spread_days=7), h.exposures(), None, now=h.NOW,
        )
        self.assertEqual(card["verdict"]["call"], "ship_variant")

    def test_a_week_since_the_start_is_not_a_week_of_data(self):
        # Started eight days ago, paused after an hour: one bucket of data.
        card = judge(self.EARLY, plan=False, started_hours_ago=8 * 24.0)
        self.assertEqual(card["verdict"]["call"], "keep_running")

    def test_plan_without_sizing_behaves_like_no_plan(self):
        data = dict(h.PLAN, assumptions={})
        card = scorecard.judge(h.experiment(started_hours_ago=1.0), h.results(self.EARLY), h.exposures(), spec.parse(data), now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "keep_running")


class FakeSettings:
    """Settings whose client is a test double, so ops can run offline."""

    environment_key = "env"

    def __init__(self, folder, client):
        self.state_dir = Path(folder)
        self._client = client

    def missing(self):
        return []

    def client(self):
        return self._client


class PlanFreezeTests(unittest.TestCase):
    def test_reapplying_cannot_shrink_the_planned_sample_of_a_started_experiment(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeFlagsmith()
            settings = FakeSettings(folder, client)
            first = ops.apply_plan(settings, h.PLAN)
            client.experiment_rows[0]["status"] = "running"
            easier = copy.deepcopy(h.PLAN)
            easier["assumptions"]["minimum_lift"] = 3.9
            again = ops.apply_plan(settings, easier)
            stored = provision.recall_plan(Path(folder), 7, "env")
            self.assertEqual(stored.assumptions.minimum_lift, 0.25)
            self.assertEqual(again["sizing"]["per_arm"], first["sizing"]["per_arm"])
            self.assertIn("kept", " ".join(again["actions"]))

    def test_plan_can_still_be_revised_before_the_start(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = FakeSettings(folder, FakeFlagsmith())
            ops.apply_plan(settings, h.PLAN)
            revised = copy.deepcopy(h.PLAN)
            revised["assumptions"]["minimum_lift"] = 0.5
            ops.apply_plan(settings, revised)
            self.assertEqual(provision.recall_plan(Path(folder), 7, "env").assumptions.minimum_lift, 0.5)

    def test_apply_notices_an_experiment_that_differs_from_the_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = FakeSettings(folder, FakeFlagsmith())
            ops.apply_plan(settings, h.PLAN)
            changed = copy.deepcopy(h.PLAN)
            changed["variants"][0]["share"] = 10
            changed["metrics"].append({"name": "Extra", "event": "extra", "goal": "not_decrease"})
            with self.assertRaises(ops.OpError) as caught:
                ops.apply_plan(settings, changed)
            self.assertIn("gets 50%; the plan says 10%", str(caught.exception))
            self.assertIn("Extra", str(caught.exception))

    def test_bad_sizing_stops_before_anything_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeFlagsmith()
            plan = copy.deepcopy(h.PLAN)
            plan["metrics"][0]["goal"] = "decrease"
            plan["assumptions"].update(baseline_rate=0.01, minimum_lift=1.5)
            with self.assertRaises(ops.OpError):
                ops.apply_plan(FakeSettings(folder, client), plan)
            self.assertEqual(client.calls, [])
            self.assertFalse(ops.check_plan(plan)["valid"])

    def test_large_decrease_is_a_valid_plan(self):
        plan = copy.deepcopy(h.PLAN)
        plan["metrics"][0]["goal"] = "decrease"
        plan["assumptions"].update(baseline_rate=0.6, minimum_lift=0.7)
        result = ops.check_plan(plan)
        self.assertTrue(result["valid"], result)
        self.assertGreater(result["sizing"]["per_arm"], 0)


class CompletedTests(unittest.TestCase):
    def test_variant_decision_without_an_id_is_not_reported_as_control(self):
        experiment = h.experiment(status="completed", rollout_decision="variant", rollout_variant=None)
        card = judge(WIN, experiment=experiment)
        self.assertEqual(card["verdict"]["headline"], "Completed: a variant was rolled out.")
        self.assertIsNone(card["verdict"]["variant_key"])

    def test_decisions_are_named_correctly(self):
        for decision, expected in (("control", "control was kept"), ("disable", "disabled"), (None, "Completed.")):
            card = judge(WIN, experiment=h.experiment(status="completed", rollout_decision=decision))
            self.assertIn(expected, card["verdict"]["headline"])

    def test_completed_without_rollout_or_plan_raises_no_false_alarm(self):
        # Flagsmith drops the rollout object on completion.
        experiment = h.experiment(status="completed", rollout_decision="variant", rollout_variant=10, experiment_rollout=None)
        card = judge(WIN, experiment=experiment, plan=False)
        self.assertEqual([c for c in card["checks"] if c["status"] == "fail"], [])
        self.assertEqual(card["verdict"]["variant_key"], "three_fields")

    def test_silent_check_works_when_results_were_never_computed(self):
        card = scorecard.judge(
            h.experiment(started_hours_ago=2.0), {}, {}, spec.parse(h.PLAN), now=h.NOW,
            warehouse={"total_events_received": 0},
        )
        self.assertEqual(card["verdict"]["call"], "investigate")


class ApprovalTests(unittest.TestCase):
    def test_variant_id_must_match_live_data(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            human = FakeFlagsmith()
            decision = ledger.propose(winning_card(), "env")
            stored = ledger.get(decision["id"])
            stored["variant_id"] = 999  # the decision file was edited
            ledger._save(stored)
            with self.assertRaises(decisions.DecisionError):
                ledger.approve(decision["id"], human, lambda _id: winning_card())
            self.assertIsNone(human.completed)

    def test_refused_approvals_are_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            agent = FakeFlagsmith()
            agent.user = None
            decision = ledger.propose(winning_card(), "env")
            with self.assertRaises(decisions.DecisionError):
                ledger.approve(decision["id"], agent, lambda _id: winning_card())
            self.assertIn("approval_refused", [e["event"] for e in ledger.entries()])
            self.assertTrue(ledger.verify()["ok"])

    def test_damaged_ledger_line_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            ledger.propose(winning_card(), "env")
            with open(str(ledger.ledger_path), "a") as handle:
                handle.write("{not json\n")
            self.assertFalse(ledger.verify()["ok"])
            ledger.propose(winning_card(), "env")  # must not raise

    def test_expired_proposal_is_listed_as_expired(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = decisions.Ledger(Path(folder))
            decision = ledger.propose(winning_card(), "env")
            stored = ledger.get(decision["id"])
            stored["expires_at"] = (h.NOW - timedelta(days=400)).isoformat()
            ledger._save(stored)
            self.assertEqual(decisions.summary(ledger.get(decision["id"]))["status"], "expired")


class McpRobustnessTests(unittest.TestCase):
    def test_malformed_messages_get_an_error_and_the_session_survives(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": "oops"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": ["x"]}},
            {"jsonrpc": "2.0", "id": 4, "method": "prompts/get", "params": {"name": "run_experiment", "arguments": ["x"]}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "check_plan", "arguments": "x"}},
            {"jsonrpc": "2.0", "id": 6, "method": 7},
            [],
            {"jsonrpc": "2.0", "id": 8, "method": "ping"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            mcp_server.serve(
                config.Settings(state_dir=Path(folder)),
                io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n"), out,
            )
        answers = {a["id"]: a for a in (json.loads(line) for line in out.getvalue().splitlines())}
        self.assertEqual(answers[1]["error"]["code"], -32602)
        self.assertEqual(answers[2]["error"]["code"], -32602)
        self.assertTrue(answers[3]["result"]["isError"])
        self.assertIn("messages", answers[4]["result"])
        self.assertTrue(answers[5]["result"]["isError"])
        self.assertEqual(answers[6]["error"]["code"], -32600)
        self.assertEqual(answers[8]["result"], {})  # still alive at the end


class _Page(BaseHTTPRequestHandler):
    body = b"<html>Welcome</html>"
    content_type = "text/html"

    def _answer(self):
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


def serve_in_thread(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ApiRobustnessTests(unittest.TestCase):
    def test_a_web_page_instead_of_the_api_is_explained(self):
        server = serve_in_thread(_Page)
        try:
            settings = config.Settings(api_url="http://127.0.0.1:%d/api/v1" % server.server_port, api_key="a.b", environment_key="e")
            with self.assertRaises(api.FlagsmithError) as caught:
                settings.client().environment()
            self.assertIn("not JSON", str(caught.exception))
            result = ops.doctor(settings)
            self.assertFalse(result["ok"])
            self.assertTrue(result["problems"])
        finally:
            server.shutdown()
            server.server_close()

    def test_login_without_a_session_key_is_explained(self):
        class TwoFactor(_Page):
            body = b'{"ephemeral_token": "abc", "method": "app"}'
            content_type = "application/json"

        server = serve_in_thread(TwoFactor)
        try:
            client = api.Flagsmith(api_url="http://127.0.0.1:%d/api/v1" % server.server_port, api_key="a.b")
            with self.assertRaises(api.FlagsmithError) as caught:
                client.login("a@example.com", "pw")
            self.assertIn("two-factor", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.app = web.App(config.Settings(state_dir=Path(self.folder.name)))
        self.server = serve_in_thread(web.make_handler(self.app))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        text = response.read().decode()
        connection.close()
        return response.status, text, response

    def test_requests_for_another_host_are_refused(self):
        status, text, _ = self.request("GET", "/", {"Host": "evil.example:8787"})
        self.assertEqual(status, 403)
        self.assertNotIn("form_token", text)
        self.assertEqual(self.request("GET", "/healthz", {"Host": "localhost:8787"})[0], 200)
        self.assertEqual(self.request("GET", "/healthz", {})[0], 403)

    def test_cross_site_posts_are_refused(self):
        body = ("form_token=%s&by=x" % self.app.form_token).encode()
        headers = {"Host": "127.0.0.1", "Origin": "https://evil.example", "Content-Length": str(len(body))}
        self.assertEqual(self.request("POST", "/decision/D-1/reject", headers, body)[0], 403)

    def test_bad_content_length_does_not_hang_or_crash(self):
        for value in ("-1", "abc"):
            status, _, _ = self.request("POST", "/decision/D-1/reject", {"Host": "127.0.0.1", "Content-Length": value})
            self.assertEqual(status, 403)  # no form token read, so refused

    def test_headers(self):
        _, _, response = self.request("GET", "/healthz", {"Host": "127.0.0.1"})
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_scorecard_html_escapes_everything(self):
        card = winning_card()
        nasty = '"><script>alert(1)</script>'
        card["experiment"].update(name=nasty, hypothesis=nasty, flag=nasty)
        card["arms"][1].update(key=nasty, value=nasty)
        card["metrics"][0]["name"] = nasty
        card["verdict"]["reasons"].append(nasty)
        card["checks"].append({"id": "x", "status": "fail", "title": nasty, "detail": nasty})
        self.assertNotIn("<script>", web.scorecard_html(card))


class ScanTests(unittest.TestCase):
    def test_symlinks_and_special_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            Path(outside, "secret.py").write_text('x = flags.get_feature_value("signup_form")\n')
            os.symlink(str(Path(outside, "secret.py")), str(Path(folder, "link.py")))
            os.symlink(folder, str(Path(folder, "loop")))
            if hasattr(os, "mkfifo"):
                os.mkfifo(str(Path(folder, "pipe.py")))
            Path(folder, "real.py").write_text("print('hi')\n")
            found = codescan.scan(Path(folder), "signup_form", [])
            self.assertEqual(found.files_scanned, 1)
            self.assertEqual(found.references, [])

    def test_value_on_the_same_line_as_the_flag_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "a.py").write_text('short = fs.get_experiment_flag("signup_form", identifier=u).value == "three_fields"\n')
            found = codescan.scan(Path(folder), "signup_form", ["three_fields"])
            self.assertIn("three_fields", found.values_seen)

    def test_chance_higher(self):
        up = stats.chance_higher(stats.Moments(2000, 2, 2), stats.Moments(2000, 300, 300))
        self.assertGreater(up, 0.999)
        self.assertEqual(stats.chance_higher(stats.Moments(100, 0, 0), stats.Moments(100, 0, 0)), 0.5)
        self.assertIsNone(stats.chance_higher(None, stats.Moments(100, 1, 1)))


if __name__ == "__main__":
    unittest.main()
