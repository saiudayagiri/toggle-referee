"""Where Referee finds Flagsmith and where it keeps its own records.

Settings come from environment variables first, then from ``referee.json`` in
the working directory (or the file named by ``REFEREE_CONFIG``). Secrets are
never written by Referee; the config file is for the non-secret parts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .api import DEFAULT_API_URL, Flagsmith

CONFIG_FILE = "referee.json"
STATE_DIR = ".referee"


@dataclass
class Settings:
    api_url: str = DEFAULT_API_URL
    api_key: str = ""
    environment_key: str = ""
    project_id: Optional[int] = None
    state_dir: Path = Path(STATE_DIR)
    dashboard_url: str = ""
    source: str = "defaults"

    def client(self) -> Flagsmith:
        return Flagsmith(
            api_url=self.api_url,
            api_key=self.api_key,
            environment_key=self.environment_key,
            project_id=self.project_id,
        )

    def missing(self) -> list:
        missing = []
        if not self.api_key:
            missing.append("FLAGSMITH_API_KEY")
        if not self.environment_key:
            missing.append("FLAGSMITH_ENVIRONMENT_KEY")
        return missing

    def public(self) -> Dict[str, Any]:
        """Settings safe to print: the API key is reduced to its first characters."""
        key = self.api_key
        return {
            "api_url": self.api_url,
            "api_key": (key[:6] + "…") if key else "",
            "environment_key": self.environment_key,
            "project_id": self.project_id,
            "state_dir": str(self.state_dir),
            "source": self.source,
        }


def load_env_file(path: Path) -> Dict[str, str]:
    """Read simple ``KEY=value`` lines. Quotes around the value are removed."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # trailing comment
        values[key.strip()] = value
    return values


def load(
    directory: Optional[Path] = None, environ: Optional[Dict[str, str]] = None
) -> Settings:
    directory = Path(directory or os.getcwd())
    environ = dict(os.environ if environ is None else environ)
    file_values: Dict[str, Any] = {}
    sources = []

    config_path = Path(environ.get("REFEREE_CONFIG") or directory / CONFIG_FILE)
    if config_path.is_file():
        try:
            file_values = json.loads(config_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError("%s is not valid JSON: %s" % (config_path, error))
        except OSError as error:
            raise ValueError("%s cannot be read: %s" % (config_path, error.strerror or error))
        if not isinstance(file_values, dict):
            raise ValueError("%s must contain a JSON object of settings." % config_path)
        sources.append(str(config_path))

    env_file_values = {}
    for name in ("flagsmith.env", ".env"):
        loaded = load_env_file(directory / name)
        if loaded:
            sources.append(str(directory / name))
        for key, value in loaded.items():
            env_file_values.setdefault(key, value)

    def pick(env_name: str, file_name: str, default: Any = "") -> Any:
        if environ.get(env_name, "").strip():
            return environ[env_name].strip()
        if env_file_values.get(env_name):
            return env_file_values[env_name]
        if file_values.get(file_name) not in (None, ""):
            return file_values[file_name]
        return default

    if any(name.startswith("FLAGSMITH_") for name in environ):
        sources.append("environment")

    project = pick("FLAGSMITH_PROJECT_ID", "project_id", None)
    api_url = str(pick("FLAGSMITH_API_URL", "api_url", DEFAULT_API_URL)).rstrip("/")
    if not api_url.endswith("/api/v1"):
        # Accept the forms other Flagsmith tools use: the bare host or .../api/.
        if api_url.endswith("/api"):
            api_url += "/v1"
        elif "/api/" not in api_url + "/":
            api_url += "/api/v1"
    api_key = str(pick("FLAGSMITH_API_KEY", "api_key", ""))
    environment_key = str(pick("FLAGSMITH_ENVIRONMENT_KEY", "environment_key", ""))
    # These go into a header and a URL path. Refuse odd characters here, with
    # a message that names the setting and never echoes a secret.
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in api_key):
        raise ValueError("FLAGSMITH_API_KEY contains spaces, line breaks or other characters a key cannot have.")
    if environment_key and not all(ch.isalnum() or ch in "_-." for ch in environment_key):
        raise ValueError("FLAGSMITH_ENVIRONMENT_KEY should be letters and digits only; check for stray spaces or comments.")
    try:
        project = int(project) if project not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("FLAGSMITH_PROJECT_ID must be a whole number.")
    state_dir = Path(pick("REFEREE_STATE_DIR", "state_dir", STATE_DIR))
    if not state_dir.is_absolute():
        state_dir = directory / state_dir
    return Settings(
        api_url=api_url,
        api_key=api_key,
        environment_key=environment_key,
        project_id=project,
        state_dir=state_dir,
        dashboard_url=str(pick("FLAGSMITH_DASHBOARD_URL", "dashboard_url", "")),
        source=", ".join(sources) or "defaults",
    )
