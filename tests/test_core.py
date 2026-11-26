import io
import json
import tempfile
import unittest
from pathlib import Path

from referee import api, codescan, config, decisions, mcp_server, ops, provision, scorecard, spec, stats
from referee.api import FlagsmithError
from tests import helpers as h


class StatsTests(unittest.TestCase):
    def test_chi_squared_matches_known_values(self):
        # 3.841 is the 5% point for one degree of freedom, 5.991 for two.
        self.assertAlmostEqual(stats.chi_squared_survival(3.841, 1), 0.05, places=3)
        self.assertAlmostEqual(stats.chi_squared_survival(5.991, 2), 0.05, places=3)
        self.assertAlmostEqual(stats.chi_squared_survival(7.815, 3), 0.05, places=3)
        self.assertEqual(stats.chi_squared_survival(0, 1), 1.0)

    def test_srm(self):
        self.assertGreater(stats.srm_p_value([500, 510], [0.5, 0.5]), 0.5)
        self.assertLess(stats.srm_p_value([1000, 800], [0.5, 0.5]), 0.001)
        self.assertGreater(stats.srm_p_value([800, 200], [0.8, 0.2]), 0.9)
        self.assertIsNone(stats.srm_p_value([0, 0], [0.5, 0.5]))
        self.assertIsNone(stats.srm_p_value([10, 10], [1.0, 0.0]))
        self.assertIsNone(stats.srm_p_value([10], [1.0]))

    def test_sample_size_moves_the_right_way(self):
        base = stats.sample_size_per_arm(0.2, 0.2)
        self.assertGreater(stats.sample_size_per_arm(0.2, 0.1), base)  # smaller effect
        self.assertGreater(stats.sample_size_per_arm(0.05, 0.2), base)  # rarer event
        self.assertGreater(stats.sample_size_per_arm(0.2, 0.2, power=0.9), base)
        self.assertGreater(stats.sample_size_per_arm(0.2, 0.2, comparisons=3), base)
        self.assertGreater(stats.sample_size_per_arm(0.2, -0.2), 0)

    def test_sample_size_rejects_nonsense(self):
        for arguments in ((0, 0.2), (1, 0.2), (0.2, 0), (0.6, 1.0)):
            with self.assertRaises(ValueError):
                stats.sample_size_per_arm(*arguments)

    def test_detectable_lift_inverts_sample_size(self):
        n = stats.sample_size_per_arm(0.15, 0.3)
        self.assertAlmostEqual(stats.detectable_lift(0.15, n), 0.3, places=2)
        self.assertIsNone(stats.detectable_lift(0.15, 1))

    def test_expected_shares(self):
        rollout = {"multivariate_feature_state_values": [
            {"multivariate_feature_option": 1, "percentage_allocation": 30.0},
            {"multivariate_feature_option": 2, "percentage_allocation": 20.0},
        ]}
        self.assertEqual(stats.expected_shares(rollout, {1: "b", 2: "c"}), {"b": 0.3, "c": 0.2, "control": 0.5})


class PlanTests(unittest.TestCase):
    def test_valid_plan(self):
        plan = spec.parse(h.PLAN)
        self.assertEqual(plan.primary.event, "signup_completed")
        self.assertEqual([m.event for m in plan.guardrails], ["signup_error"])
        self.assertEqual(plan.control_share, 50)
        self.assertEqual(plan.guardrails[0].direction, "down")
        self.assertEqual(spec.parse(plan.to_dict()).to_dict(), plan.to_dict())

    def problems(self, **changes):
        data = json.loads(json.dumps(h.PLAN))
        data.update(changes)
        with self.assertRaises(spec.PlanError) as caught:
            spec.parse(data)
        return " ".join(caught.exception.problems)

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(spec.PlanError) as caught:
            spec.parse({"flag": "Bad Name"})
        self.assertGreaterEqual(len(caught.exception.problems), 5)

    def test_rejections(self):
        self.assertIn("lower case", self.problems(flag="SignupForm"))
        self.assertIn("reserves", self.problems(variants=[{"key": "control", "value": "x", "share": 50}]))
        self.assertIn("no traffic for control", self.problems(variants=[{"key": "b", "value": "b", "share": 100}]))
        self.assertIn("used twice", self.problems(variants=[{"key": "b", "value": "b", "share": 10}, {"key": "b", "value": "c", "share": 10}]))
        self.assertIn("identical", self.problems(variants=[{"key": "b", "value": "six_fields", "share": 50}]))
        self.assertIn("guardrail", self.problems(metrics=[{"event": "e", "goal": "not_increase"}]))
        self.assertIn("'$'", self.problems(metrics=[{"event": "$flag_exposure"}]))
        self.assertIn("ISO 8601", self.problems(assumptions={"deadline": "next friday"}))
        self.assertIn("must be a number", self.problems(rollout_percentage="all"))

    def test_sizing_and_deadline(self):
        data = json.loads(json.dumps(h.PLAN))
        data["assumptions"]["deadline"] = "2999-01-01T00:00:00Z"
        sizing = spec.parse(data).sizing()
        self.assertTrue(sizing["finishes_before_deadline"])
        data["assumptions"]["deadline"] = "2000-01-01T00:00:00Z"
        sizing = spec.parse(data).sizing()
        self.assertFalse(sizing["finishes_before_deadline"])
        data["assumptions"] = {}
        self.assertIsNone(spec.parse(data).sizing()["per_arm"])

    def test_small_share_needs_more_total_traffic(self):
        data = json.loads(json.dumps(h.PLAN))
        even = spec.parse(data).sizing()["total_in_experiment"]
        data["variants"][0]["share"] = 10
        self.assertGreater(spec.parse(data).sizing()["total_in_experiment"], even * 3)

    def test_parse_time(self):
        self.assertIsNotNone(spec.parse_time("2026-11-24T00:17:42.829799Z"))
        self.assertIsNotNone(spec.parse_time("2026-11-24T00:17:42.82Z"))
        self.assertIsNotNone(spec.parse_time("2026-11-29T20:00:00+05:30"))
        self.assertIsNone(spec.parse_time("soon"))
        self.assertIsNone(spec.parse_time(None))


APP = '''
from flagsmith import Flagsmith
fs = Flagsmith(environment_key="x")

def page(user):
    flag = fs.get_experiment_flag("signup_form", identifier=user)
    if flag.value == "three_fields":
        return short()
    return long()

def done(user):
    fs.track_event("Signup_Completed", identifier=user)
'''


class CodeScanTests(unittest.TestCase):
    def scan(self, source, name="app.py", values=("three_fields",)):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, name).write_text(source)
            Path(folder, "node_modules").mkdir()
            Path(folder, "node_modules", "x.js").write_text('getValue("signup_form")')
            return codescan.scan(Path(folder), "signup_form", values)

    def test_exposing_read_and_events(self):
        found = self.scan(APP)
        self.assertEqual(len(found.exposing), 1)
        self.assertEqual(found.exposing[0].line, 6)
        self.assertIn("Signup_Completed", found.tracked_events)
        self.assertIn("three_fields", found.values_seen)
        self.assertEqual(found.files_scanned, 1)  # node_modules is skipped

    def test_plain_read_is_told_apart(self):
        found = self.scan('v = flags.get_feature_value("signup_form")\n')
        self.assertEqual([r.kind for r in found.references], ["plain_read"])

    def test_javascript(self):
        found = self.scan(
            "const f = flagsmith.getExperimentFlag(\n  'signup_form'\n);\nflagsmith.trackEvent('purchase', { value: 9 });\n",
            name="page.tsx",
        )
        self.assertEqual(len(found.exposing), 1)
        self.assertIn("purchase", found.tracked_events)

    def test_closest_finds_case_and_typo_mismatches(self):
        self.assertEqual(codescan.closest("signup_completed", ["Signup_Completed", "other"]), "Signup_Completed")
        self.assertEqual(codescan.closest("signup_completed", ["signupCompleted"]), "signupCompleted")
        self.assertEqual(codescan.closest("signup_completed", ["signup_complete"]), "signup_complete")
        self.assertIsNone(codescan.closest("signup_completed", ["purchase"]))


class FakeFlagsmith:
    """Stands in for Flagsmith so setup and approval can be tested offline."""

    def __init__(self):
        self.environment_key = "env"
        self.calls = []
        self.features_by_name = {}
        self.variant_rows = {}
        self.metric_rows = []
        self.experiment_rows = []
        self.connections = []
        self.user = {"id": 5, "email": "human@example.com"}
        self.completed = None
        self.default_shares = []  # (variant id, percent) the flag serves by default

    def environment_default(self, feature_id):
        feature = next((f for f in self.features_by_name.values() if f["id"] == feature_id), None)
        return {
            "feature_state_value": (feature or {}).get("initial_value"),
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": option, "percentage_allocation": share}
                for option, share in self.default_shares
            ],
        }

    def warehouse_connections(self):
        return self.connections

    def connect_flagsmith_database(self):
        self.connections = [{"id": 1, "name": "Flagsmith database", "status": "connected"}]
        self.calls.append("connect")

    def feature_by_name(self, name):
        return self.features_by_name.get(name)

    def create_multivariate_feature(self, name, control_value, description=""):
        feature = {"id": 3, "name": name, "type": "MULTIVARIATE", "initial_value": control_value}
        self.features_by_name[name] = feature
        self.calls.append("feature")
        return feature

    def variants(self, feature_id):
        return list(self.variant_rows.get(feature_id, []))

    def create_variant(self, feature_id, key, value):
        row = {"id": 10 + len(self.variant_rows.get(feature_id, [])), "key": None, "string_value": value, "type": "unicode"}
        self.variant_rows.setdefault(feature_id, []).append(row)
        self.calls.append("variant")
        return dict(row)  # like Flagsmith, loses the key on create

    def set_variant_key(self, feature_id, variant, key):
        for row in self.variant_rows[feature_id]:
            if row["id"] == variant["id"]:
                row["key"] = key
                self.calls.append("key")
                return dict(row)

    def metrics(self):
        return list(self.metric_rows)

    def create_metric(self, name, event, aggregation="occurrence", direction="up", description=""):
        row = {"id": 100 + len(self.metric_rows), "name": name, "aggregation": aggregation, "direction": direction, "definition": {"version": 1, "event": event}}
        self.metric_rows.append(row)
        return row

    def experiments(self, status=None):
        return [e for e in self.experiment_rows if not status or e["status"] in status]

    def create_experiment(self, body):
        row = {
            "id": 7, "name": body["name"], "status": "created", "feature": {"id": body["feature"]}, "body": body,
            "experiment_rollout": body["experiment_rollout"], "metrics": body["metrics"],
            "hypothesis": body["hypothesis"],
        }
        row["feature"] = {
            "id": body["feature"], "name": next(f["name"] for f in self.features_by_name.values() if f["id"] == body["feature"]),
            "multivariate_options": list(self.variant_rows.get(body["feature"], [])),
        }
        self.experiment_rows.append(row)
        return row

    def set_rollout_percentage(self, experiment, percentage):
        self.experiment(experiment["id"])["experiment_rollout"]["rollout_percentage"] = percentage
        self.calls.append("rollout %g" % percentage)

    def start_experiment(self, experiment_id):
        row = self.experiment(experiment_id)
        row["status"] = "running"
        row.setdefault("started_at", "2026-11-27T00:00:00+00:00")
        self.calls.append("start")
        return row

    def pause_experiment(self, experiment_id):
        row = self.experiment(experiment_id)
        row["status"] = "paused"
        self.calls.append("pause")
        return row

    def version(self):
        return {}

    def warehouse_event_names(self, _id):
        return []

    def update_hypothesis(self, experiment_id, hypothesis):
        self.experiment(experiment_id)["hypothesis"] = hypothesis
        self.calls.append("hypothesis")

    def experiment(self, experiment_id):
        return next(e for e in self.experiment_rows if e["id"] == experiment_id)

    def whoami(self):
        if self.user is None:
            raise FlagsmithError(401, "GET", "/auth/users/me/", "nope")
        return self.user

    def complete_experiment(self, experiment_id, decision, variant_id=None):
        self.completed = (experiment_id, decision, variant_id)
        return {"status": "completed", "rollout_decision": decision, "rollout_variant": variant_id, "ended_at": "now"}


class ProvisionTests(unittest.TestCase):
    def test_creates_everything_and_keys_the_variant(self):
        client = FakeFlagsmith()
        done = provision.apply(client, spec.parse(h.PLAN))
        self.assertEqual(done.experiment_id, 7)
        self.assertEqual(client.variant_rows[3][0]["key"], "three_fields")  # repaired after create
        body = client.experiment_rows[0]["body"]
        self.assertEqual(body["experiment_rollout"]["feature_state_value"], {"type": "string", "value": "six_fields"})
        self.assertEqual(body["experiment_rollout"]["multivariate_feature_state_values"][0]["percentage_allocation"], 50)
        self.assertEqual([m["expected_direction"] for m in body["metrics"]], ["increase", "not_increase"])
        self.assertEqual([m["direction"] for m in client.metric_rows], ["up", "down"])

    def test_second_run_changes_nothing(self):
        client = FakeFlagsmith()
        provision.apply(client, spec.parse(h.PLAN))
        before = list(client.calls)
        again = provision.apply(client, spec.parse(h.PLAN))
        self.assertEqual(client.calls, before)
        self.assertEqual(again.experiment_id, 7)
        self.assertEqual(len(client.metric_rows), 2)

    def test_dry_run_touches_nothing(self):
        client = FakeFlagsmith()
        done = provision.apply(client, spec.parse(h.PLAN), dry_run=True)
        self.assertEqual(client.calls, [])
        self.assertIsNone(done.experiment_id)
        self.assertTrue(all(a.startswith("Would") for a in done.actions))

    def test_existing_unkeyed_variant_gets_its_key(self):
        client = FakeFlagsmith()
        client.features_by_name["signup_form"] = {"id": 3, "name": "signup_form", "type": "MULTIVARIATE", "initial_value": "six_fields"}
        client.variant_rows[3] = [{"id": 10, "key": None, "string_value": "three_fields", "type": "unicode"}]
        provision.apply(client, spec.parse(h.PLAN))
        self.assertEqual(client.variant_rows[3][0]["key"], "three_fields")
        self.assertNotIn("variant", client.calls)

    def test_conflicts_stop_the_run(self):
        client = FakeFlagsmith()
        client.features_by_name["signup_form"] = {"id": 3, "name": "signup_form", "type": "STANDARD"}
        with self.assertRaises(provision.ProvisionError):
            provision.apply(client, spec.parse(h.PLAN))
        client = FakeFlagsmith()
        provision.apply(client, spec.parse(h.PLAN))
        other = dict(h.PLAN, name="A different experiment")
        with self.assertRaises(provision.ProvisionError):
            provision.apply(client, spec.parse(other))

    def test_plan_is_remembered_per_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            provision.remember_plan(Path(folder), 7, spec.parse(h.PLAN), "env")
            self.assertIsNotNone(provision.recall_plan(Path(folder), 7, "env"))
            self.assertIsNone(provision.recall_plan(Path(folder), 7, "another-env"))
            self.assertIsNone(provision.recall_plan(Path(folder), 8, "env"))


def winning_card():
    n = spec.parse(h.PLAN).sizing()["per_arm"]
    return scorecard.judge(
        h.experiment(), h.results({"control": (n, int(n * 0.2)), "three_fields": (n, int(n * 0.27))}),
        h.exposures(), spec.parse(h.PLAN), now=h.NOW,
    )


def undecided_card():
    return scorecard.judge(
        h.experiment(), h.results({"control": (300, 60), "three_fields": (300, 66)}),
        h.exposures(), spec.parse(h.PLAN), now=h.NOW,
    )


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = decisions.Ledger(Path(self.folder.name))
        self.human = FakeFlagsmith()

    def tearDown(self):
        self.folder.cleanup()

    def test_only_actionable_calls_can_be_proposed(self):
        with self.assertRaises(decisions.DecisionError):
            self.ledger.propose(undecided_card(), "env")
        self.assertEqual(self.ledger.all(), [])

    def test_proposal_changes_nothing_until_approved(self):
        decision = self.ledger.propose(winning_card(), "env", proposed_by="agent")
        self.assertEqual(decision["status"], "pending")
        self.assertIsNone(self.human.completed)
        applied = self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["approval"]["by"], "human@example.com")
        self.assertEqual(self.human.completed, (7, "variant", 10))

    def test_agent_key_cannot_approve(self):
        decision = self.ledger.propose(winning_card(), "env")
        self.human.user = None  # an organisation API key is not a person
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertIsNone(self.human.completed)
        self.assertEqual(self.ledger.get(decision["id"])["status"], "pending")

    def test_approval_refused_when_the_call_changed(self):
        decision = self.ledger.propose(winning_card(), "env")
        with self.assertRaises(decisions.DecisionError) as caught:
            self.ledger.approve(decision["id"], self.human, lambda _id: undecided_card())
        self.assertIn("keep_running", str(caught.exception))
        self.assertIsNone(self.human.completed)
        self.assertEqual(self.ledger.get(decision["id"])["status"], "stale")

    def test_keep_control_completes_with_control(self):
        n = spec.parse(h.PLAN).sizing()["per_arm"]
        card = scorecard.judge(
            h.experiment(), h.results({"control": (n, int(n * 0.2)), "three_fields": (n, int(n * 0.2))}),
            h.exposures(), spec.parse(h.PLAN), now=h.NOW,
        )
        decision = self.ledger.propose(card, "env")
        self.ledger.approve(decision["id"], self.human, lambda _id: card)
        self.assertEqual(self.human.completed, (7, "control", None))

    def test_cannot_approve_twice_or_after_rejection(self):
        decision = self.ledger.propose(winning_card(), "env")
        self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        second = self.ledger.propose(winning_card(), "env")
        self.ledger.reject(second["id"], "sai", "not yet")
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(second["id"], self.human, lambda _id: winning_card())

    def test_new_proposal_supersedes_the_old_one(self):
        first = self.ledger.propose(winning_card(), "env")
        second = self.ledger.propose(winning_card(), "env")
        self.assertEqual(self.ledger.get(first["id"])["status"], "superseded")
        self.assertEqual([d["id"] for d in self.ledger.pending(7, "env")], [second["id"]])

    def test_expired_decision_cannot_be_approved(self):
        decision = self.ledger.propose(winning_card(), "env")
        stored = self.ledger.get(decision["id"])
        stored["expires_at"] = "2020-01-01T00:00:00+00:00"
        self.ledger._save(stored)
        with self.assertRaises(decisions.DecisionError):
            self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertIsNone(self.human.completed)

    def test_ledger_detects_tampering(self):
        decision = self.ledger.propose(winning_card(), "env")
        self.ledger.approve(decision["id"], self.human, lambda _id: winning_card())
        self.assertTrue(self.ledger.verify()["ok"])
        lines = self.ledger.ledger_path.read_text().splitlines()
        lines[0] = lines[0].replace('"agent"', '"somebody-else"')
        self.ledger.ledger_path.write_text("\n".join(lines) + "\n")
        check = self.ledger.verify()
        self.assertFalse(check["ok"])
        self.assertEqual(check["broken_at"], 1)

    def test_decision_id_cannot_escape_the_folder(self):
        with self.assertRaises(decisions.DecisionError):
            self.ledger.get("../../etc/passwd")


class McpTests(unittest.TestCase):
    def talk(self, *messages):
        with tempfile.TemporaryDirectory() as folder:
            settings = config.Settings(state_dir=Path(folder))
            out = io.StringIO()
            mcp_server.serve(settings, io.StringIO("\n".join(json.dumps(m) if not isinstance(m, str) else m for m in messages) + "\n"), out)
            return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_handshake_and_tools(self):
        answers = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        )
        self.assertEqual(len(answers), 3)  # the notification gets no answer
        self.assertEqual(answers[0]["result"]["protocolVersion"], "2025-03-26")
        names = [t["name"] for t in answers[1]["result"]["tools"]]
        self.assertIn("propose_rollout", names)
        for forbidden in ("approve", "approve_rollout", "complete_experiment", "ship"):
            self.assertNotIn(forbidden, names)
        for tool in answers[1]["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_protocol_version_falls_back(self):
        answers = self.talk({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(answers[0]["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSIONS[0])

    def test_errors_are_well_formed(self):
        answers = self.talk(
            "this is not json",
            {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "approve", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "scorecard", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "scorecard", "arguments": {"experiment": 1, "force": True}}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "scorecard", "arguments": {"experiment": 1}}},
        )
        self.assertEqual(answers[0]["error"]["code"], -32700)
        self.assertEqual(answers[1]["error"]["code"], -32601)
        self.assertTrue(answers[2]["result"]["isError"])
        self.assertIn("Missing required", answers[3]["result"]["content"][0]["text"])
        self.assertIn("Unknown argument", answers[4]["result"]["content"][0]["text"])
        self.assertIn("not connected", answers[5]["result"]["content"][0]["text"])  # no credentials: explained, not crashed

    def test_check_plan_works_offline(self):
        answers = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "check_plan", "arguments": {"plan": h.PLAN}}})
        result = answers[0]["result"]
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["valid"])
        self.assertIn("identities per version", result["content"][0]["text"])

    def test_prompt(self):
        answers = self.talk({"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "run_experiment", "arguments": {"idea": "shorter form"}}})
        self.assertIn("shorter form", answers[0]["result"]["messages"][0]["content"]["text"])


class ConfigTests(unittest.TestCase):
    def test_sources_and_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "referee.json").write_text(json.dumps({"api_url": "http://file:8000", "environment_key": "from-file"}))
            Path(folder, "flagsmith.env").write_text('FLAGSMITH_API_KEY="abc.def"\nexport FLAGSMITH_ENVIRONMENT_KEY=from-env-file\n# comment\n')
            settings = config.load(Path(folder), environ={})
            self.assertEqual(settings.api_url, "http://file:8000/api/v1")
            self.assertEqual(settings.api_key, "abc.def")
            self.assertEqual(settings.environment_key, "from-env-file")
            settings = config.load(Path(folder), environ={"FLAGSMITH_ENVIRONMENT_KEY": "from-process", "FLAGSMITH_API_URL": "https://x/api/"})
            self.assertEqual(settings.environment_key, "from-process")
            self.assertEqual(settings.api_url, "https://x/api/v1")
            self.assertNotIn("abc.def", json.dumps(settings.public()))

    def test_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(config.load(Path(folder), environ={}).missing(), ["FLAGSMITH_API_KEY", "FLAGSMITH_ENVIRONMENT_KEY"])
            with self.assertRaises(ops.OpError):
                ops.list_experiments(config.load(Path(folder), environ={}))

    def test_auth_header(self):
        self.assertEqual(api.auth_header("prefix.secret"), "Api-Key prefix.secret")
        self.assertEqual(api.auth_header("abcdef0123"), "Token abcdef0123")
        self.assertEqual(api.auth_header("Bearer xyz.abc"), "Bearer xyz.abc")


if __name__ == "__main__":
    unittest.main()
