from __future__ import annotations

import glob as _glob
import os
import re

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

# Variable *references*: $(NAME) / ${NAME}, including substitution references
# $(NAME:.c=.o) and hyphenated/dotted names ($(run-tests)). Function calls like
# $(if ...) never match because a function name is followed by whitespace, not
# ')' / ':' / '}'.
_VAR_REF_RE = re.compile(
    r"\$\(([A-Za-z_][A-Za-z0-9_.\-]*)(?::[^)]*)?\)"
    r"|\$\{([A-Za-z_][A-Za-z0-9_.\-]*)(?::[^}]*)?\}"
)

_CONDITIONAL_START_RE = re.compile(r"^(ifeq|ifneq|ifdef|ifndef)\b\s*(.*)$")
_INCLUDE_RE = re.compile(r"^(-?include|sinclude)\s+(.+)")
_DEFINE_RE = re.compile(
    r"^define\s+([^\s=]+)\s*(=|:=|::=|\+=|\?=)?\s*$"
)
_VPATH_RE = re.compile(r"^vpath\b\s*(.*)$")
_UNDEFINE_RE = re.compile(r"^undefine\s+(.+)$")

# Recipes may recurse into sub-makes in several spellings; the -C/-f argument
# may itself be a variable reference resolved from the static table.
_MAKE_RECURSE_RE = re.compile(
    r"(?:\bcd\s+(?P<cd>\S+)\s*(?:&&|;)\s*)?"
    r"[+@-]*\$[({]MAKE[)}](?:\s+(?P<flag>-C|-f)\s+(?P<arg>\S+))?"
)

_PHONY_RE = re.compile(r"^\.PHONY\s*:\s*(.*)")
_DEFAULT_GOAL_RE = re.compile(r"^\.DEFAULT_GOAL\s*(?::{1,3}=|\?=|\+=|=)\s*(.*)")

# Assignment operators, longest first so ':::=' wins over '::=' over ':='.
_ASSIGN_OPS = (":::=", "::=", ":=", "?=", "+=", "!=", "=")

# Special targets that are directives to make, never build nodes.
_SPECIAL_TARGETS = {
    ".SUFFIXES", ".DEFAULT", ".PRECIOUS", ".INTERMEDIATE", ".SECONDARY",
    ".SECONDEXPANSION", ".DELETE_ON_ERROR", ".IGNORE", ".LOW_RESOLUTION_TIME",
    ".SILENT", ".EXPORT_ALL_VARIABLES", ".NOTPARALLEL", ".ONESHELL", ".POSIX",
    ".PHONY", ".DEFAULT_GOAL", ".WAIT",
}

# Variables that GNU make defines out of the box ("origin: default"). If a
# makefile references one without assigning it, that is not "unresolved" —
# make supplies the value.
_BUILTIN_DEFAULT_VARS = {
    "AR", "ARFLAGS", "AS", "ASFLAGS", "CC", "CFLAGS", "CO", "COFLAGS", "CPP",
    "CPPFLAGS", "CTANGLE", "CWEAVE", "CXX", "CXXFLAGS", "FC", "FFLAGS", "GET",
    "LD", "LDFLAGS", "LDLIBS", "LEX", "LFLAGS", "LINT", "LINTFLAGS", "M2C",
    "MAKE", "MAKEFLAGS", "MAKECMDGOALS", "MAKEFILE_LIST", "MAKELEVEL",
    "MAKE_VERSION", "MAKESHELL", "OBJC", "OUTPUT_OPTION", "PC", "PFLAGS", "RM",
    "SHELL", "TANGLE", "TEX", "TEXI2DVI", "WEAVE", "YACC", "YFLAGS", "CURDIR",
    ".DEFAULT_GOAL", ".RECIPEPREFIX", "VPATH", "GPATH",
}

_MAX_INCLUDE_DEPTH = 64
_MAX_RECURSION_DEPTH = 16
_MAX_STATIC_EXPAND_DEPTH = 10


def _strip_dollar_dollar(text: str) -> str:
    """Remove literal '$$' pairs so shell constructs ($$HOME, $$(pwd),
    $${USER}) are never mistaken for make variable references. '$$$(X)'
    correctly leaves '$(X)' behind."""
    return text.replace("$$", "")


def _extract_var_refs(text: str) -> list[str]:
    refs = []
    for m in _VAR_REF_RE.finditer(_strip_dollar_dollar(text)):
        name = m.group(1) or m.group(2)
        if name:
            refs.append(name)
    return refs


def _is_computed_ref(text: str) -> bool:
    t = _strip_dollar_dollar(text)
    return "$($(" in t or "${${" in t or "$(${" in t or "${$(" in t


def _split_outside_refs(line: str, chars: str) -> int:
    """Return the index of the first occurrence of any char in `chars` that
    sits outside $()/${} references (and is not a literal '$$c'), or -1."""
    depth = 0
    stack: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "$" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == "$":
                i += 2
                continue
            if nxt == "(":
                stack.append(")")
                depth += 1
                i += 2
                continue
            if nxt == "{":
                stack.append("}")
                depth += 1
                i += 2
                continue
        if depth and c == stack[-1]:
            stack.pop()
            depth -= 1
            i += 1
            continue
        if depth == 0 and c in chars:
            return i
        i += 1
    return -1


def _find_assignment(line: str) -> tuple[str, str, str] | None:
    """Tokenize `line` as `NAME <op> VALUE` if it is a variable assignment.
    The operator search is reference-aware: '=' or ':' inside $(...) never
    counts, and the ':' of ':=' / '::=' / ':::=' is part of the operator, not
    a rule separator. Returns (name, op, raw_value) or None."""
    idx = _split_outside_refs(line, "=:")
    if idx < 0:
        return None
    if line[idx] == "=":
        # plain '=' — check for ?= += != spelled with the flavor char before it
        if idx > 0 and line[idx - 1] in "?+!":
            name = line[: idx - 1]
            op = line[idx - 1 : idx + 1]
        else:
            name = line[:idx]
            op = "="
        value = line[idx + 1 :]
    else:
        # ':' — assignment only if a colon-run ends in '='  (:= ::= :::=)
        j = idx
        while j < len(line) and line[j] == ":":
            j += 1
        if j < len(line) and line[j] == "=" and (j - idx) <= 3:
            name = line[:idx]
            op = line[idx : j + 1]
            value = line[j + 1 :]
        else:
            return None
    name = name.strip()
    if not name or any(ch.isspace() for ch in name):
        return None
    return name, op, _strip_value(value)


def _strip_value(value: str) -> str:
    """Strip a trailing comment (respecting \\# escapes) and surrounding
    whitespace from an assignment's right-hand side; unescape \\#."""
    out = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n and value[i + 1] == "#":
            out.append("#")
            i += 2
            continue
        if c == "#":
            break
        out.append(c)
        i += 1
    return "".join(out).strip()


def _peel_directives(line: str) -> tuple[set[str], str]:
    """Peel stacked variable directives (export/unexport/override/private)
    off the front of a logical line. `override export VAR := x` and
    `export override VAR = y` are both legal; a fixed list of whole-line
    patterns can never enumerate the combinations."""
    directives: set[str] = set()
    rest = line
    while True:
        m = re.match(r"^(export|unexport|override|private)(?:\s+(.*))?$", rest)
        if not m:
            break
        directives.add(m.group(1))
        rest = (m.group(2) or "").strip()
        if not rest:
            break
    return directives, rest


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
        self.include_stack: list[str] = []
        self.recursion_stack: list[str] = []
        self.condition_stack: list[str] = []
        self.export_all: bool = False
        self.oneshell: bool = False
        self.recipe_prefix: str = "\t"

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
                    message=(
                        "Computed variable reference (name built from another "
                        f"variable) — reference tracking is incomplete here: {text[:80]}"
                    ),
                )
            )

    def _static_value(self, name: str) -> str | None:
        var = self.variables.get(name)
        if not var:
            return None
        for evt in reversed(var.events):
            if evt.scope == "global" and not evt.annotations.get("undefine"):
                return evt.raw_value
        return None

    def _static_expand(self, text: str, depth: int = 0) -> str | None:
        """Expand simple $(VAR)/${VAR} references from the static table.
        Returns None when something in `text` cannot be resolved statically
        ($(shell …), undefined variables, computed names)."""
        if depth > _MAX_STATIC_EXPAND_DEPTH:
            return None
        if "$" not in _strip_dollar_dollar(text):
            return text

        def sub(m: re.Match) -> str:
            name = m.group(1) or m.group(2)
            val = self._static_value(name)
            if val is None:
                raise LookupError(name)
            expanded = self._static_expand(val, depth + 1)
            if expanded is None:
                raise LookupError(name)
            return expanded

        try:
            out = _VAR_REF_RE.sub(sub, text)
        except LookupError:
            return None
        if "$" in _strip_dollar_dollar(out):
            return None
        return out

    def _event_annotations(self, directives: set[str] | None = None) -> dict:
        ann: dict = {}
        if self.condition_stack:
            ann["condition"] = " && ".join(self.condition_stack)
        for d in directives or ():
            if d == "unexport":
                ann["unexported"] = True
            else:
                ann[d] = True
        return ann


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

    if state.export_all:
        for var in state.variables.values():
            if var.events:
                var.exported = True

    # A referenced-but-never-assigned variable that make itself provides is a
    # built-in default, not a mystery.
    for var in state.variables.values():
        if not var.events and var.name in _BUILTIN_DEFAULT_VARS:
            var.origin = "built-in default"

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


def _read_logical_lines(filepath: str) -> list[tuple[str, int]] | None:
    """Read a makefile into (logical_line, start_line_no) pairs: BOM stripped,
    CRLF tolerated, backslash-newline continuations joined."""
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            raw_lines = [ln.rstrip("\n").rstrip("\r") for ln in f.readlines()]
    except OSError:
        return None

    result: list[tuple[str, int]] = []
    accum = ""
    start_line = 1
    for i, line in enumerate(raw_lines, 1):
        if line.endswith("\\") and not line.endswith("\\\\"):
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


def _parse_file(filepath: str, state: _ParserState) -> None:
    filepath = os.path.abspath(filepath)
    if filepath in state.parsed_files:
        return
    state.parsed_files.add(filepath)
    state.include_stack.append(filepath)
    try:
        _parse_file_inner(filepath, state)
    finally:
        state.include_stack.pop()


def _parse_file_inner(filepath: str, state: _ParserState) -> None:
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

    lines = _read_logical_lines(filepath)
    if lines is None:
        sf.status = "error"
        state.files.append(sf)
        state.diagnostics.append(
            Diagnostic(severity="error", message=f"Cannot read {rel_path}")
        )
        return

    state.files.append(sf)
    filedir = os.path.dirname(filepath)

    pending_doc: str | None = None
    i = 0

    while i < len(lines):
        line_text, line_no = lines[i]
        stripped = line_text.strip()
        i += 1
        loc = SourceLocation(file=rel_path, line=line_no)

        if not stripped:
            continue
        if stripped.startswith("#"):
            if stripped.startswith("##"):
                pending_doc = stripped[2:].strip()
            continue

        # --- conditionals (make's, i.e. not tab-indented recipe text) -----
        cm = _CONDITIONAL_START_RE.match(stripped)
        if cm and not line_text.startswith(state.recipe_prefix):
            state.condition_stack.append(f"{cm.group(1)} {cm.group(2).strip()}")
            continue
        if stripped == "else" or stripped.startswith("else "):
            if state.condition_stack:
                top = state.condition_stack[-1]
                state.condition_stack[-1] = f"else ({top})"
            else:
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message="'else' without matching conditional (real make rejects this)",
                    source=loc,
                ))
            continue
        if stripped == "endif":
            if state.condition_stack:
                state.condition_stack.pop()
            else:
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message="'endif' without matching conditional (real make rejects this)",
                    source=loc,
                ))
            continue

        # --- include / vpath ---------------------------------------------
        im = _INCLUDE_RE.match(stripped)
        if im:
            _handle_include(im.group(1), im.group(2).strip(), filedir, rel_path,
                            line_no, state)
            continue

        vm = _VPATH_RE.match(stripped)
        if vm:
            state.diagnostics.append(Diagnostic(
                severity="info",
                message=(
                    f"vpath directive ('vpath {vm.group(1)}') — prerequisites may "
                    "resolve through search paths the graph doesn't show"
                ),
                source=loc,
            ))
            continue

        # --- variable directives: export/unexport/override/private/undefine/define
        directives, rest = _peel_directives(stripped)
        if directives:
            handled = _handle_directive_line(
                directives, rest, rel_path, line_no, lines, i, state
            )
            if handled is not None:
                i = handled
                pending_doc = None
                continue
            # fall through: 'export'-prefixed something we could not model
            # lands in the assignment/rule logic below on the peeled rest.

        dm = _DEFINE_RE.match(rest if directives else stripped)
        if dm:
            i = _handle_define(dm, directives, rel_path, line_no, lines, i, state)
            pending_doc = None
            continue

        um = _UNDEFINE_RE.match(rest if directives else stripped)
        if um:
            for vname in um.group(1).split():
                var = state._get_or_create_variable(vname)
                ann = state._event_annotations(directives)
                ann["undefine"] = True
                var.events.append(VariableEvent(
                    source=loc, operator="undefine", scope="global",
                    raw_value="", annotations=ann,
                ))
            pending_doc = None
            continue

        # --- assignment vs rule: operator-first tokenization --------------
        # Assignments come first so `.DEFAULT_GOAL := x` / `.RECIPEPREFIX = >`
        # are read as the special-variable assignments they are, not as rules.
        target_line = rest if directives else stripped
        asg = _find_assignment(target_line)
        if asg:
            name, op, value = asg
            _record_assignment(name, op, value, directives, "global",
                               rel_path, line_no, state)
            if name == ".RECIPEPREFIX":
                state.recipe_prefix = value[-1] if value else "\t"
            elif name == ".DEFAULT_GOAL" and value.strip():
                state.default_goal = state._node_id(value.strip())
                state.explicit_default_goal = True
            pending_doc = None
            continue

        # --- special targets that are directives, not build nodes ---------
        if stripped.startswith("."):
            head = stripped.split(":", 1)[0].strip()
            if head in _SPECIAL_TARGETS and ":" in stripped:
                _handle_special_target(head, stripped, loc, state)
                pending_doc = None
                continue

        colon_idx = _split_outside_refs(target_line, ":")
        if colon_idx >= 0:
            i = _handle_rule_line(
                target_line, colon_idx, directives, pending_doc,
                rel_path, line_no, lines, i, filedir, state,
            )
            pending_doc = None
            continue

        # --- honest degradation ------------------------------------------
        _diagnose_unmodeled_line(stripped, loc, state)
        pending_doc = None


def _handle_directive_line(
    directives: set[str],
    rest: str,
    rel_path: str,
    line_no: int,
    lines: list[tuple[str, int]],
    i: int,
    state: _ParserState,
) -> int | None:
    """Handle export/unexport lines that are NOT assignments/defines:
    'export NAME…', bare 'export', bare 'unexport'. Returns the new loop
    index if fully handled here, else None to fall through."""
    loc = SourceLocation(file=rel_path, line=line_no)

    if rest == "":
        if not directives & {"export", "unexport"}:
            return None  # bare `override`/`private` — not valid make; fall through
        # bare `export` / `unexport`: the all-variables toggle
        if "export" in directives:
            state.export_all = True
        if "unexport" in directives:
            state.export_all = False
        which = "export" if "export" in directives else "unexport"
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Bare '{which}' directive: "
                + ("all variables are passed into recipe environments"
                   if which == "export" else
                   "cancels exporting all variables by default")
            ),
            source=loc,
        ))
        return i

    # `export NAME [NAME…]` / `unexport NAME` — a name list with no operator.
    if (
        directives <= {"export", "unexport", "override"}
        and _find_assignment(rest) is None
        and _split_outside_refs(rest, ":") < 0
        and not _DEFINE_RE.match(rest)
        and not _UNDEFINE_RE.match(rest)
    ):
        exporting = "unexport" not in directives
        for vname in rest.split():
            var = state._get_or_create_variable(vname)
            var.exported = exporting
            ann = state._event_annotations(directives)
            var.events.append(VariableEvent(
                source=loc,
                operator="export" if exporting else "unexport",
                scope="global",
                raw_value="",
                annotations=ann,
            ))
        return i

    return None


def _handle_define(
    dm: re.Match,
    directives: set[str],
    rel_path: str,
    start_line: int,
    lines: list[tuple[str, int]],
    i: int,
    state: _ParserState,
) -> int:
    """Consume a define…endef block (they nest) starting at lines[i]."""
    name = dm.group(1)
    op = dm.group(2) or "="
    body: list[str] = []
    depth = 1
    while i < len(lines):
        text, _ = lines[i]
        s = text.strip()
        i += 1
        if re.match(r"^(?:override\s+|export\s+)*define\s+\S+", s):
            depth += 1
        elif s == "endef" or s.startswith("endef "):
            depth -= 1
            if depth == 0:
                break
        if depth > 0:
            body.append(text)
    else:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=(
                f"'define {name}' is never closed by 'endef' "
                "(real make rejects this: missing 'endef')"
            ),
            source=SourceLocation(file=rel_path, line=start_line),
        ))

    _record_assignment(
        name, op, "\n".join(body), directives | {"define"}, "global",
        rel_path, start_line, state,
    )
    return i


def _record_assignment(
    name: str,
    op: str,
    value: str,
    directives: set[str],
    scope: str,
    rel_path: str,
    line_no: int,
    state: _ParserState,
) -> None:
    ann = state._event_annotations(directives)
    if "define" in directives:
        ann["define"] = True
    if op == "!=":
        # Shell assignment: the raw command is the value source. The static
        # pass records it and never executes it.
        ann["shell_assignment"] = True
    if "$" in _strip_dollar_dollar(name):
        ann["computed_name"] = True
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Variable name is computed at run time ('{name}') — recorded "
                "symbolically; the effective name is not statically known"
            ),
            source=SourceLocation(file=rel_path, line=line_no),
        ))

    var = state._get_or_create_variable(name)
    var.events.append(VariableEvent(
        source=SourceLocation(file=rel_path, line=line_no),
        operator=op,
        scope=scope,
        raw_value=value,
        annotations=ann,
    ))
    if "export" in directives:
        var.exported = True
    if "unexport" in directives:
        var.exported = False

    user = f"var:{name}" if scope == "global" else scope
    state._record_var_usage(value, user)


def _handle_special_target(
    head: str, stripped: str, loc: SourceLocation, state: _ParserState
) -> None:
    rest = stripped.split(":", 1)[1].strip()

    if head == ".PHONY":
        names = rest.split()
        unresolved = [n for n in names if "$" in _strip_dollar_dollar(n)]
        for n in names:
            if n not in unresolved:
                state.phony_targets.add(n)
        for n in unresolved:
            expanded = state._static_expand(n)
            if expanded is not None:
                state.phony_targets.update(expanded.split())
            else:
                state.diagnostics.append(Diagnostic(
                    severity="info",
                    message=(
                        f".PHONY list includes '{n}' which cannot be resolved "
                        "statically — some phony targets may be unmarked"
                    ),
                    source=loc,
                ))
        return

    if head == ".DEFAULT_GOAL":
        goal = rest.strip()
        if goal:
            state.default_goal = state._node_id(goal)
            state.explicit_default_goal = True
        return

    if head == ".EXPORT_ALL_VARIABLES":
        state.export_all = True
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=".EXPORT_ALL_VARIABLES in effect — every variable is passed into recipe environments",
            source=loc,
        ))
        return

    if head == ".ONESHELL":
        state.oneshell = True
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=".ONESHELL in effect — each recipe runs in a single shell invocation, not one shell per line",
            source=loc,
        ))
        return

    if head == ".SECONDEXPANSION":
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                ".SECONDEXPANSION in effect — prerequisite lists after this "
                "point are expanded twice; prerequisite analysis may be incomplete"
            ),
            source=loc,
        ))
        return

    # Remaining special targets (.SECONDARY, .PRECIOUS, .SUFFIXES, …) are
    # directives that tune build behavior; they are intentionally not nodes.


def _handle_include(
    directive: str,
    files_str: str,
    filedir: str,
    rel_path: str,
    line_no: int,
    state: _ParserState,
) -> None:
    loc = SourceLocation(file=rel_path, line=line_no)
    optional = directive in ("-include", "sinclude")

    if len(state.include_stack) > _MAX_INCLUDE_DEPTH:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"Include nesting deeper than {_MAX_INCLUDE_DEPTH} — stopping here",
            source=loc,
        ))
        return

    # $(wildcard …) inside an include is the common idiom for optional file
    # sets; expand it (and plain $(VAR) paths) statically where possible.
    wm = re.fullmatch(r"\$\(wildcard\s+(.+?)\)", files_str.strip())
    if wm:
        files_str = wm.group(1)
        optional = True  # wildcard of nothing includes nothing — never an error

    for inc_file in files_str.split():
        inc_file = inc_file.strip()
        if not inc_file:
            continue

        if "$" in _strip_dollar_dollar(inc_file):
            expanded = state._static_expand(inc_file)
            if expanded is None:
                state.diagnostics.append(Diagnostic(
                    severity="info",
                    message=(
                        f"Include path '{inc_file}' cannot be resolved statically "
                        "— the included file is not analyzed"
                    ),
                    source=loc,
                ))
                continue
            inc_file = expanded

        matches: list[str]
        if any(ch in inc_file for ch in "*?["):
            matches = sorted(_glob.glob(os.path.join(filedir, inc_file)))
            if not matches:
                continue  # glob matching nothing includes nothing
        else:
            matches = [os.path.join(filedir, inc_file)]

        for inc_path in matches:
            inc_abs = os.path.abspath(inc_path)
            inc_rel = os.path.relpath(inc_abs, os.path.dirname(state.root_path))
            if inc_abs in state.include_stack:
                cycle = [os.path.relpath(p, os.path.dirname(state.root_path))
                         for p in state.include_stack[state.include_stack.index(inc_abs):]]
                state.diagnostics.append(Diagnostic(
                    severity="warning",
                    message="Include cycle: " + " → ".join(cycle + [inc_rel]),
                    source=loc,
                ))
                continue
            if os.path.isfile(inc_abs):
                _parse_file(inc_abs, state)
                state.edges.append(Edge(src=rel_path, dst=inc_rel, kind="includes"))
            else:
                sev = "info" if optional else "warning"
                state.diagnostics.append(Diagnostic(
                    severity=sev,
                    message=(
                        f"{'Optional include' if optional else 'Include'} file "
                        f"not found: {inc_file}"
                        + ("" if optional else " (real make stops here)")
                    ),
                    source=loc,
                ))
                if inc_abs not in state.parsed_files:
                    state.files.append(SourceFile(
                        path=inc_rel, kind="makefile",
                        status="unresolved" if optional else "error",
                    ))
                    state.parsed_files.add(inc_abs)


def _handle_rule_line(
    line: str,
    colon_idx: int,
    directives: set[str],
    pending_doc: str | None,
    rel_path: str,
    line_no: int,
    lines: list[tuple[str, int]],
    i: int,
    filedir: str,
    state: _ParserState,
) -> int:
    """Parse a line whose first depth-0 token is ':' — a rule, a
    target-specific variable, or a static pattern rule."""
    loc = SourceLocation(file=rel_path, line=line_no)

    targets_str = line[:colon_idx].strip()
    after = line[colon_idx:]

    # separator: ':' '::' or grouped '&:'
    is_double_colon = after.startswith("::")
    rest = after[2:] if is_double_colon else after[1:]
    is_grouped = targets_str.endswith("&")
    if is_grouped:
        targets_str = targets_str[:-1].strip()

    rest_stripped = rest.strip()

    # --- target-specific variable? t: [private] VAR op VALUE --------------
    # _find_assignment succeeds only when the first depth-0 token after the
    # rule colon is an assignment operator, so `foo: PATH := /usr:/bin`
    # (colon in the VALUE) is an assignment while `a.o: %.o: %.c` is not.
    ts_directives, ts_rest = _peel_directives(rest_stripped)
    ts_asg = _find_assignment(ts_rest)
    if ts_asg:
        var_name, op, value = ts_asg
        for tname in targets_str.split():
            nid = state._node_id(tname)
            ann = state._event_annotations(directives | ts_directives)
            if "%" in tname:
                ann["pattern"] = tname
            elif "private" not in ts_directives:
                # target-specific values reach the target's prerequisites too —
                # that behavior surprises people, so surface it.
                ann["propagates_to_prereqs"] = True
            var = state._get_or_create_variable(var_name)
            var.events.append(VariableEvent(
                source=loc, operator=op, scope=nid, raw_value=value,
                annotations=ann,
            ))
            if "export" in (directives | ts_directives):
                var.exported = True
            state._record_var_usage(value, nid)
        return i

    # --- static pattern rule? targets : %-pattern : prereq-pattern --------
    second_colon = _split_outside_refs(rest_stripped, ":")
    static_pattern: tuple[str, str] | None = None
    if second_colon >= 0:
        target_pattern = rest_stripped[:second_colon].strip()
        prereq_pattern = rest_stripped[second_colon + 1:].strip()
        if "%" in target_pattern:
            static_pattern = (target_pattern, prereq_pattern)
        else:
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=(
                    f"Rule line has two ':' separators but no '%' pattern — "
                    f"not a static pattern rule; real make may reject this: {line[:80]}"
                ),
                source=loc,
            ))

    # --- semicolon recipe on the rule line ---------------------------------
    inline_recipe: str | None = None
    if static_pattern is None:
        semi = _split_outside_refs(rest_stripped, ";")
        if semi >= 0:
            inline_recipe = rest_stripped[semi + 1:].strip()
            rest_stripped = rest_stripped[:semi].strip()
    else:
        semi = _split_outside_refs(static_pattern[1], ";")
        if semi >= 0:
            inline_recipe = static_pattern[1][semi + 1:].strip()
            static_pattern = (static_pattern[0], static_pattern[1][:semi].strip())

    deps_str = rest_stripped if static_pattern is None else ""

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

    # Try to statically resolve variable references in the target list so
    # `$(OBJS): %.o: %.c` gets real per-target nodes when OBJS is known.
    resolved_targets: list[str] = []
    for tname in targets_str.split():
        if "$" in _strip_dollar_dollar(tname):
            expanded = state._static_expand(tname)
            if expanded is not None:
                resolved_targets.extend(expanded.split())
                continue
            state._record_var_usage(tname, state._node_id(tname))
            state.diagnostics.append(Diagnostic(
                severity="info",
                message=(
                    f"Target list contains '{tname}' which cannot be resolved "
                    "statically — shown symbolically"
                ),
                source=loc,
            ))
        resolved_targets.append(tname)

    # --- collect the recipe ------------------------------------------------
    recipe_lines: list[str] = []
    if inline_recipe:
        recipe_lines.append(inline_recipe)
    prefix = state.recipe_prefix
    while i < len(lines):
        next_text, _ = lines[i]
        if next_text.startswith(prefix):
            recipe_lines.append(next_text[len(prefix):])
            i += 1
        elif not next_text.strip() or next_text.lstrip().startswith("#"):
            # blank lines and comments inside a recipe do not end it
            j = i
            while j < len(lines) and (
                not lines[j][0].strip() or lines[j][0].lstrip().startswith("#")
            ):
                j += 1
            if j < len(lines) and lines[j][0].startswith(prefix):
                i = j
                continue
            break
        else:
            break

    recipe_prefix_notes = _recipe_prefix_notes(recipe_lines)

    for tname in resolved_targets:
        tname = tname.strip()
        if not tname:
            continue

        is_pattern = "%" in tname and static_pattern is None
        kind = "pattern_rule" if is_pattern else "target"
        nid = state._node_id(tname)

        doc = inline_doc or pending_doc
        is_hidden = tname.startswith(".")

        if nid in state.nodes:
            node = state.nodes[nid]
            # A name first seen as a prerequisite starts as a ghost; its
            # defining rule upgrades it to a real target.
            if node.kind == "ghost":
                node.kind = kind
                node.source = loc
            if recipe_lines:
                if node.recipe and not is_double_colon:
                    prev = node.source
                    state.diagnostics.append(Diagnostic(
                        severity="warning",
                        message=(
                            f"Overriding recipe for target '{tname}' — real make "
                            "warns: 'overriding recipe for target' (previous "
                            f"recipe at {prev.file}:{prev.line})" if prev else
                            f"Overriding recipe for target '{tname}'"
                        ),
                        source=loc,
                        related_node=nid,
                    ))
                    node.recipe = list(recipe_lines)
                elif is_double_colon:
                    node.recipe = node.recipe + list(recipe_lines)
                else:
                    node.recipe = list(recipe_lines)
            if doc and not node.doc:
                node.doc = doc
        else:
            node = Node(
                id=nid,
                name=tname,
                kind=kind,
                source=loc,
                recipe=list(recipe_lines),
                doc=doc,
                flags=set(),
                annotations={},
            )
            if is_hidden:
                node.flags.add("hidden")
            state.nodes[nid] = node

        if is_double_colon:
            node.annotations["double_colon"] = True
        if is_grouped:
            node.annotations["grouped"] = True

        if static_pattern:
            node.annotations["static_pattern"] = (
                f"{static_pattern[0]}: {static_pattern[1]}"
            )
        if recipe_prefix_notes:
            node.annotations["recipe_prefixes"] = recipe_prefix_notes
        if state.oneshell and recipe_lines:
            node.annotations["oneshell"] = True
        explicit_empty = inline_recipe == "" and not recipe_lines
        if explicit_empty:
            node.annotations["empty_recipe"] = True
            if is_pattern:
                node.annotations["cancels_builtin_rule"] = True

        if state.condition_stack:
            node.annotations["condition"] = " && ".join(state.condition_stack)

        if state.first_target is None and not is_pattern and not is_hidden:
            state.first_target = tname

        def _dep_edge(dep: str, edge_kind: str) -> None:
            dep = dep.strip()
            if not dep:
                return
            dep_id = state._node_id(dep)
            if dep_id not in state.nodes:
                state.nodes[dep_id] = Node(id=dep_id, name=dep, kind="ghost")
            state.edges.append(Edge(src=nid, dst=dep_id, kind=edge_kind))

        if static_pattern:
            tgt_pat, prereq_pat = static_pattern
            stem = _pattern_stem(tname, tgt_pat)
            for pp in prereq_pat.split():
                dep = pp.replace("%", stem) if stem is not None else pp
                _dep_edge(dep, "prerequisite")
        else:
            for dep in normal_deps:
                _dep_edge(dep, "prerequisite")
            for dep in order_only_deps:
                _dep_edge(dep, "order_only")

        for rline in recipe_lines:
            state._record_var_usage(rline, nid)
            _scan_recursion(rline, state, nid, filedir, rel_path, line_no)

        for dep in normal_deps + order_only_deps:
            state._record_var_usage(dep, nid)

    return i


def _pattern_stem(target: str, pattern: str) -> str | None:
    """Compute the % stem of `target` against `pattern` (e.g. a.o vs %.o → a)."""
    if "%" not in pattern:
        return None
    pre, _, post = pattern.partition("%")
    if target.startswith(pre) and target.endswith(post) and len(target) >= len(pre) + len(post):
        return target[len(pre): len(target) - len(post)] if post else target[len(pre):]
    return None


_RECIPE_PREFIX_MEANINGS = {
    "@": "@ — line is not echoed",
    "-": "- — errors from this line are ignored",
    "+": "+ — line runs even under 'make -n' (dry run)",
}


def _recipe_prefix_notes(recipe_lines: list[str]) -> list[str]:
    seen: list[str] = []
    for line in recipe_lines:
        stripped = line.lstrip()
        j = 0
        while j < len(stripped) and stripped[j] in "@-+":
            meaning = _RECIPE_PREFIX_MEANINGS[stripped[j]]
            if meaning not in seen:
                seen.append(meaning)
            j += 1
    return seen


def _scan_recursion(
    rline: str,
    state: _ParserState,
    nid: str,
    filedir: str,
    rel_path: str,
    line_no: int,
) -> None:
    loc = SourceLocation(file=rel_path, line=line_no)
    for m in _MAKE_RECURSE_RE.finditer(rline):
        cd_dir = m.group("cd")
        flag = m.group("flag")
        arg = m.group("arg")

        subdir: str | None = None
        subfile: str | None = None
        if flag == "-C" and arg:
            subdir = arg
        elif flag == "-f" and arg:
            subfile = arg
        elif cd_dir:
            subdir = cd_dir
        else:
            continue

        # resolve $(VAR) arguments from the static table
        raw_arg = subdir or subfile
        if raw_arg and "$" in _strip_dollar_dollar(raw_arg):
            expanded = state._static_expand(raw_arg)
            if expanded is None:
                ghost_id = f"sub-make:{raw_arg}"
                if ghost_id not in state.nodes:
                    state.nodes[ghost_id] = Node(
                        id=ghost_id, name=f"$(MAKE) → {raw_arg}", kind="ghost",
                    )
                state.edges.append(Edge(src=nid, dst=ghost_id, kind="invokes"))
                state.diagnostics.append(Diagnostic(
                    severity="info",
                    message=(
                        f"Recursive make into '{raw_arg}' cannot be resolved "
                        "statically — shown as an unresolved sub-make"
                    ),
                    source=loc,
                    related_node=ghost_id,
                ))
                continue
            if subdir:
                subdir = expanded
            else:
                subfile = expanded

        _handle_recursion(state, nid, subdir, subfile, filedir, rel_path, line_no)


def _diagnose_unmodeled_line(
    stripped: str, loc: SourceLocation, state: _ParserState
) -> None:
    """The taxonomy contract: 'unparseable' is only a legitimate diagnosis
    for input real make would also reject. Valid-but-unmodeled constructs are
    named, with the consequence stated."""
    if re.match(r"^\$\((?:eval|call|foreach)\b", stripped):
        fn = stripped[2:].split(None, 1)[0].rstrip(")")
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"$({fn} …) at top level generates makefile content at run "
                "time — its effects are not modeled (v1 non-goal)"
            ),
            source=loc,
        ))
        return
    if stripped.startswith("$"):
        state.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Line starts with an expansion the static pass cannot "
                f"evaluate — not modeled: {stripped[:80]}"
            ),
            source=loc,
        ))
        return
    if stripped == "endef":
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message="'endef' without matching 'define' (real make rejects this)",
            source=loc,
        ))
        return
    state.diagnostics.append(Diagnostic(
        severity="warning",
        message=(
            f"Not valid Make syntax as far as the parser can tell "
            f"(real make likely rejects this line too): {stripped[:100]}"
        ),
        source=loc,
    ))


def _handle_recursion(
    state: _ParserState,
    parent_nid: str,
    subdir: str | None,
    subfile: str | None,
    filedir: str,
    rel_path: str,
    line_no: int,
) -> None:
    if len(state.recursion_stack) >= _MAX_RECURSION_DEPTH:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=f"Sub-make nesting deeper than {_MAX_RECURSION_DEPTH} — stopping here",
            source=SourceLocation(file=rel_path, line=line_no),
        ))
        return

    if subdir:
        sub_makefile = None
        sub_abs_dir = os.path.abspath(os.path.join(filedir, subdir))
        for candidate in ("GNUmakefile", "Makefile", "makefile"):
            p = os.path.join(sub_abs_dir, candidate)
            if os.path.isfile(p):
                sub_makefile = p
                break
        if sub_makefile is None:
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=f"Recursive make target directory has no Makefile: {subdir}",
                source=SourceLocation(file=rel_path, line=line_no),
            ))
            return
        ns = subdir
    elif subfile:
        sub_makefile = os.path.abspath(os.path.join(filedir, subfile))
        if not os.path.isfile(sub_makefile):
            state.diagnostics.append(Diagnostic(
                severity="warning",
                message=f"Recursive make file not found: {subfile}",
                source=SourceLocation(file=rel_path, line=line_no),
            ))
            return
        ns = os.path.splitext(os.path.basename(subfile))[0]
    else:
        return

    if sub_makefile in state.recursion_stack:
        state.diagnostics.append(Diagnostic(
            severity="warning",
            message=(
                f"Recursive make cycle: {os.path.relpath(sub_makefile, os.path.dirname(state.root_path))} "
                "invokes itself through its parents — not followed again"
            ),
            source=SourceLocation(file=rel_path, line=line_no),
        ))
        return

    old_ns = state.namespace
    state.namespace = ns if not old_ns else f"{old_ns}/{ns}"
    state.recursion_stack.append(sub_makefile)
    try:
        _parse_file(sub_makefile, state)
    finally:
        state.recursion_stack.pop()
        state.namespace = old_ns

    sub_rel = os.path.relpath(sub_makefile, os.path.dirname(state.root_path))
    state.edges.append(Edge(src=parent_nid, dst=sub_rel, kind="invokes"))
