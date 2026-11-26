import tempfile
import unittest
from pathlib import Path

from referee import preflight, spec
from tests import helpers as h

GOOD_APP = '''
flag = fs.get_experiment_flag("signup_form", identifier=user)
if flag.value == "three_fields":
    short()
fs.track_event("signup_completed", identifier=user)
fs.track_event("signup_error", identifier=user)
'''


class Fake:
    environment_key = "env"

    def __init__(self, experiment=None, seen=(), connected=True, flags=None):
        self._experiment = experiment or h.experiment(status="created")
        self.seen = list(seen)
        self.connected = connected
        self.flags = flags

    def experiment(self, _id):
        return self._experiment

    def version(self):
        return {"experimentation_enabled": True}

    def warehouse_connections(self):
        return [{"id": 1, "name": "Flagsmith database", "status": "connected"}] if self.connected else []

    def warehouse_event_names(self, _id):
        return self.seen

    def metrics(self):
        return [
            {"id": 101, "name": "Signup completed", "definition": {"event": "signup_completed"}},
            {"id": 102, "name": "Signup error", "definition": {"event": "signup_error"}},
        ]

    def experiments(self, status=None):
        return []

    def identity_flags(self, identifier):
        return self.flags(identifier)


def run(source=GOOD_APP, client=None, plan=True, probe=0):
    with tempfile.TemporaryDirectory() as folder:
        if source is not None:
            Path(folder, "app.py").write_text(source)
        return preflight.run(
            client or Fake(), 7, plan=spec.parse(h.PLAN) if plan else None,
            code_root=Path(folder) if source is not None else None, probe=probe,
        )


def status_of(result, check_id):
    found = [c["status"] for c in result["checks"] if c["id"] == check_id]
    return found[0] if found else None


class PreflightTests(unittest.TestCase):
    def test_well_instrumented_app_is_ready(self):
        result = run()
        self.assertTrue(result["ready"], result["checks"])
        self.assertEqual(status_of(result, "code_exposure"), "pass")
        self.assertEqual(status_of(result, "code_event:signup_completed"), "pass")

    def test_no_warehouse(self):
        result = run(client=Fake(connected=False))
        self.assertFalse(result["ready"])
        self.assertEqual(status_of(result, "warehouse"), "fail")

    def test_unkeyed_variant(self):
        result = run(client=Fake(h.experiment(status="created", keys=False)))
        self.assertEqual(status_of(result, "variants"), "fail")

    def test_plain_read_records_no_exposure(self):
        result = run(GOOD_APP.replace("get_experiment_flag", "get_feature_value"))
        self.assertFalse(result["ready"])
        self.assertEqual(status_of(result, "code_exposure"), "fail")

    def test_mixed_reads_warn(self):
        result = run(GOOD_APP + 'other = fs.get_feature_value("signup_form")\n')
        self.assertTrue(result["ready"])
        self.assertEqual(status_of(result, "code_exposure"), "warn")

    def test_flag_missing_from_code(self):
        result = run("print('hello')\n")
        self.assertEqual(status_of(result, "code_flag"), "fail")

    def test_event_case_mismatch_in_code(self):
        result = run(GOOD_APP.replace('"signup_completed"', '"Signup_Completed"'))
        self.assertFalse(result["ready"])
        check = [c for c in result["checks"] if c["id"] == "code_event:signup_completed"][0]
        self.assertIn("Signup_Completed", check["detail"])
        self.assertIn("case-sensitive", check["detail"])

    def test_event_never_sent(self):
        result = run(GOOD_APP.replace('fs.track_event("signup_error", identifier=user)', ""))
        self.assertEqual(status_of(result, "code_event:signup_error"), "fail")

    def test_unhandled_version_warns(self):
        result = run(GOOD_APP.replace('"three_fields"', '"short"'))
        self.assertEqual(status_of(result, "code_variants"), "warn")

    def test_events_arriving_under_another_name(self):
        result = run(source=None, client=Fake(seen=["$flag_exposure", "signupCompleted", "signup_error"]))
        self.assertFalse(result["ready"])
        self.assertEqual(status_of(result, "event_seen:signup_completed"), "fail")
        self.assertEqual(status_of(result, "event_seen:signup_error"), "pass")

    def test_no_events_yet_is_only_information(self):
        result = run(source=None)
        self.assertTrue(result["ready"])
        self.assertEqual(status_of(result, "event_seen:signup_completed"), "info")

    def test_server_key_in_source_warns(self):
        result = run(GOOD_APP + 'KEY = "ser.AbCdEfGhIjKlMnOpQrStUv"\n')
        self.assertEqual(status_of(result, "code_secret"), "warn")

    def test_plan_that_cannot_finish_warns(self):
        import json
        data = json.loads(json.dumps(h.PLAN))
        data["assumptions"]["deadline"] = "2000-01-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as folder:
            result = preflight.run(Fake(), 7, plan=spec.parse(data), code_root=None)
        self.assertEqual(status_of(result, "sizing"), "warn")

    def probe(self, assign):
        def flags(identifier):
            variant, inside = assign(int(identifier[-6:]))
            return {"flags": [{"feature": {"name": "signup_form"}, "variant": variant,
                               "metadata": {"experiment": {"in_experiment": inside}}}]}
        return run(source=None, client=Fake(h.experiment(status="running"), flags=flags), probe=200)

    def test_probe_passes_on_a_fair_split(self):
        result = self.probe(lambda i: ("three_fields" if i % 2 else "control", True))
        self.assertEqual(status_of(result, "probe"), "pass")

    def test_probe_catches_a_version_that_is_never_served(self):
        result = self.probe(lambda i: ("control", True))
        self.assertEqual(status_of(result, "probe"), "fail")

    def test_probe_catches_nobody_in_the_experiment(self):
        result = self.probe(lambda i: ("control", False))
        self.assertEqual(status_of(result, "probe"), "fail")

    def test_probe_waits_until_running(self):
        result = run(source=None, probe=50)
        self.assertEqual(status_of(result, "probe"), "info")


if __name__ == "__main__":
    unittest.main()
