"""Minimal GitLab REST client — stdlib only.

The whole remote feature speaks through this one class so tests can swap in
a fake with the same surface. Network access happens nowhere else in
pipeview except `fetch.py`'s `include:remote` download, which reuses this
module's opener so TLS options apply uniformly.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

USER_AGENT = "pipeview-gitlab"

# One page is one screen-plus of TUI; GitLab caps per_page at 100.
PER_PAGE = 50


class GitLabError(Exception):
    """Any API failure. `status` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GitLabAuthError(GitLabError):
    """401 — token missing/expired/insufficient."""


class GitLabForbidden(GitLabError):
    """403 — authenticated but not allowed."""


class GitLabNotFound(GitLabError):
    """404 — also what GitLab returns for objects the token cannot see."""


def _error_for(status: int, message: str) -> GitLabError:
    if status == 401:
        return GitLabAuthError(message, status)
    if status == 403:
        return GitLabForbidden(message, status)
    if status == 404:
        return GitLabNotFound(message, status)
    return GitLabError(message, status)


def build_ssl_context(ca_bundle: str | None = None, insecure: bool = False) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    # Corporate proxies/CAs: honor the bundles other tooling already uses.
    bundle = ca_bundle or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    return ssl.create_default_context(cafile=bundle or None)


def encode_project(path_or_id: str | int) -> str:
    """GitLab accepts a numeric id or a URL-encoded full path for :id."""
    s = str(path_or_id)
    if s.isdigit():
        return s
    return urllib.parse.quote(s, safe="")


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        ca_bundle: str | None = None,
        insecure: bool = False,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl = build_ssl_context(ca_bundle, insecure)

    # -- transport ----------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None,
                 raw: bool = False) -> tuple[Any, dict[str, str]]:
        """GET `path` (API-relative or absolute URL). Returns (body, headers).
        Body is parsed JSON unless `raw`, then it is the decoded text."""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}/api/v4/{path.lstrip('/')}"
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)

        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                payload = json.loads(e.read().decode("utf-8", errors="replace"))
                detail = payload.get("message") or payload.get("error") or ""
                if isinstance(detail, (dict, list)):
                    detail = json.dumps(detail)
            except Exception:
                pass
            msg = f"GitLab API {e.code} for {url.split('?')[0]}"
            if detail:
                msg += f": {detail}"
            raise _error_for(e.code, msg) from None
        except urllib.error.URLError as e:
            raise GitLabError(f"Cannot reach {self.base_url}: {e.reason}") from None

        if raw:
            return body, headers
        try:
            return json.loads(body) if body else None, headers
        except json.JSONDecodeError:
            raise GitLabError(f"Non-JSON response from {url.split('?')[0]}") from None

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if self.token:
            # PATs, project/group access tokens, and CI job tokens all work
            # as PRIVATE-TOKEN; OAuth tokens (glpat- prefix absent, gl-oauth
            # style) also accept it on modern GitLab, but Bearer is the
            # documented header for them.
            if self.token.startswith("oauth:"):
                h["Authorization"] = "Bearer " + self.token[len("oauth:"):]
            else:
                h["PRIVATE-TOKEN"] = self.token
        return h

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _ = self._request(path, params)
        return body

    def get_page(self, path: str, params: dict[str, Any] | None = None
                 ) -> tuple[list, int | None]:
        """One page of a list endpoint. Returns (items, next_page or None)."""
        body, headers = self._request(path, params)
        nxt = headers.get("x-next-page") or ""
        return (body or []), (int(nxt) if nxt.isdigit() else None)

    def iter_all(self, path: str, params: dict[str, Any] | None = None,
                 max_pages: int = 20) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        page: int | None = 1
        for _ in range(max_pages):
            if page is None:
                return
            params["page"] = page
            items, page = self.get_page(path, params)
            yield from items

    # -- endpoints ----------------------------------------------------------

    def current_user(self) -> dict:
        return self.get_json("user")

    def version(self) -> dict:
        return self.get_json("version")

    def list_projects(self, search: str | None = None, page: int = 1,
                      per_page: int = PER_PAGE, membership: bool = True
                      ) -> tuple[list[dict], int | None]:
        return self.get_page("projects", {
            "membership": "true" if membership else None,
            "search": search or None,
            "order_by": "last_activity_at",
            "simple": "true",
            "per_page": per_page,
            "page": page,
        })

    def get_project(self, path_or_id: str | int) -> dict:
        return self.get_json(f"projects/{encode_project(path_or_id)}")

    def list_branches(self, path_or_id: str | int, search: str | None = None,
                      page: int = 1, per_page: int = PER_PAGE) -> tuple[list[dict], int | None]:
        return self.get_page(
            f"projects/{encode_project(path_or_id)}/repository/branches",
            {"search": search or None, "per_page": per_page, "page": page},
        )

    def list_tags(self, path_or_id: str | int, page: int = 1,
                  per_page: int = PER_PAGE) -> tuple[list[dict], int | None]:
        return self.get_page(
            f"projects/{encode_project(path_or_id)}/repository/tags",
            {"per_page": per_page, "page": page},
        )

    def iter_tree(self, path_or_id: str | int, ref: str,
                  max_pages: int = 20) -> Iterator[dict]:
        """Repository file listing (recursive) — used only to expand
        wildcard include:local patterns, capped so a monorepo can't stall
        the fetch."""
        yield from self.iter_all(
            f"projects/{encode_project(path_or_id)}/repository/tree",
            {"ref": ref, "recursive": "true", "per_page": 100},
            max_pages=max_pages,
        )

    def get_raw_file(self, path_or_id: str | int, file_path: str, ref: str) -> str:
        enc = urllib.parse.quote(file_path.lstrip("/"), safe="")
        body, _ = self._request(
            f"projects/{encode_project(path_or_id)}/repository/files/{enc}/raw",
            {"ref": ref},
            raw=True,
        )
        return body

    def ci_lint(self, path_or_id: str | int, ref: str | None = None,
                include_jobs: bool = False) -> dict:
        """Project-scoped CI Lint: returns GitLab's own resolution of the
        config — `merged_yaml` has every include expanded server-side.

        Sends both the ≥16.10 (`content_ref`) and legacy (`sha`) parameter
        spellings; Grape ignores whichever one the instance doesn't declare.
        """
        return self.get_json(f"projects/{encode_project(path_or_id)}/ci/lint", {
            "content_ref": ref or None,
            "sha": ref or None,
            "include_jobs": "true" if include_jobs else None,
        })

    def get_ci_template(self, name: str) -> dict:
        """Instance-level CI template, e.g. name='Jobs/SAST'."""
        return self.get_json(
            "templates/gitlab_ci_ymls/" + urllib.parse.quote(name, safe="")
        )

    def get_url_raw(self, url: str) -> str:
        """Plain HTTPS fetch through the same opener (for include:remote)."""
        body, _ = self._request(url, raw=True)
        return body
