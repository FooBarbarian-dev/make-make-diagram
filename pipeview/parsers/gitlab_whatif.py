"""What-if compiler: turns GitLab rules/only/except/artifacts into a
structured, evaluatable program embedded in the report model.

Everything semantic happens here, in Python, where pytest can pin it:
- `rules:if` strings are parsed into a JSON expression AST,
- legacy `only`/`except` (including the implicit `only: [branches, tags]`
  default on rule-less jobs) is normalized into the same program shape,
- `rules:exists` is evaluated NOW against the repo files and baked in,
- artifact produce/consume information is extracted per job.

The report's embedded JS (templates/whatif.js) is a dumb tri-state
interpreter over this data. It never parses GitLab syntax.

Failure philosophy matches the parser: an unparseable expression degrades to
an `opaque` node (evaluates to *unknown*) plus one diagnostic — one bad rule
degrades one rule, never the report.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pipeview.model import Diagnostic, SourceLocation

# The simulator's simplified ref world (see the design spec): two protected
# long-lived branches and one generic feature branch. The names are surfaced
# in the report so the UI and the user share the same assumptions.
DEFAULT_BRANCH = "main"
PROTECTED_REFS = ["main", "dev"]

WHATIF_VERSION = 1

_MAX_EXISTS_FILES = 20000

# RE2 (GitLab's engine) rejects lookaround and backreferences; JS accepts
# them. Flag patterns that would fail on the real server.
_NON_RE2 = re.compile(r"\(\?<?[=!]|\\[1-9]")

_VAR_IN_PATH = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

_LEGACY_REF_KEYWORDS = frozenset({
    "api", "branches", "chat", "external", "external_pull_requests",
    "merge_requests", "pipelines", "pushes", "schedules", "tags",
    "triggers", "web",
})


# ---------------------------------------------------------------------------
# Expression parser: GitLab CI/CD variable expressions → JSON AST
# ---------------------------------------------------------------------------
#
# Grammar (docs.gitlab.com/ci/jobs/job_rules/#cicd-variable-expressions):
#   or    := and ('||' and)*
#   and   := unary ('&&' unary)*
#   unary := '!' unary | primary
#   primary := '(' or ')' | comparison
#   comparison := term (('==' | '!=' | '=~' | '!~') term)?
#   term  := $VAR | 'string' | "string" | /regex/flags | null
#
# A standalone term is a truthiness test (defined and non-empty). The
# strings "false" and "0" are truthy — only unset/empty are falsy.

class _ExprError(Exception):
    pass


def _tokenize(src: str) -> list[tuple]:
    tokens: list[tuple] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c == "$":
            m = re.match(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                         src[i:])
            if not m:
                raise _ExprError(f"bad variable reference at offset {i}")
            tokens.append(("var", m.group(1) or m.group(2)))
            i += m.end()
            continue
        if c in "\"'":
            j = src.find(c, i + 1)
            if j < 0:
                raise _ExprError("unterminated string literal")
            tokens.append(("str", src[i + 1:j]))
            i = j + 1
            continue
        if c == "/":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "/":
                    break
                j += 1
            if j >= n:
                raise _ExprError("unterminated regex literal")
            k = j + 1
            while k < n and src[k].isalpha():
                k += 1
            tokens.append(("re", src[i + 1:j], src[j + 1:k]))
            i = k
            continue
        two = src[i:i + 2]
        if two in ("==", "!=", "=~", "!~", "&&", "||"):
            tokens.append(("op", two))
            i += 2
            continue
        if c in "()!":
            tokens.append(("op", c))
            i += 1
            continue
        m = re.match(r"null\b", src[i:])
        if m:
            tokens.append(("null",))
            i += m.end()
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
        args = [self.parse_unary()]
        while self.peek() == ("op", "&&"):
            self.take()
            args.append(self.parse_unary())
        return args[0] if len(args) == 1 else {"op": "and", "args": args}

    def parse_unary(self) -> dict:
        if self.peek() == ("op", "!"):
            self.take()
            return {"op": "not", "arg": self.parse_unary()}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        if self.peek() == ("op", "("):
            self.take()
            node = self.parse_or()
            if self.peek() != ("op", ")"):
                raise _ExprError("missing closing parenthesis")
            self.take()
            return node
        return self.parse_comparison()

    def parse_comparison(self) -> dict:
        left = self.parse_term()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("==", "!=", "=~", "!~"):
            self.take()
            right = self.parse_term()
            if tok[1] in ("=~", "!~") and right.get("t") == "re":
                if _NON_RE2.search(right["source"]):
                    self.notes.append(
                        f"/{right['source']}/ uses lookaround or backreferences — "
                        "GitLab's RE2 engine rejects these patterns"
                    )
            return {"op": "cmp", "cmp": tok[1], "left": left, "right": right}
        return {"op": "truthy", "term": left}

    def parse_term(self) -> dict:
        tok = self.take()
        if tok[0] == "var":
            return {"t": "var", "name": tok[1]}
        if tok[0] == "str":
            return {"t": "str", "value": tok[1]}
        if tok[0] == "re":
            return {"t": "re", "source": tok[1], "flags": tok[2]}
        if tok[0] == "null":
            return {"t": "null"}
        raise _ExprError(f"expected a value, got {tok!r}")


def parse_expression(src: Any) -> tuple[dict, list[str], str | None]:
    """Parse one variable expression. Returns (ast, notes, error).
    On failure the AST is {"op": "opaque", "src": …} which the evaluator
    treats as *unknown* — never a crash, never a guess."""
    text = str(src)
    try:
        parser = _Parser(_tokenize(text))
        ast = parser.parse()
        return ast, parser.notes, None
    except _ExprError as e:
        return {"op": "opaque", "src": text}, [], str(e)


# ---------------------------------------------------------------------------
# Glob translation (Ruby File.fnmatch with PATHNAME|DOTMATCH|EXTGLOB — the
# flags GitLab documents for rules:changes / rules:exists)
# ---------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern | None:
    """Translate one GitLab path glob to an anchored regex, or None when the
    pattern can't be translated."""
    out = []
    i, n = 0, len(pattern)
    try:
        while i < n:
            c = pattern[i]
            if c == "*":
                if pattern[i:i + 3] == "**/":
                    out.append(r"(?:[^/]+/)*")
                    i += 3
                elif pattern[i:i + 2] == "**":
                    out.append(r".*")
                    i += 2
                else:
                    out.append(r"[^/]*")
                    i += 1
            elif c == "?":
                out.append(r"[^/]")
                i += 1
            elif c == "[":
                j = pattern.find("]", i + 1)
                if j < 0:
                    out.append(re.escape(c))
                    i += 1
                else:
                    inner = pattern[i + 1:j]
                    if inner.startswith("!"):
                        inner = "^" + inner[1:]
                    out.append("[" + inner + "]")
                    i = j + 1
            elif c == "{":
                j = pattern.find("}", i + 1)
                if j < 0:
                    out.append(re.escape(c))
                    i += 1
                else:
                    alts = pattern[i + 1:j].split(",")
                    out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                    i = j + 1
            else:
                out.append(re.escape(c))
                i += 1
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def _expand_path_vars(pattern: str, globals_map: dict[str, str]) -> str:
    """GitLab substitutes known CI/CD variables in changes/exists paths and
    leaves unknown `$` intact as a literal path character."""
    def sub(m: re.Match) -> str:
        return globals_map.get(m.group(1), m.group(0))
    return _VAR_IN_PATH.sub(sub, pattern)


# ---------------------------------------------------------------------------
# exists: evaluated at generation time against the actual repo files
# ---------------------------------------------------------------------------

def _repo_file_list(repo_root: str) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel_dir = os.path.relpath(dirpath, repo_root)
        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            files.append(rel.replace(os.sep, "/"))
            if len(files) >= _MAX_EXISTS_FILES:
                return files
    return files


def _eval_exists(
    paths: list[str], repo_files: list[str], globals_map: dict[str, str]
) -> bool | None:
    result = False
    for raw in paths:
        pattern = _expand_path_vars(str(raw), globals_map)
        if pattern.endswith("/"):
            pattern += "**"
        rx = glob_to_regex(pattern)
        if rx is None:
            return None
        if any(rx.match(f) for f in repo_files):
            result = True
    return result


# ---------------------------------------------------------------------------
# Rule compilation
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _compile_rule(
    rule: Any, ctx: "_CompileCtx", where: str, line: int | None
) -> dict | None:
    """One rules: entry → RULE json. Returns None for entries that are not
    mappings (GitLab rejects those; the parser already warns elsewhere)."""
    if not isinstance(rule, dict):
        return None
    out: dict[str, Any] = {
        "if": None, "raw_if": None, "changes": None, "exists": None,
        "when": None, "allow_failure": None, "start_in": None,
        "variables": None,
    }
    if "if" in rule:
        ast, notes, err = parse_expression(rule["if"])
        out["if"] = ast
        out["raw_if"] = str(rule["if"])
        for note in notes:
            ctx.diag("info", f"{where}: {note}", line)
        if err:
            ctx.diag(
                "warning",
                f"{where}: cannot parse rules:if expression "
                f"{str(rule['if'])!r} ({err}) — the what-if simulation "
                "treats it as unknown",
                line,
            )
    if "changes" in rule:
        ch = rule["changes"]
        if isinstance(ch, dict):
            out["changes"] = {
                "paths": [str(p) for p in _as_list(ch.get("paths"))],
                "compare_to": str(ch["compare_to"]) if "compare_to" in ch else None,
            }
            if "regexp" in ch:
                out["changes"]["regexp"] = str(ch["regexp"])
        else:
            out["changes"] = {"paths": [str(p) for p in _as_list(ch)],
                              "compare_to": None}
    if "exists" in rule:
        ex = rule["exists"]
        if isinstance(ex, dict):
            paths = [str(p) for p in _as_list(ex.get("paths"))]
            if "project" in ex or "regexp" in ex:
                # another project's tree (or a Ruby regex) — unknowable here
                out["exists"] = {"paths": paths, "result": None,
                                 "reason": "exists:project/regexp is not "
                                           "resolvable offline"}
            else:
                out["exists"] = {
                    "paths": paths,
                    "result": _eval_exists(paths, ctx.repo_files, ctx.globals_map),
                    "reason": None,
                }
        else:
            paths = [str(p) for p in _as_list(ex)]
            out["exists"] = {
                "paths": paths,
                "result": _eval_exists(paths, ctx.repo_files, ctx.globals_map),
                "reason": None,
            }
    if "when" in rule:
        out["when"] = str(rule["when"])
    if "allow_failure" in rule:
        out["allow_failure"] = bool(rule["allow_failure"])
    if "start_in" in rule:
        out["start_in"] = str(rule["start_in"])
    if isinstance(rule.get("variables"), dict):
        out["variables"] = {str(k): _plain_value(v) for k, v in rule["variables"].items()}
    return out


def _plain_value(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, dict):  # hash form: value/description/options
        return _plain_value(v.get("value", ""))
    return str(v)


def _compile_only_except(spec: Any, ctx: "_CompileCtx", where: str,
                         line: int | None) -> dict:
    """only:/except: value → {refs, variables, changes, unsupported}."""
    out: dict[str, Any] = {"refs": None, "variables": None, "changes": None,
                           "unsupported": []}
    if isinstance(spec, (str, list)):
        out["refs"] = [str(r) for r in _as_list(spec)]
        return out
    if not isinstance(spec, dict):
        out["unsupported"].append(str(type(spec).__name__))
        return out
    if "refs" in spec:
        out["refs"] = [str(r) for r in _as_list(spec["refs"])]
    if "variables" in spec:
        asts = []
        for expr in _as_list(spec["variables"]):
            ast, notes, err = parse_expression(expr)
            asts.append(ast)
            for note in notes:
                ctx.diag("info", f"{where}: {note}", line)
            if err:
                ctx.diag(
                    "warning",
                    f"{where}: cannot parse variable expression "
                    f"{str(expr)!r} ({err}) — treated as unknown",
                    line,
                )
        out["variables"] = asts
    if "changes" in spec:
        out["changes"] = [str(p) for p in _as_list(spec["changes"])]
    for key in spec:
        if key not in ("refs", "variables", "changes"):
            out["unsupported"].append(str(key))
    return out


# ---------------------------------------------------------------------------
# Per-job extraction
# ---------------------------------------------------------------------------

def _compile_needs(needs: Any) -> list[dict] | None:
    """Structured needs list for the evaluator. `None` means the job has no
    needs: key at all (stage-default artifact download applies); `[]` means
    an explicit needs: []."""
    if needs is None or not isinstance(needs, list):
        return None if needs is None else []
    out: list[dict] = []
    for need in needs:
        if isinstance(need, str):
            out.append({"job": need, "optional": False, "artifacts": True})
        elif isinstance(need, dict):
            if "pipeline" in need:
                out.append({"kind": "cross_pipeline",
                            "ref": _plain_value(need["pipeline"])})
                continue
            if "project" in need:
                out.append({"kind": "cross_project",
                            "ref": _plain_value(need["project"]),
                            "job": _plain_value(need.get("job", ""))})
                continue
            out.append({
                "job": _plain_value(need.get("job", "")),
                "optional": bool(need.get("optional", False)),
                "artifacts": need.get("artifacts", True) is not False,
            })
    return out


def _compile_artifacts(artifacts: Any) -> dict:
    paths: list[str] = []
    dotenv: list[str] = []
    if isinstance(artifacts, dict):
        paths = [str(p) for p in _as_list(artifacts.get("paths"))]
        reports = artifacts.get("reports")
        if isinstance(reports, dict) and "dotenv" in reports:
            dotenv = [str(p) for p in _as_list(reports["dotenv"])]
    return {"paths": paths, "dotenv": dotenv}


def _compile_trigger(trigger: Any) -> dict | None:
    if trigger is None:
        return None
    if isinstance(trigger, str):
        return {"project": trigger}
    if not isinstance(trigger, dict):
        return None
    includes = trigger.get("include")
    if includes is not None:
        children = []
        for inc in _as_list(includes):
            if isinstance(inc, str):
                children.append(inc.lstrip("/"))
            elif isinstance(inc, dict) and "local" in inc:
                children.append(str(inc["local"]).lstrip("/"))
        return {"children": children}
    if "project" in trigger:
        return {"project": str(trigger["project"])}
    return None


# ---------------------------------------------------------------------------
# The compile pass
# ---------------------------------------------------------------------------

class _CompileCtx:
    def __init__(self, state) -> None:
        self.state = state
        self.repo_files = _repo_file_list(state.repo_root)
        self.globals_map = _globals_map(state)
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


def _globals_map(state) -> dict[str, str]:
    out: dict[str, str] = {}
    for var in state.variables.values():
        for evt in var.events:
            if evt.scope == "global":
                out[var.name] = evt.raw_value
    return out


def _stage_order(state) -> list[str]:
    declared = state.stages
    order = list(declared) if declared is not None else \
        [".pre", "build", "test", "deploy", ".post"]
    if ".pre" not in order:
        order.insert(0, ".pre")
    if ".post" not in order:
        order.append(".post")
    return order


def compile_whatif(state) -> dict:
    """Attach a what-if program to every job node and return the report-level
    what-if annotation. Called at the end of parse_gitlab, after nodes are
    built (so flat configs and !reference resolution are available)."""
    from pipeview.parsers.gitlab_parser import (  # local import: avoid cycle
        _flatten_job,
        _resolve_nested_refs,
    )

    ctx = _CompileCtx(state)

    for job_id in state.job_configs:
        node = state.nodes.get(job_id)
        if node is None or node.kind != "job":
            continue
        rel_path, line_no, _, namespace = state.job_meta[job_id]
        plain_name = job_id.rsplit("::", 1)[-1]
        if plain_name.startswith("."):
            continue  # templates never run

        flat = _flatten_job(job_id, state)
        flat = {k: v for k, v in flat.items() if not k.startswith("__")}
        where = f"job '{job_id}'"

        program: dict[str, Any]
        rules = flat.get("rules")
        if rules is not None:
            rules, reason = _resolve_nested_refs(rules, state, ())
            if reason is not None:
                program = {"kind": "unknown", "reason": reason}
                ctx.diag(
                    "info",
                    f"{where}: rules use {reason} — the what-if simulation "
                    "treats this job as conditional",
                    line_no, rel_path, job_id,
                )
            elif isinstance(rules, list):
                compiled = [r for r in
                            (_compile_rule(r, ctx, where, line_no) for r in rules)
                            if r is not None]
                program = {"kind": "rules", "rules": compiled}
                _lint_final_bare_when(compiled, job_id, ctx, rel_path, line_no)
            else:
                program = {"kind": "unknown", "reason": "rules is not a list"}
        elif "only" in flat or "except" in flat:
            program = {
                "kind": "legacy",
                "only": _compile_only_except(flat["only"], ctx, where, line_no)
                if "only" in flat else None,
                "except": _compile_only_except(flat["except"], ctx, where, line_no)
                if "except" in flat else None,
                "implicit_default": False,
            }
        else:
            # The documented default for jobs with no rules/only/except —
            # and the #1 duplicate-pipeline cause when mixed with rules jobs.
            program = {
                "kind": "legacy",
                "only": {"refs": ["branches", "tags"], "variables": None,
                         "changes": None, "unsupported": []},
                "except": None,
                "implicit_default": True,
            }

        variables = {}
        if isinstance(flat.get("variables"), dict):
            variables = {str(k): _plain_value(v)
                         for k, v in flat["variables"].items()}

        deps = flat.get("dependencies")
        node.annotations["whatif"] = {
            "program": program,
            "when": _plain_value(flat.get("when", "on_success")),
            "allow_failure": bool(flat["allow_failure"])
            if "allow_failure" in flat else None,
            "start_in": _plain_value(flat["start_in"])
            if "start_in" in flat else None,
            "stage": _plain_value(flat.get("stage", "test")),
            "variables": variables,
            "needs": _compile_needs(flat.get("needs")),
            "dependencies": [_plain_value(d) for d in deps]
            if isinstance(deps, list) else None,
            "artifacts": _compile_artifacts(flat.get("artifacts")),
            "trigger": _compile_trigger(flat.get("trigger")),
            "child_of": namespace.rstrip(":") if namespace else None,
            "name": plain_name,
        }

    workflow = None
    raw_workflow = getattr(state, "workflow_raw", None)
    if raw_workflow:
        root_file = os.path.relpath(state.root_path, state.repo_root)
        compiled = [r for r in
                    (_compile_rule(r, ctx, "workflow", None) for r in raw_workflow)
                    if r is not None]
        for rule in compiled:
            if rule["when"] not in (None, "always", "never"):
                ctx.diag(
                    "warning",
                    f"workflow:rules has when: {rule['when']} — GitLab only "
                    "allows always or never here",
                    1, root_file,
                )
        workflow = {"name": state.workflow_name, "rules": compiled}

    return {
        "version": WHATIF_VERSION,
        "default_branch": DEFAULT_BRANCH,
        "protected_refs": list(PROTECTED_REFS),
        "workflow": workflow,
        "globals": ctx.globals_map,
        "stages": _stage_order(state),
        "lint": ctx.lint,
    }


def _lint_final_bare_when(rules: list[dict], job_id: str, ctx: _CompileCtx,
                          rel_path: str, line_no: int) -> None:
    """GitLab's own pipeline warning, statically: a final catch-all rule
    (`when` with no condition, not `never`) lets one push start both a branch
    pipeline and a merge request pipeline."""
    if not rules:
        return
    last = rules[-1]
    has_condition = last["if"] is not None or last["changes"] is not None \
        or last["exists"] is not None
    if not has_condition and last["when"] != "never":
        message = (
            f"Job '{job_id}': the final rule is an unconditional "
            f"'when: {last['when'] or 'on_success'}' — one push to a branch "
            "with an open merge request may start duplicate pipelines "
            "(GitLab shows the same warning)"
        )
        ctx.lint.append({"job": job_id, "message": message})
        ctx.diag("warning", message, line_no, rel_path, job_id)
