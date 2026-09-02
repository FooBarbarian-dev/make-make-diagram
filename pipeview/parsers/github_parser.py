"""GitHub Actions workflow parser.

Parses a repository's ``.github/workflows/`` tree (or a single workflow
file) into the same normalized model the GitLab and Make parsers emit —
the renderer never asks which CI system a report came from.

Structural mapping, chosen to mirror the GitLab report as closely as the
two systems allow:

- One report covers ALL workflows of a repository (an event can trigger
  several workflows at once — the What-If tab shows them side by side the
  way it shows GitLab's candidate pipelines).
- Job node ids are namespaced by workflow file: ``ci.yml::build``. Each
  workflow becomes a collapsible group in the Graph view via the same
  ``annotations["child_pipeline"]`` key GitLab child pipelines use.
- ``needs:`` becomes ``needs`` edges. There are no stages: the DAG is the
  whole ordering story.
- A job calling a reusable workflow (``uses:``) mirrors a GitLab trigger
  job: local reusable workflows are parsed into the report and linked with
  an ``invokes`` edge; cross-repository ones become ghosts with a typed
  ``uses_info``/``trigger_info`` record the rollup can resolve.

Failure philosophy matches the other parsers: one bad value degrades one
value with a diagnostic, never the file, never the report.
"""

from __future__ import annotations

import os
import re
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

from pipeview.model import (
    Diagnostic,
    Edge,
    Node,
    Report,
    SourceFile,
    SourceLocation,
    Variable,
    VariableEvent,
)
from pipeview.parsers.github_predefined import (
    CONTEXT_FIELD_DOCS,
    PREDEFINED_VAR_DOCS,
)
from pipeview.parsers.github_whatif import compile_github_whatif

# Top-level keys of a workflow file (GitHub rejects anything else).
_WORKFLOW_KEYS = frozenset({
    "name", "run-name", "on", "permissions", "env", "defaults",
    "concurrency", "jobs",
})

# Keys a job mapping may carry (GitHub rejects unknown keys).
_JOB_KEYS = frozenset({
    "name", "permissions", "needs", "if", "runs-on", "environment",
    "concurrency", "outputs", "env", "defaults", "steps", "timeout-minutes",
    "strategy", "continue-on-error", "container", "services", "uses",
    "with", "secrets",
})

# GitHub's documented job-id rule: start with a letter or _, then
# alphanumeric, - or _ only.
_JOB_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# Shell-style references in run: scripts ($VAR / ${VAR}).
_SHELL_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# ${{ env.X }} / vars.X / secrets.X / inputs.X / matrix.X references.
_CTX_VAR_RE = re.compile(
    r"\$\{\{\s*(env|vars|secrets|inputs|matrix)\.([A-Za-z_][A-Za-z0-9_-]*)"
)

_PREDEFINED_VAR_RE = re.compile(r"^(GITHUB|RUNNER)_|^CI$")

# GitHub's own ceilings.
_MAX_MATRIX_COMBOS = 256
_MAX_REUSABLE_DEPTH = 4

_WORKFLOW_FILE_RE = re.compile(r"\.ya?ml$")

# jobs.<id>.uses: owner/repo/.github/workflows/name.yml@ref  (remote)
#                 ./.github/workflows/name.yml               (local)
_REMOTE_USES_RE = re.compile(
    r"^(?P<owner>[^/@\s]+)/(?P<repo>[^/@\s]+)/(?P<path>[^@]+)@(?P<ref>.+)$"
)


class _PipeviewGithubLoader(SafeLoader):
    """SafeLoader subclass that detects duplicate mapping keys (PyYAML keeps
    the last silently — data loss wearing a valid-YAML costume)."""

    def __init__(self, stream):
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int]] = []

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen: set = set()
            for key_node, _ in node.value:
                try:
                    key = self.construct_object(key_node, deep=deep)
                except yaml.YAMLError:
                    continue
                if isinstance(key, (str, int, float, bool)) or key is None:
                    if key in seen:
                        self.duplicate_keys.append(
                            (str(key), key_node.start_mark.line + 1)
                        )
                    seen.add(key)
        return super().construct_mapping(node, deep)


def _key_str(key: Any) -> str:
    """Normalize a YAML key to the string GitHub would use. Unquoted ``on:``
    arrives as Python True under YAML 1.1 — the single most important case."""
    if key is True:
        return "on"      # YAML 1.1 reads unquoted `on` as a boolean
    if key is False:
        return "off"
    return str(key)


def _scalar_str(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_github(
    path: str,
    *,
    repo_root: str | None = None,
    external_resolver=None,
) -> Report:
    """Parse GitHub Actions workflows rooted at `path`.

    `path` may be a repository directory (containing ``.github/workflows``),
    the workflows directory itself, or a single workflow file.

    - `repo_root`: the repository root ``paths:`` filters and local
      ``uses:`` references resolve against; derived from the workflows
      directory when omitted.
    - `external_resolver`: callable(uses_ref: str) -> local file path (or
      None) for a cross-repository reusable workflow that has been
      materialized on disk by the remote-fetch layer (`pipeview.github`);
      offline behavior is unchanged when omitted — such calls ghost.
    """
    abs_path = os.path.abspath(path)
    workflow_files: list[str] = []
    workflows_dir: str | None = None

    if os.path.isdir(abs_path):
        candidate = abs_path
        nested = os.path.join(abs_path, ".github", "workflows")
        if os.path.isdir(nested):
            candidate = nested
        workflows_dir = candidate
        try:
            entries = sorted(os.listdir(candidate))
        except OSError as e:
            return Report(
                root=path, format="github_actions",
                diagnostics=[Diagnostic(
                    severity="error",
                    message=f"Cannot read workflows directory {path}: {e}",
                )],
            )
        for name in entries:
            full = os.path.join(candidate, name)
            if os.path.isfile(full) and _WORKFLOW_FILE_RE.search(name):
                workflow_files.append(full)
        if not workflow_files:
            return Report(
                root=path, format="github_actions",
                diagnostics=[Diagnostic(
                    severity="error",
                    message=f"No workflow files (*.yml) in {path}",
                )],
            )
    else:
        workflows_dir = os.path.dirname(abs_path)
        workflow_files = [abs_path]

    if repo_root is None:
        # <repo>/.github/workflows → <repo>; anything else: the dir itself.
        parent = os.path.dirname(workflows_dir)
        if (os.path.basename(workflows_dir) == "workflows"
                and os.path.basename(parent) == ".github"):
            repo_root = os.path.dirname(parent)
        else:
            repo_root = workflows_dir

    state = _ParserState(workflows_dir, repo_root)
    state.external_resolver = external_resolver

    for wf_path in workflow_files:
        _parse_workflow_file(wf_path, state, namespace=state.rel(wf_path))

    # Resolve reusable-workflow calls after every directly-discovered
    # workflow is parsed (a caller may precede its callee alphabetically).
    _resolve_reusable_calls(state)
    _build_jobs(state)
    _label_predefined_variables(state)
    whatif = compile_github_whatif(state)

    report = Report(
        root=path,
        format="github_actions",
        nodes=list(state.nodes.values()),
        edges=state.edges,
        variables=list(state.variables.values()),
        files=state.files,
        diagnostics=state.diagnostics,
        default_goal=None,
    )
    report.annotations["whatif"] = whatif
    report.annotations["workflows"] = [
        state.workflows[k]["summary"] for k in state.workflows
    ]
    docs = dict(PREDEFINED_VAR_DOCS)
    docs.update(CONTEXT_FIELD_DOCS)
    report.annotations["predefined_var_docs"] = docs
    return report


class _ParserState:
    def __init__(self, workflows_dir: str, repo_root: str):
        self.workflows_dir = os.path.abspath(workflows_dir)
        self.repo_root = os.path.abspath(repo_root)
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.variables: dict[str, Variable] = {}
        self.files: list[SourceFile] = []
        self.diagnostics: list[Diagnostic] = []
        self.external_resolver = None
        # (abs dir, display prefix) for files parsed from outside the
        # workflows dir (remote reusable workflows materialized on disk).
        self.path_aliases: list[tuple[str, str]] = []
        self.parsed_files: set[str] = set()
        # namespace → per-workflow record:
        #   {"file", "name", "on": raw, "env": {}, "raw": data, "summary": {},
        #    "invalid": [msgs], "concurrency", "defaults", "reusable": bool}
        self.workflows: dict[str, dict[str, Any]] = {}
        # job_id ("ns::key") → raw job config
        self.job_configs: dict[str, dict[str, Any]] = {}
        # job_id → (rel_path, line, doc, namespace)
        self.job_meta: dict[str, tuple[str, int, str | None, str]] = {}
        # rel_path → (top_lines, nested_lines) from one compose pass
        self.file_maps: dict[str, tuple[dict, dict]] = {}
        # pending remote reusable calls: (job_id, uses_str, rel, line)
        self.pending_remote_uses: list[tuple[str, str, str, int]] = []
        self.reusable_depth: dict[str, int] = {}

    def _get_or_create_variable(self, name: str) -> Variable:
        if name not in self.variables:
            self.variables[name] = Variable(name=name)
        return self.variables[name]

    def rel(self, path: str) -> str:
        """Display/reference path of a workflow file: its name relative to
        the workflows directory (``ci.yml``) — this doubles as the job-id
        namespace, mirroring how GitLab child-pipeline paths do. Files that
        live elsewhere (materialized cross-repo reusables) display under a
        registered alias instead of a ../../ relpath."""
        fp = os.path.abspath(path)
        for prefix, display in self.path_aliases:
            if fp == prefix or fp.startswith(prefix + os.sep):
                sub = os.path.relpath(fp, prefix).replace(os.sep, "/")
                return f"{display} {sub}"
        return os.path.relpath(fp, self.workflows_dir).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# File-level parsing
# ---------------------------------------------------------------------------

def _load_yaml(raw_text: str):
    """Returns (data, duplicate_keys, extra_docs, error)."""
    loader = _PipeviewGithubLoader(raw_text)
    try:
        data = loader.get_data() if loader.check_data() else None
        extra_docs = loader.check_data()
    except yaml.YAMLError as e:
        return None, [], False, e
    finally:
        dup = list(loader.duplicate_keys)
        loader.dispose()
    return data, dup, extra_docs, None


def _build_file_maps(raw_text: str) -> tuple[dict, dict]:
    """One compose pass: top-level key → line, and (top key, nested key) →
    line (job keys, env entries, ...)."""
    top: dict[str, int] = {}
    nested: dict[tuple[str, str], int] = {}
    try:
        root = yaml.compose(raw_text, SafeLoader)
    except yaml.YAMLError:
        return top, nested
    if not isinstance(root, yaml.MappingNode):
        return top, nested
    for key_node, val_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        # compose keeps the raw scalar text, so unquoted `on:` is already
        # the string "on" here — no YAML 1.1 bool coercion to undo.
        kname = key_node.value
        top.setdefault(kname, key_node.start_mark.line + 1)
        if isinstance(val_node, yaml.MappingNode):
            for k2, v2 in val_node.value:
                if isinstance(k2, yaml.ScalarNode):
                    nested.setdefault((kname, k2.value),
                                      k2.start_mark.line + 1)
                    if isinstance(v2, yaml.MappingNode):
                        for k3, _v3 in v2.value:
                            if isinstance(k3, yaml.ScalarNode):
                                nested.setdefault(
                                    (f"{kname}.{k2.value}", k3.value),
                                    k3.start_mark.line + 1,
                                )
    return top, nested


def _parse_workflow_file(filepath: str, state: _ParserState,
                         namespace: str, depth: int = 0) -> None:
    filepath = os.path.abspath(filepath)
    if filepath in state.parsed_files:
        return
    state.parsed_files.add(filepath)

    rel_path = state.rel(filepath)
    sf = SourceFile(path=rel_path, kind="github_yaml", status="ok")

    if not os.path.isfile(filepath):
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="warning", message=f"File not found: {rel_path}")
        )
        return

    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            raw_text = f.read()
    except OSError as e:
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="error", message=f"Cannot read {rel_path}: {e}")
        )
        return

    data, duplicate_keys, extra_docs, err = _load_yaml(raw_text)

    if err is not None:
        sf.status = "error"
        state.files.append(sf)
        mark = getattr(err, "problem_mark", None)
        line = (mark.line + 1) if mark else 1
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=f"YAML parse error in {rel_path}: {err}",
            source=SourceLocation(file=rel_path, line=line),
        ))
        return

    if extra_docs:
        sf.status = "error"
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=(
                f"{rel_path} contains multiple YAML documents — GitHub reads "
                "a single document per workflow file; only the first is shown"
            ),
            source=SourceLocation(file=rel_path, line=1),
        ))

    state.files.append(sf)

    if data is None:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=(
                f"{rel_path} is empty — GitHub reports it as an invalid "
                "workflow and never runs it"
            ),
            source=SourceLocation(file=rel_path, line=1),
        ))
        return

    if not isinstance(data, dict):
        sf.status = "error"
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=(
                f"Top level of {rel_path} is not a mapping "
                f"(got {type(data).__name__}) — GitHub rejects the workflow"
            ),
            source=SourceLocation(file=rel_path, line=1),
        ))
        return

    for key, line in duplicate_keys:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=(
                f"Duplicate key '{key}' — YAML silently keeps the last "
                "definition and drops the earlier one"
            ),
            source=SourceLocation(file=rel_path, line=line),
        ))

    top_lines, nested_lines = _build_file_maps(raw_text)
    state.file_maps[rel_path] = (top_lines, nested_lines)
    raw_lines = raw_text.splitlines()

    data = {_key_str(k): v for k, v in data.items()}

    wf: dict[str, Any] = {
        "file": rel_path,
        "name": str(data["name"]) if isinstance(data.get("name"), str) else None,
        "on": None,
        "env": {},
        "raw": data,
        "invalid": [],
        "concurrency": data.get("concurrency"),
        "defaults": data.get("defaults"),
        "reusable": False,
        "depth": depth,
    }
    state.workflows[namespace] = wf

    for key in data:
        if key not in _WORKFLOW_KEYS:
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"Unknown top-level key '{key}' in {rel_path} — GitHub "
                    "rejects workflow files with unexpected keys"
                ),
                source=SourceLocation(file=rel_path, line=top_lines.get(key, 1)),
            ))

    # on: — normalize the three spellings (scalar, list, mapping)
    if "on" not in data:
        wf["invalid"].append("no 'on:' trigger — the workflow can never run")
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=(
                f"{rel_path} has no 'on:' trigger — GitHub marks the "
                "workflow invalid and it never runs"
            ),
            source=SourceLocation(file=rel_path, line=1),
        ))
    else:
        wf["on"] = _normalize_on(data["on"], wf, state, rel_path,
                                 top_lines.get("on", 1))
        wf["reusable"] = (
            isinstance(wf["on"], dict)
            and set(wf["on"]) == {"workflow_call"}
        )

    if isinstance(data.get("env"), dict):
        wf["env"] = {_key_str(k): _scalar_str(v)
                     for k, v in data["env"].items()}
        _record_env_events(data["env"], "global", rel_path, "env",
                           nested_lines, top_lines.get("env", 1), state,
                           extra_annotations={"workflow": namespace})

    # workflow_dispatch / workflow_call inputs → Variables-tab entries
    if isinstance(wf["on"], dict):
        for trig in ("workflow_dispatch", "workflow_call"):
            cfg = wf["on"].get(trig)
            if isinstance(cfg, dict) and isinstance(cfg.get("inputs"), dict):
                _record_inputs(cfg["inputs"], trig, namespace, rel_path,
                               top_lines.get("on", 1), state)

    jobs = data.get("jobs")
    if "jobs" not in data or not isinstance(jobs, dict) or not jobs:
        wf["invalid"].append("no jobs defined")
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=(
                f"{rel_path} defines no jobs — GitHub rejects a workflow "
                "without at least one job"
            ),
            source=SourceLocation(file=rel_path,
                                  line=top_lines.get("jobs", 1)),
        ))
        jobs = {}

    for job_key_raw, job_val in jobs.items():
        job_key = _key_str(job_key_raw)
        line_no = nested_lines.get(("jobs", job_key),
                                   top_lines.get("jobs", 1))
        if not _JOB_ID_RE.match(job_key):
            wf["invalid"].append(f"invalid job id '{job_key}'")
            state.diagnostics.append(Diagnostic(
                severity="error",
                message=(
                    f"Job id '{job_key}' in {rel_path} is invalid — ids "
                    "must start with a letter or _ and contain only "
                    "alphanumeric characters, - or _ (GitHub rejects the "
                    "workflow)"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
            ))
        if not isinstance(job_val, dict):
            wf["invalid"].append(f"job '{job_key}' is not a mapping")
            state.diagnostics.append(Diagnostic(
                severity="error",
                message=(
                    f"Job '{job_key}' in {rel_path} is not a mapping — "
                    "GitHub rejects the workflow"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
            ))
            continue
        doc = _extract_doc_above(raw_lines, line_no)
        job_id = f"{namespace}::{job_key}"
        state.job_configs[job_id] = {_key_str(k): v for k, v in job_val.items()}
        state.job_meta[job_id] = (rel_path, line_no, doc, namespace)

    wf["summary"] = {
        "file": rel_path,
        "name": wf["name"],
        "reusable": wf["reusable"],
        "triggers": _on_summary(wf["on"]),
        "invalid": list(wf["invalid"]),
    }


# ---------------------------------------------------------------------------
# on: normalization
# ---------------------------------------------------------------------------

_FILTER_PAIRS = (
    ("branches", "branches-ignore"),
    ("tags", "tags-ignore"),
    ("paths", "paths-ignore"),
)


def _normalize_on(on_value: Any, wf: dict, state: _ParserState,
                  rel_path: str, line_no: int) -> dict[str, dict]:
    """Normalize on: into {event_name: config_dict}. Filter values become
    lists of strings; schedule becomes {"crons": [...]}."""
    loc = SourceLocation(file=rel_path, line=line_no)
    out: dict[str, dict] = {}

    if isinstance(on_value, str):
        out[_key_str(on_value)] = {}
        return out
    if isinstance(on_value, list):
        for ev in on_value:
            out[_key_str(ev)] = {}
        return out
    if not isinstance(on_value, dict):
        state.diagnostics.append(Diagnostic(
            severity="error",
            message=(
                f"'on:' in {rel_path} is neither an event name, a list, nor "
                "a mapping — GitHub rejects the workflow"
            ),
            source=loc,
        ))
        wf["invalid"].append("malformed 'on:'")
        return out

    for ev_raw, cfg in on_value.items():
        ev = _key_str(ev_raw)
        if ev == "schedule":
            crons: list[str] = []
            for entry in _as_list(cfg):
                if isinstance(entry, dict) and "cron" in entry:
                    cron = str(entry["cron"])
                    crons.append(cron)
                    if len(cron.split()) != 5:
                        state.diagnostics.append(Diagnostic(
                            severity="warning",
                            message=(
                                f"Schedule cron '{cron}' in {rel_path} does "
                                "not have 5 fields — GitHub rejects it"
                            ),
                            source=loc,
                        ))
            out[ev] = {"crons": crons}
            continue
        if cfg is None:
            out[ev] = {}
            continue
        if not isinstance(cfg, dict):
            out[ev] = {}
            continue
        norm: dict[str, Any] = {}
        cfg = {_key_str(k): v for k, v in cfg.items()}
        for pos_key, neg_key in _FILTER_PAIRS:
            if pos_key in cfg and neg_key in cfg:
                msg = (
                    f"on: {ev} uses both {pos_key} and {neg_key} — GitHub "
                    "rejects the workflow (use one, with ! patterns for "
                    "exclusions)"
                )
                wf["invalid"].append(msg)
                state.diagnostics.append(Diagnostic(
                    severity="error", message=f"{rel_path}: {msg}", source=loc,
                ))
        for fkey in ("branches", "branches-ignore", "tags", "tags-ignore",
                     "paths", "paths-ignore"):
            if fkey in cfg:
                norm[fkey.replace("-", "_")] = [
                    _scalar_str(p) for p in _as_list(cfg[fkey])
                ]
        if "types" in cfg:
            norm["types"] = [_scalar_str(t) for t in _as_list(cfg["types"])]
        if ev == "workflow_run" and "workflows" in cfg:
            norm["workflows"] = [_scalar_str(w)
                                 for w in _as_list(cfg["workflows"])]
        if ev in ("workflow_dispatch", "workflow_call") \
                and isinstance(cfg.get("inputs"), dict):
            inputs: dict[str, dict] = {}
            for iname, ispec in cfg["inputs"].items():
                spec = ispec if isinstance(ispec, dict) else {}
                inputs[_key_str(iname)] = {
                    "default": _scalar_str(spec["default"])
                    if "default" in spec else None,
                    "type": _scalar_str(spec.get("type", "string")),
                    "required": bool(spec.get("required", False)),
                    "options": [_scalar_str(o)
                                for o in _as_list(spec.get("options"))]
                    or None,
                    "description": _scalar_str(spec["description"])
                    if "description" in spec else None,
                }
            norm["inputs"] = inputs
        out[ev] = norm

    # push filters on a tag-only event etc. are user knowledge, not errors.
    return out


def _on_summary(on: dict | None) -> list[str]:
    if not isinstance(on, dict):
        return []
    parts = []
    for ev, cfg in on.items():
        bits = []
        if isinstance(cfg, dict):
            for key in ("branches", "branches_ignore", "tags", "tags_ignore",
                        "paths", "paths_ignore", "types", "workflows"):
                if cfg.get(key):
                    bits.append(f"{key.replace('_', '-')}: "
                                + ", ".join(cfg[key]))
            if cfg.get("crons"):
                bits.append(", ".join(cfg["crons"]))
        parts.append(ev + (f" ({'; '.join(bits)})" if bits else ""))
    return parts


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def _record_env_events(
    env_dict: dict, scope: str, rel_path: str, owner_key: str,
    nested_lines: dict, fallback_line: int, state: _ParserState,
    extra_annotations: dict | None = None,
) -> None:
    operator = ("workflow" if scope == "global"
                else "step" if (extra_annotations or {}).get("step")
                else "job")
    for vname_raw, vval in env_dict.items():
        vname = _key_str(vname_raw)
        var = state._get_or_create_variable(vname)
        line_no = nested_lines.get((owner_key, vname), fallback_line)
        var.events.append(VariableEvent(
            source=SourceLocation(file=rel_path, line=line_no),
            operator=operator,
            scope=scope,
            raw_value=_scalar_str(vval),
            annotations=dict(extra_annotations or {}),
        ))


def _record_inputs(inputs: dict, trigger: str, namespace: str,
                   rel_path: str, line_no: int, state: _ParserState) -> None:
    for iname_raw, ispec in inputs.items():
        iname = _key_str(iname_raw)
        spec = ispec if isinstance(ispec, dict) else {}
        annotations: dict[str, Any] = {"workflow": namespace,
                                       "input_of": trigger}
        if spec.get("description"):
            annotations["description"] = _scalar_str(spec["description"])
        if spec.get("type"):
            annotations["type"] = _scalar_str(spec["type"])
        if spec.get("required"):
            annotations["required"] = True
        var = state._get_or_create_variable(f"inputs.{iname}")
        var.events.append(VariableEvent(
            source=SourceLocation(file=rel_path, line=line_no),
            operator="input",
            scope="global",
            raw_value=_scalar_str(spec.get("default", "")),
            annotations=annotations,
        ))


def _label_predefined_variables(state: _ParserState) -> None:
    for var in state.variables.values():
        if var.events:
            continue
        if _PREDEFINED_VAR_RE.match(var.name):
            var.origin = "predefined"
        elif var.name.startswith("secrets."):
            var.origin = "secret"
        elif var.name.startswith("vars."):
            var.origin = "repository variable"


# ---------------------------------------------------------------------------
# Reusable-workflow calls (jobs.<id>.uses)
# ---------------------------------------------------------------------------

def _resolve_reusable_calls(state: _ParserState) -> None:
    """Local reusable workflows are parsed into the report (they live in the
    same directory, so they usually already are); cross-repository ones
    resolve via the remote-fetch layer or ghost. Nested calls (a reusable
    workflow calling another) are followed to GitHub's own depth limit."""
    for _ in range(_MAX_REUSABLE_DEPTH + 1):
        new_files = False
        for job_id, config in list(state.job_configs.items()):
            uses = config.get("uses")
            if not isinstance(uses, str) or job_id in state.reusable_depth:
                continue
            rel_path, line_no, _, namespace = state.job_meta[job_id]
            depth = state.workflows.get(namespace, {}).get("depth", 0)
            state.reusable_depth[job_id] = depth
            if depth >= _MAX_REUSABLE_DEPTH:
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=(
                        f"Job '{job_id}' calls a reusable workflow more than "
                        f"{_MAX_REUSABLE_DEPTH} levels deep — GitHub rejects "
                        "nesting past this limit"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))
                continue
            if uses.startswith("./"):
                if _REMOTE_USES_RE.match(namespace):
                    # a local call inside a workflow materialized from
                    # ANOTHER repository resolves in that repository —
                    # canonicalize it and retry as a remote call
                    m_ns = _REMOTE_USES_RE.match(namespace)
                    canonical = (f"{m_ns.group('owner')}/{m_ns.group('repo')}"
                                 f"/{uses[2:]}@{m_ns.group('ref')}")
                    config["uses"] = canonical
                    del state.reusable_depth[job_id]
                    new_files = True
                    continue
                local_rel = uses[2:]
                abs_path = os.path.abspath(
                    os.path.join(state.repo_root, local_rel))
                if os.path.isfile(abs_path):
                    if abs_path not in state.parsed_files:
                        _parse_workflow_file(abs_path, state,
                                             namespace=state.rel(abs_path),
                                             depth=depth + 1)
                        new_files = True
                else:
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            f"Job '{job_id}' uses reusable workflow "
                            f"'{uses}' which does not exist in the "
                            "repository"
                        ),
                        source=SourceLocation(file=rel_path, line=line_no),
                        related_node=job_id,
                    ))
            elif _REMOTE_USES_RE.match(uses):
                resolved = None
                if state.external_resolver is not None:
                    try:
                        resolved = state.external_resolver(uses)
                    except Exception:
                        resolved = None
                if resolved and os.path.isfile(resolved):
                    m = _REMOTE_USES_RE.match(uses)
                    alias_ns = (f"{m.group('owner')}/{m.group('repo')}/"
                                f"{os.path.basename(m.group('path'))}"
                                f"@{m.group('ref')}")
                    alias = (os.path.dirname(os.path.abspath(resolved)),
                             f"[{m.group('owner')}/{m.group('repo')}@{m.group('ref')}]")
                    if alias not in state.path_aliases:
                        state.path_aliases.append(alias)
                    if os.path.abspath(resolved) not in state.parsed_files:
                        _parse_workflow_file(resolved, state,
                                             namespace=alias_ns,
                                             depth=depth + 1)
                        new_files = True
                    config["__resolved_uses_ns__"] = alias_ns
            elif not uses.startswith("docker://"):
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=(
                        f"Job '{job_id}' uses '{uses}' which is neither a "
                        "local path (./…) nor owner/repo/path@ref — GitHub "
                        "rejects the workflow"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))
        if not new_files:
            return


# ---------------------------------------------------------------------------
# Job → node construction
# ---------------------------------------------------------------------------

def _steps_to_recipe(steps: Any, job_id: str, rel_path: str, line_no: int,
                     state: _ParserState) -> tuple[list[str], list[str]]:
    """Recipe lines plus the list of action refs used. GitHub semantics:
    each step is either uses: or run: (both → invalid)."""
    lines: list[str] = []
    actions: list[str] = []
    if not isinstance(steps, list):
        return lines, actions
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        step = {_key_str(k): v for k, v in step.items()}
        name = step.get("name")
        if "uses" in step and "run" in step:
            state.diagnostics.append(Diagnostic(
                severity="error",
                message=(
                    f"Step {i} of '{job_id}' has both uses: and run: — "
                    "GitHub rejects the workflow"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
                related_node=job_id,
            ))
        if isinstance(name, str):
            lines.append(f"[step] {name}")
        if "uses" in step:
            ref = _scalar_str(step["uses"])
            actions.append(ref)
            lines.append(f"[uses] {ref}")
            if isinstance(step.get("with"), dict):
                pairs = ", ".join(
                    f"{_key_str(k)}: {_scalar_str(v)}"
                    for k, v in step["with"].items()
                )
                lines.append(f"[with] {pairs}")
            if ref.startswith("./"):
                target = os.path.join(state.repo_root, ref[2:])
                if not (os.path.isdir(target) or os.path.isfile(target)):
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            f"Step {i} of '{job_id}' uses local action "
                            f"'{ref}' which does not exist in the repository"
                        ),
                        source=SourceLocation(file=rel_path, line=line_no),
                        related_node=job_id,
                    ))
            elif "@" not in ref and not ref.startswith("docker://"):
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=(
                        f"Step {i} of '{job_id}' uses '{ref}' without a "
                        "version (@ref) — GitHub requires one for "
                        "repository actions"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))
        elif "run" in step:
            run_val = step["run"]
            for run_line in _scalar_str(run_val).splitlines():
                if run_line.strip():
                    lines.append(f"[run] {run_line}")
        elif "uses" not in step:
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"Step {i} of '{job_id}' has neither uses: nor run: — "
                    "GitHub rejects the workflow"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
                related_node=job_id,
            ))
        if "if" in step:
            lines.append(f"[if] {_scalar_str(step['if'])}")
    return lines, actions


def _expand_matrix(strategy: Any) -> tuple[dict | None, dict[str, list[str]]]:
    """strategy.matrix → (whatif matrix config, axes for the Variables tab).
    GitHub's include/exclude algorithm, capped at the documented 256
    combinations. A matrix built from an expression is honestly dynamic."""
    if not isinstance(strategy, dict):
        return None, {}
    matrix = strategy.get("matrix")
    if matrix is None:
        return None, {}
    if isinstance(matrix, str):
        return {"kind": "dynamic", "raw": matrix}, {}
    if not isinstance(matrix, dict):
        return None, {}

    matrix = {_key_str(k): v for k, v in matrix.items()}
    include = [e for e in _as_list(matrix.get("include"))
               if isinstance(e, dict)]
    exclude = [e for e in _as_list(matrix.get("exclude"))
               if isinstance(e, dict)]
    axes_raw = {k: v for k, v in matrix.items()
                if k not in ("include", "exclude")}
    if any(isinstance(v, str) and "${{" in v for v in axes_raw.values()):
        return {"kind": "dynamic", "raw": "expression axes"}, {}

    combos: list[dict[str, str]] = [{}]
    for axis, vals in axes_raw.items():
        vlist = vals if isinstance(vals, list) else [vals]
        combos = [dict(c, **{axis: _scalar_str(v)})
                  for c in combos for v in vlist]
    if combos == [{}]:
        combos = []

    def matches(combo: dict, entry: dict) -> bool:
        return all(_scalar_str(entry[k]) == combo.get(k)
                   for k in entry if k in combo)

    combos = [c for c in combos
              if not any(matches(c, {_key_str(k): v for k, v in e.items()})
                         and all(_key_str(k) in c for k in e)
                         for e in exclude)]

    axis_keys = set(axes_raw)
    for entry in include:
        entry = {_key_str(k): _scalar_str(v) for k, v in entry.items()}
        overlapping = [k for k in entry if k in axis_keys]
        if combos and overlapping:
            hit = [c for c in combos
                   if all(c.get(k) == entry[k] for k in overlapping)]
            if hit:
                for c in hit:
                    for k, v in entry.items():
                        if k not in axis_keys:
                            c[k] = v
                continue
            combos.append(dict(entry))
        elif combos and not overlapping:
            for c in combos:
                for k, v in entry.items():
                    c.setdefault(k, v)
        else:
            combos.append(dict(entry))

    combos = combos[:_MAX_MATRIX_COMBOS]
    axes: dict[str, list[str]] = {}
    for c in combos:
        for k, v in c.items():
            vals = axes.setdefault(k, [])
            if v not in vals:
                vals.append(v)
    return {"kind": "matrix",
            "combos": [{"vars": dict(c)} for c in combos]}, axes


def _build_jobs(state: _ParserState) -> None:
    for job_id, config in state.job_configs.items():
        rel_path, line_no, doc, namespace = state.job_meta[job_id]
        _, nested_lines = state.file_maps.get(rel_path, ({}, {}))
        wf = state.workflows.get(namespace, {})
        plain_name = job_id.rsplit("::", 1)[-1]

        flags: set[str] = set()
        annotations: dict[str, Any] = {
            # Reuses the child-pipeline grouping key: every workflow folds
            # into one expandable group in the Graph view.
            "child_pipeline": namespace,
            "workflow": namespace,
        }
        if wf.get("reusable"):
            annotations["reusable_workflow"] = True

        for key in config:
            if key not in _JOB_KEYS and not key.startswith("__"):
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=(
                        f"Job '{job_id}' has unknown key '{key}' — GitHub "
                        "rejects the workflow"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))

        if isinstance(config.get("name"), str):
            annotations["display_name"] = config["name"]

        if "if" in config:
            annotations["if"] = _scalar_str(config["if"])

        runs_on = config.get("runs-on")
        if isinstance(runs_on, str):
            annotations["tags"] = [runs_on]
        elif isinstance(runs_on, list):
            annotations["tags"] = [_scalar_str(r) for r in runs_on]
        elif isinstance(runs_on, dict):
            labels = _as_list(runs_on.get("labels"))
            group = runs_on.get("group")
            annotations["tags"] = [_scalar_str(r) for r in labels]
            if group:
                annotations["tags"].append(f"group: {_scalar_str(group)}")

        container = config.get("container")
        if isinstance(container, str):
            annotations["image"] = container
        elif isinstance(container, dict) and "image" in container:
            annotations["image"] = _scalar_str(container["image"])

        env_cfg = config.get("environment")
        if isinstance(env_cfg, str):
            annotations["environment"] = env_cfg
            annotations["environment_note"] = (
                "deployment environments can require manual approval — "
                "configured in repository settings, not visible here"
            )
        elif isinstance(env_cfg, dict):
            if "name" in env_cfg:
                annotations["environment"] = _scalar_str(env_cfg["name"])
                annotations["environment_note"] = (
                    "deployment environments can require manual approval — "
                    "configured in repository settings, not visible here"
                )
            if "url" in env_cfg:
                annotations["environment_url"] = _scalar_str(env_cfg["url"])

        if config.get("continue-on-error"):
            flags.add("allow_failure")
        if "timeout-minutes" in config:
            annotations["timeout"] = f"{_scalar_str(config['timeout-minutes'])} minutes"
        if isinstance(config.get("concurrency"), (str, dict)):
            conc = config["concurrency"]
            if isinstance(conc, dict):
                annotations["concurrency"] = _scalar_str(conc.get("group", ""))
                if conc.get("cancel-in-progress"):
                    annotations["concurrency"] += " (cancel-in-progress)"
            else:
                annotations["concurrency"] = _scalar_str(conc)
        if isinstance(config.get("outputs"), dict):
            annotations["outputs"] = sorted(
                _key_str(k) for k in config["outputs"])
        if isinstance(config.get("permissions"), dict):
            annotations["permissions"] = ", ".join(
                f"{_key_str(k)}: {_scalar_str(v)}"
                for k, v in config["permissions"].items())
        elif isinstance(config.get("permissions"), str):
            annotations["permissions"] = config["permissions"]

        strategy = config.get("strategy")
        matrix_cfg, axes = _expand_matrix(strategy)
        if matrix_cfg is not None:
            flags.add("parallel")
            if matrix_cfg["kind"] == "matrix":
                annotations["matrix"] = {
                    "count": len(matrix_cfg["combos"]),
                    "variables": axes,
                }
                for axis, vals in axes.items():
                    var = state._get_or_create_variable(axis)
                    var.events.append(VariableEvent(
                        source=SourceLocation(file=rel_path, line=line_no),
                        operator="matrix",
                        scope=job_id,
                        raw_value=" | ".join(vals),
                        annotations={"matrix_axis": True},
                    ))
            else:
                annotations["matrix"] = {
                    "count": None,
                    "dynamic": (
                        "matrix built from an expression — instances are "
                        "known only at run time"
                    ),
                }
        if isinstance(strategy, dict):
            if strategy.get("fail-fast") is False:
                annotations["fail_fast"] = "false"
            if "max-parallel" in strategy:
                annotations["max_parallel"] = _scalar_str(strategy["max-parallel"])

        uses = config.get("uses")
        recipe: list[str] = []
        if isinstance(uses, str):
            if "steps" in config or "runs-on" in config:
                state.diagnostics.append(Diagnostic(
                    severity="error",
                    message=(
                        f"Job '{job_id}' combines uses: with "
                        f"{'steps' if 'steps' in config else 'runs-on'} — a "
                        "reusable-workflow call job cannot have its own "
                        "steps or runner (GitHub rejects the workflow)"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))
            recipe.append(f"[uses] {uses}")
            if isinstance(config.get("with"), dict):
                for k, v in config["with"].items():
                    recipe.append(f"[with] {_key_str(k)}: {_scalar_str(v)}")
            if config.get("secrets") == "inherit":
                recipe.append("[secrets] inherit")
            elif isinstance(config.get("secrets"), dict):
                recipe.append("[secrets] "
                              + ", ".join(map(_key_str, config["secrets"])))
        else:
            steps = config.get("steps")
            if steps is None:
                state.diagnostics.append(Diagnostic(
                    severity="error",
                    message=(
                        f"Job '{job_id}' has neither steps: nor uses: — "
                        "GitHub rejects a job with nothing to execute"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                ))
            else:
                recipe, actions = _steps_to_recipe(
                    steps, job_id, rel_path, line_no, state)
                if actions:
                    annotations["actions"] = actions

        node = Node(
            id=job_id,
            name=plain_name,
            kind="job",
            source=SourceLocation(file=rel_path, line=line_no),
            recipe=recipe,
            doc=doc,
            flags=flags,
            annotations=annotations,
        )
        state.nodes[job_id] = node

        if isinstance(config.get("env"), dict):
            _record_env_events(
                config["env"], job_id, rel_path, f"jobs.{plain_name}",
                nested_lines, line_no, state)
        if isinstance(config.get("steps"), list):
            for step in config["steps"]:
                if isinstance(step, dict) and isinstance(step.get("env"), dict):
                    sname = step.get("name") or step.get("id") or "step"
                    _record_env_events(
                        step["env"], job_id, rel_path,
                        f"jobs.{plain_name}", nested_lines, line_no, state,
                        extra_annotations={"step": _scalar_str(sname)},
                    )

        if isinstance(uses, str):
            _process_uses(uses, job_id, config, rel_path, line_no, state)

        for line in recipe:
            for m in _SHELL_VAR_RE.finditer(line):
                vname = m.group(1)
                var = state._get_or_create_variable(vname)
                if job_id not in var.used_by:
                    var.used_by.append(job_id)
            for m in _CTX_VAR_RE.finditer(line):
                ctx, vname = m.group(1), m.group(2)
                full = vname if ctx in ("env", "matrix") else f"{ctx}.{vname}"
                var = state._get_or_create_variable(full)
                if job_id not in var.used_by:
                    var.used_by.append(job_id)

    # needs edges — resolved against the full job table of the same
    # workflow, so definition order is irrelevant.
    for job_id, config in state.job_configs.items():
        node = state.nodes.get(job_id)
        if node is None:
            continue
        rel_path, line_no, _, namespace = state.job_meta[job_id]
        needs = config.get("needs")
        if needs is None:
            continue
        details = []
        for need in _as_list(needs):
            need_name = _scalar_str(need)
            need_id = f"{namespace}::{need_name}"
            if need_id not in state.job_configs:
                if need_id not in state.nodes:
                    state.nodes[need_id] = Node(
                        id=need_id, name=need_name, kind="ghost",
                        annotations={"child_pipeline": namespace},
                    )
                state.diagnostics.append(Diagnostic(
                    severity="error",
                    message=(
                        f"Job '{job_id}' needs '{need_name}' which is not "
                        "defined in this workflow — GitHub rejects the "
                        "workflow"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=need_id,
                ))
                wf = state.workflows.get(namespace)
                if wf is not None:
                    msg = f"job '{node.name}' needs undefined job '{need_name}'"
                    if msg not in wf["invalid"]:
                        wf["invalid"].append(msg)
                        wf["summary"]["invalid"] = list(wf["invalid"])
            state.edges.append(Edge(src=job_id, dst=need_id, kind="needs"))
            details.append(need_name)
        if details:
            node.annotations["needs_details"] = details


def _process_uses(uses: str, job_id: str, config: dict, rel_path: str,
                  line_no: int, state: _ParserState) -> None:
    node = state.nodes[job_id]
    with_inputs = sorted(map(_key_str, config["with"])) \
        if isinstance(config.get("with"), dict) else []
    secrets = config.get("secrets")
    secrets_summary = ("inherit" if secrets == "inherit"
                       else sorted(map(_key_str, secrets))
                       if isinstance(secrets, dict) else None)

    if uses.startswith("./"):
        target_ns = state.rel(
            os.path.abspath(os.path.join(state.repo_root, uses[2:])))
        node.annotations["uses_info"] = {
            "kind": "local", "workflow": target_ns, "raw": uses,
            "inputs": with_inputs, "secrets": secrets_summary,
        }
        node.annotations["trigger"] = f"reusable workflow: {target_ns}"
        if target_ns in state.workflows:
            state.edges.append(Edge(src=job_id, dst=target_ns, kind="invokes"))
        else:
            ghost_id = f"reusable:{uses}"
            if ghost_id not in state.nodes:
                state.nodes[ghost_id] = Node(
                    id=ghost_id, name=uses, kind="ghost")
            state.edges.append(Edge(src=job_id, dst=ghost_id, kind="invokes"))
        return

    m = _REMOTE_USES_RE.match(uses)
    if m is None:
        return
    project = f"{m.group('owner')}/{m.group('repo')}"
    resolved_ns = config.get("__resolved_uses_ns__")
    node.annotations["uses_info"] = {
        "kind": "remote", "project": project, "file": m.group("path"),
        "ref": m.group("ref"), "raw": uses,
        "inputs": with_inputs, "secrets": secrets_summary,
    }
    node.annotations["trigger"] = f"reusable workflow: {uses}"
    # Typed record the cross-repository rollup resolves, mirroring GitLab
    # trigger jobs.
    node.annotations["trigger_info"] = {
        "mode": "multi_project",
        "project": project,
        "ref": m.group("ref"),
        "strategy": "mirror",   # a caller job tracks the called run's status
        "forward": {"yaml_variables": bool(with_inputs),
                    "pipeline_variables": secrets_summary == "inherit"},
        "unresolved": [],
        "file": m.group("path"),
    }
    if resolved_ns:
        state.edges.append(Edge(src=job_id, dst=resolved_ns, kind="invokes"))
    else:
        ghost_id = f"downstream:{uses}"
        if ghost_id not in state.nodes:
            state.nodes[ghost_id] = Node(
                id=ghost_id, name=uses, kind="ghost",
                annotations={"downstream_project": project},
            )
        state.edges.append(Edge(src=job_id, dst=ghost_id, kind="invokes"))


# ---------------------------------------------------------------------------
# Docstrings
# ---------------------------------------------------------------------------

def _extract_doc_above(lines: list[str], line_no: int) -> str | None:
    if line_no <= 1:
        return None
    prev_line = lines[line_no - 2].strip() if line_no - 1 < len(lines) else ""
    if prev_line.startswith("##"):
        return prev_line[2:].strip()
    return None
