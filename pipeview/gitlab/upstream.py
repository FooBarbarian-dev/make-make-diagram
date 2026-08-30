"""Resolve cross-repository includes via the checkout's git upstream.

`pipeview <path> --upstream` analyzes the *local* working tree — real
line numbers, uncommitted edits included — but uses the repository's own
git remote to answer "where do the other pipeline files live?":
the remote URL names the GitLab host and this project's path, and every
non-local include (project/template/remote/component) is fetched from
that host exactly like the `files` strategy does.

Network access happens only here, before the parse step, and only when
`--upstream` was explicitly given — the offline guarantee for plain
`pipeview <path>` runs is untouched.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from pipeview.gitlab import auth as auth_mod
from pipeview.gitlab.api import GitLabClient, GitLabError
from pipeview.gitlab.config import GitLabConfig
from pipeview.gitlab.fetch import fetch_local_externals, materialize, slugify
from pipeview.model import Diagnostic

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 10  # seconds per git invocation


class UpstreamError(Exception):
    """Upstream detection failed; the message says why and what to do."""


@dataclass
class Upstream:
    host: str           # API base URL, e.g. https://gitlab.example.com
    project_path: str   # group/sub/app
    remote_name: str    # git remote the URL came from
    url: str            # the remote URL, userinfo/credentials stripped
    toplevel: str       # absolute path of the repository root
    branch: str         # current branch name, or "HEAD" when detached


@dataclass
class UpstreamResolution:
    """What `--upstream` produced for one GitLab CI root — always usable:
    on any failure `resolver` is None and `diagnostics` says why."""
    resolver: Callable[[dict], list[str] | None] | None = None
    local_roots: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    annotation: dict | None = None
    repo_root: str | None = None


# ---------------------------------------------------------------------------
# Remote URL parsing (pure)
# ---------------------------------------------------------------------------

# scp-style ssh: [user@]host:path — no scheme, host part has no slashes.
_SCP_RE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:]+):(?P<path>[^:]+)$")


def strip_userinfo(url: str) -> str:
    """The URL without any user[:password]@ part. Token-in-URL remotes are
    common in CI checkouts, and Upstream.url ends up in shareable report
    artifacts and diagnostics — never carry credentials there. (scp-style
    URLs keep their ssh user: that syntax cannot carry a password.)"""
    if "://" not in url:
        return url
    scheme, sep, rest = url.partition("://")
    host_part, slash, path = rest.partition("/")
    return f"{scheme}{sep}{host_part.rpartition('@')[2]}{slash}{path}"


def parse_remote_url(url: str) -> tuple[str, str] | None:
    """(api_base_url, project_path) from a git remote URL, or None.

    ssh/scp/git URLs assume the API at https://<host> (the ssh port is
    not the API port); http(s) URLs keep their scheme and port.
    """
    url = url.strip()
    if not url:
        return None

    if "://" in url:
        scheme, _, rest = url.partition("://")
        scheme = scheme.lower()
        if scheme not in ("ssh", "git", "http", "https"):
            return None
        host_part, _, path = rest.partition("/")
        host_part = host_part.rpartition("@")[2]        # strip userinfo
        if scheme in ("ssh", "git"):
            host_part = host_part.partition(":")[0]     # drop the ssh port
            base = f"https://{host_part}"
        else:
            base = f"{scheme}://{host_part}"
        if not host_part:
            return None
    else:
        m = _SCP_RE.match(url)
        if not m:
            return None
        base = f"https://{m.group('host')}"
        path = m.group("path")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    # A GitLab project path is at least namespace/project.
    if path.count("/") < 1 or not all(path.split("/")):
        return None
    return base, path


# ---------------------------------------------------------------------------
# Detection (runs git)
# ---------------------------------------------------------------------------

def _git(repo_dir: str, *args: str) -> str | None:
    """stdout of `git -C repo_dir <args>`, or None on a non-zero exit.
    Raises UpstreamError when git itself cannot run."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise UpstreamError(
            "git is not installed (or not on PATH) — cannot detect the "
            "repository's upstream"
        ) from None
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpstreamError(f"git failed: {e}") from None
    if proc.returncode != 0:
        log.debug("git %s exited %d: %s", " ".join(args), proc.returncode,
                  proc.stderr.strip())
        return None
    return proc.stdout.strip()


def detect_upstream(repo_dir: str, remote: str | None = None) -> Upstream:
    """The GitLab host+project the checkout at `repo_dir` points at.

    Remote selection: the explicit `remote` argument, else the current
    branch's tracking remote, else `origin`, else the sole remote.
    Raises UpstreamError with an actionable message on every failure.
    """
    toplevel = _git(repo_dir, "rev-parse", "--show-toplevel")
    if not toplevel:
        raise UpstreamError(f"{repo_dir} is not inside a git repository")

    remotes = (_git(repo_dir, "remote") or "").split()
    if not remotes:
        raise UpstreamError(
            "the repository has no git remotes — nothing to use as the "
            "upstream reference"
        )

    if remote:
        if remote not in remotes:
            raise UpstreamError(
                f"no git remote named {remote!r} (repository has: "
                f"{', '.join(remotes)})"
            )
        chosen = remote
    else:
        tracking = _git(repo_dir, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{upstream}")
        if tracking and "/" in tracking and tracking.split("/", 1)[0] in remotes:
            chosen = tracking.split("/", 1)[0]
        elif "origin" in remotes:
            chosen = "origin"
        elif len(remotes) == 1:
            chosen = remotes[0]
        else:
            raise UpstreamError(
                "the current branch tracks no remote and there are several "
                f"remotes ({', '.join(remotes)}) — pass --upstream-remote"
            )

    url = _git(repo_dir, "remote", "get-url", chosen)
    if not url:
        raise UpstreamError(f"cannot read the URL of remote {chosen!r}")

    parsed = parse_remote_url(url)
    if parsed is None:
        raise UpstreamError(
            f"cannot infer a GitLab host from remote {chosen!r} "
            f"({strip_userinfo(url)!r})"
        )
    host, project_path = parsed

    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    return Upstream(host=host, project_path=project_path, remote_name=chosen,
                    url=strip_userinfo(url), toplevel=toplevel, branch=branch)


# ---------------------------------------------------------------------------
# Orchestration for the CLI
# ---------------------------------------------------------------------------

def resolve_upstream_includes(
    root_path: str,
    outdir: str,
    *,
    remote: str | None = None,
    token: str | None = None,
    ca_bundle: str | None = None,
    insecure: bool = False,
    timeout: float = 30.0,
    bundled_templates: bool = True,
    config: GitLabConfig | None = None,
) -> UpstreamResolution:
    """Detect the upstream, fetch the externally-included files, and hand
    back parse_gitlab hooks. Never raises: every failure mode degrades to
    a warning diagnostic and the report renders with ghosts."""
    res = UpstreamResolution()
    repo_dir = os.path.dirname(os.path.abspath(root_path))

    try:
        upstream = detect_upstream(repo_dir, remote)
    except UpstreamError as e:
        res.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"--upstream: {e} — cross-repository includes stay "
                    "unresolved",
        ))
        return res

    res.repo_root = upstream.toplevel
    res.annotation = {
        "host": upstream.host,
        "project": upstream.project_path,
        "remote": upstream.remote_name,
        "url": upstream.url,
        "branch": upstream.branch,
    }
    log.info("Upstream: remote %r -> %s on %s (branch %s)",
             upstream.remote_name, upstream.project_path, upstream.host,
             upstream.branch)

    if config is None:
        config = GitLabConfig(os.environ.get("PIPEVIEW_GITLAB_CONFIG") or None)
    resolved_token, source = auth_mod.resolve_token(upstream.host, token, config)
    if resolved_token is None:
        res.diagnostics.append(Diagnostic(
            severity="warning",
            message=(
                f"--upstream: no API token for {upstream.host} — "
                "cross-repository includes stay unresolved. Export "
                "PIPEVIEW_GITLAB_TOKEN (or GITLAB_TOKEN), pass --token, or "
                f"run `pipeview gitlab auth --host {upstream.host}` once"
            ),
        ))
        return res
    log.info("Using API token from %s", source)

    client = GitLabClient(upstream.host, resolved_token, ca_bundle=ca_bundle,
                          insecure=insecure, timeout=timeout)

    try:
        project = client.get_project(upstream.project_path)
    except GitLabError as e:
        # The included projects may still be reachable — degrade, don't stop.
        res.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"--upstream: cannot look up {upstream.project_path} on "
                    f"{upstream.host}: {e}",
        ))
        project = {"path_with_namespace": upstream.project_path}

    try:
        result = fetch_local_externals(
            client, project, upstream.branch,
            repo_root=upstream.toplevel, root_file=root_path,
            bundled_templates=bundled_templates,
        )
    except GitLabError as e:
        res.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"--upstream: fetching includes from {upstream.host} "
                    f"failed: {e}",
        ))
        return res

    for severity, message in result.notes:
        res.diagnostics.append(Diagnostic(severity=severity, message=message))

    if result.files:
        workdir = os.path.join(
            os.path.abspath(outdir), "fetched",
            f"{slugify(upstream.project_path)}@upstream",
        )
        _, res.resolver, res.local_roots = materialize(result, workdir)
        res.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Cross-repository includes resolved via upstream "
                f"{upstream.remote_name!r} ({upstream.project_path} on "
                f"{upstream.host}): {len(result.files)} file(s) fetched "
                f"under {workdir}"
            ),
        ))
    else:
        res.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Upstream {upstream.remote_name!r} "
                f"({upstream.project_path} on {upstream.host}): no "
                "cross-repository includes to fetch"
            ),
        ))
    return res
