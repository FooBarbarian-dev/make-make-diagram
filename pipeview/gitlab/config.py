"""On-disk configuration for the GitLab feature.

One JSON file, keyed by host, holding the stored token and the tracked
projects list. Tokens are secrets: the file is created 0600 and re-chmodded
on every save. Users who refuse on-disk tokens use the env vars instead
(see auth.py) — this file then only carries tracked lists.
"""

from __future__ import annotations

import json
import os
from typing import Any

_FILENAME = "gitlab.json"


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "pipeview", _FILENAME)


class GitLabConfig:
    def __init__(self, path: str | None = None):
        self.path = path or config_path()
        self.data: dict[str, Any] = {"default_host": None, "hosts": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("hosts", {})

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # -- hosts --------------------------------------------------------------

    @staticmethod
    def normalize_host(host: str) -> str:
        host = host.strip().rstrip("/")
        if host and "://" not in host:
            host = "https://" + host
        return host

    def host_entry(self, host: str) -> dict[str, Any]:
        host = self.normalize_host(host)
        return self.data["hosts"].setdefault(host, {"token": None, "tracked": []})

    def known_hosts(self) -> list[str]:
        return sorted(self.data["hosts"])

    @property
    def default_host(self) -> str | None:
        return self.data.get("default_host")

    @default_host.setter
    def default_host(self, host: str | None) -> None:
        self.data["default_host"] = self.normalize_host(host) if host else None

    # -- tokens -------------------------------------------------------------

    def stored_token(self, host: str) -> str | None:
        entry = self.data["hosts"].get(self.normalize_host(host))
        return entry.get("token") if entry else None

    def set_token(self, host: str, token: str | None) -> None:
        self.host_entry(host)["token"] = token

    # -- tracked projects ---------------------------------------------------

    def tracked(self, host: str) -> list[str]:
        entry = self.data["hosts"].get(self.normalize_host(host))
        return list(entry.get("tracked", [])) if entry else []

    def track(self, host: str, project_path: str) -> bool:
        """Returns True if newly added."""
        lst = self.host_entry(host).setdefault("tracked", [])
        if project_path in lst:
            return False
        lst.append(project_path)
        lst.sort()
        return True

    def untrack(self, host: str, project_path: str) -> bool:
        lst = self.host_entry(host).setdefault("tracked", [])
        if project_path not in lst:
            return False
        lst.remove(project_path)
        return True

    def is_tracked(self, host: str, project_path: str) -> bool:
        return project_path in self.tracked(host)
