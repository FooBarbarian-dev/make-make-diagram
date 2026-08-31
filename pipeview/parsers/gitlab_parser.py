from __future__ import annotations

import glob as _glob
import os
import re
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

from pipeview import gitlab_templates
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
from pipeview.parsers.gitlab_predefined import PREDEFINED_VAR_DOCS
from pipeview.parsers.gitlab_whatif import compile_whatif, flatten_rules

# Top-level configuration keywords — never jobs. `pages` is deliberately NOT
# here: it is a real job with special deploy behavior.
_GITLAB_KEYWORDS = frozenset({
    "stages", "variables", "default", "include", "workflow",
    "image", "services", "types", "before_script", "after_script", "cache",
})

# Deprecated top-level forms that act as defaults with the lowest precedence.
_LEGACY_DEFAULT_KEYS = ("image", "services", "before_script", "after_script", "cache")

# Keys `default:` may provide to jobs that lack them (GitLab CI reference,
# `default`).
_DEFAULT_KEYS = (
    "image", "services", "before_script", "after_script", "tags", "cache",
    "artifacts", "retry", "timeout", "interruptible", "hooks", "id_tokens",
)

_DEFAULT_STAGES = [".pre", "build", "test", "deploy", ".post"]

_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_PREDEFINED_VAR_RE = re.compile(r"^(CI|GITLAB)_")

_SCRIPT_SECTIONS = ("before_script", "script", "after_script")

# GitLab's own documented ceiling is 150 includes per pipeline.
_MAX_INCLUDE_DEPTH = 150
_MAX_REFERENCE_DEPTH = 32


class _Reference:
    """Value object for a `!reference [Job, key, …]` tag. Never constructed
    into arbitrary objects — the loader stays safe."""

    __slots__ = ("path", "line")

    def __init__(self, path: list, line: int):
        self.path = [str(p) for p in path]
        self.line = line

    def __repr__(self) -> str:  # keeps accidental str() honest
        return f"!reference [{', '.join(self.path)}]"


class _PipeviewLoader(SafeLoader):
    """SafeLoader subclass: captures !reference tags as value objects and
    detects duplicate mapping keys (which PyYAML otherwise resolves silently
    by keeping the last — data loss wearing a valid-YAML costume)."""

    def __init__(self, stream):
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int]] = []
        self.merge_lines: set[int] = set()

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            if any(
                isinstance(k, yaml.ScalarNode) and k.tag == "tag:yaml.org,2002:merge"
                for k, _ in node.value
            ):
                self.merge_lines.add(node.start_mark.line + 1)
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


def _construct_reference(loader: _PipeviewLoader, node: yaml.Node) -> _Reference:
    if isinstance(node, yaml.SequenceNode):
        seq = loader.construct_sequence(node)
    else:
        seq = [loader.construct_scalar(node)]
    return _Reference(seq, node.start_mark.line + 1)


_PipeviewLoader.add_constructor("!reference", _construct_reference)


def _key_str(key: Any) -> str:
    """Normalize a YAML key to the string GitLab would use. Unquoted `true:`
    or `on:` arrive as Python bools; job names are strings."""
    if key is True:
        return "true"
    if key is False:
        return "false"
    return str(key)


def _scalar_str(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def parse_gitlab(
    path: str,
    *,
    repo_root: str | None = None,
    external_resolver=None,
    local_roots: list[str] | None = None,
    bundled_templates: bool = True,
) -> Report:
    """Parse a GitLab CI file tree rooted at `path`.

    Most keyword arguments exist for the remote-fetch layer
    (`pipeview.gitlab`), which materializes files GitLab served into a work
    directory; offline behavior is unchanged when they are omitted.

    - `repo_root`: directory include:local paths resolve against (GitLab
      resolves them against the repository root, which is not the config
      file's directory when `ci_config_path` is customized).
    - `external_resolver`: callable(include_dict) -> list of local paths for
      a non-local include (project/template/remote/component) that has been
      materialized on disk, or None to leave it unresolved (ghosts).
    - `local_roots`: directories that are repository roots of *other*
      projects materialized inside this tree — include:local inside a file
      under one of them resolves against that root, matching GitLab's
      nested cross-project include semantics.
    - `bundled_templates`: resolve `include:template` entries nothing else
      resolved from pipeview's bundled snapshot of GitLab's built-in
      templates (`pipeview.gitlab_templates`) instead of ghosting them.
      Still fully offline — the snapshot ships inside the package.
    """
    root_path = os.path.abspath(path)
    if os.path.isdir(root_path):
        root_path = os.path.join(root_path, ".gitlab-ci.yml")
        if not os.path.isfile(root_path):
            return Report(
                root=path,
                format="gitlab_ci",
                diagnostics=[
                    Diagnostic(severity="error", message=f"No .gitlab-ci.yml in {path}")
                ],
            )

    state = _ParserState(root_path, repo_root=repo_root)
    state.external_resolver = external_resolver
    state.bundled_templates = bundled_templates
    state.local_roots = [
        os.path.abspath(p).rstrip(os.sep) for p in (local_roots or [])
    ]
    _parse_file(root_path, state, namespace="")
    _prefetch_child_pipelines(state)
    if state.workflow_merged:
        _process_workflow(state.workflow_merged, state)
    _build_jobs(state)
    _resolve_pending_var_refs(state)
    _build_stage_structure(state)
    _label_predefined_variables(state)
    whatif = compile_whatif(state)

    report = Report(
        root=path,
        format="gitlab_ci",
        nodes=list(state.nodes.values()),
        edges=state.edges,
        variables=list(state.variables.values()),
        files=state.files,
        diagnostics=state.diagnostics,
        default_goal=None,
    )
    if state.workflow_rules:
        report.annotations["workflow_rules"] = state.workflow_rules
    if state.workflow_name:
        report.annotations["workflow_name"] = state.workflow_name
    report.annotations["whatif"] = whatif
    report.annotations["predefined_var_docs"] = PREDEFINED_VAR_DOCS
    return report


class _ParserState:
    def __init__(self, root_path: str, repo_root: str | None = None):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.variables: dict[str, Variable] = {}
        self.files: list[SourceFile] = []
        self.diagnostics: list[Diagnostic] = []
        self.root_path = root_path
        self.repo_root = os.path.abspath(repo_root) if repo_root else os.path.dirname(root_path)
        # Remote-fetch hooks (see parse_gitlab docstring); inert by default.
        self.external_resolver = None
        self.local_roots: list[str] = []
        self.bundled_templates = True
        # Files parsed from OUTSIDE the repo tree (today: the bundled GitLab
        # template snapshot) display under an alias, not a ../../ relpath:
        # (abs dir, display prefix), consulted by rel().
        self.path_aliases: list[tuple[str, str]] = []
        self.parsed_files: set[str] = set()
        self.include_stack: list[str] = []
        self.stages: list[str] | None = None   # declared `stages:` (root wins)
        self.global_defaults: dict[str, Any] = {}
        self.legacy_defaults: dict[str, Any] = {}
        self.workflow_rules: list[str] = []
        self.workflow_raw: list = []   # raw workflow:rules entries for the what-if compiler
        self.workflow_name: str | None = None
        # main-pipeline workflow: accumulated across files in merge order
        # (GitLab deep-merges it per key like any other top-level hash);
        # processed once when parsing completes
        self.workflow_merged: dict[str, Any] = {}
        # child-pipeline namespace → its raw workflow:rules (children
        # evaluate their own workflow, independent of the parent's)
        self.child_workflows: dict[str, list] = {}
        # rel path of a conditionally-included file → its include:rules
        # (compiled into a per-job gate by the what-if compiler)
        self.include_gates: dict[str, list] = {}
        self.child_workflow_entry: set[str] = set()   # namespaces whose ENTRY file set it
        # raw (pre-extends) job configs, insertion-ordered; name → config
        self.job_configs: dict[str, dict[str, Any]] = {}
        # name → (rel_path, line, doc, namespace)
        self.job_meta: dict[str, tuple[str, int, str | None, str]] = {}
        # jobs defined in several files (GitLab deep-merges them):
        # name → ["file:line", …] in merge order, later entries winning
        self.job_merged_sources: dict[str, list[str]] = {}
        # rel_path → (top_lines, nested_lines, raw_scalar_map) from one
        # compose pass per file (replaces per-key full-file regex rescans)
        self.file_maps: dict[str, tuple[dict, dict, dict]] = {}
        # variable events holding a !reference, resolved once every job is
        # known (a global variable may reference a job defined later/elsewhere)
        self.pending_var_refs: list[tuple[VariableEvent, _Reference, str]] = []
        self.unresolved_includes: list[str] = []
        self.flat_cache: dict[str, dict[str, Any]] = {}
        self.extends_cycles: set[str] = set()

    def _get_or_create_variable(self, name: str) -> Variable:
        if name not in self.variables:
            self.variables[name] = Variable(name=name)
        return self.variables[name]

    def rel(self, path: str) -> str:
        """Display/reference path of a parsed file: repo-relative, unless the
        file lives under a registered alias (e.g. the bundled template
        snapshot -> "[template] Jobs/Build.gitlab-ci.yml")."""
        fp = os.path.abspath(path)
        for prefix, display in self.path_aliases:
            if fp == prefix or fp.startswith(prefix + os.sep):
                sub = os.path.relpath(fp, prefix).replace(os.sep, "/")
                return f"{display} {sub}"
        return os.path.relpath(fp, self.repo_root)

    def local_root_for(self, filepath: str) -> str:
        """The repository root include:local resolves against for a file —
        a materialized foreign project's root when the file sits inside one,
        else this report's repo root."""
        fp = os.path.abspath(filepath)
        for root in self.local_roots:
            if fp == root or fp.startswith(root + os.sep):
                return root
        return self.repo_root


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _load_yaml(raw_text: str):
    """Load with the pipeview SafeLoader subclass. Returns
    (data, duplicate_keys, merge_lines, extra_docs, error)."""
    loader = _PipeviewLoader(raw_text)
    try:
        data = loader.get_data() if loader.check_data() else None
        extra_docs = loader.check_data()
    except yaml.YAMLError as e:
        return None, [], set(), False, e
    finally:
        dup = list(loader.duplicate_keys)
        merges = set(loader.merge_lines)
        loader.dispose()
    return data, dup, merges, extra_docs, None


def _build_file_maps(
    raw_text: str,
) -> tuple[dict[str, int], dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """One compose pass over the document, yielding:
    - top-level key → line
    - (top-level key, nested key) → line (job keys and variables entries)
    - (owner, var-name) → raw scalar text of `variables:` values — GitLab
      passes variable values as strings, so the raw text, not PyYAML's 1.1
      coercion, is what users must see.
    Replaces the old per-key full-file regex rescan (O(n·m))."""
    top: dict[str, int] = {}
    nested: dict[tuple[str, str], int] = {}
    raw_scalars: dict[tuple[str, str], str] = {}
    try:
        root = yaml.compose(raw_text, SafeLoader)
    except yaml.YAMLError:
        return top, nested, raw_scalars
    if not isinstance(root, yaml.MappingNode):
        return top, nested, raw_scalars

    def record_vars(owner: str, owner_key: str, vars_node: yaml.Node) -> None:
        if not isinstance(vars_node, yaml.MappingNode):
            return
        for k, v in vars_node.value:
            if isinstance(k, yaml.ScalarNode):
                nested.setdefault(
                    (owner_key + ".variables" if owner_key else "variables", k.value),
                    k.start_mark.line + 1,
                )
                if isinstance(v, yaml.ScalarNode):
                    raw_scalars[(owner, k.value)] = v.value

    for key_node, val_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        kname = key_node.value
        top.setdefault(kname, key_node.start_mark.line + 1)
        if kname == "variables":
            record_vars("", "", val_node)
        elif isinstance(val_node, yaml.MappingNode):
            for k2, v2 in val_node.value:
                if isinstance(k2, yaml.ScalarNode):
                    nested.setdefault((kname, k2.value), k2.start_mark.line + 1)
                    if k2.value == "variables":
                        record_vars(kname, kname, v2)
    return top, nested, raw_scalars


def _parse_file(filepath: str, state: _ParserState, namespace: str) -> None:
    filepath = os.path.abspath(filepath)
    if filepath in state.parsed_files:
        return
    state.parsed_files.add(filepath)
    state.include_stack.append(filepath)
    try:
        _parse_file_inner(filepath, state, namespace)
    finally:
        state.include_stack.pop()


def _parse_file_inner(filepath: str, state: _ParserState, namespace: str) -> None:
    rel_path = state.rel(filepath)
    sf = SourceFile(path=rel_path, kind="gitlab_yaml", status="ok")
    is_root = filepath == os.path.abspath(state.root_path)

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

    data, duplicate_keys, merge_lines, extra_docs, err = _load_yaml(raw_text)

    if err is not None:
        # A true YAML syntax error is input the real tool also rejects —
        # the one case where file-level `error` status is honest.
        sf.status = "error"
        state.files.append(sf)
        mark = getattr(err, "problem_mark", None)
        line = (mark.line + 1) if mark else 1
        state.diagnostics.append(
            Diagnostic(
                severity="error",
                message=f"YAML parse error in {rel_path}: {err}",
                source=SourceLocation(file=rel_path, line=line),
            )
        )
        return

    if extra_docs:
        sf.status = "error"
        state.diagnostics.append(
            Diagnostic(
                severity="error",
                message=(
                    f"{rel_path} contains multiple YAML documents — GitLab "
                    "reads a single document per file; only the first is shown"
                ),
                source=SourceLocation(file=rel_path, line=1),
            )
        )

    state.files.append(sf)

    if data is None:
        state.diagnostics.append(
            Diagnostic(
                severity="warning" if is_root else "info",
                message=(
                    f"{rel_path} is empty"
                    + (" — GitLab requires at least one job in the root file"
                       if is_root else " — it contributes nothing")
                ),
                source=SourceLocation(file=rel_path, line=1),
            )
        )
        return

    if not isinstance(data, dict):
        sf.status = "error"
        state.diagnostics.append(
            Diagnostic(
                severity="error",
                message=(
                    f"Top level of {rel_path} is not a mapping "
                    f"(got {type(data).__name__}) — GitLab rejects this"
                ),
                source=SourceLocation(file=rel_path, line=1),
            )
        )
        return

    for key, line in duplicate_keys:
        state.diagnostics.append(
            Diagnostic(
                severity="warning",
                message=(
                    f"Duplicate key '{key}' — YAML silently keeps the last "
                    "definition and drops the earlier one (GitLab's linter "
                    "flags this)"
                ),
                source=SourceLocation(file=rel_path, line=line),
            )
        )

    top_lines, nested_lines, raw_scalar_map = _build_file_maps(raw_text)
    state.file_maps[rel_path] = (top_lines, nested_lines, raw_scalar_map)
    raw_lines = raw_text.splitlines()

    data = {_key_str(k): v for k, v in data.items()}

    # Includes FIRST, wherever the `include:` key sits in the file: GitLab
    # merges included files before the including file's own content, so
    # everything this file defines takes precedence over what it includes
    # (Gitlab::Ci::Config::External::Processor — external files deep-merge
    # in include order, then the file's own values deep-merge on top).
    if "include" in data:
        _process_includes(
            data["include"], rel_path, filepath,
            top_lines.get("include", 1), state, namespace,
        )

    if "stages" in data and isinstance(data["stages"], list) and not namespace:
        # Arrays replace whole in GitLab's deep merge, so the last file in
        # merge order wins — and this file is later than everything it
        # includes. Child-pipeline files never touch the parent's stages.
        state.stages = [_scalar_str(s) for s in data["stages"]]

    if "variables" in data and isinstance(data["variables"], dict):
        _process_variables(
            data["variables"], "global", rel_path,
            nested_lines, raw_scalar_map, "", top_lines.get("variables", 1), state,
            # child-pipeline globals must not leak into the parent's rules
            extra_annotations={"namespace": namespace.rstrip(":")}
            if namespace else None,
        )

    if "default" in data and isinstance(data["default"], dict) and not namespace:
        # Hashes deep-merge key by key across files; this file wins.
        state.global_defaults = _deep_merge(state.global_defaults, data["default"])

    for legacy_key in _LEGACY_DEFAULT_KEYS:
        if legacy_key in data and not namespace:
            first_sighting = legacy_key not in state.legacy_defaults
            state.legacy_defaults[legacy_key] = _deep_merge(
                state.legacy_defaults.get(legacy_key), data[legacy_key])
            if first_sighting:
                state.diagnostics.append(
                    Diagnostic(
                        severity="info",
                        message=(
                            f"Top-level '{legacy_key}:' is a deprecated global "
                            "default — prefer 'default:'"
                        ),
                        source=SourceLocation(
                            file=rel_path, line=top_lines.get(legacy_key, 1)
                        ),
                    )
                )

    if "workflow" in data and isinstance(data["workflow"], dict):
        wf = data["workflow"]
        wf_rules = wf.get("rules")
        if namespace:
            # child pipeline: its own workflow gates it — from the entry
            # file (wins) or any file the child includes (last include wins,
            # mirroring GitLab's include merge)
            ns_key = namespace.rstrip(":")
            is_entry = rel_path.replace(os.sep, "/") + "::" == namespace
            if isinstance(wf_rules, list) and wf_rules:
                if is_entry:
                    state.child_workflows[ns_key] = wf_rules
                    state.child_workflow_entry.add(ns_key)
                elif ns_key not in state.child_workflow_entry:
                    state.child_workflows[ns_key] = wf_rules
        else:
            # Main pipeline: workflow: deep-merges per key across files like
            # everything else — an include can supply rules while the root
            # supplies only name. Files arrive here in merge order (includes
            # first, root last), so accumulate and process once at the end.
            state.workflow_merged = _deep_merge(state.workflow_merged, wf)

    for key, value in data.items():
        if key in _GITLAB_KEYWORDS:
            continue
        line_no = top_lines.get(key, 1)
        if not isinstance(value, dict):
            state.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    message=(
                        f"Top-level key '{key}' is neither a keyword nor a "
                        "job (a job's value must be a mapping) — GitLab "
                        "rejects unknown root keys"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                )
            )
            continue

        doc = _extract_doc_above(raw_lines, line_no)
        job_id = f"{namespace}{key}"
        config = {_key_str(k): v for k, v in value.items()}
        if merge_lines & _mapping_line_range(nested_lines, key):
            config["__merged_via_anchor__"] = True
        prev = state.job_configs.get(job_id)
        if prev is not None:
            # Same job name in another file: GitLab deep-merges the
            # definitions in merge order — hashes (variables:, …) merge key
            # by key, arrays and scalars (script:, rules:, …) are replaced
            # whole. This file is later in merge order, so it wins. This is
            # how a local job customizes an included/template job without
            # restating its script.
            prev_file, prev_line, prev_doc, _ = state.job_meta[job_id]
            sources = state.job_merged_sources.setdefault(
                job_id, [f"{prev_file}:{prev_line}"])
            sources.append(f"{rel_path}:{line_no}")
            config = _deep_merge(prev, config)
            doc = doc or prev_doc
            state.diagnostics.append(
                Diagnostic(
                    severity="info",
                    message=(
                        f"Job '{key}' is also defined in {prev_file}:"
                        f"{prev_line} — GitLab deep-merges the two, this "
                        "definition taking precedence (hashes merge key by "
                        "key; script, rules and other arrays or scalars are "
                        "replaced whole)"
                    ),
                    source=SourceLocation(file=rel_path, line=line_no),
                    related_node=job_id,
                )
            )
        state.job_configs[job_id] = config
        state.job_meta[job_id] = (rel_path, line_no, doc, namespace)


def _mapping_line_range(
    nested_lines: dict[tuple[str, str], int], key: str
) -> set[int]:
    return {ln for (k, _), ln in nested_lines.items() if k == key}


def _process_workflow(workflow: dict, state: _ParserState) -> None:
    name = workflow.get("name")
    if isinstance(name, str):
        state.workflow_name = name
    rules = workflow.get("rules")
    if isinstance(rules, list):
        rules = flatten_rules(rules)   # GitLab flattens nested rules arrays
        state.workflow_raw = rules
        for rule in rules:
            if isinstance(rule, dict):
                parts = []
                if "if" in rule:
                    parts.append(f"if: {rule['if']}")
                if "when" in rule:
                    parts.append(f"when: {rule['when']}")
                if "changes" in rule:
                    parts.append("changes: …")
                if "exists" in rule:
                    parts.append("exists: …")
                if "variables" in rule and isinstance(rule["variables"], dict):
                    parts.append(
                        "variables: "
                        + ", ".join(f"{k}={v}" for k, v in rule["variables"].items())
                    )
                state.workflow_rules.append(", ".join(parts) if parts else str(rule))
            else:
                state.workflow_rules.append(_scalar_str(rule))


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def _process_variables(
    vars_dict: dict,
    scope: str,
    rel_path: str,
    nested_lines: dict[tuple[str, str], int],
    raw_scalar_map: dict[tuple[str, str], str],
    owner_key: str,
    fallback_line: int,
    state: _ParserState,
    extra_annotations: dict | None = None,
) -> None:
    operator = "global" if scope == "global" else "job"
    for vname_raw, vval in vars_dict.items():
        vname = _key_str(vname_raw)
        var = state._get_or_create_variable(vname)
        lookup_owner = owner_key + ".variables" if owner_key else ""
        line_no = nested_lines.get(
            (lookup_owner or "variables", vname),
            nested_lines.get((owner_key, "variables"), fallback_line),
        )
        annotations: dict[str, Any] = dict(extra_annotations or {})

        description = None
        if isinstance(vval, dict):
            # hash form: value / description / options
            description = vval.get("description")
            vval = vval.get("value", "")
        if description:
            annotations["description"] = str(description)

        if isinstance(vval, _Reference):
            raw_value = repr(vval)  # placeholder until the job table is complete
        elif vval is None:
            raw_value = ""
        elif isinstance(vval, (bool, int, float)):
            raw = raw_scalar_map.get((owner_key, vname))
            raw_value = raw if raw is not None else _scalar_str(vval)
            coerced = str(vval)
            if raw is not None and raw.lower() != coerced.lower():
                annotations["yaml_coercion"] = f"YAML reads '{raw}' as {coerced}"
                state.diagnostics.append(
                    Diagnostic(
                        severity="info",
                        message=(
                            f"Variable {vname}: unquoted YAML value '{raw}' "
                            f"parses as {coerced!r}, but GitLab passes it as "
                            f"the string '{raw}' — quote it to avoid ambiguity"
                        ),
                        source=SourceLocation(file=rel_path, line=line_no),
                    )
                )
        else:
            raw_value = str(vval)

        event = VariableEvent(
            source=SourceLocation(file=rel_path, line=line_no),
            operator=operator,
            scope=scope,
            raw_value=raw_value,
            annotations=annotations,
        )
        var.events.append(event)
        if isinstance(vval, _Reference):
            state.pending_var_refs.append((event, vval, vname))


# ---------------------------------------------------------------------------
# Includes
# ---------------------------------------------------------------------------

def _process_includes(
    includes: Any,
    rel_path: str,
    filepath: str,
    line_no: int,
    state: _ParserState,
    namespace: str,
) -> None:
    loc = SourceLocation(file=rel_path, line=line_no)
    if isinstance(includes, (str, dict)):
        includes = [includes]
    elif not isinstance(includes, list):
        return

    if len(state.include_stack) > _MAX_INCLUDE_DEPTH:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"Include nesting deeper than {_MAX_INCLUDE_DEPTH} — stopping here",
            source=loc,
        ))
        return

    for inc in includes:
        if isinstance(inc, str):
            inc = {"local": inc}
        if not isinstance(inc, dict):
            continue

        inc_rules = inc["rules"] if isinstance(inc.get("rules"), list) else None
        if "rules" in inc:
            state.diagnostics.append(Diagnostic(
                severity="info",
                message=(
                    f"Include of {inc.get('local', inc.get('project', '…'))} is "
                    "conditional (include:rules) — the What-If tab gates its "
                    "jobs on those rules"
                ),
                source=loc,
            ))

        if "local" in inc:
            local_path = str(inc["local"])
            pattern = os.path.join(state.local_root_for(filepath), local_path.lstrip("/"))
            if any(ch in local_path for ch in "*?["):
                matches = sorted(_glob.glob(pattern))
                if not matches:
                    state.diagnostics.append(Diagnostic(
                        severity="info",
                        message=f"Include wildcard matched no files: {local_path}",
                        source=loc,
                    ))
                    continue
            else:
                matches = [pattern]

            for abs_path in matches:
                abs_path = os.path.abspath(abs_path)
                inc_rel = state.rel(abs_path)
                if abs_path in state.include_stack:
                    cycle = [state.rel(p)
                             for p in state.include_stack[state.include_stack.index(abs_path):]]
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            "Include cycle: " + " → ".join(cycle + [inc_rel])
                            + " — GitLab rejects circular includes"
                        ),
                        source=loc,
                    ))
                    continue
                if os.path.isfile(abs_path):
                    if inc_rules:
                        state.include_gates[inc_rel.replace(os.sep, "/")] = inc_rules
                    _parse_file(abs_path, state, namespace)
                    state.edges.append(Edge(src=rel_path, dst=inc_rel, kind="includes"))
                else:
                    state.files.append(
                        SourceFile(path=inc_rel, kind="gitlab_yaml", status="error")
                    )
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=f"Local include not found: {local_path}",
                        source=loc,
                    ))
        else:
            inc_type = next(
                (k for k in ("project", "remote", "template", "component") if k in inc),
                "unknown",
            )

            # The remote-fetch layer may have materialized this include on
            # disk; its resolver maps the include entry to those local paths.
            resolved: list[str] | None = None
            if state.external_resolver is not None and inc_type != "unknown":
                try:
                    resolved = state.external_resolver(inc)
                except Exception:
                    resolved = None
            if not resolved and inc_type == "template" and state.bundled_templates:
                # GitLab's built-in templates ship with the GitLab
                # installation, so nothing else can materialize them —
                # resolve from the snapshot pipeview bundles instead of
                # ghosting every job they define.
                bundled = gitlab_templates.template_path(str(inc["template"]))
                if bundled is not None:
                    alias = (os.path.abspath(gitlab_templates.bundled_root()),
                             "[template]")
                    if alias not in state.path_aliases:
                        state.path_aliases.append(alias)
                    state.diagnostics.append(Diagnostic(
                        severity="info",
                        message=(
                            f"include:template {inc['template']} resolved from "
                            f"pipeview's bundled {gitlab_templates.bundled_version()}"
                            " — the GitLab instance running this pipeline may "
                            "ship a different version"
                        ),
                        source=loc,
                    ))
                    resolved = [bundled]
            if resolved:
                for abs_path in resolved:
                    abs_path = os.path.abspath(abs_path)
                    ext_rel = state.rel(abs_path)
                    if abs_path in state.include_stack:
                        cycle = [state.rel(p)
                                 for p in state.include_stack[
                                     state.include_stack.index(abs_path):]]
                        state.diagnostics.append(Diagnostic(
                            severity="warning",
                            message=(
                                "Include cycle: " + " → ".join(cycle + [ext_rel])
                                + " — GitLab rejects circular includes"
                            ),
                            source=loc,
                        ))
                        continue
                    if os.path.isfile(abs_path):
                        if inc_rules:
                            state.include_gates[ext_rel.replace(os.sep, "/")] = inc_rules
                        _parse_file(abs_path, state, namespace)
                        state.edges.append(Edge(src=rel_path, dst=ext_rel, kind="includes"))
                    else:
                        state.files.append(
                            SourceFile(path=ext_rel, kind="gitlab_yaml", status="error")
                        )
                        state.diagnostics.append(Diagnostic(
                            severity="warning",
                            message=f"Fetched include missing on disk: {ext_rel}",
                            source=loc,
                        ))
                continue

            inc_ref = str(inc.get(inc_type, inc))
            display = f"[{inc_type}:{inc_ref}]"
            state.unresolved_includes.append(display)
            state.files.append(
                SourceFile(path=display, kind="gitlab_yaml", status="unresolved")
            )
            if state.external_resolver is not None:
                why = "the fetch layer could not retrieve it"
            else:
                why = "pipeview never fetches remote content"
            if inc_type == "template" and state.bundled_templates:
                why += (", and it is not in pipeview's bundled "
                        f"{gitlab_templates.bundled_version()}")
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"Cannot resolve {inc_type} include: {inc_ref} — {why}; "
                    "jobs it defines appear as ghosts where referenced"
                ),
                source=loc,
            ))


# ---------------------------------------------------------------------------
# extends flattening (GitLab semantics: hashes deep-merge, arrays replace)
# ---------------------------------------------------------------------------

def _deep_merge(parent: Any, child: Any) -> Any:
    if isinstance(parent, dict) and isinstance(child, dict):
        out = dict(parent)
        for k, v in child.items():
            out[k] = _deep_merge(parent.get(k), v) if k in parent else v
        return out
    # arrays and scalars replace, never concatenate
    return child


def _flatten_job(job_id: str, state: _ParserState, stack: tuple = ()) -> dict[str, Any]:
    """extends-flattened config for job_id, with provenance markers:
    __inherited__[key] = parent name, __overrides__[key] = parent name."""
    if job_id in state.flat_cache:
        return state.flat_cache[job_id]
    if job_id in stack:
        cycle = list(stack[stack.index(job_id):]) + [job_id]
        cyc_key = "→".join(cycle)
        if cyc_key not in state.extends_cycles:
            state.extends_cycles.add(cyc_key)
            meta = state.job_meta.get(job_id)
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    "extends cycle: " + " → ".join(cycle)
                    + " — GitLab rejects circular extends; inheritance "
                    "stops here"
                ),
                source=SourceLocation(file=meta[0], line=meta[1]) if meta else None,
                related_node=job_id,
            ))
        return dict(state.job_configs.get(job_id, {}))

    config = state.job_configs.get(job_id)
    if config is None:
        return {}

    parents = config.get("extends", [])
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, list):
        parents = []

    _, _, _, namespace = state.job_meta.get(job_id, ("", 1, None, ""))

    # Later parents override earlier ones; the child overrides all parents.
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for parent_name in parents:
        parent_id = f"{namespace}{_key_str(parent_name)}"
        if parent_id not in state.job_configs:
            continue
        parent_flat = _flatten_job(parent_id, state, stack + (job_id,))
        parent_flat = {k: v for k, v in parent_flat.items()
                       if not k.startswith("__")}
        for k in parent_flat:
            provenance[k] = _key_str(parent_name)
        merged = _deep_merge(merged, parent_flat)

    own_keys = {k for k in config if k != "extends" and not k.startswith("__")}
    flat = _deep_merge(merged, {k: v for k, v in config.items()
                                if k != "extends" and not k.startswith("__")})
    flat["__inherited__"] = {
        k: provenance[k] for k in provenance if k not in own_keys
    }
    flat["__overrides__"] = {
        k: provenance[k] for k in provenance
        if k in own_keys and not isinstance(config.get(k), dict)
    }
    if config.get("__merged_via_anchor__"):
        flat["__merged_via_anchor__"] = True
    if parents:
        flat["__extends__"] = [_key_str(p) for p in parents]

    state.flat_cache[job_id] = flat
    return flat


# ---------------------------------------------------------------------------
# !reference resolution (against the raw job table, cycle-safe)
# ---------------------------------------------------------------------------

def _resolve_reference(
    ref: _Reference, state: _ParserState, stack: tuple = ()
) -> tuple[Any, str | None]:
    """Returns (value, None) on success or (None, reason) when unresolvable."""
    if len(stack) >= _MAX_REFERENCE_DEPTH:
        return None, "reference chain deeper than the resolver follows"
    key = tuple(ref.path)
    if key in stack:
        cycle = " → ".join("[" + ", ".join(k) + "]" for k in stack + (key,))
        return None, f"reference cycle: {cycle}"
    if not ref.path:
        return None, "empty !reference"

    target = ref.path[0]
    if target not in state.job_configs:
        return None, "target-missing"

    value: Any = state.job_configs[target]
    for part in ref.path[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None, (
                f"'{target}' has no key '{'.'.join(ref.path[1:])}'"
            )

    return _resolve_nested_refs(value, state, stack + (key,))


def _resolve_nested_refs(
    value: Any, state: _ParserState, stack: tuple
) -> tuple[Any, str | None]:
    if isinstance(value, _Reference):
        return _resolve_reference(value, state, stack)
    if isinstance(value, list):
        out = []
        for item in value:
            resolved, reason = _resolve_nested_refs(item, state, stack)
            if reason:
                return None, reason
            if isinstance(item, _Reference) and isinstance(resolved, list):
                out.extend(resolved)   # references splice into lists
            else:
                out.append(resolved)
        return out, None
    if isinstance(value, dict):
        out_d = {}
        for k, v in value.items():
            resolved, reason = _resolve_nested_refs(v, state, stack)
            if reason:
                return None, reason
            out_d[k] = resolved
        return out_d, None
    return value, None


# ---------------------------------------------------------------------------
# Job → node construction
# ---------------------------------------------------------------------------

def _flatten_script(
    section: str,
    value: Any,
    job_id: str,
    rel_path: str,
    line_no: int,
    state: _ParserState,
) -> list[str]:
    """Build recipe lines for one script section: strings pass through,
    nested lists flatten (GitLab semantics), !reference splices or degrades
    to a placeholder — one external reference degrades one value, never the
    job, never the file."""
    lines: list[str] = []

    def emit(item: Any) -> None:
        if isinstance(item, _Reference):
            resolved, reason = _resolve_reference(item, state)
            if reason is None and isinstance(resolved, list):
                for sub in resolved:
                    emit(sub)
                return
            if reason is None:
                emit(resolved)
                return
            target = item.path[0] if item.path else "?"
            if reason == "target-missing":
                likely = ""
                if state.unresolved_includes:
                    likely = (
                        " — likely defined in unresolved include "
                        + ", ".join(state.unresolved_includes)
                    )
                lines.append(
                    f"[{section}] (steps from '{target}' — defined externally,"
                    " not resolvable offline)"
                )
                ghost_id = target
                if ghost_id not in state.nodes and ghost_id not in state.job_configs:
                    state.nodes[ghost_id] = Node(
                        id=ghost_id, name=target, kind="ghost",
                        annotations={"external_reference": repr(item)},
                    )
                # config reuse, not execution ordering → extends edge
                state.edges.append(Edge(src=job_id, dst=ghost_id, kind="extends"))
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=(
                        f"{repr(item)} in '{job_id}' {section} points at a job "
                        f"not defined in the local files{likely}"
                    ),
                    source=SourceLocation(file=rel_path, line=item.line or line_no),
                    related_node=ghost_id,
                ))
            else:
                lines.append(f"[{section}] (unresolved {repr(item)}: {reason})")
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message=f"Cannot resolve {repr(item)} in '{job_id}' {section}: {reason}",
                    source=SourceLocation(file=rel_path, line=item.line or line_no),
                    related_node=job_id,
                ))
            return
        if isinstance(item, list):
            for sub in item:
                emit(sub)
            return
        if item is None:
            return
        lines.append(f"[{section}] {_scalar_str(item)}")

    emit(value)
    return lines


def _summarize_rules(config: dict, flags: set[str], annotations: dict,
                     job_id: str, rel_path: str, nested_lines, raw_scalar_map,
                     state: _ParserState) -> None:
    rules = config.get("rules")
    if not isinstance(rules, list):
        return
    # Display what GitLab evaluates: !reference entries spliced in, nested
    # arrays flattened (both are legal composition forms).
    resolved, _reason = _resolve_nested_refs(rules, state, ())
    if _reason is None and isinstance(resolved, list):
        rules = flatten_rules(resolved)
    else:
        rules = flatten_rules(rules)
    rules_summary = []
    for rule in rules:
        if not isinstance(rule, dict):
            rules_summary.append(_scalar_str(rule))
            continue
        parts = []
        if "if" in rule:
            parts.append(f"if: {rule['if']}")
        when = rule.get("when")
        if when:
            parts.append(f"when: {when}")
            if when == "manual":
                flags.add("manual")
            elif when == "delayed":
                flags.add("delayed")
                if "start_in" in rule:
                    parts.append(f"start_in: {rule['start_in']}")
        if "allow_failure" in rule:
            parts.append(f"allow_failure: {rule['allow_failure']}")
        if "changes" in rule:
            parts.append("changes: …")
        if "exists" in rule:
            parts.append("exists: …")
        if isinstance(rule.get("variables"), dict):
            cond = rule.get("if", "…")
            _process_variables(
                rule["variables"], job_id, rel_path, nested_lines,
                raw_scalar_map, job_id,
                state.job_meta[job_id][1], state,
                extra_annotations={"rule": f"if: {cond}"},
            )
            parts.append("variables: " + ", ".join(map(_key_str, rule["variables"])))
        rules_summary.append(", ".join(parts) if parts else str(rule))
    annotations["rules"] = rules_summary


def _prefetch_child_pipelines(state: _ParserState) -> None:
    """Parse `trigger:include:local` child-pipeline files (they live in the
    repo — analyze them, don't ghost them) before node construction, so the
    job table is stable while nodes are built. Children may trigger
    grandchildren; iterate until no new files appear."""
    for _ in range(10):
        new_files = False
        for job_id in list(state.job_configs):
            flat = _flatten_job(job_id, state)
            trigger = flat.get("trigger")
            if not isinstance(trigger, dict):
                continue
            includes = trigger.get("include")
            if isinstance(includes, (str, dict)):
                includes = [includes]
            for inc in includes if isinstance(includes, list) else []:
                if isinstance(inc, str):
                    inc = {"local": inc}
                if not (isinstance(inc, dict) and "local" in inc):
                    continue
                child_rel = str(inc["local"]).lstrip("/")
                child_abs = os.path.abspath(
                    os.path.join(state.repo_root, child_rel))
                if child_abs not in state.parsed_files and os.path.isfile(child_abs):
                    _parse_file(child_abs, state, namespace=f"{child_rel}::")
                    new_files = True
        if not new_files:
            return


def _build_jobs(state: _ParserState) -> None:
    declared_stages = state.stages
    known_stages = list(declared_stages) if declared_stages else list(_DEFAULT_STAGES)
    if declared_stages is not None:
        for edge_stage in (".pre", ".post"):
            if edge_stage not in known_stages:
                if edge_stage == ".pre":
                    known_stages.insert(0, edge_stage)
                else:
                    known_stages.append(edge_stage)

    for job_id, config in state.job_configs.items():
        rel_path, line_no, doc, namespace = state.job_meta[job_id]
        flat = _flatten_job(job_id, state)
        inherited: dict = flat.get("__inherited__", {})
        overrides: dict = flat.get("__overrides__", {})
        merged_via_anchor = bool(
            flat.get("__merged_via_anchor__") or config.get("__merged_via_anchor__")
        )
        flat = {k: v for k, v in flat.items() if not k.startswith("__")}
        _, nested_lines, raw_scalar_map = state.file_maps.get(
            rel_path, ({}, {}, {}))

        plain_name = job_id.rsplit("::", 1)[-1]
        is_template = plain_name.startswith(".")

        # default: and deprecated top-level defaults, honoring inherit:default
        inherit_cfg = flat.get("inherit", {}) if isinstance(flat.get("inherit"), dict) else {}
        inherit_default = inherit_cfg.get("default", True)
        default_sources: list[tuple[str, dict]] = []
        if state.global_defaults:
            default_sources.append(("default", state.global_defaults))
        if state.legacy_defaults:
            default_sources.append(("top-level (deprecated)", state.legacy_defaults))
        if not is_template:
            for label, source in default_sources:
                for key in _DEFAULT_KEYS:
                    if key in flat or key not in source:
                        continue
                    allowed = (
                        inherit_default is True
                        or (isinstance(inherit_default, list) and key in inherit_default)
                    )
                    if not allowed:
                        continue
                    flat[key] = source[key]
                    inherited.setdefault(key, label)

        flags: set[str] = set()
        annotations: dict[str, Any] = {}
        if is_template:
            flags.add("template")
        if namespace:
            annotations["child_pipeline"] = namespace.rstrip(":")

        stage = _scalar_str(flat.get("stage", "test"))
        annotations["stage"] = stage
        if (
            not is_template
            and not namespace
            and stage not in known_stages
        ):
            state.diagnostics.append(Diagnostic(
                severity="error",
                message=(
                    f"Job '{job_id}' uses stage '{stage}' which is not in the "
                    f"pipeline's stages {known_stages} — GitLab rejects the "
                    "pipeline ('chosen stage does not exist')"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
                related_node=job_id,
            ))

        if flat.get("when") == "manual":
            flags.add("manual")
        if flat.get("when") == "delayed":
            flags.add("delayed")
            if "start_in" in flat:
                annotations["start_in"] = _scalar_str(flat["start_in"])
        if flat.get("allow_failure"):
            flags.add("allow_failure")
        if "image" in flat:
            img = flat["image"]
            annotations["image"] = (
                img.get("name", str(img)) if isinstance(img, dict)
                else _scalar_str(img)
            )
        if isinstance(flat.get("tags"), list):
            annotations["tags"] = [_scalar_str(t) for t in flat["tags"]]

        env = flat.get("environment")
        if isinstance(env, dict):
            if "name" in env:
                annotations["environment"] = _scalar_str(env["name"])
            if "url" in env:
                annotations["environment_url"] = _scalar_str(env["url"])
        elif env is not None:
            annotations["environment"] = _scalar_str(env)

        for simple_key in ("resource_group", "timeout", "interruptible"):
            if simple_key in flat:
                annotations[simple_key] = _scalar_str(flat[simple_key])
        if "retry" in flat:
            retry = flat["retry"]
            annotations["retry"] = (
                f"max {retry.get('max')} ({retry.get('when', 'always')})"
                if isinstance(retry, dict) else _scalar_str(retry)
            )
        if isinstance(flat.get("artifacts"), dict):
            arts = flat["artifacts"]
            bits = []
            if "paths" in arts:
                bits.append("paths: " + ", ".join(map(_scalar_str, arts.get("paths") or [])))
            if "expire_in" in arts:
                bits.append(f"expire_in: {arts['expire_in']}")
            annotations["artifacts"] = "; ".join(bits) if bits else "configured"
        if isinstance(flat.get("cache"), dict):
            cache = flat["cache"]
            bits = []
            if "key" in cache:
                bits.append(f"key: {cache['key']}")
            if "paths" in cache:
                bits.append("paths: " + ", ".join(map(_scalar_str, cache.get("paths") or [])))
            annotations["cache"] = "; ".join(bits) if bits else "configured"

        parallel = flat.get("parallel")
        if isinstance(parallel, int):
            flags.add("parallel")
            annotations["parallel"] = f"{parallel} parallel jobs from one definition"
        elif isinstance(parallel, dict) and isinstance(parallel.get("matrix"), list):
            count = 0
            axes: dict[str, list[str]] = {}
            for entry in parallel["matrix"]:
                if not isinstance(entry, dict):
                    continue
                combo = 1
                for axis, values in entry.items():
                    vals = values if isinstance(values, list) else [values]
                    vals = [_scalar_str(v) for v in vals]
                    combo *= max(1, len(vals))
                    axes.setdefault(_key_str(axis), []).extend(
                        v for v in vals if v not in axes.get(_key_str(axis), [])
                    )
                count += combo
            flags.add("parallel")
            annotations["matrix"] = {
                "count": count,
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

        if "dependencies" in flat and isinstance(flat["dependencies"], list):
            # artifact flow, NOT ordering — never conflated with needs
            annotations["artifact_dependencies"] = [
                _scalar_str(d) for d in flat["dependencies"]
            ]

        recipe: list[str] = []
        for section in _SCRIPT_SECTIONS:
            if section in flat:
                sec_lines = _flatten_script(
                    section, flat[section], job_id, rel_path, line_no, state,
                )
                recipe.extend(sec_lines)

        script_overrides = {
            k: v for k, v in overrides.items() if k in _SCRIPT_SECTIONS
        }
        if script_overrides:
            annotations["overrides"] = {
                k: f"replaces {parent}'s {k} entirely (extends arrays replace, never merge)"
                for k, parent in script_overrides.items()
            }
        inherited_display = [
            f"{k} from {src}" for k, src in inherited.items()
        ]
        if inherited_display:
            annotations["inherited"] = inherited_display
        if merged_via_anchor:
            annotations["merged_via_anchor"] = (
                "parts of this job come from a YAML anchor (<<:) — line numbers "
                "for merged values point at the anchor's definition"
            )
        if isinstance(inherit_cfg.get("variables"), (bool, list)):
            iv = inherit_cfg["variables"]
            annotations["inherit_variables"] = (
                "does not receive global variables (inherit:variables: false)"
                if iv is False else
                "receives only these global variables: " + ", ".join(map(str, iv))
                if isinstance(iv, list) else "receives all global variables"
            )

        _summarize_rules(flat, flags, annotations, job_id, rel_path,
                         nested_lines, raw_scalar_map, state)

        if "only" in flat:
            annotations["only"] = flat["only"]
        if "except" in flat:
            annotations["except"] = flat["except"]

        if job_id in state.job_merged_sources:
            # Defined in several files; GitLab deep-merged them (later
            # entries take precedence).
            annotations["merged_from"] = list(state.job_merged_sources[job_id])

        if (
            not is_template
            and not any(k in flat for k in ("script", "run", "trigger"))
        ):
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"Job '{job_id}' has no script, run, or trigger — GitLab "
                    "rejects jobs with nothing to execute"
                ),
                source=SourceLocation(file=rel_path, line=line_no),
                related_node=job_id,
            ))

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

        if flat.get("__extends__") or "extends" in state.job_configs[job_id]:
            parents = state.job_configs[job_id].get("extends", [])
            if isinstance(parents, str):
                parents = [parents]
            for parent_name in parents if isinstance(parents, list) else []:
                parent_id = f"{namespace}{_key_str(parent_name)}"
                if parent_id not in state.job_configs and parent_id not in state.nodes:
                    state.nodes[parent_id] = Node(
                        id=parent_id, name=_key_str(parent_name), kind="ghost",
                    )
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            f"Job '{job_id}' extends '{parent_name}' which is "
                            "not defined in the local files"
                            + (" — likely defined in unresolved include "
                               + ", ".join(state.unresolved_includes)
                               if state.unresolved_includes else "")
                        ),
                        source=SourceLocation(file=rel_path, line=line_no),
                        related_node=parent_id,
                    ))
                state.edges.append(Edge(src=job_id, dst=parent_id, kind="extends"))

        if isinstance(flat.get("variables"), dict):
            own_vars = state.job_configs[job_id].get("variables")
            own_names = (
                {_key_str(o) for o in own_vars}
                if isinstance(own_vars, dict) else set()
            )
            own = {k: v for k, v in flat["variables"].items()
                   if _key_str(k) in own_names}
            inh = {k: v for k, v in flat["variables"].items()
                   if _key_str(k) not in own_names}
            _process_variables(own, job_id, rel_path, nested_lines,
                               raw_scalar_map, plain_name, line_no, state)
            if inh:
                src_names = inherited.get("variables", "extends parent")
                _process_variables(
                    inh, job_id, rel_path, nested_lines, raw_scalar_map,
                    plain_name, line_no, state,
                    extra_annotations={"inherited_from": src_names},
                )

        if "trigger" in flat:
            _process_trigger(flat["trigger"], job_id, rel_path, line_no, state)

        for line in recipe:
            for m in _VAR_REF_RE.finditer(line):
                vname = m.group(1)
                var = state._get_or_create_variable(vname)
                if job_id not in var.used_by:
                    var.used_by.append(job_id)

    # needs edges — resolved against the FULL job table, so definition order
    # in the file(s) is irrelevant.
    for job_id, config in state.job_configs.items():
        flat = state.flat_cache.get(job_id, config)
        needs = flat.get("needs")
        node = state.nodes.get(job_id)
        if node is None:
            continue
        if needs == []:
            node.flags.add("starts_immediately")
            node.annotations["needs"] = (
                "needs: [] — starts immediately, ignoring stage order"
            )
            continue
        if not isinstance(needs, list):
            continue
        rel_path, line_no, _, namespace = state.job_meta[job_id]
        details = []
        for need in needs:
            if isinstance(need, str):
                need_name, extras = _key_str(need), []
            elif isinstance(need, dict):
                if "pipeline" in need:
                    ghost_id = f"pipeline:{need['pipeline']}"
                    if ghost_id not in state.nodes:
                        state.nodes[ghost_id] = Node(
                            id=ghost_id, name=f"pipeline {need['pipeline']}",
                            kind="ghost",
                            annotations={"cross_pipeline_need": True},
                        )
                    state.edges.append(Edge(src=job_id, dst=ghost_id, kind="needs"))
                    details.append(f"pipeline {need['pipeline']} (cross-pipeline)")
                    continue
                need_name = _key_str(need.get("job", need))
                extras = []
                if need.get("optional"):
                    extras.append("optional")
                if need.get("artifacts") is False:
                    extras.append("no artifacts")
                if "project" in need:
                    ghost_id = f"{need['project']}::{need_name}"
                    if ghost_id not in state.nodes:
                        state.nodes[ghost_id] = Node(
                            id=ghost_id,
                            name=f"{need_name} @ {need['project']}",
                            kind="ghost",
                            annotations={"cross_project_need": str(need["project"])},
                        )
                    state.edges.append(Edge(src=job_id, dst=ghost_id, kind="needs"))
                    details.append(
                        f"{need_name} @ {need['project']} (cross-project)")
                    # Typed record on the needing job (the ghost is shared and
                    # ref-less): what a cross-report linker needs to resolve it.
                    node.annotations.setdefault("cross_project_needs", []).append({
                        "project": str(need["project"]),
                        "job": need_name,
                        "ref": _scalar_str(need["ref"]) if "ref" in need else None,
                        "ghost": ghost_id,
                    })
                    continue
            else:
                continue
            need_id = f"{namespace}{need_name}"
            # "job: [x86, linux]" addresses one parallel:matrix instance —
            # the edge goes to the parent definition, never a ghost
            if need_id not in state.nodes and need_id not in state.job_configs \
                    and ": [" in need_name:
                base_id = f"{namespace}{need_name.split(': [', 1)[0]}"
                if base_id in state.nodes or base_id in state.job_configs:
                    need_id = base_id
                    details.append(f"{need_name} (matrix instance)")
            if need_id not in state.nodes:
                optional = isinstance(need, dict) and need.get("optional")
                state.nodes[need_id] = Node(
                    id=need_id, name=need_name, kind="ghost",
                )
                if not optional:
                    likely = (
                        " — likely defined in unresolved include "
                        + ", ".join(state.unresolved_includes)
                        if state.unresolved_includes else ""
                    )
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            f"Job '{job_id}' needs '{need_name}' which is not "
                            f"defined in the local files{likely}"
                        ),
                        source=SourceLocation(file=rel_path, line=line_no),
                        related_node=need_id,
                    ))
            state.edges.append(Edge(src=job_id, dst=need_id, kind="needs"))
            if extras:
                details.append(f"{need_name} ({', '.join(extras)})")
        if details:
            node.annotations["needs_details"] = details


def _trigger_info(trigger: Any) -> dict | None:
    """Typed summary of a trigger job's semantics (additive, schema v4).

    mode "child" = parent-child pipeline (trigger:include — same project,
    ref, and SHA, even when the config FILE comes from another project);
    mode "multi_project" = trigger:project / string shorthand (runs in the
    other project on its own ref and config). Values are recorded raw;
    anything containing CI variables is flagged unresolved, never guessed.
    """
    # trigger:forward defaults (same as the what-if compiler): yaml
    # variables ARE forwarded, pipeline variables are NOT
    forward = {"yaml_variables": True, "pipeline_variables": False}
    if isinstance(trigger, dict):
        fwd = trigger.get("forward")
        if isinstance(fwd, dict):
            if "yaml_variables" in fwd:
                forward["yaml_variables"] = fwd["yaml_variables"] is not False
            if "pipeline_variables" in fwd:
                forward["pipeline_variables"] = fwd["pipeline_variables"] is True

    info: dict[str, Any] = {
        "mode": "multi_project",
        "project": None,
        "ref": None,
        "strategy": None,
        "forward": forward,
        "unresolved": [],
    }

    if not isinstance(trigger, dict):
        project = _scalar_str(trigger)
        info["project"] = project
        if "$" in project:
            info["unresolved"].append("project uses CI variables")
        return info

    if isinstance(trigger.get("strategy"), str):
        info["strategy"] = trigger["strategy"]
    if isinstance(trigger.get("inputs"), dict):
        info["inputs"] = sorted(map(str, trigger["inputs"]))

    includes = trigger.get("include")
    if includes is not None:
        info["mode"] = "child"
        info["includes"] = []
        if isinstance(includes, (str, dict)):
            includes = [includes]
        for inc in includes if isinstance(includes, list) else []:
            if isinstance(inc, str):
                inc = {"local": inc}
            if not isinstance(inc, dict):
                continue
            inc_type = next(
                (k for k in ("local", "project", "template", "artifact", "remote")
                 if k in inc), "unknown")
            entry: dict[str, Any] = {"type": inc_type,
                                     "value": str(inc.get(inc_type, "?"))}
            if inc_type == "project":
                entry["file"] = str(inc.get("file", "?"))
                if "ref" in inc:
                    entry["ref"] = _scalar_str(inc["ref"])
            if inc_type == "artifact":
                entry["job"] = str(inc.get("job", "?"))
                info["unresolved"].append(
                    "child config generated at runtime by job "
                    f"'{entry['job']}' (dynamic child pipeline)")
            info["includes"].append(entry)
        return info

    project = trigger.get("project")
    info["project"] = _scalar_str(project) if project is not None \
        else _scalar_str(trigger)
    if "$" in info["project"]:
        info["unresolved"].append("project uses CI variables")
    branch = trigger.get("branch")
    if branch is not None:
        info["ref"] = _scalar_str(branch)
        if "$" in info["ref"]:
            info["unresolved"].append("ref uses CI variables")
    return info


def _process_trigger(
    trigger: Any, job_id: str, rel_path: str, line_no: int, state: _ParserState
) -> None:
    node = state.nodes[job_id]
    info = _trigger_info(trigger)
    if info is not None:
        node.annotations["trigger_info"] = info
    if isinstance(trigger, dict):
        if isinstance(trigger.get("strategy"), str):
            node.annotations["trigger_strategy"] = trigger["strategy"]
        includes = trigger.get("include")
        if includes is not None:
            if isinstance(includes, (str, dict)):
                includes = [includes]
            for inc in includes if isinstance(includes, list) else []:
                if isinstance(inc, str):
                    inc = {"local": inc}
                if isinstance(inc, dict) and "local" in inc:
                    child_rel = str(inc["local"]).lstrip("/")
                    child_abs = os.path.abspath(
                        os.path.join(state.repo_root, child_rel))
                    if os.path.isfile(child_abs):
                        # already parsed by _prefetch_child_pipelines
                        node.annotations["trigger"] = f"child pipeline: {child_rel}"
                        state.edges.append(
                            Edge(src=job_id, dst=child_rel, kind="invokes"))
                    else:
                        node.annotations["trigger"] = f"child pipeline: {child_rel} (file missing)"
                        state.diagnostics.append(Diagnostic(
                            severity="warning",
                            message=(
                                f"Trigger child-pipeline file not found: {child_rel}"
                            ),
                            source=SourceLocation(file=rel_path, line=line_no),
                            related_node=job_id,
                        ))
                else:
                    ref = str(inc)
                    ghost_id = f"downstream:{ref}"
                    if ghost_id not in state.nodes:
                        state.nodes[ghost_id] = Node(
                            id=ghost_id, name=ref, kind="ghost")
                    state.edges.append(
                        Edge(src=job_id, dst=ghost_id, kind="invokes"))
            return
        downstream = trigger.get("project")
        if downstream is None:
            downstream = str(trigger)
    else:
        downstream = _scalar_str(trigger)

    ghost_id = f"downstream:{downstream}"
    if ghost_id not in state.nodes:
        state.nodes[ghost_id] = Node(
            id=ghost_id, name=str(downstream), kind="ghost",
            annotations={"downstream_project": str(downstream)},
        )
    state.edges.append(Edge(src=job_id, dst=ghost_id, kind="invokes"))


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _build_stage_structure(state: _ParserState) -> None:
    declared = state.stages
    order = list(declared) if declared is not None else list(_DEFAULT_STAGES)
    if ".pre" not in order:
        order.insert(0, ".pre")
    if ".post" not in order:
        order.append(".post")

    used_stages = {
        n.annotations.get("stage")
        for n in state.nodes.values()
        if n.kind == "job" and "template" not in n.flags
        and "child_pipeline" not in n.annotations
    }
    used_stages.discard(None)

    shown = [s for s in order if s in used_stages or (declared and s in declared)]
    root_file = state.rel(state.root_path)
    for stage_name in shown:
        sid = f"stage:{stage_name}"
        if sid not in state.nodes:
            state.nodes[sid] = Node(
                id=sid, name=stage_name, kind="stage",
                source=SourceLocation(file=root_file, line=1),
                annotations={} if declared else {"default_stages": True},
            )

    for a, b in zip(shown, shown[1:]):
        state.edges.append(
            Edge(src=f"stage:{a}", dst=f"stage:{b}", kind="stage_order"))

    jobs_with_needs = {e.src for e in state.edges if e.kind == "needs"}
    starts_immediately = {
        n.id for n in state.nodes.values() if "starts_immediately" in n.flags
    }

    for nid, node in state.nodes.items():
        if node.kind != "job" or "template" in node.flags:
            continue
        if "child_pipeline" in node.annotations:
            continue
        if nid in jobs_with_needs or nid in starts_immediately:
            continue
        stage = node.annotations.get("stage")
        if stage not in order:
            continue
        idx = order.index(stage)
        for prev in reversed(order[:idx]):
            prev_jobs = [
                n.id for n in state.nodes.values()
                if n.kind == "job" and n.annotations.get("stage") == prev
                and "template" not in n.flags
                and "child_pipeline" not in n.annotations
            ]
            if prev_jobs:
                for pj in prev_jobs:
                    state.edges.append(Edge(src=nid, dst=pj, kind="stage_order"))
                break


def _resolve_pending_var_refs(state: _ParserState) -> None:
    """Resolve !reference values inside `variables:` once every job — from
    every included file — is known. Unresolvable references degrade the one
    value: an explicit placeholder plus a named diagnostic."""
    for event, ref, vname in state.pending_var_refs:
        resolved, reason = _resolve_reference(ref, state)
        if reason is None and not isinstance(resolved, (dict, list)):
            event.raw_value = _scalar_str(resolved)
            event.annotations["via_reference"] = repr(ref)
            continue
        target = ref.path[0] if ref.path else "?"
        if reason == "target-missing":
            likely = (
                " — likely defined in unresolved include "
                + ", ".join(state.unresolved_includes)
                if state.unresolved_includes else ""
            )
            event.raw_value = f"(value from '{target}' — defined externally)"
            event.annotations["external_reference"] = repr(ref)
            if target not in state.nodes and target not in state.job_configs:
                state.nodes[target] = Node(
                    id=target, name=target, kind="ghost",
                    annotations={"external_reference": repr(ref)},
                )
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"{repr(ref)} for variable {vname} points at a job not "
                    f"defined in the local files{likely}"
                ),
                source=event.source,
                related_node=target,
            ))
        else:
            event.annotations["external_reference"] = repr(ref)
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=f"Cannot resolve {repr(ref)} for variable {vname}: {reason}",
                source=event.source,
            ))


def _label_predefined_variables(state: _ParserState) -> None:
    for var in state.variables.values():
        if not var.events and _PREDEFINED_VAR_RE.match(var.name):
            var.origin = "predefined"


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
