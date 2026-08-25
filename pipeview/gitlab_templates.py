"""Bundled snapshot of GitLab's built-in CI templates.

GitLab resolves `include: template: Jobs/Build.gitlab-ci.yml` against the
template tree shipped inside the GitLab installation itself
(lib/gitlab/ci/templates). Nothing outside that installation can fetch most
of those files: the REST template API only exposes the flattened "dropdown"
keys (top-level names plus the basenames of Pages/, Verify/ and Security/),
so Jobs/*, Workflows/* and every category-qualified spelling 404 on every
GitLab version. pipeview therefore ships a verbatim snapshot of the tree
(pipeview/data/gitlab_ci_templates, MIT-licensed, refreshed by
scripts/update_gitlab_templates.py) and falls back to it whenever the
instance cannot serve a template — and when running fully offline.

The snapshot is a *reference* copy from one GitLab release; `bundled_version`
says which, so every fallback can tell the user their instance's own copy
may differ.
"""

from __future__ import annotations

import json
import os
import posixpath
from functools import lru_cache

_SUFFIX = ".gitlab-ci.yml"


def bundled_root() -> str:
    """Directory the snapshot lives in (files exist even from a wheel —
    pipeview installs as a plain package, never zipped)."""
    return os.path.join(os.path.dirname(__file__), "data", "gitlab_ci_templates")


@lru_cache(maxsize=1)
def bundled_meta() -> dict:
    """The snapshot's _meta.json (source repo, ref, version). {} if absent."""
    try:
        with open(os.path.join(bundled_root(), "_meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def bundled_version() -> str:
    """Human label for provenance messages, e.g. 'GitLab 19.3.0 templates'."""
    version = bundled_meta().get("gitlab_version")
    return f"GitLab {version} templates" if version else "bundled GitLab templates"


def template_path(name: str) -> str | None:
    """Absolute path of the bundled template `name`, or None.

    `name` is what the user wrote after `template:` — normally
    'Jobs/Build.gitlab-ci.yml' (GitLab requires the suffix; be tolerant of
    it missing). Never resolves outside the snapshot directory.
    """
    rel = posixpath.normpath(str(name).strip().lstrip("/"))
    if not rel or rel == "." or rel.startswith("..") or os.path.isabs(rel):
        return None
    root = bundled_root()
    for candidate in (rel,) if rel.endswith(_SUFFIX) else (rel, rel + _SUFFIX):
        path = os.path.join(root, *candidate.split("/"))
        if os.path.isfile(path):
            return path
    return None


def template_names() -> list[str]:
    """Every bundled template name (posix-relative, sorted)."""
    root = bundled_root()
    names: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(_SUFFIX):
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                names.append(rel.replace(os.sep, "/"))
    return sorted(names)
