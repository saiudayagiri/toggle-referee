"""A small client for the Flagsmith Admin API and SDK events endpoint.

Only the standard library is used so Referee runs anywhere Python 3.9+ does.
Every call Referee makes to Flagsmith goes through this module, which keeps the
list of endpoints it depends on in one place.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_API_URL = "http://localhost:8000/api/v1"
EXPOSURE_EVENT = "$flag_exposure"
CONTROL_KEY = "control"
USER_AGENT = "flagsmith-referee/0.1"


class FlagsmithError(Exception):
    """An API call failed. ``status`` is 0 when Flagsmith could not be reached."""

    def __init__(self, status: int, method: str, path: str, detail: Any):
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail
        super().__init__(self._message())

    def _message(self) -> str:
        if self.status == 0:
            return "Could not reach Flagsmith (%s %s): %s" % (
                self.method,
                self.path,
                self.detail,
            )
        detail = self.detail
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail)
        return "Flagsmith answered %d to %s %s: %s" % (
            self.status,
            self.method,
            self.path,
            str(detail)[:600],
        )


def auth_header(credential: str) -> str:
    """Build the Authorization header value for a Flagsmith credential.

    Organisation API keys look like ``prefix.secret`` and use the ``Api-Key``
    scheme; a user's own token is a bare hex string and uses ``Token``. A value
    that already names its scheme is passed through.
    """
    credential = credential.strip()
    lowered = credential.lower()
    if lowered.startswith(("api-key ", "token ", "bearer ")):
        return credential
    if "." in credential:
        return "Api-Key " + credential
    return "Token " + credential


class Flagsmith:
    """Calls to one Flagsmith environment."""

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        api_key: str = "",
        environment_key: str = "",
        project_id: Optional[int] = None,
        timeout: float = 20.0,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.environment_key = environment_key
        self._project_id = project_id
        self.timeout = timeout

    # ------------------------------------------------------------------ http

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        all_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if headers is None:
            if not self.api_key:
                raise FlagsmithError(
                    0, method, path, "no API key configured (set FLAGSMITH_API_KEY)"
                )
            all_headers["Authorization"] = auth_header(self.api_key)
        else:
            all_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            raise FlagsmithError(error.code, method, path, _decode(raw)) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise FlagsmithError(0, method, path, str(reason)) from None
        except ValueError:
            # http.client rejects malformed headers and URLs with a message
            # that quotes them, credential included. Say what is wrong instead.
            raise FlagsmithError(
                0, method, path,
                "the request could not be built; check the API URL, API key and environment key for stray characters",
            ) from None
        data = _decode(raw)
        if isinstance(data, str):
            # A page instead of JSON: a dashboard host, a proxy or a login wall.
            raise FlagsmithError(
                0, method, path,
                "the server answered with something that is not JSON. Check FLAGSMITH_API_URL; "
                "it should be the API, for example https://api.flagsmith.com/api/v1",
            )
        return data

    def _env(self, suffix: str = "") -> str:
        if not self.environment_key:
            raise FlagsmithError(
                0,
                "-",
                "-",
                "no environment key configured (set FLAGSMITH_ENVIRONMENT_KEY)",
            )
        return "/environments/%s/%s" % (self.environment_key, suffix)

    def _all_pages(self, path: str, query: Optional[Dict[str, Any]] = None) -> List[Any]:
        """Collect a list endpoint that may or may not be paginated."""
        query = dict(query or {})
        query.setdefault("page_size", 100)
        items: List[Any] = []
        page = 1
        while True:
            query["page"] = page
            data = self.request("GET", path, query=query)
            if isinstance(data, list):
                return data
            if not isinstance(data, dict):
                raise FlagsmithError(0, "GET", path, "unexpected answer (not a list)")
            items.extend(data.get("results") or [])
            if not data.get("next"):
                return items
            page += 1

    # ----------------------------------------------------- project and setup

    def environment(self) -> Dict[str, Any]:
        data = self.request("GET", self._env())
        if not isinstance(data, dict):
            raise FlagsmithError(0, "GET", self._env(), "unexpected answer for the environment")
        return data

    @property
    def project_id(self) -> int:
        if self._project_id is None:
            project = self.environment().get("project")
            if project is None:
                raise FlagsmithError(0, "GET", self._env(), "the environment does not name its project")
            self._project_id = int(project)
        return self._project_id

    def version(self) -> Dict[str, Any]:
        root = self.api_url.rsplit("/api/", 1)[0]
        req = urllib.request.Request(root + "/version/", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = _decode(response.read())
                return data if isinstance(data, dict) else {}
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def warehouse_connections(self) -> List[Dict[str, Any]]:
        data = self.request("GET", self._env("warehouse-connections/"))
        if isinstance(data, dict):
            data = data.get("results") or []
        return [c for c in (data or []) if isinstance(c, dict)]

    def connect_flagsmith_database(self) -> Dict[str, Any]:
        """Store events in Flagsmith's own Postgres (the evaluation warehouse)."""
        return self.request(
            "POST",
            self._env("warehouse-connections/"),
            {"warehouse_type": "postgres", "config": {"source": "flagsmith_database"}},
        )

    def warehouse_event_names(self, connection_id: int) -> List[str]:
        data = self.request(
            "GET", self._env("warehouse-connections/%d/events/" % connection_id)
        )
        return list((data or {}).get("events") or [])

    # -------------------------------------------------------------- features

    def features(self) -> List[Dict[str, Any]]:
        return self._all_pages("/projects/%d/features/" % self.project_id)

    def feature_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for feature in self._all_pages(
            "/projects/%d/features/" % self.project_id, {"search": name}
        ):
            if feature["name"] == name:
                return feature
        return None

    def create_multivariate_feature(
        self, name: str, control_value: str, description: str = ""
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/projects/%d/features/" % self.project_id,
            {
                "name": name,
                "type": "MULTIVARIATE",
                "initial_value": control_value,
                "default_enabled": True,
                "description": description or None,
            },
        )

    def environment_default(self, feature_id: int) -> Optional[Dict[str, Any]]:
        """What the flag serves in this environment when no override applies:
        its base value and the default share of each variant."""
        rows = self._all_pages(self._env("featurestates/"), {"feature": feature_id})
        plain = [
            r for r in rows
            if isinstance(r, dict) and r.get("feature_segment") is None and r.get("identity") is None
        ]
        return sorted(plain, key=lambda r: r.get("id") or 0)[-1] if plain else None

    def variants(self, feature_id: int) -> List[Dict[str, Any]]:
        return self._all_pages(
            "/projects/%d/features/%d/mv-options/" % (self.project_id, feature_id)
        )

    def create_variant(self, feature_id: int, key: str, value: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/projects/%d/features/%d/mv-options/" % (self.project_id, feature_id),
            {
                "feature": feature_id,
                "type": "unicode",
                "string_value": value,
                "key": key,
                "default_percentage_allocation": 0,
            },
        )

    def set_variant_key(self, feature_id: int, variant: Dict[str, Any], key: str) -> Dict[str, Any]:
        """Give an existing variant a key.

        A full PUT is used on purpose: a partial update of this resource is not
        reliable on current Flagsmith builds.
        """
        body = {
            "feature": feature_id,
            "type": variant.get("type", "unicode"),
            "string_value": variant.get("string_value"),
            "integer_value": variant.get("integer_value"),
            "boolean_value": variant.get("boolean_value"),
            "default_percentage_allocation": variant.get(
                "default_percentage_allocation", 0
            ),
            "key": key,
        }
        return self.request(
            "PUT",
            "/projects/%d/features/%d/mv-options/%d/"
            % (self.project_id, feature_id, variant["id"]),
            body,
        )

    def upload_code_references(
        self, repository_url: str, revision: str, references: List[Dict[str, Any]]
    ) -> Any:
        return self.request(
            "POST",
            "/projects/%d/code-references/" % self.project_id,
            {
                "repository_url": repository_url,
                "revision": revision,
                "code_references": references,
            },
        )

    # --------------------------------------------------------------- metrics

    def metrics(self) -> List[Dict[str, Any]]:
        return self._all_pages(self._env("experiment-metrics/"))

    def create_metric(
        self,
        name: str,
        event: str,
        aggregation: str = "occurrence",
        direction: str = "up",
        description: str = "",
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            self._env("experiment-metrics/"),
            {
                "name": name,
                "description": description,
                "aggregation": aggregation,
                "direction": direction,
                "definition": {"version": 1, "event": event},
            },
        )

    # ----------------------------------------------------------- experiments

    def experiments(self, status: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        query = {"status": list(status)} if status else None
        return self._all_pages(self._env("experiments/"), query)

    def experiment(self, experiment_id: int) -> Dict[str, Any]:
        data = self.request("GET", self._env("experiments/%d/" % experiment_id))
        if not isinstance(data, dict):
            raise FlagsmithError(0, "GET", self._env("experiments/%d/" % experiment_id), "unexpected answer for the experiment")
        return data

    def create_experiment(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", self._env("experiments/"), body)

    def set_rollout_percentage(self, experiment: Dict[str, Any], percentage: float) -> Dict[str, Any]:
        """Change how much of the audience is in the experiment, keeping its split.

        ``experiment`` is the experiment as Flagsmith returns it; its current
        rollout supplies the control value and variant shares.
        """
        rollout = experiment.get("experiment_rollout") or {}
        body = {
            "enabled": rollout.get("enabled", True),
            "rollout_percentage": percentage,
            "feature_state_value": rollout.get("feature_state_value"),
            "multivariate_feature_state_values": rollout.get("multivariate_feature_state_values") or [],
        }
        return self.request("PATCH", self._env("experiments/%d/rollout/" % experiment["id"]), body)

    def update_hypothesis(self, experiment_id: int, hypothesis: str) -> Dict[str, Any]:
        return self.request("PATCH", self._env("experiments/%d/" % experiment_id), {"hypothesis": hypothesis})

    def delete_experiment(self, experiment_id: int) -> None:
        self.request("DELETE", self._env("experiments/%d/" % experiment_id))

    def start_experiment(self, experiment_id: int) -> Dict[str, Any]:
        return self.request("POST", self._env("experiments/%d/start/" % experiment_id))

    def pause_experiment(self, experiment_id: int) -> Dict[str, Any]:
        return self.request("POST", self._env("experiments/%d/pause/" % experiment_id))

    def complete_experiment(
        self, experiment_id: int, decision: str, variant_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """End the experiment and serve the decided outcome from the same flag.

        ``decision`` is ``control``, ``variant`` (with ``variant_id``) or
        ``disable``.
        """
        body: Dict[str, Any] = {"rollout_decision": decision}
        if variant_id is not None:
            body["rollout_variant"] = variant_id
        return self.request(
            "POST", self._env("experiments/%d/complete/" % experiment_id), body
        )

    def results(self, experiment_id: int) -> Dict[str, Any]:
        data = self.request("GET", self._env("experiments/%d/results/" % experiment_id))
        return (data or {}).get("results") or {}

    def exposures(self, experiment_id: int) -> Dict[str, Any]:
        data = self.request(
            "GET", self._env("experiments/%d/exposures/" % experiment_id)
        )
        return (data or {}).get("exposures") or {}

    def refresh(self, experiment_id: int) -> Dict[str, Any]:
        """Ask Flagsmith to recompute results and exposures.

        Flagsmith allows one refresh every five minutes per experiment. A
        throttled request is not an error here: the answer says how many
        seconds remain (``retry_after``) so callers can tell people how old
        the numbers are.
        """
        done: Dict[str, Any] = {"retry_after": None}
        for what in ("results", "exposures"):
            try:
                self.request(
                    "POST",
                    self._env("experiments/%d/%s/refresh/" % (experiment_id, what)),
                )
                done[what] = True
            except FlagsmithError as error:
                if error.status != 429:
                    raise
                done[what] = False
                found = re.search(r"(\d+)\s+seconds", str(error.detail))
                if found:
                    done["retry_after"] = max(done["retry_after"] or 0, int(found.group(1)))
        return done

    def fresh_results(self, experiment_id: int, wait_seconds: float = 20.0) -> Dict[str, Any]:
        """Refresh, then wait briefly for the task processor to recompute.

        Returns the results object with a ``refresh`` entry describing whether
        the recompute was accepted or throttled.
        """
        before = self.results(experiment_id).get("as_of")
        requested = self.refresh(experiment_id)
        current = self.results(experiment_id)
        if requested.get("results"):
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if current.get("as_of") and current.get("as_of") != before:
                    break
                time.sleep(1.0)
                current = self.results(experiment_id)
        current["refresh"] = requested
        return current

    # ------------------------------------------------------------ SDK surface

    def identity_flags(self, identifier: str) -> Dict[str, Any]:
        """Evaluate flags for one identity exactly as an SDK would."""
        return self.request(
            "GET",
            "/identities/",
            headers={"X-Environment-Key": self.environment_key},
            query={"identifier": identifier},
        )

    def send_events(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Send exposure or metric events the way the SDKs do."""
        return self.request(
            "POST",
            "/events/",
            {"events": events},
            headers={"X-Environment-Key": self.environment_key},
        )

    def audit_log(self, search: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"page_size": limit}
        if search:
            query["search"] = search
        data = self.request(
            "GET", "/projects/%d/audit/" % self.project_id, query=query
        )
        if isinstance(data, list):
            return data
        return data.get("results", [])

    # ------------------------------------------------------------------ users

    def login(self, email: str, password: str) -> str:
        """Exchange a person's Flagsmith login for a personal token."""
        data = self.request(
            "POST",
            "/auth/login/",
            {"email": email, "password": password},
            headers={},
        )
        key = (data or {}).get("key") if isinstance(data, dict) else None
        if not key:
            # Accounts with two-factor authentication get an ephemeral token
            # here and need a second step that Referee does not implement.
            raise FlagsmithError(
                401, "POST", "/auth/login/",
                "Flagsmith did not return a session for this account. Accounts that use two-factor "
                "authentication or single sign-on cannot approve through Referee yet.",
            )
        return key

    def whoami(self) -> Dict[str, Any]:
        data = self.request("GET", "/auth/users/me/")
        return data if isinstance(data, dict) else {}

    def as_user(self, credential: str) -> "Flagsmith":
        """A copy of this client that acts with another credential."""
        return Flagsmith(
            api_url=self.api_url,
            api_key=credential,
            environment_key=self.environment_key,
            project_id=self._project_id,
            timeout=self.timeout,
        )


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text[:800]


def now_ms() -> int:
    return int(time.time() * 1000)
