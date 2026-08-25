"""Fetch a project's CI configuration from GitLab.

Two strategies (see docs/superpowers/specs/2026-08-25-gitlab-remote-fetch-design.md):

- "lint": one call to the project-scoped CI Lint endpoint
  (GET /projects/:id/ci/lint), whose `merged_yaml` is the complete
  configuration with every include — cross-repo ones included — expanded
  server-side. No repository traversal needed.
- "files": fetch the root CI file and walk `include:` recursively across
  repositories via the repository-files API. Used when the lint endpoint is
  unavailable (old instance, restricted permissions) or when the user wants
  real per-file line numbers.

Both produce a FetchResult that `materialize()` writes into a work
directory; from there the ordinary offline parser takes over.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable

import yaml

from pipeview import gitlab_templates
from pipeview.gitlab.api import (
    GitLabError,
    GitLabForbidden,
    GitLabNotFound,
)

log = logging.getLogger(__name__)

# GitLab's own documented ceiling is 150 includes per pipeline.
MAX_FILES = 150

EXTERNAL_DIR = "_external"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Filesystem-safe single path segment."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-.")
    return out or "x"


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that swallows unknown tags (!reference et al) — this
    loader only ever hunts for the `include:` key."""


def _tolerant_unknown(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TolerantLoader.add_multi_constructor("!", _tolerant_unknown)


def _extract_includes(text: str) -> list[dict]:
    """The raw `include:` entries of a config file, normalized to dicts.
    Parse errors return [] — the real parser reports them properly later."""
    try:
        data = yaml.load(text, Loader=_TolerantLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    inc = data.get("include")
    if inc is None:
        return []
    if isinstance(inc, (str, dict)):
        inc = [inc]
    if not isinstance(inc, list):
        return []
    out: list[dict] = []
    for entry in inc:
        if isinstance(entry, str):
            out.append({"local": entry})
        elif isinstance(entry, dict):
            out.append(entry)
    return out


def include_keys(inc: dict) -> list[str]:
    """Canonical identity strings for a non-local include entry. The fetch
    traversal and the parse-time resolver both compute these, so they must
    agree byte-for-byte; refs are keyed as *written* (empty when omitted)."""
    if "project" in inc:
        proj = str(inc["project"])
        ref = str(inc.get("ref") or "")
        files = inc.get("file")
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            return []
        return [f"project:{proj}@{ref}:{str(f).lstrip('/')}" for f in files]
    if "template" in inc:
        return [f"template:{inc['template']}"]
    if "remote" in inc:
        return [f"remote:{inc['remote']}"]
    if "component" in inc:
        return [f"component:{inc['component']}"]
    return []


# Template subdirectories whose files the REST template API serves under the
# bare basename ("Security/SAST.gitlab-ci.yml" -> key "SAST"). Everything
# else in a subdirectory (Jobs/*, Workflows/*, …) the API cannot serve at
# all, under any spelling — verified against gitlab.com; that is what the
# bundled-snapshot fallback exists for.
_FLATTENED_TEMPLATE_DIRS = ("Pages", "Verify", "Security")


def template_api_keys(name: str) -> list[str]:
    """API key candidates for `include:template` name, most specific first:
    the suffix-stripped name, the category-flattened basename where the API
    flattens it, then the raw name (harmless, and what older pipeview sent)."""
    stem = name
    for suffix in (".gitlab-ci.yml", ".yml"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    candidates = [stem]
    first, _, rest = stem.partition("/")
    if rest and first in _FLATTENED_TEMPLATE_DIRS and "/" not in rest:
        candidates.append(rest)
    candidates.append(name)
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


_WILDCARD_CHARS = set("*?[")


def _wildcard_regex(pattern: str) -> re.Pattern:
    """GitLab include:local globbing: `*` stays inside a directory,
    `**` crosses directories."""
    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class FetchedFile:
    rel_path: str   # destination inside the work directory (posix separators)
    content: str
    origin: str     # "root" | "local" | "project" | "template" | "remote"
                    # | "component" | "merged"
    source: str     # human provenance, e.g. "group/lib@main:ci/build.yml"


@dataclass
class FetchResult:
    strategy: str            # "lint" | "files"
    host: str
    project: dict            # project attributes as GitLab returned them
    ref: str
    root_rel: str            # rel path of the root file within the workdir
    files: list[FetchedFile] = field(default_factory=list)
    # include key (include_keys) -> rel destination; consumed by the
    # parse-time resolver. Empty for the lint strategy (nothing to resolve —
    # merged_yaml has no includes left).
    external_map: dict[str, str] = field(default_factory=dict)
    # rel dirs that are repository roots of *other* projects; include:local
    # under them resolves there (parse_gitlab local_roots).
    local_root_prefixes: list[str] = field(default_factory=list)
    lint: dict | None = None          # raw CI Lint response when available
    notes: list[tuple[str, str]] = field(default_factory=list)  # (severity, msg)


# ---------------------------------------------------------------------------
# Strategy driver
# ---------------------------------------------------------------------------

def fetch_config(client, project: dict, ref: str, strategy: str = "auto",
                 bundled_templates: bool = True) -> FetchResult:
    """Fetch the CI configuration of `project` at `ref`.

    strategy: "auto" (lint, falling back to files), "lint", or "files".
    bundled_templates: let the files strategy fall back to pipeview's
    bundled snapshot of GitLab's built-in templates when the instance's
    template API cannot serve one (it cannot serve most of them).
    """
    if strategy not in ("auto", "lint", "files"):
        raise ValueError(f"Unknown strategy: {strategy}")

    notes: list[tuple[str, str]] = []
    project_id = project.get("path_with_namespace") or project.get("id")
    lint: dict | None = None

    def _add_note(severity: str, message: str) -> None:
        (log.warning if severity in ("warning", "error") else log.info)("%s", message)
        notes.append((severity, message))

    log.info("Fetching CI config of %s@%s (strategy=%s)", project_id, ref, strategy)
    if strategy in ("auto", "lint"):
        try:
            log.info("Asking the CI Lint API for the merged configuration")
            lint = client.ci_lint(project_id, ref)
        except (GitLabNotFound, GitLabForbidden) as e:
            if strategy == "lint":
                raise GitLabError(f"CI Lint endpoint unavailable: {e}") from None
            _add_note(
                "info",
                f"CI Lint endpoint unavailable ({e}) — falling back to "
                "file-by-file fetching",
            )
        except GitLabError as e:
            if strategy == "lint":
                raise
            _add_note(
                "warning",
                f"CI Lint call failed ({e}) — falling back to file-by-file fetching",
            )

    if lint is not None and lint.get("merged_yaml"):
        log.info(
            "CI Lint returned the merged configuration (%d bytes, valid=%s, "
            "%d include(s) recorded)",
            len(lint["merged_yaml"]), lint.get("valid"),
            len(lint.get("includes") or []),
        )
        return _result_from_lint(client, project, ref, lint, notes)

    if strategy == "lint":
        errs = "; ".join((lint or {}).get("errors") or []) or "no merged_yaml in response"
        raise GitLabError(f"CI Lint returned no merged configuration: {errs}")

    if lint is not None:
        errs = (lint.get("errors") or [])[:3]
        detail = f" (GitLab says: {'; '.join(str(e) for e in errs)})" if errs else ""
        _add_note(
            "info",
            "CI Lint returned no merged configuration"
            f"{detail} — falling back to file-by-file fetching",
        )

    fetcher = _FilesFetcher(client, project, ref, notes,
                            bundled_templates=bundled_templates)
    return fetcher.run(lint)


def _result_from_lint(client, project: dict, ref: str, lint: dict,
                      notes: list[tuple[str, str]]) -> FetchResult:
    proj_path = project.get("path_with_namespace") or str(project.get("id"))
    merged = lint["merged_yaml"]
    if not merged.endswith("\n"):
        merged += "\n"
    root = FetchedFile(
        rel_path=".gitlab-ci.yml",
        content=merged,
        origin="merged",
        source=f"{proj_path}@{ref} — merged by GitLab CI Lint (all includes expanded)",
    )
    return FetchResult(
        strategy="lint",
        host=getattr(client, "base_url", ""),
        project=project,
        ref=ref,
        root_rel=".gitlab-ci.yml",
        files=[root],
        lint=lint,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Files strategy — recursive include traversal across repositories
# ---------------------------------------------------------------------------

class _FilesFetcher:
    def __init__(self, client, project: dict, ref: str,
                 notes: list[tuple[str, str]], bundled_templates: bool = True):
        self.client = client
        self.project = project
        self.ref = ref
        self.notes = notes
        self.bundled_templates = bundled_templates
        self.main_path = project.get("path_with_namespace") or str(project.get("id"))
        self.files: dict[str, FetchedFile] = {}
        self.external_map: dict[str, str] = {}
        self.local_root_prefixes: set[str] = set()
        self.seen_keys: set[str] = set()
        self.proj_cache: dict[str, dict | None] = {self.main_path: project}
        self.tree_cache: dict[tuple[str, str], list[str] | None] = {}
        self.truncated = False

    # -- plumbing -----------------------------------------------------------

    def _note(self, severity: str, message: str) -> None:
        # Notes end up as report diagnostics; mirror them into the log so
        # -v shows them the moment they happen, with the fetch context.
        (log.warning if severity in ("warning", "error") else log.info)("%s", message)
        self.notes.append((severity, message))

    def _full(self) -> bool:
        if len(self.files) >= MAX_FILES:
            if not self.truncated:
                self.truncated = True
                self._note("warning", (
                    f"Stopped after fetching {MAX_FILES} files (GitLab's own "
                    "include ceiling) — the include tree may be incomplete"
                ))
            return True
        return False

    def _get_project(self, path: str) -> dict | None:
        if path not in self.proj_cache:
            try:
                self.proj_cache[path] = self.client.get_project(path)
            except GitLabError as e:
                self.proj_cache[path] = None
                self._note("warning", f"Cannot look up project {path}: {e}")
        return self.proj_cache[path]

    def _tree(self, project_path: str, ref: str) -> list[str] | None:
        key = (project_path, ref)
        if key not in self.tree_cache:
            try:
                self.tree_cache[key] = [
                    e["path"] for e in self.client.iter_tree(project_path, ref)
                    if e.get("type") == "blob"
                ]
            except GitLabError as e:
                self.tree_cache[key] = None
                self._note("warning",
                           f"Cannot list files of {project_path}@{ref}: {e}")
        return self.tree_cache[key]

    @staticmethod
    def _dest(prefix: str, path: str) -> str | None:
        dest = posixpath.normpath(posixpath.join(prefix, path.lstrip("/")))
        base = posixpath.normpath(prefix) if prefix else ""
        if dest.startswith("..") or (base and not dest.startswith(base + "/")):
            return None   # a path traversal escape — refuse to materialize
        return dest

    # -- entry point --------------------------------------------------------

    def run(self, lint: dict | None) -> FetchResult:
        root_rel = self._seed_root()
        if root_rel is None:
            raise GitLabError(
                f"Could not fetch the CI configuration of {self.main_path}@{self.ref}"
            )
        return FetchResult(
            strategy="files",
            host=getattr(self.client, "base_url", ""),
            project=self.project,
            ref=self.ref,
            root_rel=root_rel,
            files=list(self.files.values()),
            external_map=self.external_map,
            local_root_prefixes=sorted(self.local_root_prefixes),
            lint=lint,
            notes=self.notes,
        )

    def _seed_root(self) -> str | None:
        # ci_config_path: "", "path/file.yml", "file.yml@group/project", or a URL.
        ci_path = (self.project.get("ci_config_path") or "").strip() or ".gitlab-ci.yml"

        if ci_path.startswith(("http://", "https://")):
            dest = f"{EXTERNAL_DIR}/remote/root.yml"
            try:
                content = self.client.get_url_raw(ci_path)
            except GitLabError as e:
                self._note("error", f"Cannot fetch remote CI config {ci_path}: {e}")
                return None
            self.files[dest] = FetchedFile(dest, content, "root", ci_path)
            self._note("info", (
                f"CI configuration is remote ({ci_path}) — include:local "
                "entries inside it cannot be resolved"
            ))
            self._walk(content, None, None, prefix=f"{EXTERNAL_DIR}/remote",
                       allow_local=False, src=ci_path)
            return dest

        if "@" in ci_path:
            file_part, proj_part = ci_path.split("@", 1)
            target = self._get_project(proj_part)
            if target is None:
                return None
            tgt_ref = target.get("default_branch") or "main"
            prefix = f"{EXTERNAL_DIR}/{slugify(proj_part)}@{slugify(tgt_ref)}"
            dest = self._dest(prefix, file_part)
            if dest is None:
                self._note("error", f"Unsafe ci_config_path: {ci_path}")
                return None
            self.local_root_prefixes.add(prefix)
            self._note("info", (
                f"CI configuration lives in {proj_part} "
                f"(ci_config_path={ci_path})"
            ))
            ok = self._repo_file(proj_part, tgt_ref, file_part.lstrip("/"), dest,
                                 origin="root", prefix=prefix)
            return dest if ok else None

        dest = self._dest("", ci_path)
        if dest is None:
            self._note("error", f"Unsafe ci_config_path: {ci_path}")
            return None
        ok = self._repo_file(self.main_path, self.ref, ci_path.lstrip("/"), dest,
                             origin="root", prefix="")
        return dest if ok else None

    # -- fetch + walk -------------------------------------------------------

    def _repo_file(self, project_path: str, ref: str, file_path: str, dest: str,
                   origin: str, prefix: str, key: str | None = None) -> bool:
        if dest in self.files:
            if key:
                self.external_map[key] = dest
            return True
        if self._full():
            return False
        src = f"{project_path}@{ref}:{file_path}"
        try:
            content = self.client.get_raw_file(project_path, file_path, ref)
        except GitLabError as e:
            self._note("warning", f"Cannot fetch {src}: {e}")
            return False
        log.info("Fetched %s (%s, %d bytes) -> %s", src, origin, len(content), dest)
        self.files[dest] = FetchedFile(dest, content, origin, src)
        if key:
            self.external_map[key] = dest
        self._walk(content, project_path, ref, prefix, allow_local=True, src=src)
        return True

    def _walk(self, content: str, ctx_project: str | None, ctx_ref: str | None,
              prefix: str, allow_local: bool, src: str) -> None:
        for inc in _extract_includes(content):
            if "local" in inc:
                if not allow_local or ctx_project is None:
                    self._note("warning", (
                        f"include:local {inc['local']!r} inside {src} has no "
                        "repository context — skipped"
                    ))
                    continue
                self._local(inc, ctx_project, ctx_ref or "HEAD", prefix)
            elif "project" in inc:
                self._project(inc, src)
            elif "template" in inc:
                self._template(inc, src)
            elif "remote" in inc:
                self._remote(inc, src)
            elif "component" in inc:
                self._component(inc, src)

    def _local(self, inc: dict, ctx_project: str, ctx_ref: str, prefix: str) -> None:
        pattern = str(inc["local"]).lstrip("/")
        if _WILDCARD_CHARS & set(pattern):
            tree = self._tree(ctx_project, ctx_ref)
            if tree is None:
                return
            rx = _wildcard_regex(pattern)
            matches = sorted(p for p in tree if rx.match(p))
            if not matches:
                return  # the parser reports empty wildcards itself
        else:
            matches = [pattern]
        origin = "local" if prefix == "" else "project"
        for path in matches:
            dest = self._dest(prefix, path)
            if dest is None:
                self._note("warning", f"Unsafe include path skipped: {path}")
                continue
            self._repo_file(ctx_project, ctx_ref, path, dest, origin, prefix)

    def _project(self, inc: dict, src: str) -> None:
        keys = include_keys(inc)
        if not keys:
            self._note("warning",
                       f"include:project without file: in {src} — skipped")
            return
        proj_path = str(inc["project"])
        files = inc.get("file")
        if isinstance(files, str):
            files = [files]
        written_ref = inc.get("ref")
        if written_ref is not None:
            actual_ref = str(written_ref)
        else:
            target = self._get_project(proj_path)
            if target is None:
                return
            actual_ref = target.get("default_branch") or "main"
        prefix = f"{EXTERNAL_DIR}/{slugify(proj_path)}@{slugify(actual_ref)}"
        for key, file_path in zip(keys, [str(f) for f in files]):
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)
            dest = self._dest(prefix, file_path)
            if dest is None:
                self._note("warning", f"Unsafe include path skipped: {file_path}")
                continue
            if self._repo_file(proj_path, actual_ref, file_path.lstrip("/"),
                               dest, "project", prefix, key=key):
                self.local_root_prefixes.add(prefix)

    def _template(self, inc: dict, src: str) -> None:
        name = str(inc["template"])
        key = f"template:{name}"
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        if self._full():
            return
        # The instance API first — its copy matches the instance's GitLab
        # version and covers custom instance templates.
        content: str | None = None
        source = f"template:{name}"
        for candidate in template_api_keys(name):
            try:
                tpl = self.client.get_ci_template(candidate)
                content = tpl.get("content") if isinstance(tpl, dict) else None
                if content:
                    log.info("Template %s served by the instance API (key %r)",
                             name, candidate)
                    break
            except GitLabError:
                continue
        if not content and self.bundled_templates:
            # The API cannot serve most subdirectory templates (Jobs/*,
            # Workflows/*) on ANY GitLab version — fall back to the snapshot
            # pipeview ships.
            bundled = gitlab_templates.template_path(name)
            if bundled is not None:
                try:
                    with open(bundled, encoding="utf-8") as f:
                        content = f.read()
                except OSError as e:   # a broken install shouldn't kill the fetch
                    log.warning("Cannot read bundled template %s: %s", bundled, e)
                    content = None
            if content:
                source = (f"template:{name} (pipeview's bundled "
                          f"{gitlab_templates.bundled_version()})")
                self._note("info", (
                    f"Template {name} is not served by the instance's template "
                    f"API — using pipeview's bundled copy "
                    f"({gitlab_templates.bundled_version()}); the instance's "
                    "own version may differ"
                ))
        if not content:
            detail = (f" and not in pipeview's bundled "
                      f"{gitlab_templates.bundled_version()}"
                      if self.bundled_templates else "")
            self._note("warning", (
                f"Cannot fetch template {name} (from {src}) — not served by "
                f"the instance's template API{detail}"
            ))
            return
        dest = self._dest(f"{EXTERNAL_DIR}/templates", name)
        if dest is None:
            self._note("warning", f"Unsafe template name skipped: {name}")
            return
        if not dest.endswith((".yml", ".yaml")):
            dest += ".yml"
        log.info("Fetched template %s (%d bytes) -> %s", name, len(content), dest)
        self.files[dest] = FetchedFile(dest, content, "template", source)
        self.external_map[key] = dest
        self._walk(content, None, None, prefix=f"{EXTERNAL_DIR}/templates",
                   allow_local=False, src=f"template:{name}")

    def _remote(self, inc: dict, src: str) -> None:
        url = str(inc["remote"])
        key = f"remote:{url}"
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        if self._full():
            return
        try:
            content = self.client.get_url_raw(url)
        except GitLabError as e:
            self._note("warning", f"Cannot fetch remote include {url}: {e}")
            return
        digest = hashlib.sha1(url.encode()).hexdigest()[:12]
        base = slugify(posixpath.basename(url.split("?")[0]) or "remote.yml")
        if not base.endswith((".yml", ".yaml")):
            base += ".yml"
        dest = f"{EXTERNAL_DIR}/remote/{digest}-{base}"
        log.info("Fetched remote include %s (%d bytes) -> %s", url, len(content), dest)
        self.files[dest] = FetchedFile(dest, content, "remote", url)
        self.external_map[key] = dest
        self._walk(content, None, None, prefix=f"{EXTERNAL_DIR}/remote",
                   allow_local=False, src=url)

    def _component(self, inc: dict, src: str) -> None:
        address = str(inc["component"])
        key = f"component:{address}"
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        parsed = _parse_component(address)
        if parsed is None:
            self._note("warning", f"Cannot parse component address {address}")
            return
        host, proj_path, name, version = parsed
        own_host = getattr(self.client, "base_url", "")
        own_netloc = own_host.split("://", 1)[-1].rstrip("/")
        if own_netloc and host != own_netloc:
            self._note("warning", (
                f"Component {address} lives on {host}, not {own_netloc} — "
                "cross-instance components are not fetched"
            ))
            return
        actual_ref = version
        if version == "~latest":
            try:
                tags, _ = self.client.list_tags(proj_path, per_page=1)
                actual_ref = tags[0]["name"] if tags else ""
            except GitLabError:
                actual_ref = ""
            if not actual_ref:
                target = self._get_project(proj_path)
                actual_ref = (target or {}).get("default_branch") or "main"
            self._note("info", (
                f"Component {address}: ~latest resolved to {actual_ref} "
                "(newest tag or default branch — releases API not consulted)"
            ))
        prefix = f"{EXTERNAL_DIR}/components/{slugify(proj_path)}@{slugify(actual_ref)}"
        for candidate in (f"templates/{name}.yml", f"templates/{name}/template.yml"):
            dest = self._dest(prefix, candidate)
            if dest is None:
                continue
            if self._repo_file(proj_path, actual_ref, candidate, dest,
                               "component", prefix, key=key):
                self.local_root_prefixes.add(prefix)
                return
        self._note("warning", (
            f"Component {address}: no templates/{name}.yml or "
            f"templates/{name}/template.yml at {proj_path}@{actual_ref}"
        ))


def _parse_component(address: str) -> tuple[str, str, str, str] | None:
    """'gitlab.example.com/group/comps/name@1.0' ->
    (host, 'group/comps', 'name', '1.0')."""
    addr = address.split("://", 1)[-1]
    if "@" not in addr:
        return None
    path_part, version = addr.rsplit("@", 1)
    segments = [s for s in path_part.split("/") if s]
    if len(segments) < 3:
        return None
    return segments[0], "/".join(segments[1:-1]), segments[-1], version


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def materialize(result: FetchResult, workdir: str
                ) -> tuple[str, Callable[[dict], list[str] | None] | None, list[str]]:
    """Write the fetched files under `workdir`. Returns
    (root_abs_path, external_resolver_or_None, local_roots_abs)."""
    workdir = os.path.abspath(workdir)
    log.info("Materializing %d fetched file(s) under %s", len(result.files), workdir)
    for ff in result.files:
        path = os.path.join(workdir, *ff.rel_path.split("/"))
        log.debug("write %s (%d bytes, origin=%s, source=%s)",
                  ff.rel_path, len(ff.content), ff.origin, ff.source)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(ff.content)

    root_abs = os.path.join(workdir, *result.root_rel.split("/"))
    resolver = None
    if result.external_map:
        resolver = make_resolver(workdir, dict(result.external_map))
    roots = [os.path.join(workdir, *p.split("/"))
             for p in result.local_root_prefixes]
    return root_abs, resolver, roots


def make_resolver(workdir: str, external_map: dict[str, str]
                  ) -> Callable[[dict], list[str] | None]:
    """The parse_gitlab external_resolver over a materialized FetchResult."""
    def resolve(inc: dict) -> list[str] | None:
        keys = include_keys(inc)
        if not keys:
            return None
        paths = []
        for key in keys:
            rel = external_map.get(key)
            if rel is None:
                return None   # unknown include — the parser ghosts it honestly
            paths.append(os.path.join(workdir, *rel.split("/")))
        return paths
    return resolve
