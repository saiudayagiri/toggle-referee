"""Regression tests for the second verification round."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from referee import codescan, config, decisions, mcp_server, ops, preflight, provision, scorecard, spec, web
from tests import helpers as h
from tests.test_core import FakeFlagsmith, winning_card
from tests.test_preflight import Fake
from tests.test_review import FakeSettings

EARLY = {"control": (300, 60), "three_fields": (300, 84)}


class DeadlineTests(unittest.TestCase):
    def plan(self, **assumptions):
        data = copy.deepcopy(h.PLAN)
        data["assumptions"] = assumptions
        return spec.parse(data)

    def test_passed_deadline_does_not_ship_an_early_lead(self):
        plan = self.plan(baseline_rate=0.2, minimum_lift=0.05, daily_identities=1e9, deadline="2020-01-01T00:00:00Z")
        card = scorecard.judge(h.experiment(), h.results(EARLY), h.exposures(), plan, now=h.NOW)
        self.assertLess(card["progress"]["fraction"], 0.1)
        self.assertEqual(card["verdict"]["call"], "keep_running")

    def test_passed_deadline_without_sizing_does_not_ship_either(self):
        card = scorecard.judge(h.experiment(), h.results(EARLY), h.exposures(), self.plan(deadline="2020-01-01T00:00:00Z"), now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "keep_running")

    def test_passed_deadline_without_a_winner_is_not_called_for_control(self):
        plan = self.plan(baseline_rate=0.2, minimum_lift=0.05, daily_identities=1e9, deadline="2020-01-01T00:00:00Z")
        flat = {"control": (300, 60), "three_fields": (300, 62)}
        card = scorecard.judge(h.experiment(), h.results(flat), h.exposures(), plan, now=h.NOW)
        self.assertEqual(card["verdict"]["call"], "keep_running")
        self.assertIn("deadline has passed", " ".join(card["verdict"]["reasons"]))


class StatusQuoTests(unittest.TestCase):
    def client_serving(self, base):
        client = FakeFlagsmith()
        client.features_by_name["signup_form"] = {"id": 3, "name": "signup_form", "type": "MULTIVARIATE", "initial_value": base}
        return client

    def test_control_must_be_what_users_get_today(self):
        # The flag serves six_fields; the plan calls the new form "control".
        swapped = copy.deepcopy(h.PLAN)
        swapped["control"] = {"value": "three_fields"}
        swapped["variants"] = [{"key": "legacy", "value": "six_fields", "share": 10}]
        client = self.client_serving("six_fields")
        with self.assertRaises(provision.ProvisionError) as caught:
            provision.apply(client, spec.parse(swapped))
        self.assertIn("Control has to be what users get now", str(caught.exception))
        self.assertEqual(client.calls, [])  # nothing was created

    def test_flag_already_serving_a_variant_by_default_is_refused(self):
        client = self.client_serving("six_fields")
        client.variant_rows[3] = [{"id": 10, "key": "three_fields", "string_value": "three_fields", "type": "unicode"}]
        client.default_shares = [(10, 100.0)]
        with self.assertRaises(provision.ProvisionError) as caught:
            provision.apply(client, spec.parse(h.PLAN))
        self.assertIn("currently serves 'three_fields' (100%)", str(caught.exception))
        self.assertEqual(client.calls, [])

    def test_matching_control_is_accepted(self):
        self.assertIsNotNone(provision.apply(self.client_serving("six_fields"), spec.parse(h.PLAN)).experiment_id)

    def test_preflight_fails_an_outside_experiment_whose_control_is_new(self):
        client = Fake(h.experiment(status="created"))
        client.environment_default = lambda _id: {"feature_state_value": "something_else"}
        result = preflight.run(client, 7)
        self.assertIn("control_baseline", [c["id"] for c in result["checks"] if c["status"] == "fail"])
        client.environment_default = lambda _id: {"feature_state_value": "six_fields"}
        self.assertTrue(preflight.run(client, 7)["ready"])


class ApplyOrderTests(unittest.TestCase):
    def test_mismatching_apply_creates_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeFlagsmith()
            settings = FakeSettings(folder, client)
            ops.apply_plan(settings, h.PLAN)
            before = (list(client.calls), len(client.metric_rows), len(client.variant_rows[3]))
            changed = copy.deepcopy(h.PLAN)
            changed["variants"].append({"key": "third", "value": "third", "share": 10})
            changed["metrics"][1]["goal"] = "not_decrease"
            changed["metrics"].append({"name": "Extra", "event": "extra", "goal": "not_decrease"})
            with self.assertRaises(ops.OpError) as caught:
                ops.apply_plan(settings, changed)
            self.assertEqual((list(client.calls), len(client.metric_rows), len(client.variant_rows[3])), before)
            text = str(caught.exception)
            self.assertIn("Variant 'third' is in the plan but not on the flag", text)
            self.assertIn("is expected to 'not increase' there; the plan says 'not decrease'", text)
            self.assertIn("Extra", text)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = decisions.Ledger(Path(self.folder.name))
        self.human = FakeFlagsmith()

    def tearDown(self):
        self.folder.cleanup()

    def test_results_dated_in_the_future_are_refused(self):
        decision = self.ledger.propose(winning_card(), "env")
        future = winning_card()
        future["freshness"]["age_minutes"] = -500.0
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: future)
        self.assertIsNone(self.human.completed)

    def test_refusals_on_closed_decisions_are_recorded(self):
        decision = self.ledger.propose(winning_card(), "env")
        self.ledger.reject(decision["id"], "sai")
        count = len(self.ledger.entries())
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertEqual(len(self.ledger.entries()), count + 1)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_key_refusal_message_has_no_html(self):
        decision = self.ledger.propose(winning_card(), "env")
        self.human.user = None
        with self.assertRaises(decisions.DecisionError) as caught:
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertNotIn("<", str(caught.exception))


class InputTests(unittest.TestCase):
    def test_mcp_survives_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            data = b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xff\xfe"}\n{"jsonrpc":"2.0","id":"alive","method":"ping"}\n'
            mcp_server.serve(config.Settings(state_dir=Path(folder)), io.BytesIO(data), out)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(answers[0]["error"]["code"], -32700)
        self.assertEqual(answers[1], {"jsonrpc": "2.0", "id": "alive", "result": {}})

    def test_non_ascii_digits_are_not_ids(self):
        self.assertFalse(web._is_id("\u00b2"))
        self.assertTrue(web._is_id("12"))

        class Client:
            def experiments(self):
                return []

        with self.assertRaises(ops.OpError):
            ops.resolve_experiment(Client(), "\u00b2")

    def test_host_header_must_be_a_plain_local_name(self):
        app = web.App(config.Settings())
        for good in ("localhost", "localhost:8787", "127.0.0.1:1", "[::1]:8787"):
            self.assertTrue(app.host_allowed(good), good)
        for bad in ("localhost:80@evil.com", "evil.com", "localhost.evil.com", "127.0.0.1.nip.io", "", None, "localhost:80/x"):
            self.assertFalse(app.host_allowed(bad), bad)


class ScannerTests(unittest.TestCase):
    def scan(self, name, source, flag="f"):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, name).write_text(source)
            return codescan.scan(Path(folder), flag, [])

    def test_comments_of_every_kind_are_ignored(self):
        found = self.scan("a.py", (
            'x = 1  # fs.track_event("trailing")\n'
            '"""doc\nfs.get_experiment_flag("f"); fs.track_event("docstring")\n"""\n'
            'url = "http://x#y"; fs.track_event("real", identifier=u)\n'
        ))
        self.assertEqual(sorted(found.tracked_events), ["real"])
        self.assertEqual(found.exposing, [])
        found = self.scan("b.js", '/*\n fs.getExperimentFlag("f"); fs.trackEvent("block")\n*/\nconst u = "http://a//b"; fs.trackEvent("real")\n')
        self.assertEqual(sorted(found.tracked_events), ["real"])
        self.assertEqual(found.exposing, [])
        found = self.scan("c.html", '<!--\n<script>fs.trackEvent("hidden")</script>\n-->\n<script>fs.trackEvent("real")</script>\n')
        self.assertEqual(sorted(found.tracked_events), ["real"])

    def test_real_calls_in_awkward_shapes_are_found(self):
        found = self.scan("m.go", 'c.TrackEvent(r.Context(), "one", nil)\nc.TrackEvent(context.Background(), "two", nil)\n')
        self.assertEqual(sorted(found.tracked_events), ["one", "two"])
        found = self.scan("p.ts", 'flagsmith.trackEvent(\n  "three",\n  { value: 1 },\n);\n')
        self.assertEqual(found.tracked_events["three"][0].line, 1)

    def test_strip_comments_keeps_line_count_and_strings(self):
        source = 'a = "# not a comment"  # comment\nb = 2\n'
        stripped = codescan.strip_comments(source, ".py")
        self.assertEqual(len(stripped), len(source))
        self.assertIn('"# not a comment"', stripped)
        self.assertNotIn("# comment", stripped)


if __name__ == "__main__":
    unittest.main()
