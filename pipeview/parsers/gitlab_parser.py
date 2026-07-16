from __future__ import annotations

import os
import re
from typing import Any

import yaml

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

_GITLAB_KEYWORDS = frozenset({
    "stages", "variables", "default", "include", "workflow",
    "image", "services", "before_script", "after_script", "cache",
    "pages",
})

_VAR_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def parse_gitlab(path: str) -> Report:
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

    state = _ParserState(root_path)
    _parse_file(root_path, state)
    _resolve_extends(state)
    _build_stage_edges(state)

    return Report(
        root=path,
        format="gitlab_ci",
        nodes=list(state.nodes.values()),
        edges=state.edges,
        variables=list(state.variables.values()),
        files=state.files,
        diagnostics=state.diagnostics,
        default_goal=None,
    )


class _ParserState:
    def __init__(self, root_path: str):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.variables: dict[str, Variable] = {}
        self.files: list[SourceFile] = []
        self.diagnostics: list[Diagnostic] = []
        self.root_path = root_path
        self.repo_root = os.path.dirname(root_path)
        self.parsed_files: set[str] = set()
        self.stages: list[str] = []
        self.global_defaults: dict[str, Any] = {}
        self.job_configs: dict[str, dict[str, Any]] = {}
        self.extends_map: dict[str, str | list[str]] = {}

    def _get_or_create_variable(self, name: str) -> Variable:
        if name not in self.variables:
            self.variables[name] = Variable(name=name)
        return self.variables[name]


def _parse_file(filepath: str, state: _ParserState) -> None:
    filepath = os.path.abspath(filepath)
    if filepath in state.parsed_files:
        return
    state.parsed_files.add(filepath)

    rel_path = os.path.relpath(filepath, state.repo_root)
    sf = SourceFile(path=rel_path, kind="gitlab_yaml", status="ok")

    if not os.path.isfile(filepath):
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="warning", message=f"File not found: {rel_path}")
        )
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()
    except OSError as e:
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="error", message=f"Cannot read {rel_path}: {e}")
        )
        return

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(
                severity="error",
                message=f"YAML parse error in {rel_path}: {e}",
                source=SourceLocation(file=rel_path, line=1),
            )
        )
        return

    state.files.append(sf)

    if not isinstance(data, dict):
        state.diagnostics.append(
            Diagnostic(
                severity="error",
                message=f"Expected YAML mapping in {rel_path}, got {type(data).__name__}",
                source=SourceLocation(file=rel_path, line=1),
            )
        )
        return

    raw_lines = raw_text.splitlines()

    if "stages" in data and isinstance(data["stages"], list):
        state.stages = [str(s) for s in data["stages"]]
        for stage_name in state.stages:
            sid = f"stage:{stage_name}"
            if sid not in state.nodes:
                state.nodes[sid] = Node(
                    id=sid,
                    name=stage_name,
                    kind="stage",
                    source=SourceLocation(file=rel_path, line=_find_key_line(raw_lines, "stages")),
                )

    if "variables" in data and isinstance(data["variables"], dict):
        _process_variables(data["variables"], "global", rel_path, raw_lines, state)

    if "default" in data and isinstance(data["default"], dict):
        state.global_defaults = data["default"]

    if "include" in data:
        _process_includes(data["include"], rel_path, filepath, state)

    _extract_docs_from_lines(raw_lines, rel_path)

    for key, value in data.items():
        if key in _GITLAB_KEYWORDS:
            continue
        if not isinstance(value, dict):
            continue

        _process_job(key, value, rel_path, raw_lines, state)


def _process_job(
    name: str, config: dict, rel_path: str, raw_lines: list[str], state: _ParserState
) -> None:
    is_template = name.startswith(".")
    line_no = _find_key_line(raw_lines, name)

    doc = _extract_doc_above(raw_lines, line_no)

    recipe: list[str] = []
    for section in ("before_script", "script", "after_script"):
        if section in config and isinstance(config[section], list):
            for item in config[section]:
                recipe.append(f"[{section}] {item}")

    flags: set[str] = set()
    annotations: dict[str, Any] = {}

    if is_template:
        flags.add("template")

    stage = config.get("stage", ".pre")
    annotations["stage"] = stage

    if config.get("when") == "manual":
        flags.add("manual")
    if config.get("allow_failure"):
        flags.add("allow_failure")
    if "image" in config:
        annotations["image"] = config["image"]
    if "tags" in config and isinstance(config["tags"], list):
        annotations["tags"] = config["tags"]

    if "rules" in config and isinstance(config["rules"], list):
        rules_summary = []
        for rule in config["rules"]:
            if isinstance(rule, dict):
                parts = []
                if "if" in rule:
                    parts.append(f"if: {rule['if']}")
                if "when" in rule:
                    parts.append(f"when: {rule['when']}")
                    if rule["when"] == "manual":
                        flags.add("manual")
                if "changes" in rule:
                    parts.append("changes: ...")
                rules_summary.append(", ".join(parts) if parts else str(rule))
        annotations["rules"] = rules_summary

    if "only" in config:
        annotations["only"] = config["only"]
    if "except" in config:
        annotations["except"] = config["except"]

    nid = name
    node = Node(
        id=nid,
        name=name,
        kind="job",
        source=SourceLocation(file=rel_path, line=line_no),
        recipe=recipe,
        doc=doc,
        flags=flags,
        annotations=annotations,
    )
    state.nodes[nid] = node
    state.job_configs[nid] = config

    if "extends" in config:
        state.extends_map[nid] = config["extends"]

    if "needs" in config and isinstance(config["needs"], list):
        for need in config["needs"]:
            need_name = need if isinstance(need, str) else need.get("job", str(need))
            if need_name not in state.nodes:
                state.nodes[need_name] = Node(
                    id=need_name, name=need_name, kind="ghost",
                )
                state.diagnostics.append(
                    Diagnostic(
                        severity="info",
                        message=f"Needed job '{need_name}' not yet defined (may come from include)",
                        source=SourceLocation(file=rel_path, line=line_no),
                        related_node=need_name,
                    )
                )
            state.edges.append(Edge(src=nid, dst=need_name, kind="needs"))

    if "variables" in config and isinstance(config["variables"], dict):
        _process_variables(config["variables"], nid, rel_path, raw_lines, state)

    if "trigger" in config:
        trigger = config["trigger"]
        if isinstance(trigger, dict):
            downstream = trigger.get("project", str(trigger))
        else:
            downstream = str(trigger)
        ghost_id = f"downstream:{downstream}"
        if ghost_id not in state.nodes:
            state.nodes[ghost_id] = Node(
                id=ghost_id, name=downstream, kind="ghost",
            )
        state.edges.append(Edge(src=nid, dst=ghost_id, kind="invokes"))

    for line in recipe:
        for m in _VAR_REF_RE.finditer(line):
            vname = m.group(1)
            var = state._get_or_create_variable(vname)
            if nid not in var.used_by:
                var.used_by.append(nid)


def _process_variables(
    vars_dict: dict, scope: str, rel_path: str, raw_lines: list[str],
    state: _ParserState,
) -> None:
    operator = "global" if scope == "global" else "job"
    for vname, vval in vars_dict.items():
        var = state._get_or_create_variable(str(vname))
        line_no = _find_key_line(raw_lines, str(vname))
        var.events.append(
            VariableEvent(
                source=SourceLocation(file=rel_path, line=line_no),
                operator=operator,
                scope=scope,
                raw_value=str(vval) if vval is not None else "",
            )
        )


def _process_includes(
    includes: Any, rel_path: str, filepath: str, state: _ParserState
) -> None:
    if isinstance(includes, str):
        includes = [{"local": includes}]
    elif isinstance(includes, dict):
        includes = [includes]
    elif not isinstance(includes, list):
        return

    for inc in includes:
        if isinstance(inc, str):
            inc = {"local": inc}
        if not isinstance(inc, dict):
            continue

        if "local" in inc:
            local_path = inc["local"]
            if local_path.startswith("/"):
                abs_path = os.path.join(state.repo_root, local_path.lstrip("/"))
            else:
                abs_path = os.path.join(state.repo_root, local_path)
            abs_path = os.path.abspath(abs_path)

            if os.path.isfile(abs_path):
                _parse_file(abs_path, state)
                inc_rel = os.path.relpath(abs_path, state.repo_root)
                state.edges.append(Edge(src=rel_path, dst=inc_rel, kind="includes"))
            else:
                inc_rel = os.path.relpath(abs_path, state.repo_root)
                state.files.append(
                    SourceFile(path=inc_rel, kind="gitlab_yaml", status="error")
                )
                state.diagnostics.append(
                    Diagnostic(
                        severity="warning",
                        message=f"Local include not found: {local_path}",
                        source=SourceLocation(file=rel_path, line=1),
                    )
                )
        else:
            inc_type = next(
                (k for k in ("project", "remote", "template", "component") if k in inc),
                "unknown",
            )
            inc_ref = inc.get(inc_type, str(inc))
            sf = SourceFile(
                path=f"[{inc_type}:{inc_ref}]",
                kind="gitlab_yaml",
                status="unresolved",
            )
            state.files.append(sf)
            state.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    message=f"Cannot resolve {inc_type} include: {inc_ref} (not fetched by policy)",
                    source=SourceLocation(file=rel_path, line=1),
                )
            )


def _resolve_extends(state: _ParserState) -> None:
    resolved: set[str] = set()

    def resolve(job_id: str) -> None:
        if job_id in resolved:
            return
        resolved.add(job_id)

        if job_id not in state.extends_map:
            return

        parents = state.extends_map[job_id]
        if isinstance(parents, str):
            parents = [parents]

        node = state.nodes.get(job_id)
        if not node:
            return

        for parent_name in parents:
            if parent_name in state.extends_map:
                resolve(parent_name)

            if parent_name in state.nodes:
                parent_node = state.nodes[parent_name]
                state.edges.append(Edge(src=job_id, dst=parent_name, kind="extends"))

                parent_config = state.job_configs.get(parent_name, {})
                job_config = state.job_configs.get(job_id, {})

                parent_image = parent_config.get("image") or parent_node.annotations.get("image")
                if "image" not in job_config and "image" not in node.annotations and parent_image:
                    node.annotations["image"] = parent_image
                    node.annotations.setdefault("inherited", []).append(
                        f"image from {parent_name} ({parent_node.source.file}:{parent_node.source.line})"
                        if parent_node.source else f"image from {parent_name}"
                    )

                if "before_script" in parent_config and "before_script" not in job_config:
                    inherited_lines = [
                        f"[before_script] {line}" for line in parent_config.get("before_script", [])
                    ]
                    node.recipe = inherited_lines + node.recipe

                if "variables" in parent_config and isinstance(parent_config["variables"], dict):
                    for vname, vval in parent_config["variables"].items():
                        var = state._get_or_create_variable(str(vname))
                        already_has = any(
                            e.scope == job_id for e in var.events
                        )
                        if not already_has:
                            var.events.append(
                                VariableEvent(
                                    source=parent_node.source or SourceLocation(file="unknown", line=0),
                                    operator="extends-inherited",
                                    scope=job_id,
                                    raw_value=str(vval) if vval is not None else "",
                                )
                            )
            else:
                ghost_id = parent_name
                state.nodes[ghost_id] = Node(
                    id=ghost_id, name=parent_name, kind="ghost",
                )
                state.edges.append(Edge(src=job_id, dst=ghost_id, kind="extends"))
                state.diagnostics.append(
                    Diagnostic(
                        severity="warning",
                        message=f"Extends undefined key: {parent_name}",
                        source=node.source,
                        related_node=ghost_id,
                    )
                )

    for job_id in list(state.extends_map.keys()):
        resolve(job_id)

    if state.global_defaults:
        for nid, node in state.nodes.items():
            if node.kind != "job":
                continue
            if "template" in node.flags:
                continue
            config = state.job_configs.get(nid, {})
            if "image" not in config and "image" not in node.annotations and "image" in state.global_defaults:
                node.annotations["image"] = state.global_defaults["image"]
                node.annotations.setdefault("inherited", []).append("image from default")


def _build_stage_edges(state: _ParserState) -> None:
    if not state.stages:
        return

    for i in range(len(state.stages) - 1):
        src_id = f"stage:{state.stages[i]}"
        dst_id = f"stage:{state.stages[i + 1]}"
        if src_id in state.nodes and dst_id in state.nodes:
            state.edges.append(Edge(src=src_id, dst=dst_id, kind="stage_order"))

    jobs_with_needs = {e.src for e in state.edges if e.kind == "needs"}

    for nid, node in state.nodes.items():
        if node.kind != "job":
            continue
        stage = node.annotations.get("stage")
        if not stage:
            continue
        stage_id = f"stage:{stage}"
        if stage_id not in state.nodes:
            continue

        if nid not in jobs_with_needs:
            stage_idx = state.stages.index(stage) if stage in state.stages else -1
            if stage_idx > 0:
                prev_stage = state.stages[stage_idx - 1]
                prev_jobs = [
                    n.id for n in state.nodes.values()
                    if n.kind == "job" and n.annotations.get("stage") == prev_stage
                    and "template" not in n.flags
                ]
                for pj in prev_jobs:
                    state.edges.append(Edge(src=nid, dst=pj, kind="stage_order"))


def _find_key_line(lines: list[str], key: str) -> int:
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    for i, line in enumerate(lines, 1):
        if pattern.match(line):
            return i
    return 1


def _extract_doc_above(lines: list[str], line_no: int) -> str | None:
    if line_no <= 1:
        return None
    prev_line = lines[line_no - 2].strip() if line_no - 1 < len(lines) else ""
    if prev_line.startswith("##"):
        return prev_line[2:].strip()
    return None


def _extract_docs_from_lines(lines: list[str], rel_path: str) -> dict[int, str]:
    docs: dict[int, str] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("##"):
            docs[i + 1] = stripped[2:].strip()
    return docs
