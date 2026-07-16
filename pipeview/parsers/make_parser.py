from __future__ import annotations

import os
import re
from pathlib import Path

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

_VAR_REF_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VAR_ASSIGN_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*(::=|:=|\?=|\+=|!=|=)\s*(.*)"
)
_TARGET_SPECIFIC_VAR_RE = re.compile(
    r"^([^:=]+?):\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*(::=|:=|\?=|\+=|!=|=)\s*(.*)"
)
_RULE_RE = re.compile(r"^([^:=]+?)\s*(::?)\s*(.*)")
_INCLUDE_RE = re.compile(r"^(-?include|sinclude)\s+(.+)")
_CONDITIONAL_RE = re.compile(r"^(ifeq|ifneq|ifdef|ifndef)\s*(.*)")
_MAKE_RECURSE_RE = re.compile(
    r"\$\(MAKE\)\s+(?:-C\s+(\S+)|-f\s+(\S+))"
    r"|"
    r"\$\{MAKE\}\s+(?:-C\s+(\S+)|-f\s+(\S+))"
)
_PHONY_RE = re.compile(r"^\.PHONY\s*:\s*(.*)")
_DEFAULT_GOAL_RE = re.compile(r"^\.DEFAULT_GOAL\s*(?::?=|:)\s*(.*)")
_DEFINE_RE = re.compile(r"^define\s+([A-Za-z_][A-Za-z0-9_.\-]*)\s*(?:=|:=|\?=|\+=|::=)?$")


def _extract_var_refs(text: str) -> list[str]:
    refs = []
    for m in _VAR_REF_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name:
            refs.append(name)
    return refs


def _is_computed_ref(text: str) -> bool:
    return "$($(" in text or "${${" in text or "$($" in text


class _ParserState:
    def __init__(self, root_path: str, namespace: str = ""):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.variables: dict[str, Variable] = {}
        self.files: list[SourceFile] = []
        self.diagnostics: list[Diagnostic] = []
        self.default_goal: str | None = None
        self.explicit_default_goal: bool = False
        self.first_target: str | None = None
        self.phony_targets: set[str] = set()
        self.namespace = namespace
        self.root_path = root_path
        self.parsed_files: set[str] = set()
        self.condition_stack: list[str] = []

    def _node_id(self, name: str) -> str:
        if self.namespace:
            return f"{self.namespace}:{name}"
        return name

    def _get_or_create_variable(self, name: str) -> Variable:
        if name not in self.variables:
            self.variables[name] = Variable(name=name)
        return self.variables[name]

    def _record_var_usage(self, text: str, user_id: str) -> None:
        for vname in _extract_var_refs(text):
            var = self._get_or_create_variable(vname)
            if user_id not in var.used_by:
                var.used_by.append(user_id)

        if _is_computed_ref(text):
            self.diagnostics.append(
                Diagnostic(
                    severity="info",
                    message=f"Computed variable reference in: {text[:80]}",
                )
            )


def parse_makefile(path: str, namespace: str = "") -> Report:
    root_path = os.path.abspath(path)
    if os.path.isdir(root_path):
        for candidate in ("GNUmakefile", "Makefile", "makefile"):
            p = os.path.join(root_path, candidate)
            if os.path.isfile(p):
                root_path = p
                break
        else:
            return Report(
                root=path,
                format="makefile",
                diagnostics=[
                    Diagnostic(severity="error", message=f"No makefile found in {path}")
                ],
            )

    state = _ParserState(root_path, namespace)
    _parse_file(root_path, state)

    for name in state.phony_targets:
        nid = state._node_id(name)
        if nid in state.nodes:
            state.nodes[nid].flags.add("phony")

    if not state.explicit_default_goal and state.first_target:
        state.default_goal = state._node_id(state.first_target)
    if state.default_goal and state.default_goal in state.nodes:
        state.nodes[state.default_goal].flags.add("default_goal")

    report = Report(
        root=path,
        format="makefile",
        nodes=list(state.nodes.values()),
        edges=state.edges,
        variables=list(state.variables.values()),
        files=state.files,
        diagnostics=state.diagnostics,
        default_goal=state.default_goal,
    )
    return report


def _parse_file(filepath: str, state: _ParserState) -> None:
    filepath = os.path.abspath(filepath)
    if filepath in state.parsed_files:
        return
    state.parsed_files.add(filepath)

    rel_path = os.path.relpath(filepath, os.path.dirname(state.root_path))
    sf = SourceFile(path=rel_path, kind="makefile", status="ok")

    if not os.path.isfile(filepath):
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(
                severity="warning",
                message=f"Included file not found: {rel_path}",
            )
        )
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except OSError as e:
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="error", message=f"Cannot read {rel_path}: {e}")
        )
        return

    state.files.append(sf)
    lines = _join_continuations(raw_lines)
    filedir = os.path.dirname(filepath)

    pending_doc: str | None = None
    i = 0
    in_define: str | None = None
    define_lines: list[str] = []
    define_start_line: int = 0

    while i < len(lines):
        line_text, line_no = lines[i]
        stripped = line_text.strip()
        i += 1

        if in_define is not None:
            if stripped == "endef":
                var = state._get_or_create_variable(in_define)
                var.events.append(
                    VariableEvent(
                        source=SourceLocation(file=rel_path, line=define_start_line),
                        operator="=",
                        scope="global",
                        raw_value="\n".join(define_lines),
                    )
                )
                if state.condition_stack:
                    var.events[-1].source  # already set
                in_define = None
                define_lines = []
            else:
                define_lines.append(line_text.rstrip("\n"))
            continue

        if not stripped or stripped.startswith("#"):
            if stripped.startswith("##"):
                pending_doc = stripped[2:].strip()
            continue

        dm = _DEFINE_RE.match(stripped)
        if dm:
            in_define = dm.group(1)
            define_start_line = line_no
            define_lines = []
            continue

        cm = _CONDITIONAL_RE.match(stripped)
        if cm:
            cond_type = cm.group(1)
            cond_expr = cm.group(2).strip()
            state.condition_stack.append(f"{cond_type} {cond_expr}")
            continue

        if stripped == "else":
            if state.condition_stack:
                top = state.condition_stack[-1]
                state.condition_stack[-1] = f"else ({top})"
            continue

        if stripped == "endif":
            if state.condition_stack:
                state.condition_stack.pop()
            continue

        im = _INCLUDE_RE.match(stripped)
        if im:
            directive = im.group(1)
            files_str = im.group(2).strip()
            optional = directive in ("-include", "sinclude")
            for inc_file in files_str.split():
                inc_file = inc_file.strip()
                if not inc_file:
                    continue
                if "$(" in inc_file or "${" in inc_file:
                    state.diagnostics.append(
                        Diagnostic(
                            severity="info",
                            message=f"Include path contains variable reference: {inc_file}",
                            source=SourceLocation(file=rel_path, line=line_no),
                        )
                    )
                    continue
                inc_path = os.path.join(filedir, inc_file)
                inc_abs = os.path.abspath(inc_path)
                if os.path.isfile(inc_abs):
                    _parse_file(inc_abs, state)
                    inc_rel = os.path.relpath(inc_abs, os.path.dirname(state.root_path))
                    state.edges.append(
                        Edge(src=rel_path, dst=inc_rel, kind="includes")
                    )
                else:
                    sev: str = "info" if optional else "warning"
                    state.diagnostics.append(
                        Diagnostic(
                            severity=sev,
                            message=f"{'Optional include' if optional else 'Include'} file not found: {inc_file}",
                            source=SourceLocation(file=rel_path, line=line_no),
                        )
                    )
                    inc_rel = os.path.relpath(inc_abs, os.path.dirname(state.root_path))
                    sf_inc = SourceFile(
                        path=inc_rel, kind="makefile",
                        status="unresolved" if optional else "error",
                    )
                    if inc_abs not in state.parsed_files:
                        state.files.append(sf_inc)
                        state.parsed_files.add(inc_abs)
            continue

        dgm = _DEFAULT_GOAL_RE.match(stripped)
        if dgm:
            goal_name = dgm.group(1).strip()
            if goal_name:
                state.default_goal = state._node_id(goal_name)
                state.explicit_default_goal = True
            continue

        pm = _PHONY_RE.match(stripped)
        if pm:
            targets = pm.group(1).strip().split()
            state.phony_targets.update(targets)
            continue

        if stripped.startswith(".") and ":" in stripped:
            special = stripped.split(":")[0].strip()
            if special in (
                ".SUFFIXES", ".DEFAULT", ".PRECIOUS", ".INTERMEDIATE",
                ".SECONDARY", ".SECONDEXPANSION", ".DELETE_ON_ERROR",
                ".IGNORE", ".LOW_RESOLUTION_TIME", ".SILENT",
                ".EXPORT_ALL_VARIABLES", ".NOTPARALLEL", ".ONESHELL",
                ".POSIX",
            ):
                continue

        tsvm = _TARGET_SPECIFIC_VAR_RE.match(stripped)
        if tsvm:
            target_names_str = tsvm.group(1).strip()
            var_name = tsvm.group(2)
            op = tsvm.group(3)
            val = tsvm.group(4).strip()

            for tname in target_names_str.split():
                tname = tname.strip()
                if not tname:
                    continue
                nid = state._node_id(tname)
                var = state._get_or_create_variable(var_name)
                var.events.append(
                    VariableEvent(
                        source=SourceLocation(file=rel_path, line=line_no),
                        operator=op,
                        scope=nid,
                        raw_value=val,
                    )
                )
                state._record_var_usage(val, nid)
            pending_doc = None
            continue

        vam = _VAR_ASSIGN_RE.match(stripped)
        if vam:
            var_name = vam.group(1)
            op = vam.group(2)
            val = vam.group(3).strip()

            var = state._get_or_create_variable(var_name)
            evt = VariableEvent(
                source=SourceLocation(file=rel_path, line=line_no),
                operator=op,
                scope="global",
                raw_value=val,
            )
            var.events.append(evt)
            if state.condition_stack:
                condition_str = " && ".join(state.condition_stack)
                # Store condition context in annotations — variable events don't have annotations,
                # so we note it in the raw_value context via diagnostics
                state.diagnostics.append(
                    Diagnostic(
                        severity="info",
                        message=f"Variable '{var_name}' assigned under condition: {condition_str}",
                        source=SourceLocation(file=rel_path, line=line_no),
                    )
                )
            state._record_var_usage(val, f"var:{var_name}")
            pending_doc = None
            continue

        rm = _RULE_RE.match(stripped)
        if rm:
            targets_str = rm.group(1).strip()
            separator = rm.group(2)
            deps_str = rm.group(3).strip()
            is_double_colon = separator == "::"

            inline_doc: str | None = None
            if "##" in deps_str:
                parts = deps_str.split("##", 1)
                deps_str = parts[0].strip()
                inline_doc = parts[1].strip()

            order_only_deps: list[str] = []
            normal_deps: list[str] = []
            if "|" in deps_str:
                normal_part, oo_part = deps_str.split("|", 1)
                normal_deps = normal_part.split()
                order_only_deps = oo_part.split()
            else:
                normal_deps = deps_str.split()

            target_names = targets_str.split()

            recipe_lines: list[str] = []
            while i < len(lines):
                next_text, _ = lines[i]
                if next_text.startswith("\t"):
                    recipe_lines.append(next_text[1:].rstrip("\n"))
                    i += 1
                else:
                    break

            for tname in target_names:
                tname = tname.strip()
                if not tname:
                    continue

                is_pattern = "%" in tname
                kind = "pattern_rule" if is_pattern else "target"
                nid = state._node_id(tname)

                doc = inline_doc or pending_doc
                is_hidden = tname.startswith(".")

                if nid in state.nodes:
                    node = state.nodes[nid]
                    if recipe_lines:
                        node.recipe = recipe_lines
                    if doc and not node.doc:
                        node.doc = doc
                else:
                    node = Node(
                        id=nid,
                        name=tname,
                        kind=kind,
                        source=SourceLocation(file=rel_path, line=line_no),
                        recipe=recipe_lines,
                        doc=doc,
                        flags=set(),
                        annotations={},
                    )
                    if is_hidden:
                        node.flags.add("hidden")
                    state.nodes[nid] = node

                if is_double_colon:
                    node.annotations["double_colon"] = True

                if state.condition_stack:
                    node.annotations["condition"] = " && ".join(state.condition_stack)

                if state.first_target is None and not is_pattern and not is_hidden:
                    state.first_target = tname

                for dep in normal_deps:
                    dep = dep.strip()
                    if not dep:
                        continue
                    dep_id = state._node_id(dep)
                    if dep_id not in state.nodes:
                        state.nodes[dep_id] = Node(
                            id=dep_id, name=dep, kind="ghost",
                        )
                    state.edges.append(
                        Edge(src=nid, dst=dep_id, kind="prerequisite")
                    )

                for dep in order_only_deps:
                    dep = dep.strip()
                    if not dep:
                        continue
                    dep_id = state._node_id(dep)
                    if dep_id not in state.nodes:
                        state.nodes[dep_id] = Node(
                            id=dep_id, name=dep, kind="ghost",
                        )
                    state.edges.append(
                        Edge(src=nid, dst=dep_id, kind="order_only")
                    )

                for rline in recipe_lines:
                    state._record_var_usage(rline, nid)
                    for m in _MAKE_RECURSE_RE.finditer(rline):
                        subdir = m.group(1) or m.group(3)
                        subfile = m.group(2) or m.group(4)
                        _handle_recursion(
                            state, nid, subdir, subfile,
                            filedir, rel_path, line_no,
                        )

                for dep in normal_deps + order_only_deps:
                    state._record_var_usage(dep, nid)

            pending_doc = None
            continue

        state.diagnostics.append(
            Diagnostic(
                severity="warning",
                message=f"Unparseable line: {stripped[:100]}",
                source=SourceLocation(file=rel_path, line=line_no),
            )
        )
        pending_doc = None


def _handle_recursion(
    state: _ParserState,
    parent_nid: str,
    subdir: str | None,
    subfile: str | None,
    filedir: str,
    rel_path: str,
    line_no: int,
) -> None:
    if subdir:
        sub_makefile = None
        sub_abs_dir = os.path.abspath(os.path.join(filedir, subdir))
        for candidate in ("GNUmakefile", "Makefile", "makefile"):
            p = os.path.join(sub_abs_dir, candidate)
            if os.path.isfile(p):
                sub_makefile = p
                break
        if sub_makefile is None:
            state.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    message=f"Recursive make target directory has no Makefile: {subdir}",
                    source=SourceLocation(file=rel_path, line=line_no),
                )
            )
            return
        ns = subdir
    elif subfile:
        sub_makefile = os.path.abspath(os.path.join(filedir, subfile))
        if not os.path.isfile(sub_makefile):
            state.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    message=f"Recursive make file not found: {subfile}",
                    source=SourceLocation(file=rel_path, line=line_no),
                )
            )
            return
        ns = os.path.splitext(os.path.basename(subfile))[0]
    else:
        return

    old_ns = state.namespace
    state.namespace = ns if not old_ns else f"{old_ns}/{ns}"
    _parse_file(sub_makefile, state)
    state.namespace = old_ns

    sub_rel = os.path.relpath(sub_makefile, os.path.dirname(state.root_path))
    state.edges.append(Edge(src=parent_nid, dst=sub_rel, kind="invokes"))


def _join_continuations(raw_lines: list[str]) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    accum = ""
    start_line = 1
    for i, line in enumerate(raw_lines, 1):
        line = line.rstrip("\n")
        if line.endswith("\\"):
            if not accum:
                start_line = i
            accum += line[:-1] + " "
        else:
            if accum:
                accum += line
                result.append((accum, start_line))
                accum = ""
            else:
                result.append((line, i))
    if accum:
        result.append((accum, start_line))
    return result
