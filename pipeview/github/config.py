"""On-disk configuration for the GitHub feature.

The same shape as the GitLab file (one JSON keyed by host: stored token
plus the tracked repositories list), stored beside it as
``~/.config/pipeview/github.json``. The entry algebra is identical —
``owner/repo`` follows the default branch, ``owner/repo@ref`` pins one —
because GitHub repository names cannot contain ``@`` either, so the class
is reused whole.
"""

from __future__ import annotations

import os

from pipeview.gitlab.config import GitLabConfig

_FILENAME = "github.json"


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "pipeview", _FILENAME)


class GitHubConfig(GitLabConfig):
    def __init__(self, path: str | None = None):
        super().__init__(path or config_path())
