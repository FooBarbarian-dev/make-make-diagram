"""Minimal GitHub REST client — stdlib only.

The whole remote feature speaks through this one class so tests can swap
in a fake with the same surface — the GitHub twin of
``pipeview.gitlab.api``. Network access happens nowhere else in the
GitHub feature.

Repository dicts are returned in a GitLab-compatible shape
(``path_with_namespace``/``default_branch``/``last_activity_at``/
``web_url`` filled in beside the raw GitHub fields) so the shared curses
browser (`pipeview.gitlab.tui`) drives either provider unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

USER_AGENT = "pipeview-github"

# One page is one screen-plus of TUI; GitHub caps per_page at 100.
PER_PAGE = 50

_API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """Any API failure. `status` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GitHubAuthError(GitHubError):
    """401 — token missing/expired/insufficient."""


class GitHubForbidden(GitHubError):
    """403 — authenticated but not allowed (or rate limited)."""


class GitHubNotFound(GitHubError):
    """404 — also what GitHub returns for repos the token cannot see."""


def _error_for(status: int, message: str) -> GitHubError:
    if status == 401:
        return GitHubAuthError(message, status)
    if status == 403:
        return GitHubForbidden(message, status)
    if status == 404:
        return GitHubNotFound(message, status)
    return GitHubError(message, status)


def build_ssl_context(ca_bundle: str | None = None,
                      insecure: bool = False) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    bundle = ca_bundle or os.environ.get("REQUESTS_CA_BUNDLE") \
        or os.environ.get("CURL_CA_BUNDLE")
    return ssl.create_default_context(cafile=bundle or None)


def api_root(host: str) -> str:
    """The REST root for a host: github.com uses api.github.com, a GitHub
    Enterprise Server serves the API under /api/v3."""
    host = host.rstrip("/")
    parsed = urllib.parse.urlparse(host)
    if parsed.hostname in ("github.com", "www.github.com"):
        return "https://api.github.com"
    return f"{host}/api/v3"


def _map_repo(raw: dict) -> dict:
    """Add the GitLab-shaped keys the shared TUI and report layer read."""
    out = dict(raw)
    out.setdefault("path_with_namespace", raw.get("full_name") or "")
    out.setdefault("last_activity_at",
                   raw.get("pushed_at") or raw.get("updated_at") or "")
    out.setdefault("web_url", raw.get("html_url") or "")
    return out


class GitHubClient:
    def __init__(
        self,
        base_url: str = "https://github.com",
        token: str | None = None,
        *,
        ca_bundle: str | None = None,
        insecure: bool = False,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = api_root(self.base_url)
        self.token = token
        self.timeout = timeout
        self._ssl = build_ssl_context(ca_bundle, insecure)

    # -- transport ----------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None,
                 raw: bool = False, accept: str | None = None
                 ) -> tuple[Any, dict[str, str]]:
        """GET `path` (API-relative or absolute URL). Returns (body, headers).
        Body is parsed JSON unless `raw`, then it is the decoded text."""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.api_url}/{path.lstrip('/')}"
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") \
                    + urllib.parse.urlencode(clean)

        req = urllib.request.Request(url, headers=self._headers(accept))
        log.debug("GET %s", url)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ssl) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                payload = json.loads(e.read().decode("utf-8",
                                                     errors="replace"))
                detail = payload.get("message") or ""
                errors = payload.get("errors")
                if errors:
                    detail += f" {json.dumps(errors)}"
            except Exception:
                pass
            log.debug("GET %s -> HTTP %d in %.0f ms%s", url, e.code,
                      (time.monotonic() - started) * 1000,
                      f": {detail}" if detail else "")
            msg = f"GitHub API {e.code} for {url.split('?')[0]}"
            if detail:
                msg += f": {detail}"
            raise _error_for(e.code, msg) from None
        except urllib.error.URLError as e:
            log.debug("GET %s -> unreachable: %s", url, e.reason)
            raise GitHubError(
                f"Cannot reach {self.api_url}: {e.reason}") from None

        log.debug("GET %s -> %d bytes in %.0f ms%s", url, len(body),
                  (time.monotonic() - started) * 1000,
                  " (more pages)" if 'rel="next"' in headers.get("link", "")
                  else "")

        if raw:
            return body, headers
        try:
            return json.loads(body) if body else None, headers
        except json.JSONDecodeError:
            raise GitHubError(
                f"Non-JSON response from {url.split('?')[0]}") from None

    def _headers(self, accept: str | None = None) -> dict[str, str]:
        h = {
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _ = self._request(path, params)
        return body

    def get_page(self, path: str, params: dict[str, Any] | None = None,
                 page: int = 1) -> tuple[list, int | None]:
        """One page of a list endpoint. GitHub paginates via the Link
        header; the caller thinks in page numbers, like the GitLab client.
        Returns (items, next_page or None)."""
        params = dict(params or {})
        params["page"] = page
        body, headers = self._request(path, params)
        has_next = 'rel="next"' in (headers.get("link") or "")
        return (body or []), (page + 1 if has_next else None)

    # -- endpoints ----------------------------------------------------------

    def current_user(self) -> dict:
        return self.get_json("user")

    def list_repos(self, search: str | None = None, page: int = 1,
                   per_page: int = PER_PAGE) -> tuple[list[dict], int | None]:
        """Repositories the token can access, most recently pushed first.
        `search` filters client-side on the full name (the affiliated-repos
        endpoint has no server-side search)."""
        items, nxt = self.get_page("user/repos", {
            "per_page": per_page,
            "sort": "pushed",
            "direction": "desc",
        }, page=page)
        repos = [_map_repo(r) for r in items]
        if search:
            needle = search.lower()
            repos = [r for r in repos
                     if needle in (r.get("full_name") or "").lower()]
        return repos, nxt

    # GitLab-compatible aliases so the shared TUI drives this client.
    def list_projects(self, search: str | None = None, page: int = 1,
                      per_page: int = PER_PAGE, membership: bool = True
                      ) -> tuple[list[dict], int | None]:
        return self.list_repos(search=search, page=page, per_page=per_page)

    def get_repo(self, full_name: str) -> dict:
        return _map_repo(self.get_json(f"repos/{full_name}"))

    def get_project(self, full_name: str) -> dict:
        return self.get_repo(str(full_name))

    def list_branches(self, full_name: str, search: str | None = None,
                      page: int = 1, per_page: int = PER_PAGE
                      ) -> tuple[list[dict], int | None]:
        items, nxt = self.get_page(f"repos/{full_name}/branches",
                                   {"per_page": per_page}, page=page)
        if search:
            needle = search.lower()
            items = [b for b in items
                     if needle in (b.get("name") or "").lower()]
        return items, nxt

    def list_tags(self, full_name: str, page: int = 1,
                  per_page: int = PER_PAGE) -> tuple[list[dict], int | None]:
        return self.get_page(f"repos/{full_name}/tags",
                             {"per_page": per_page}, page=page)

    def list_dir(self, full_name: str, dir_path: str, ref: str) -> list[dict]:
        """Directory listing via the contents API: [{name, path, type}]."""
        enc = urllib.parse.quote(dir_path.strip("/"))
        body = self.get_json(f"repos/{full_name}/contents/{enc}",
                             {"ref": ref})
        if isinstance(body, dict):   # a file, not a directory
            return [body]
        return body or []

    def get_raw_file(self, full_name: str, file_path: str, ref: str) -> str:
        enc = urllib.parse.quote(file_path.lstrip("/"))
        body, _ = self._request(
            f"repos/{full_name}/contents/{enc}",
            {"ref": ref},
            raw=True,
            accept="application/vnd.github.raw+json",
        )
        return body
