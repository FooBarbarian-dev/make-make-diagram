"""What-if compiler for GitHub Actions: turns ``on:`` triggers and job
``if:`` expressions into a structured, evaluatable program embedded in the
report model — the GitHub twin of ``gitlab_whatif.py``.

Everything semantic happens here, in Python, where pytest can pin it:
- ``if:`` expressions are parsed into a JSON expression AST (the GitHub
  expression grammar: literals, context paths, ``!``/comparisons/``&&``/
  ``||``, and the documented function calls),
- ``on:`` filters (already normalized by the parser) are validated and
  linted,
- classic always-truthy / never-matching gotchas become lint entries.

The report's embedded JS (templates/whatif_github.js) is a dumb tri-state
interpreter over this data. It never parses GitHub syntax.

Failure philosophy matches the parser: an unparseable expression degrades to
an ``opaque`` node (evaluates to *unknown*) plus one diagnostic — one bad
condition degrades one condition, never the report.

The compiled program carries ``provider: "github"`` at the report level so
consumers (the report template, trigger docs, the Python evaluator twin)
can dispatch; a program without the key is GitLab's.
"""

from __future__ import annotations

import re
from typing import Any

from pipeview.model import Diagnostic, SourceLocation

# The simulator's simplified world, mirroring the GitLab one: a protected
# default branch, and every other branch a generic unprotected feature
# branch. Surfaced in the report so the UI and the user share assumptions.
DEFAULT_BRANCH = "main"
PROTECTED_REFS = ["main"]

WHATIF_VERSION = 1

# Functions the GitHub expression language documents. Anything else is a
# workflow-file error on the real server.
KNOWN_FUNCTIONS = frozenset({
    "contains", "startswith", "endswith", "format", "join",
    "tojson", "fromjson", "hashfiles",
    "success", "always", "cancelled", "failure",
})

# Status functions change how needs-skipping applies to a job.
STATUS_FUNCTIONS = frozenset({"success", "always", "cancelled", "failure"})

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")
_NUMBER_RE = re.compile(r"-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")

_TEMPLATE_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


class _ExprError(Exception):
    pass


# ---------------------------------------------------------------------------
# Expression parser: GitHub Actions expressions → JSON AST
# ---------------------------------------------------------------------------
#
# Grammar (docs.github.com/actions/learn-github-actions/expressions):
#   or         := and ('||' and)*
#   and        := equality ('&&' equality)*
#   equality   := relational (('==' | '!=') relational)*    (left-assoc)
#   relational := unary (('<' | '<=' | '>' | '>=') unary)*
#   unary      := '!' unary | primary
#   primary    := '(' or ')' | literal | call | contextpath
#   literal    := null | true | false | number | 'single-quoted string'
#   call       := ident '(' (or (',' or)*)? ')'
#   contextpath:= ident ('.' ident | '[' or ']' | '.*')*
#
# There are no arithmetic operators, so a leading '-' always starts a
# number literal.

def _tokenize(src: str) -> list[tuple]:
    tokens: list[tuple] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c == "'":
            j = i + 1
            buf = []
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":   # '' escapes '
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            if j >= n:
                raise _ExprError("unterminated string literal")
            tokens.append(("str", "".join(buf)))
            i = j + 1
            continue
        if c == '"':
            raise _ExprError(
                "double-quoted strings are not valid in expressions — "
                "use single quotes"
            )
        two = src[i:i + 2]
        if two in ("==", "!=", "<=", ">=", "&&", "||"):
            tokens.append(("op", two))
            i += 2
            continue
        if c in "()!<>,[]":
            tokens.append(("op", c))
            i += 1
            continue
        m = _NUMBER_RE.match(src, i)
        if m and (c.isdigit() or c == "-"):
            text = m.group(0)
            value: float | int
            if text.lower().startswith(("0x", "-0x")):
                value = int(text, 16)
            else:
                value = float(text)
                if value.is_integer() and "e" not in text.lower() \
                        and "." not in text:
                    value = int(value)
            tokens.append(("num", value))
            i = m.end()
            continue
        m = _IDENT_RE.match(src, i)
        if m:
            word = m.group(0)
            low = word.lower()
            if low == "true":
                tokens.append(("bool", True))
            elif low == "false":
                tokens.append(("bool", False))
            elif low == "null":
                tokens.append(("null",))
            else:
                tokens.append(("ident", word))
            i = m.end()
            continue
        if c == ".":
            tokens.append(("op", "."))
            i += 1
            continue
        if c == "*":
            tokens.append(("op", "*"))
            i += 1
            continue
        raise _ExprError(f"unexpected character {c!r} at offset {i}")
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple]):
        self.tokens = tokens
        self.pos = 0
        self.notes: list[str] = []

    def peek(self) -> tuple | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple:
        tok = self.peek()
        if tok is None:
            raise _ExprError("unexpected end of expression")
        self.pos += 1
        return tok

    def parse(self) -> dict:
        node = self.parse_or()
        if self.peek() is not None:
            raise _ExprError(f"trailing tokens starting at {self.peek()!r}")
        return node

    def parse_or(self) -> dict:
        args = [self.parse_and()]
        while self.peek() == ("op", "||"):
            self.take()
            args.append(self.parse_and())
        return args[0] if len(args) == 1 else {"op": "or", "args": args}

    def parse_and(self) -> dict:
        args = [self.parse_equality()]
        while self.peek() == ("op", "&&"):
            self.take()
            args.append(self.parse_equality())
        return args[0] if len(args) == 1 else {"op": "and", "args": args}

    def parse_equality(self) -> dict:
        left = self.parse_relational()
        while True:
            tok = self.peek()
            if not (tok and tok[0] == "op" and tok[1] in ("==", "!=")):
                break
            self.take()
            right = self.parse_relational()
            left = {"op": "cmp", "cmp": tok[1], "left": left, "right": right}
        return left

    def parse_relational(self) -> dict:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if not (tok and tok[0] == "op" and tok[1] in ("<", "<=", ">", ">=")):
                break
            self.take()
            right = self.parse_unary()
            left = {"op": "cmp", "cmp": tok[1], "left": left, "right": right}
        return left

    def parse_unary(self) -> dict:
        if self.peek() == ("op", "!"):
            self.take()
            return {"op": "not", "arg": self.parse_unary()}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        tok = self.peek()
        if tok == ("op", "("):
            self.take()
            node = self.parse_or()
            if self.peek() != ("op", ")"):
                raise _ExprError("missing closing parenthesis")
            self.take()
            return node
        if tok is None:
            raise _ExprError("unexpected end of expression")
        if tok[0] == "str":
            self.take()
            return {"t": "lit", "value": tok[1]}
        if tok[0] == "num":
            self.take()
            return {"t": "lit", "value": tok[1]}
        if tok[0] == "bool":
            self.take()
            return {"t": "lit", "value": tok[1]}
        if tok[0] == "null":
            self.take()
            return {"t": "lit", "value": None}
        if tok[0] == "ident":
            return self.parse_ident()
        raise _ExprError(f"expected a value, got {tok!r}")

    def parse_ident(self) -> dict:
        tok = self.take()
        name = tok[1]
        if self.peek() == ("op", "("):
            low = name.lower()
            if low not in KNOWN_FUNCTIONS:
                raise _FnError(f"unknown function {name}()")
            self.take()
            args: list[dict] = []
            if self.peek() != ("op", ")"):
                args.append(self.parse_or())
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.parse_or())
            if self.peek() != ("op", ")"):
                raise _ExprError(f"missing ) after {name}(")
            self.take()
            return {"op": "call", "fn": low, "args": args}
        # context path: dotted segments, [literal] indexes, .* filters
        segments = [name.lower()]
        opaque = False
        while True:
            if self.peek() == ("op", "."):
                self.take()
                nxt = self.peek()
                if nxt == ("op", "*"):
                    self.take()
                    segments.append("*")
                    opaque = True
                    continue
                if nxt is None or nxt[0] != "ident":
                    raise _ExprError("expected a property name after '.'")
                self.take()
                segments.append(nxt[1].lower())
                continue
            if self.peek() == ("op", "["):
                self.take()
                idx = self.parse_or()
                if self.peek() != ("op", "]"):
                    raise _ExprError("missing closing bracket")
                self.take()
                if idx.get("t") == "lit" and not isinstance(idx["value"], bool):
                    segments.append(str(idx["value"]).lower())
                else:
                    segments.append("*")
                    opaque = True
                continue
            break
        node: dict[str, Any] = {"t": "ctx", "path": ".".join(segments)}
        if opaque:
            node["dynamic"] = True
        return node


class _FnError(_ExprError):
    """Unknown function — the real server rejects the workflow file."""


def parse_condition(src: Any) -> tuple[dict, list[str], str | None]:
    """Parse one ``if:`` value. Returns (ast, notes, error).

    Failure classes differ in meaning:
    - an unknown function or double-quoted string is rejected by GitHub
      itself → {"op": "invalid"} so the caller can flag the workflow;
    - structure this grammar can't parse → {"op": "opaque"} which
      evaluates to *unknown* — never a guess;
    - ``${{ }}`` mixed with literal text becomes a *template*: GitHub
      substitutes and then checks the resulting string, which is almost
      always truthy → {"op": "opaque"} plus a note the compiler turns
      into the always-truthy lint.
    """
    if isinstance(src, bool):
        return {"t": "lit", "value": src}, [], None
    text = str(src).strip()
    notes: list[str] = []

    if "${{" in text:
        stripped = _TEMPLATE_RE.sub("", text).strip()
        inner = _TEMPLATE_RE.findall(text)
        if stripped == "" and len(inner) == 1:
            text = inner[0].strip()
        else:
            notes.append(
                "mixes ${{ }} with literal text — GitHub substitutes the "
                "expression into a string and checks the string, which is "
                "truthy whenever any text remains; this condition is "
                "probably always true"
            )
            return {"op": "opaque", "src": str(src)}, notes, None

    try:
        tokens = _tokenize(text)
    except _ExprError as e:
        return {"op": "invalid", "src": text}, notes, str(e)
    if not tokens:
        return {"op": "invalid", "src": text}, notes, "empty expression"
    try:
        parser = _Parser(tokens)
        ast = parser.parse()
        return ast, notes + parser.notes, None
    except _FnError as e:
        return {"op": "invalid", "src": text}, notes, str(e)
    except _ExprError as e:
        return {"op": "opaque", "src": text}, notes, str(e)


def collect_ctx_paths(ast: Any, out: list[str] | None = None) -> list[str]:
    """All context paths an AST references (for the Variables tab's
    "referenced in rules" ranking and unknown-variable panels)."""
    if out is None:
        out = []
    if isinstance(ast, dict):
        if ast.get("t") == "ctx":
            if ast["path"] not in out:
                out.append(ast["path"])
        for key in ("args", "arg", "left", "right"):
            if key in ast:
                collect_ctx_paths(ast[key], out)
    if isinstance(ast, list):
        for item in ast:
            collect_ctx_paths(item, out)
    return out


def uses_status_function(ast: Any, which: frozenset[str] | None = None) -> bool:
    if which is None:
        which = STATUS_FUNCTIONS
    if isinstance(ast, dict):
        if ast.get("op") == "call" and ast.get("fn") in which:
            return True
        return any(
            uses_status_function(ast[k], which)
            for k in ("args", "arg", "left", "right") if k in ast
        )
    if isinstance(ast, list):
        return any(uses_status_function(a, which) for a in ast)
    return False


# ---------------------------------------------------------------------------
# Filter-pattern translation (GitHub's branch/tag/path glob dialect)
# ---------------------------------------------------------------------------
#
# `*` matches anything but `/`; `**` matches anything; `?` makes the
# PRECEDING atom optional; `+` repeats it; `[...]` is a class; `\`
# escapes; a leading `!` negates the pattern (handled by the evaluator's
# pattern-list walk, not here).

def _pattern_translate(pattern: str) -> str:
    atoms: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            atoms.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "*":
            if pattern[i:i + 2] == "**":
                atoms.append(r".*")
                i += 2
            else:
                atoms.append(r"[^/]*")
                i += 1
        elif c == "?":
            if atoms:
                atoms[-1] = f"(?:{atoms[-1]})?"
            i += 1
        elif c == "+":
            if atoms:
                atoms[-1] = f"(?:{atoms[-1]})+"
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                atoms.append(re.escape(c))
                i += 1
            else:
                atoms.append("[" + pattern[i + 1:j] + "]")
                i = j + 1
        else:
            atoms.append(re.escape(c))
            i += 1
    return "".join(atoms)


def pattern_to_regex(pattern: str) -> re.Pattern | None:
    """One GitHub filter pattern → anchored regex (negation prefix already
    stripped by the caller), or None when untranslatable."""
    try:
        return re.compile("^" + _pattern_translate(pattern) + "$")
    except re.error:
        return None


def match_pattern_list(value: str, patterns: list[str]) -> bool | None:
    """GitHub's ordered include/exclude walk: the LAST matching pattern
    decides, `!` patterns exclude, and at least one positive pattern must
    have matched overall. None when a pattern is untranslatable."""
    include = False
    matched_positive = False
    unknown = False
    for raw in patterns:
        neg = raw.startswith("!")
        body = raw[1:] if neg else raw
        rx = pattern_to_regex(body)
        if rx is None:
            unknown = True
            continue
        if rx.match(value):
            include = not neg
            if not neg:
                matched_positive = True
    if unknown and not include:
        return None
    return include and matched_positive


# ---------------------------------------------------------------------------
# The compile pass
# ---------------------------------------------------------------------------

class _CompileCtx:
    def __init__(self, state) -> None:
        self.state = state
        self.lint: list[dict] = []

    def diag(self, severity: str, message: str, line: int | None,
             file: str | None = None, node: str | None = None) -> None:
        src = None
        if file is not None:
            src = SourceLocation(file=file, line=line or 1)
        self.state.diagnostics.append(
            Diagnostic(severity=severity, message=message, source=src,
                       related_node=node)
        )


# `github.ref` keeps its refs/ prefix; comparing it against a bare branch
# name is the classic never-matching condition.
_REF_PATH = "github.ref"
_BARE_REF_OK = ("refs/",)


def _lint_ref_comparisons(ast: Any, where: str, ctx: _CompileCtx,
                          rel_path: str, line_no: int, job_id: str) -> None:
    if not isinstance(ast, dict):
        return
    if ast.get("op") == "cmp" and ast["cmp"] in ("==", "!="):
        sides = (ast.get("left"), ast.get("right"))
        for a, b in (sides, sides[::-1]):
            if (isinstance(a, dict) and a.get("t") == "ctx"
                    and a.get("path") == _REF_PATH
                    and isinstance(b, dict) and b.get("t") == "lit"
                    and isinstance(b.get("value"), str)
                    and b["value"]
                    and not b["value"].startswith(_BARE_REF_OK)):
                message = (
                    f"{where}: compares github.ref to '{b['value']}' — "
                    "github.ref keeps the refs/heads/ (or refs/tags/) "
                    "prefix, so this never matches; compare "
                    "github.ref_name, or the full "
                    f"'refs/heads/{b['value']}'"
                )
                ctx.lint.append({"job": job_id, "message": message})
                ctx.diag("warning", message, line_no, rel_path, job_id)
    for key in ("args", "arg", "left", "right"):
        if key in ast:
            val = ast[key]
            for sub in val if isinstance(val, list) else [val]:
                _lint_ref_comparisons(sub, where, ctx, rel_path, line_no,
                                      job_id)


def _lint_push_pr_duplicates(state, ctx: _CompileCtx) -> None:
    """GitHub's classic duplicate-run cause: one workflow subscribed to
    both push and pull_request. One push to a branch with an open PR
    starts it twice."""
    for ns, wf in state.workflows.items():
        on = wf.get("on")
        if not isinstance(on, dict):
            continue
        if "push" in on and ("pull_request" in on
                             or "pull_request_target" in on):
            push_cfg = on.get("push") or {}
            if push_cfg.get("branches") or push_cfg.get("branches_ignore"):
                # a branch filter often IS the dedup — don't cry wolf
                continue
            other = ("pull_request" if "pull_request" in on
                     else "pull_request_target")
            message = (
                f"Workflow {wf['file']} triggers on both push and {other} — "
                "one push to a branch with an open pull request starts it "
                "twice; filter push branches (e.g. branches: [main]) or "
                "drop one trigger"
            )
            ctx.lint.append({"job": ns, "message": message})
            ctx.diag("warning", message, 1, wf["file"])


def _needs_cycles(state, ctx: _CompileCtx) -> None:
    for ns, wf in state.workflows.items():
        jobs = {jid: cfg for jid, cfg in state.job_configs.items()
                if state.job_meta[jid][3] == ns}
        needs_of = {}
        for jid, cfg in jobs.items():
            raw = cfg.get("needs")
            items = raw if isinstance(raw, list) else \
                [raw] if raw is not None else []
            needs_of[jid] = [f"{ns}::{n}" for n in map(str, items)]
        mark: dict[str, int] = {}

        def dfs(node_id: str, path: list[str]) -> None:
            mark[node_id] = 1
            for nxt in needs_of.get(node_id, []):
                if nxt not in needs_of:
                    continue
                if mark.get(nxt) == 1:
                    cyc = path[path.index(nxt):] + [nxt] if nxt in path \
                        else [node_id, nxt]
                    msg = ("circular needs dependency: "
                           + " → ".join(cyc)
                           + " — GitHub rejects the workflow")
                    if msg not in wf["invalid"]:
                        wf["invalid"].append(msg)
                        wf["summary"]["invalid"] = list(wf["invalid"])
                        ctx.diag("error", f"Workflow {wf['file']}: {msg}",
                                 1, wf["file"])
                    continue
                if mark.get(nxt) is None:
                    dfs(nxt, path + [nxt])
            mark[node_id] = 2

        for jid in needs_of:
            if mark.get(jid) is None:
                dfs(jid, [jid])


def compile_github_whatif(state) -> dict:
    """Attach a what-if program to every job node and return the
    report-level what-if annotation. Called at the end of parse_github,
    after nodes are built."""
    ctx = _CompileCtx(state)

    for job_id, config in state.job_configs.items():
        node = state.nodes.get(job_id)
        if node is None or node.kind != "job":
            continue
        rel_path, line_no, _, namespace = state.job_meta[job_id]
        plain_name = job_id.rsplit("::", 1)[-1]
        where = f"job '{job_id}'"

        if_ast = None
        raw_if = None
        if "if" in config:
            raw_if = str(config["if"])
            if_ast, notes, err = parse_condition(config["if"])
            for note in notes:
                message = f"{where}: if: {note}"
                ctx.lint.append({"job": job_id, "message": message})
                ctx.diag("warning", message, line_no, rel_path, job_id)
            if if_ast.get("op") == "invalid":
                wf = state.workflows.get(namespace)
                msg = (f"invalid if: expression {raw_if!r} ({err}) — "
                       "GitHub rejects the workflow")
                ctx.diag("error", f"{where}: {msg}", line_no, rel_path,
                         job_id)
                if wf is not None and msg not in wf["invalid"]:
                    wf["invalid"].append(msg)
                    wf["summary"]["invalid"] = list(wf["invalid"])
            elif err:
                ctx.diag(
                    "warning",
                    f"{where}: cannot parse if: expression {raw_if!r} "
                    f"({err}) — the what-if simulation treats it as unknown",
                    line_no, rel_path, job_id,
                )
            else:
                _lint_ref_comparisons(if_ast, where, ctx, rel_path,
                                      line_no, job_id)
                for path in collect_ctx_paths(if_ast):
                    if path.startswith("secrets."):
                        message = (
                            f"{where}: if: references {path} — the secrets "
                            "context is not available in job-level if: "
                            "conditions (GitHub evaluates it as empty)"
                        )
                        ctx.lint.append({"job": job_id, "message": message})
                        ctx.diag("warning", message, line_no, rel_path,
                                 job_id)

        needs_raw = config.get("needs")
        needs = None
        if needs_raw is not None:
            items = needs_raw if isinstance(needs_raw, list) else [needs_raw]
            needs = [{"job": str(n), "optional": False, "artifacts": True}
                     for n in items]

        env = {}
        if isinstance(config.get("env"), dict):
            env = {str(k): _plain(v) for k, v in config["env"].items()}

        uses_prog = None
        uses_info = node.annotations.get("uses_info")
        if isinstance(uses_info, dict):
            with_map = {}
            if isinstance(config.get("with"), dict):
                with_map = {str(k): _plain(v)
                            for k, v in config["with"].items()}
            uses_prog = {
                "kind": uses_info["kind"],
                "workflow": uses_info.get("workflow"),
                "project": uses_info.get("project"),
                "ref": uses_info.get("ref"),
                "raw": uses_info["raw"],
                "with": with_map,
                "secrets": uses_info.get("secrets"),
            }

        environment = None
        env_cfg = config.get("environment")
        if isinstance(env_cfg, str):
            environment = {"name": env_cfg, "url": None}
        elif isinstance(env_cfg, dict):
            environment = {
                "name": _plain(env_cfg.get("name")) or None,
                "url": _plain(env_cfg.get("url")) or None,
            }

        matrix_cfg = None
        mat = node.annotations.get("matrix")
        if isinstance(mat, dict) and "dynamic" in mat:
            matrix_cfg = {"kind": "dynamic"}
        elif isinstance(mat, dict):
            # reuse the parser's expansion via the annotations; the parser
            # stored counts/axes only, so re-expand for instance naming
            from pipeview.parsers.github_parser import _expand_matrix
            expanded, _ = _expand_matrix(config.get("strategy"))
            if expanded and expanded.get("kind") == "matrix":
                combos = []
                for combo in expanded["combos"]:
                    vals = ", ".join(combo["vars"].values())
                    combos.append({
                        "vars": combo["vars"],
                        "name": f"{plain_name} ({vals})",
                    })
                matrix_cfg = {"kind": "matrix", "combos": combos}

        program: dict[str, Any] = {
            "provider": "github",
            "workflow": namespace,
            "name": plain_name,
            "display_name": node.annotations.get("display_name"),
            "stage": namespace,
            "if": if_ast,
            "raw_if": raw_if,
            "needs": needs,
            "env": env,
            "uses": uses_prog,
            "environment": environment,
            "continue_on_error": bool(config["continue-on-error"])
            if "continue-on-error" in config else None,
            "child_of": None,
        }
        if matrix_cfg is not None:
            program["parallel"] = matrix_cfg
        node.annotations["whatif"] = program

    _needs_cycles(state, ctx)
    _lint_push_pr_duplicates(state, ctx)

    workflows: dict[str, Any] = {}
    for ns, wf in state.workflows.items():
        conc = wf.get("concurrency")
        concurrency = None
        if isinstance(conc, str):
            concurrency = {"group": conc, "cancel_in_progress": None}
        elif isinstance(conc, dict):
            concurrency = {
                "group": _plain(conc.get("group")),
                "cancel_in_progress": bool(conc["cancel-in-progress"])
                if "cancel-in-progress" in conc else None,
            }
        workflows[ns] = {
            "file": wf["file"],
            "name": wf["name"],
            "on": wf["on"] if isinstance(wf["on"], dict) else {},
            "env": dict(wf.get("env") or {}),
            "invalid": list(wf["invalid"]),
            "reusable": bool(wf["reusable"]),
            "concurrency": concurrency,
        }

    return {
        "version": WHATIF_VERSION,
        "provider": "github",
        "default_branch": DEFAULT_BRANCH,
        "protected_refs": list(PROTECTED_REFS),
        "workflows": workflows,
        "stages": [],
        "globals": {},
        "lint": ctx.lint,
        "fatal": [],
        "unresolved_includes": [],
    }


def _plain(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)
