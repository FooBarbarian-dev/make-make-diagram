"""Fetching GitHub Actions workflows for offline analysis.

One strategy (GitHub has no server-side merged-config API like GitLab's
CI Lint): list ``.github/workflows/`` via the contents API, fetch every
workflow file, then follow cross-repository reusable-workflow calls
(``jobs.<id>.uses: owner/repo/path@ref``) into ``_external/`` — including
the called workflow's own nested calls, to GitHub's documented limits
(4 levels deep, 20 unique reusable workflows per run).

Reuses the GitLab fetch layer's value objects and materialization
(`FetchedFile`, `FetchResult`, `materialize`) — the on-disk layout is the
same:

    <outdir>/fetched/<owner-repo>@<ref>/
      .github/workflows/ci.yml               # the repo's own workflows
      _external/octo-org-shared@v2/.github/workflows/notify.yml

Everything the parser needs afterward is on disk; the offline pipeline
runs unchanged.
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Callable

import yaml

from pipeview.github.api import GitHubClient, GitHubError
from pipeview.gitlab.fetch import (
    EXTERNAL_DIR,
    FetchedFile,
    FetchResult,
    slugify,
)

log = logging.getLogger(__name__)

WORKFLOWS_DIR = ".github/workflows"

# GitHub's own documented ceilings for reusable workflows.
MAX_REUSABLE_DEPTH = 4
MAX_REUSABLE_FILES = 20

_WORKFLOW_FILE_RE = re.compile(r"\.ya?ml$")
_REMOTE_USES_RE = re.compile(
    r"^(?P<owner>[^/@\s]+)/(?P<repo>[^/@\s]+)/(?P<path>[^@]+)@(?P<ref>.+)$"
)


def uses_key(uses: str) -> str:
    """The identity the fetch traversal and the parse-time resolver both
    compute for one cross-repo reusable-workflow call — they must agree
    byte for byte."""
    return f"uses:{uses}"


def extract_job_uses(text: str) -> list[str]:
    """jobs.<id>.uses values from one workflow's YAML — a tolerant
    pre-pass; YAML errors return [] (the real parser reports them)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return []
    out = []
    for job in jobs.values():
        if isinstance(job, dict) and isinstance(job.get("uses"), str):
            out.append(job["uses"])
    return out


def fetch_config(client: GitHubClient, repo: dict, ref: str) -> FetchResult:
    """Fetch every workflow of `repo` at `ref`, plus the cross-repository
    reusable workflows they call. Raises GitHubError when the repo has no
    workflows at all."""
    full_name = repo.get("full_name") or repo.get("path_with_namespace")
    notes: list[tuple[str, str]] = []

    def note(severity: str, message: str) -> None:
        (log.warning if severity in ("warning", "error") else log.info)(
            "%s", message)
        notes.append((severity, message))

    try:
        entries = client.list_dir(full_name, WORKFLOWS_DIR, ref)
    except GitHubError as e:
        raise GitHubError(
            f"Could not list {WORKFLOWS_DIR} of {full_name}@{ref}: {e}",
            getattr(e, "status", 0)) from None

    names = sorted(
        e["name"] for e in entries
        if e.get("type") == "file" and _WORKFLOW_FILE_RE.search(e.get("name", "")))
    if not names:
        raise GitHubError(
            f"{full_name}@{ref} has no workflow files under {WORKFLOWS_DIR}")

    files: dict[str, FetchedFile] = {}
    external_map: dict[str, str] = {}
    fetched_reusables: set[str] = set()

    for name in names:
        rel = f"{WORKFLOWS_DIR}/{name}"
        try:
            content = client.get_raw_file(full_name, rel, ref)
        except GitHubError as e:
            note("warning", f"Cannot fetch {full_name}@{ref}:{rel}: {e}")
            continue
        log.info("Fetched %s@%s:%s (%d bytes)", full_name, ref, rel,
                 len(content))
        files[rel] = FetchedFile(rel_path=rel, content=content,
                                 origin="root",
                                 source=f"{full_name}@{ref}:{rel}")

    def fetch_reusable(uses: str, depth: int) -> None:
        """One cross-repo reusable workflow, then its own calls."""
        key = uses_key(uses)
        if key in external_map or uses in fetched_reusables:
            return
        fetched_reusables.add(uses)
        if len(external_map) >= MAX_REUSABLE_FILES:
            note("warning",
                 f"More than {MAX_REUSABLE_FILES} reusable workflows — "
                 f"{uses} not fetched (GitHub's own per-run ceiling)")
            return
        if depth > MAX_REUSABLE_DEPTH:
            note("warning",
                 f"Reusable workflows nested deeper than "
                 f"{MAX_REUSABLE_DEPTH} levels — {uses} not fetched "
                 "(GitHub rejects such nesting)")
            return
        m = _REMOTE_USES_RE.match(uses)
        if m is None:
            return
        other = f"{m.group('owner')}/{m.group('repo')}"
        other_ref = m.group("ref")
        path = m.group("path").lstrip("/")
        prefix = f"{EXTERNAL_DIR}/{slugify(other)}@{slugify(other_ref)}"
        dest = posixpath.normpath(posixpath.join(prefix, path))
        if dest.startswith("..") or not dest.startswith(prefix + "/"):
            note("warning", f"Unsafe reusable-workflow path skipped: {uses}")
            return
        if dest in files:
            external_map[key] = dest
            return
        try:
            content = client.get_raw_file(other, path, other_ref)
        except GitHubError as e:
            note("warning",
                 f"Cannot fetch reusable workflow {uses}: {e} — its jobs "
                 "appear as a ghost where called")
            return
        log.info("Fetched %s@%s:%s (%d bytes) -> %s", other, other_ref,
                 path, len(content), dest)
        files[dest] = FetchedFile(rel_path=dest, content=content,
                                  origin="project",
                                  source=f"{other}@{other_ref}:{path}")
        external_map[key] = dest
        for nested in extract_job_uses(content):
            if nested.startswith("./"):
                # a local call inside the OTHER repo — canonicalize it to
                # that repo/ref so the parser can resolve it too
                fetch_reusable(f"{other}/{nested[2:]}@{other_ref}",
                               depth + 1)
            elif _REMOTE_USES_RE.match(nested):
                fetch_reusable(nested, depth + 1)

    for fetched in list(files.values()):
        if fetched.origin != "root":
            continue
        for uses in extract_job_uses(fetched.content):
            if _REMOTE_USES_RE.match(uses) and not uses.startswith("./"):
                fetch_reusable(uses, depth=1)

    return FetchResult(
        strategy="files",
        host=getattr(client, "base_url", ""),
        project=repo,
        ref=ref,
        root_rel=WORKFLOWS_DIR,
        files=list(files.values()),
        external_map=external_map,
        local_root_prefixes=[],
        lint=None,
        notes=notes,
    )


def make_github_resolver(workdir: str,
                         external_map: dict[str, str]) -> Callable[[str], Any]:
    """The external_resolver `parse_github` consumes: one cross-repo uses
    string → the materialized absolute path, or None (the parser ghosts it
    honestly)."""
    import os

    def resolve(uses: str) -> str | None:
        dest = external_map.get(uses_key(uses))
        if dest is None:
            return None
        return os.path.join(workdir, *dest.split("/"))

    return resolve
