"""Python twin of the report's embedded GitHub What-If evaluator.

This is a literal port of templates/whatif_github.js — same functions, same
output keys (camelCase, as the report JS consumes them), same note/error
message strings. The two are pinned together by a shared vector suite
(tests/github_whatif_vectors.json + tests/test_github_whatif_parity.py),
exactly like the GitLab pair.

Entry point: ``evaluate_event(report_dict, config)`` where config is the
camelCase knob set the What-If tab produces:

    {scenario: push_branch|push_tag|pr|schedule|workflow_dispatch|release,
     branch, tag, newBranch, openPR, prAction, target, draft,
     changedFiles: None|'all'|[paths], commitMessage,
     dispatchWorkflow, inputs: {}, releaseAction, overrides: {}}

``overrides`` pins values the simulator cannot know: keys with a dot are
expression-context paths (``vars.DEPLOY``, ``github.repository``), plain
keys are environment variable names.

The output structure matches the GitLab evaluator's shape (candidates /
duplicates / lint / fatal; per-job outcomes with runs / skipped /
conditional states and rule-by-rule traces) so the report UI renders both
with the same code.
"""

from __future__ import annotations

import json
from typing import Any

from pipeview.parsers.github_whatif import (
    WHATIF_VERSION,
    match_pattern_list,
    pattern_to_regex,
)


class WhatifVersionError(Exception):
    """The report's what-if program speaks a version this evaluator doesn't."""


# ---------------- tri-state logic (None = unknown) ----------------

def tri_and(values: list) -> bool | None:
    unknown = False
    for v in values:
        if v is False:
            return False
        if v is None:
            unknown = True
    return None if unknown else True


def tri_or(values: list) -> bool | None:
    unknown = False
    for v in values:
        if v is True:
            return True
        if v is None:
            unknown = True
    return None if unknown else False


def tri_not(v: bool | None) -> bool | None:
    return None if v is None else not v


# ---------------- value model ----------------

class _Unknown:
    """Sentinel for runtime-only values; never leaks into JSON output."""


UNKNOWN = _Unknown()

FAKE_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0"
PR_NUMBER = "1234"


def truthy(value) -> bool | None:
    """GitHub expression truthiness: false, 0, '' and null are falsy;
    every other value — including the STRING 'false' — is truthy."""
    if value is UNKNOWN:
        return None
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _to_number(value) -> float:
    if value is None:
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return 0.0
        try:
            if s.lower().startswith(("0x", "-0x")):
                return float(int(s, 16))
            return float(s)
        except ValueError:
            return float("nan")
    return float("nan")


def loose_equal(a, b) -> bool:
    """GitHub's loose equality: same-type strings compare case-insensitively;
    mixed types coerce to numbers (NaN equals nothing)."""
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, bool) and isinstance(b, bool):
        return a == b
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and not isinstance(a, bool) \
            and isinstance(b, (int, float)) and not isinstance(b, bool):
        return float(a) == float(b)
    na, nb = _to_number(a), _to_number(b)
    if na != na or nb != nb:   # NaN
        return False
    return na == nb


# ---------------- context lookup ----------------

_RUNTIME_PREFIXES = ("runner.", "steps.", "job.", "strategy.")

_MISSING = object()


def _ci_get(mapping, key):
    """Case-insensitive lookup (GitHub context property names are
    case-insensitive; the compiler lowercases AST paths). Exact hit first,
    then a scan. Returns _MISSING when absent."""
    if not mapping:
        return _MISSING
    if key in mapping:
        return mapping[key]
    low = key.lower()
    for k, v in mapping.items():
        if k.lower() == low:
            return v
    return _MISSING


def _lookup_ctx(path, ctx, notes):
    """Resolve one context path against the candidate world. Returns a
    value, None (deliberately empty), or UNKNOWN."""
    overrides = ctx.get("overrides") or {}
    v = _ci_get(overrides, path)
    if v is not _MISSING:
        return v
    v = _ci_get(ctx["contexts"], path)
    if v is not _MISSING:
        return v

    if path.startswith("env."):
        name = path[4:]
        env_chain = ctx.get("envChain") or {}
        v = _ci_get(overrides, name)
        if v is not _MISSING:
            return v
        v = _ci_get(env_chain, name)
        if v is not _MISSING:
            if isinstance(v, str) and "${{" in v:
                if notes is not None:
                    notes.append("env." + name + " is built from an "
                                 "expression — not simulated")
                return UNKNOWN
            return v
        return None   # env vars default to empty
    if path.startswith("vars."):
        if notes is not None:
            notes.append(path + " is a repository/organization variable — "
                         "not visible in the workflow files; add it in the "
                         "variables panel to pin a value")
        return UNKNOWN
    if path.startswith("secrets."):
        if notes is not None:
            notes.append(path + " is a secret — values are never visible; "
                         "treated as unknown")
        return UNKNOWN
    if path.startswith("inputs.") or path.startswith("github.event.inputs."):
        # inputs the candidate does not define are empty
        if ctx.get("hasInputs"):
            return None
        if notes is not None:
            notes.append(path + " — this run was not started by a dispatch "
                         "or a reusable-workflow call, so inputs are empty")
        return None
    if path.startswith("needs."):
        parts = path.split(".")
        if len(parts) >= 3 and parts[2] == "result":
            v = _ci_get(ctx.get("needResults") or {}, parts[1])
            if v is not _MISSING:
                return v
            return UNKNOWN
        if notes is not None:
            notes.append(path + " is produced at run time — not simulated")
        return UNKNOWN
    if path.startswith("matrix."):
        matrix_vars = ctx.get("matrixVars")
        if matrix_vars is not None:
            v = _ci_get(matrix_vars, path[7:])
            return None if v is _MISSING else v
        return None
    if path.startswith(_RUNTIME_PREFIXES):
        if notes is not None:
            notes.append(path + " is known only at run time — not simulated")
        return UNKNOWN
    if path == "github.token":
        return UNKNOWN
    if path.startswith("github."):
        controlled = ctx.get("controlled") or {"names": [], "prefixes": []}
        decided = path in controlled["names"] \
            or any(path.startswith(p) for p in controlled["prefixes"])
        if decided:
            return None   # deliberately absent for this event
        if notes is not None:
            notes.append(path + " is set by GitHub at run time — not "
                         "simulated; add it in the variables panel to pin "
                         "a value")
        return UNKNOWN
    return None


# ---------------- expression evaluation ----------------

def eval_value(ast, ctx, notes=None):
    """Evaluate an expression AST to a VALUE (string/number/bool/None) or
    UNKNOWN. `&&`/`||` return operand values, like the real evaluator."""
    if notes is None:
        notes = []
    if not ast:
        return True
    if ast.get("t") == "lit":
        return ast["value"]
    if ast.get("t") == "ctx":
        if ast.get("dynamic"):
            notes.append("dynamic index/filter in " + ast["path"]
                         + " — not simulated")
            return UNKNOWN
        return _lookup_ctx(ast["path"], ctx, notes)
    op = ast.get("op")
    if op == "opaque":
        notes.append("expression could not be analyzed: "
                     + (ast.get("src") or ""))
        return UNKNOWN
    if op == "invalid":
        notes.append("GitHub rejects this expression: "
                     + (ast.get("src") or ""))
        return UNKNOWN
    if op == "and":
        # && returns operand values, but a definitely-falsy operand decides
        # the outcome even past an unknown one — evaluate them all
        vals = [eval_value(a, ctx, notes) for a in ast["args"]]
        ts = [truthy(v) for v in vals]
        agg = tri_and(ts)
        if agg is True:
            return vals[-1] if vals else True
        if agg is False:
            for v, t in zip(vals, ts):
                if t is False:
                    return v
            return False
        return UNKNOWN
    if op == "or":
        vals = [eval_value(a, ctx, notes) for a in ast["args"]]
        ts = [truthy(v) for v in vals]
        agg = tri_or(ts)
        if agg is True:
            for v, t in zip(vals, ts):
                if t is True:
                    return v
            return True
        if agg is False:
            return vals[-1] if vals else False
        return UNKNOWN
    if op == "not":
        t = truthy(eval_value(ast["arg"], ctx, notes))
        return UNKNOWN if t is None else not t
    if op == "cmp":
        left = eval_value(ast["left"], ctx, notes)
        right = eval_value(ast["right"], ctx, notes)
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        c = ast["cmp"]
        if c == "==":
            return loose_equal(left, right)
        if c == "!=":
            return not loose_equal(left, right)
        na, nb = _to_number(left), _to_number(right)
        if na != na or nb != nb:
            return False
        if c == "<":
            return na < nb
        if c == "<=":
            return na <= nb
        if c == ">":
            return na > nb
        if c == ">=":
            return na >= nb
        return UNKNOWN
    if op == "call":
        return _eval_call(ast, ctx, notes)
    return UNKNOWN


def _eval_call(ast, ctx, notes):
    fn = ast["fn"]
    if fn in ("success", "always", "failure", "cancelled"):
        needs_state = ctx.get("needsState", True)
        if fn == "always":
            return True
        if fn == "cancelled":
            return False
        if fn == "success":
            if needs_state is None:
                return UNKNOWN
            if needs_state is False:
                return False
            return True   # the simulation assumes completed jobs succeed
        # failure(): a skipped dependency is not a failed one, and the
        # simulation assumes no job fails
        notes.append("failure() — the simulation assumes no dependency "
                     "fails, so this is false; the job runs only in real "
                     "failure scenarios")
        return False
    args = [eval_value(a, ctx, notes) for a in ast.get("args") or []]
    if any(a is UNKNOWN for a in args):
        return UNKNOWN
    if fn == "contains":
        if len(args) < 2:
            return False
        hay, needle = args[0], args[1]
        if isinstance(hay, list):
            return any(loose_equal(h, needle) for h in hay)
        return _to_str(needle).lower() in _to_str(hay).lower()
    if fn == "startswith":
        if len(args) < 2:
            return False
        return _to_str(args[0]).lower().startswith(_to_str(args[1]).lower())
    if fn == "endswith":
        if len(args) < 2:
            return False
        return _to_str(args[0]).lower().endswith(_to_str(args[1]).lower())
    if fn == "format":
        if not args:
            return ""
        out = _to_str(args[0])
        out = out.replace("{{", "\x00").replace("}}", "\x01")
        for i, a in enumerate(args[1:]):
            out = out.replace("{" + str(i) + "}", _to_str(a))
        return out.replace("\x00", "{").replace("\x01", "}")
    if fn == "join":
        if not args:
            return ""
        sep = _to_str(args[1]) if len(args) > 1 else ","
        if isinstance(args[0], list):
            return sep.join(_to_str(a) for a in args[0])
        return _to_str(args[0])
    if fn == "tojson":
        # compact separators match the JS twin's JSON.stringify byte-for-byte
        return json.dumps(_json_norm(args[0]) if args else None,
                          separators=(",", ":"))
    if fn == "fromjson":
        if not args:
            return UNKNOWN
        try:
            return json.loads(_to_str(args[0]))
        except (ValueError, TypeError):
            notes.append("fromJSON() argument is not valid JSON")
            return UNKNOWN
    if fn == "hashfiles":
        notes.append("hashFiles() depends on workspace content — not "
                     "simulated")
        return UNKNOWN
    return UNKNOWN


def _json_norm(v):
    """Whole floats serialize as integers, like JSON.stringify."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_json_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _json_norm(x) for k, x in v.items()}
    return v


def _to_str(v) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def eval_condition(ast, ctx, notes=None) -> bool | None:
    """AST → tri-state boolean (the job-inclusion question)."""
    return truthy(eval_value(ast, ctx, notes))


def _collect_ast_paths(ast, out: dict) -> None:
    if not isinstance(ast, dict):
        return
    if ast.get("t") == "ctx":
        out[ast["path"]] = True
        return
    for k in ("left", "right", "arg"):
        if ast.get(k):
            _collect_ast_paths(ast[k], out)
    for a in ast.get("args") or []:
        _collect_ast_paths(a, out)


def _display_value(path, ctx):
    v = _lookup_ctx(path, ctx, None)
    if v is UNKNOWN:
        return {"name": path, "value": None, "runtime": True}
    return {"name": path, "value": None if v is None else _to_str(v),
            "runtime": False}


def uses_status_fn(ast) -> bool:
    if not isinstance(ast, dict):
        return False
    if ast.get("op") == "call" and ast.get("fn") in (
            "success", "always", "failure", "cancelled"):
        return True
    for k in ("left", "right", "arg"):
        if ast.get(k) and uses_status_fn(ast[k]):
            return True
    return any(uses_status_fn(a) for a in ast.get("args") or [])


# ---------------- trigger (on:) evaluation ----------------

def _short_ref(candidate) -> str:
    return candidate["ref"]


def match_paths(patterns, changed_files) -> bool | None:
    """paths: — run when at least one changed file matches the ordered
    pattern list."""
    unknown = False
    for f in changed_files:
        m = match_pattern_list(f, patterns)
        if m is True:
            return True
        if m is None:
            unknown = True
    return None if unknown else False


def match_paths_ignore(patterns, changed_files) -> bool | None:
    """paths-ignore: — run when at least one changed file matches NONE of
    the patterns."""
    unknown = False
    for f in changed_files:
        hit = False
        for p in patterns:
            rx = pattern_to_regex(p)
            if rx is None:
                unknown = True
                continue
            if rx.match(f):
                hit = True
                break
        if not hit:
            return True
    return None if unknown else False


_DEFAULT_PR_TYPES = ["opened", "synchronize", "reopened"]


def _eval_trigger(wf, event, candidate, config, trace):
    """Does this workflow's on: subscribe to this fired event, with these
    filters? → tri-state created + trace entries."""
    on = wf.get("on") or {}
    cfg = on.get(event["name"])
    if cfg is None:
        trace.append({"rule": None, "desc": "on: has no " + event["name"]
                      + " trigger", "verdict": "no match"})
        return False
    parts: list[bool | None] = []

    def add(desc, verdict_v, notes=None):
        verdict = "matched" if verdict_v is True \
            else "no match" if verdict_v is False else "unknown"
        trace.append({"rule": len(trace), "desc": desc, "verdict": verdict,
                      "notes": notes or []})
        parts.append(verdict_v)

    if event["name"] == "push":
        is_tag = candidate["refType"] == "tag"
        b, bi = cfg.get("branches"), cfg.get("branches_ignore")
        t, ti = cfg.get("tags"), cfg.get("tags_ignore")
        if is_tag:
            if t is not None:
                add("tags: " + ", ".join(t),
                    match_pattern_list(candidate["ref"], t))
            elif ti is not None:
                m = match_pattern_list(candidate["ref"], ti)
                add("tags-ignore: " + ", ".join(ti), tri_not(m))
            elif b is not None or bi is not None:
                add("push filters only branches — tag pushes do not "
                    "trigger it", False)
        else:
            if b is not None:
                add("branches: " + ", ".join(b),
                    match_pattern_list(candidate["ref"], b))
            elif bi is not None:
                m = match_pattern_list(candidate["ref"], bi)
                add("branches-ignore: " + ", ".join(bi), tri_not(m))
            elif t is not None or ti is not None:
                add("push filters only tags — branch pushes do not "
                    "trigger it", False)
    elif event["name"] in ("pull_request", "pull_request_target"):
        types = cfg.get("types") or _DEFAULT_PR_TYPES
        action = event.get("action") or "synchronize"
        add("types: " + ", ".join(types)
            + (" (default)" if not cfg.get("types") else ""),
            action in types)
        target = candidate.get("target") or ""
        b, bi = cfg.get("branches"), cfg.get("branches_ignore")
        if b is not None:
            add("branches (the PR's BASE branch): " + ", ".join(b),
                match_pattern_list(target, b))
        elif bi is not None:
            add("branches-ignore (the PR's BASE branch): " + ", ".join(bi),
                tri_not(match_pattern_list(target, bi)))
    elif event["name"] == "release":
        types = cfg.get("types")
        if types:
            action = event.get("action") or "published"
            add("types: " + ", ".join(types), action in types)
    elif event["name"] == "schedule":
        crons = cfg.get("crons") or []
        add("schedule: " + (", ".join(crons) if crons else "(no cron)"),
            bool(crons),
            ["each cron fires on its own schedule — shown together here"]
            if crons else [])
    elif event["name"] == "workflow_dispatch":
        add("workflow_dispatch — started manually for a chosen ref", True)

    # paths filters (push and pull_request family)
    if event["name"] in ("push", "pull_request", "pull_request_target"):
        p, pi = cfg.get("paths"), cfg.get("paths_ignore")
        changed = config.get("changedFiles")
        if p is not None:
            if candidate["refType"] == "tag":
                add("paths: " + ", ".join(p), True,
                    ["paths filters do not apply to tag pushes"])
            elif changed == "all":
                add("paths: " + ", ".join(p), True,
                    ["assuming every paths: pattern matches"])
            elif changed is None:
                add("paths: " + ", ".join(p), None,
                    ["depends on which files changed — fill in the "
                     "changed-files list"])
            else:
                add("paths: " + ", ".join(p), match_paths(p, changed))
        elif pi is not None:
            if candidate["refType"] == "tag":
                add("paths-ignore: " + ", ".join(pi), True,
                    ["paths filters do not apply to tag pushes"])
            elif changed == "all":
                add("paths-ignore: " + ", ".join(pi), True,
                    ["assuming some changed file escapes the ignore list"])
            elif changed is None:
                add("paths-ignore: " + ", ".join(pi), None,
                    ["depends on which files changed — fill in the "
                     "changed-files list"])
            else:
                add("paths-ignore: " + ", ".join(pi),
                    match_paths_ignore(pi, changed))

    if not parts:
        trace.append({"rule": None, "desc": "on: " + event["name"]
                      + " (no filters)", "verdict": "matched"})
        return True
    return tri_and(parts)


# ---------------- world construction ----------------

# Context paths the world-builder decides about even when it leaves them
# unset — so the evaluator can tell "deliberately absent" from "not
# simulated".
_CONTROLLED = {
    "names": ["github.base_ref", "github.head_ref",
              "github.event.action", "github.event.created",
              "github.event.deleted", "github.event.forced"],
    "prefixes": ["github.event.pull_request.", "github.event.release.",
                 "github.event.head_commit.", "github.event.inputs.",
                 "inputs."],
}


def _controlled_for(contexts):
    names = list(contexts.keys())
    for n in _CONTROLLED["names"]:
        if n not in names:
            names.append(n)
    return {"names": names, "prefixes": list(_CONTROLLED["prefixes"])}


def _is_protected(ref, ref_type, whatif, config):
    if ref_type == "tag":
        return bool(config.get("tagProtected"))
    return ref in (whatif.get("protected_refs") or [])


def _build_world(candidate, config, whatif, wf):
    """The github.* context tree and GITHUB_* env for one candidate, flat
    dotted paths. Mirrors whatif_github.js buildWorld."""
    msg = config.get("commitMessage") or "Update code"
    source = candidate["source"]
    ref_type = candidate["refType"]
    ref = candidate["ref"]
    default_branch = whatif["default_branch"]
    wf_name = wf.get("name") or wf.get("file") or candidate.get("workflow")

    if ref_type == "pull_request":
        full_ref = "refs/pull/" + PR_NUMBER + "/merge"
        ref_name = PR_NUMBER + "/merge"
        git_ref_type = "branch"
        protected = False
    elif source == "pull_request_target":
        # pull_request_target runs in the context of the BASE ref
        full_ref = "refs/heads/" + (candidate.get("target") or default_branch)
        ref_name = candidate.get("target") or default_branch
        git_ref_type = "branch"
        protected = _is_protected(ref_name, "branch", whatif, config)
    elif ref_type == "tag":
        full_ref = "refs/tags/" + ref
        ref_name = ref
        git_ref_type = "tag"
        protected = _is_protected(ref, "tag", whatif, config)
    else:
        full_ref = "refs/heads/" + ref
        ref_name = ref
        git_ref_type = "branch"
        protected = _is_protected(ref, "branch", whatif, config)

    contexts: dict[str, Any] = {
        "github.event_name": source,
        "github.ref": full_ref,
        "github.ref_name": ref_name,
        "github.ref_type": git_ref_type,
        "github.ref_protected": protected,
        "github.sha": FAKE_SHA,
        "github.workflow": wf_name,
        "github.default_branch": default_branch,
    }
    env: dict[str, str] = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": source,
        "GITHUB_REF": full_ref,
        "GITHUB_REF_NAME": ref_name,
        "GITHUB_REF_TYPE": git_ref_type,
        "GITHUB_REF_PROTECTED": "true" if protected else "false",
        "GITHUB_SHA": FAKE_SHA,
        "GITHUB_WORKFLOW": wf_name or "",
    }

    if source in ("pull_request", "pull_request_target"):
        target = candidate.get("target") or default_branch
        head = candidate.get("headBranch") or config.get("branch") \
            or "feature/widget"
        contexts["github.base_ref"] = target
        contexts["github.head_ref"] = head
        contexts["github.event.action"] = candidate.get("action") \
            or "synchronize"
        contexts["github.event.pull_request.number"] = float(PR_NUMBER)
        contexts["github.event.pull_request.draft"] = \
            bool(config.get("draft"))
        contexts["github.event.pull_request.base.ref"] = target
        contexts["github.event.pull_request.head.ref"] = head
        contexts["github.event.pull_request.title"] = "Example pull request"
        contexts["github.event.pull_request.merged"] = False
        env["GITHUB_BASE_REF"] = target
        env["GITHUB_HEAD_REF"] = head
    elif source == "push":
        contexts["github.event.created"] = bool(config.get("newBranch")) \
            and ref_type == "branch"
        contexts["github.event.deleted"] = False
        contexts["github.event.forced"] = False
        nl = msg.find("\n")
        contexts["github.event.head_commit.message"] = msg
        env["GITHUB_EVENT_NAME"] = "push"
        contexts["github.event.head_commit.title"] = \
            msg[:nl] if nl >= 0 else msg
    elif source == "release":
        contexts["github.event.action"] = candidate.get("action") \
            or "published"
        contexts["github.event.release.tag_name"] = ref
        contexts["github.event.release.draft"] = False
        contexts["github.event.release.prerelease"] = False
    elif source == "workflow_dispatch":
        contexts["github.event.action"] = None

    return contexts, env


def _candidate_inputs(wf, config, candidate):
    """The inputs context for a dispatch run or a reusable-workflow call:
    supplied values over declared defaults. Returns (map, notes)."""
    notes: list[str] = []
    trig = "workflow_call" if candidate.get("childOf") else "workflow_dispatch"
    declared = ((wf.get("on") or {}).get(trig) or {}).get("inputs") or {}
    supplied = candidate.get("inputs") if candidate.get("childOf") \
        else (config.get("inputs") or {})
    supplied = supplied or {}
    out: dict[str, Any] = {}
    for name, spec in declared.items():
        if name in supplied:
            raw = supplied[name]
        else:
            raw = spec.get("default")
            if raw is None and spec.get("required"):
                notes.append("required input '" + name + "' has no value — "
                             "GitHub refuses the "
                             + ("call" if candidate.get("childOf")
                                else "dispatch") + " without it")
        if raw is None:
            out[name] = None
        elif spec.get("type") == "boolean":
            out[name] = raw if isinstance(raw, bool) \
                else _to_str(raw).lower() == "true"
        elif spec.get("type") == "number":
            out[name] = _to_number(raw)
        else:
            out[name] = _to_str(raw)
    for name, raw in supplied.items():
        if name not in out:
            out[name] = _to_str(raw)
            notes.append("input '" + name + "' is not declared by the "
                         "workflow — GitHub rejects it")
    return out, notes


# ---------------- job evaluation ----------------

def might_run(outcome) -> bool:
    if not outcome:
        return False
    if outcome["state"] == "conditional":
        return bool(outcome.get("included"))
    return outcome["state"] in ("runs", "manual", "delayed")


def _job_outcome_runs(job_whatif):
    return {"included": True, "state": "runs", "when": "on_success",
            "allow_failure": bool(job_whatif.get("continue_on_error")),
            "start_in": None, "variables": None}


def _evaluate_job_once(job_whatif, ctx, trace):
    """One (matrix instance of a) job: needs gate + if condition."""
    needs_state = ctx.get("needsState", True)
    ast = job_whatif.get("if")
    has_status = uses_status_fn(ast) if ast else False

    needs_desc = None
    if job_whatif.get("needs"):
        needs_desc = "needs: " + ", ".join(
            n["job"] for n in job_whatif["needs"])

    if ast is None:
        cond = needs_state
        if needs_desc:
            trace.append({
                "rule": 0, "desc": needs_desc,
                "verdict": "matched" if cond is True
                else "no match" if cond is False else "unknown",
                "notes": [] if cond is True
                else ["a needed job is " + (ctx.get("needsBlockedBy") or
                                            "skipped")
                      + " — this job is skipped too"] if cond is False
                else ["whether the needed jobs run is uncertain"],
            })
        else:
            trace.append({"rule": None, "desc": "no if: condition",
                          "verdict": "matched"})
        if cond is True:
            return _job_outcome_runs(job_whatif)
        if cond is False:
            return {"included": False, "state": "skipped",
                    "reason": "a needed job is "
                    + (ctx.get("needsBlockedBy") or "skipped")}
        return {
            "state": "conditional",
            "condition": needs_desc or "needs uncertain",
            "conditionNotes": ["whether the needed jobs run is uncertain"],
            "then": _job_outcome_runs(job_whatif),
            "otherwise": {"included": False, "state": "skipped"},
            "included": True,
        }

    notes: list[str] = []
    desc = "if: " + (job_whatif.get("raw_if") or "")
    v = eval_condition(ast, ctx, notes)
    paths: dict[str, bool] = {}
    _collect_ast_paths(ast, paths)
    vars_ = [_display_value(p, ctx) for p in sorted(paths)]

    # without a status function, GitHub prepends the implicit success()
    # check on the needed jobs
    gate_parts = [v]
    gate_note = None
    if not has_status and job_whatif.get("needs"):
        gate_parts.append(needs_state)
        if needs_state is False:
            gate_note = ("a needed job is "
                         + (ctx.get("needsBlockedBy") or "skipped")
                         + " — without always(), this job is skipped too")
        elif needs_state is None:
            gate_note = "whether the needed jobs run is uncertain"
    cond = tri_and(gate_parts)
    if gate_note:
        notes.append(gate_note)

    trace.append({
        "rule": 0, "desc": desc,
        "verdict": "matched" if cond is True
        else "no match" if cond is False else "unknown",
        "notes": notes, "vars": vars_,
    })
    if cond is True:
        return _job_outcome_runs(job_whatif)
    if cond is False:
        reason = "if: condition is false" if v is False \
            else "a needed job is " + (ctx.get("needsBlockedBy") or "skipped")
        return {"included": False, "state": "skipped", "reason": reason}
    return {
        "state": "conditional",
        "condition": desc,
        "conditionNotes": notes,
        "then": _job_outcome_runs(job_whatif),
        "otherwise": {"included": False, "state": "skipped"},
        "included": True,
    }


_MATRIX_ORDER = ["runs", "conditional", "skipped", "not-added"]


def _evaluate_job(job_whatif, ctx):
    trace: list[dict] = []
    par = job_whatif.get("parallel")
    if not par:
        out = _evaluate_job_once(job_whatif, ctx, trace)
        out["trace"] = trace
        return out
    if par.get("kind") == "dynamic":
        out = _evaluate_job_once(job_whatif, ctx, trace)
        out["trace"] = trace
        out["matrixCount"] = None
        out["matrixDynamic"] = True
        return out

    per = []
    for c in par.get("combos") or []:
        sub_ctx = dict(ctx)
        sub_ctx["matrixVars"] = c["vars"]
        sub_trace: list[dict] = []
        per.append({"name": c["name"],
                    "outcome": _evaluate_job_once(job_whatif, sub_ctx,
                                                  sub_trace),
                    "trace": sub_trace})
    if not per:
        out = _evaluate_job_once(job_whatif, ctx, trace)
        out["trace"] = trace
        return out

    def order(state):
        return _MATRIX_ORDER.index(state) if state in _MATRIX_ORDER \
            else len(_MATRIX_ORDER)

    pick = per[0]
    for p in per:
        if order(p["outcome"]["state"]) < order(pick["outcome"]["state"]):
            pick = p
    base = dict(pick["outcome"])
    base["trace"] = pick["trace"]
    base["matrix"] = [{"name": p["name"], "state": p["outcome"]["state"],
                       "when": p["outcome"].get("when")} for p in per]
    base["matrixCount"] = len(per)
    base["matrixPartial"] = any(
        p["outcome"]["state"] != per[0]["outcome"]["state"] for p in per)
    base["included"] = any(might_run(p["outcome"]) for p in per)
    return base


def _topo_order(jobs):
    """Jobs in needs-respecting order (definition order among ready jobs)."""
    by_name = {j["whatif"]["name"]: j for j in jobs}
    done: set[str] = set()
    out = []
    remaining = list(jobs)
    while remaining:
        progressed = False
        for j in list(remaining):
            needs = j["whatif"].get("needs") or []
            if all(n["job"] in done or n["job"] not in by_name
                   for n in needs):
                out.append(j)
                done.add(j["whatif"]["name"])
                remaining.remove(j)
                progressed = True
        if not progressed:   # cycle — parser already flagged it
            out.extend(remaining)
            break
    return out


# ---------------- candidate evaluation ----------------

_MAX_CALL_DEPTH = 4


def _job_index(report):
    jobs = []
    for n in report.get("nodes") or []:
        w = (n.get("annotations") or {}).get("whatif")
        if n.get("kind") == "job" and w:
            jobs.append({"id": n["id"], "node": n, "whatif": w})
    return jobs


def _evaluate_candidate(candidate, all_jobs, config, whatif, report):
    wf_key = candidate["workflow"]
    wf = (whatif.get("workflows") or {}).get(wf_key) or {}
    contexts, env = _build_world(candidate, config, whatif, wf)
    inputs_map: dict[str, Any] = {}
    input_notes: list[str] = []
    is_input_run = bool(candidate.get("childOf")) \
        or candidate["source"] == "workflow_dispatch"
    if is_input_run:
        inputs_map, input_notes = _candidate_inputs(wf, config, candidate)
        for name, val in inputs_map.items():
            contexts["inputs." + name] = val
            contexts["github.event.inputs." + name] = \
                None if val is None else _to_str(val)
    controlled = _controlled_for(contexts)

    trace: list[dict] = []
    invalid = wf.get("invalid") or []
    if invalid:
        created: bool | None = False
        reason = ("invalid workflow file — GitHub refuses to run it: "
                  + "; ".join(invalid))
        trace.append({"rule": None, "desc": reason, "verdict": "no match"})
    elif candidate.get("source") == "workflow_run" \
            and candidate.get("triggerReason"):
        created = candidate.get("triggerVerdict", True)
        reason = candidate.get("triggerReason") or "workflow_run trigger"
        trace.append({"rule": None, "desc": reason,
                      "verdict": "matched" if created is True
                      else "unknown" if created is None else "no match"})
    elif candidate.get("childOf"):
        created = True
        reason = ("called by '" + (candidate.get("parentJob") or "?")
                  + "' — a reusable workflow runs whenever its caller does")
        trace.append({"rule": None, "desc": reason, "verdict": "matched"})
    else:
        event = {"name": candidate["source"],
                 "action": candidate.get("action")}
        created = _eval_trigger(wf, event, candidate, config, trace)
        if created is True:
            reason = "on: " + candidate["source"] + " matches this event"
        elif created is False:
            reason = ("on: " + candidate["source"]
                      + " filters exclude this event")
        else:
            reason = ("whether on: " + candidate["source"]
                      + " matches depends on unknown facts")

    base_ctx = {
        "contexts": contexts,
        "envChain": {**(wf.get("env") or {})},
        "overrides": config.get("overrides") or {},
        "controlled": controlled,
        "hasInputs": is_input_run,
    }

    jobs = [j for j in all_jobs if j["whatif"].get("workflow") == wf_key]
    ordered = _topo_order(jobs)
    results: dict[str, dict] = {}
    outcomes_by_name: dict[str, dict] = {}
    for job in ordered:
        jw = job["whatif"]
        needs = jw.get("needs") or []
        need_states = []
        need_results: dict[str, str] = {}
        blocked_by = None
        for n in needs:
            no = outcomes_by_name.get(n["job"])
            if no is None:
                need_states.append(None)
                continue
            if no["state"] == "conditional":
                need_states.append(None)
                need_results[n["job"]] = "unknown"
            elif might_run(no):
                need_states.append(True)
                need_results[n["job"]] = "success"
            else:
                need_states.append(False)
                need_results[n["job"]] = "skipped"
                blocked_by = blocked_by or ("skipped ('" + n["job"] + "')")
        need_results = {k: v for k, v in need_results.items()
                        if v != "unknown"}
        ctx = dict(base_ctx)
        ctx["envChain"] = {**(wf.get("env") or {}), **(jw.get("env") or {})}
        ctx["needsState"] = tri_and(need_states) if need_states else True
        ctx["needsBlockedBy"] = blocked_by
        ctx["needResults"] = need_results
        outcome = _evaluate_job(jw, ctx)
        results[job["id"]] = outcome
        outcomes_by_name[jw["name"]] = outcome

    artifacts = {"notes": [], "errors": [], "producers": []}
    for note in input_notes:
        artifacts["notes"].append({"job": None, "kind": "inputs",
                                   "message": note})
    conc = wf.get("concurrency")
    if conc and conc.get("cancel_in_progress"):
        artifacts["notes"].append({
            "job": None, "kind": "concurrency",
            "message": "concurrency group '" + (conc.get("group") or "")
            + "' with cancel-in-progress — a newer run of this workflow "
            + "cancels this one"})
    for job in ordered:
        jw = job["whatif"]
        envc = jw.get("environment")
        if envc and envc.get("name") and might_run(results[job["id"]]):
            artifacts["notes"].append({
                "job": job["id"], "kind": "environment",
                "message": '"' + jw["name"] + '" deploys to environment "'
                + envc["name"] + '" — protection rules there (required '
                + "reviewers, wait timers) can hold it for approval; "
                + "configured in repository settings, not visible here"})

    # reusable-workflow calls spawn child candidates (recursive)
    children: list[dict] = []
    lineage = candidate.get("lineage") or []
    for job in ordered:
        jw = job["whatif"]
        uses = jw.get("uses")
        if not uses or not might_run(results[job["id"]]) \
                or created is False:
            continue
        if uses.get("kind") == "local" and uses.get("workflow") \
                in (whatif.get("workflows") or {}):
            target = uses["workflow"]
            if target == wf_key or target in lineage:
                artifacts["notes"].append({
                    "job": job["id"], "kind": "downstream",
                    "message": '"' + jw["name"] + '" re-calls ' + target
                    + " which is already in this chain — cycle not "
                    + "expanded"})
                continue
            if len(lineage) >= _MAX_CALL_DEPTH:
                artifacts["notes"].append({
                    "job": job["id"], "kind": "downstream",
                    "message": "reusable workflows nested deeper than "
                    + str(_MAX_CALL_DEPTH)
                    + " levels are not expanded (GitHub's own limit)"})
                continue
            child_candidate = {
                "id": candidate["id"] + ">" + job["id"] + ">" + target,
                "source": candidate["source"],
                "refType": candidate["refType"],
                "ref": candidate["ref"],
                "target": candidate.get("target"),
                "action": candidate.get("action"),
                "headBranch": candidate.get("headBranch"),
                "label": "Called workflow: " + target,
                "workflow": target,
                "childOf": target,
                "parentJob": job["id"],
                "parentConditional":
                    results[job["id"]]["state"] == "conditional",
                "inputs": uses.get("with") or {},
                "lineage": lineage + [wf_key],
            }
            child_result = _evaluate_candidate(child_candidate, all_jobs,
                                               config, whatif, report)
            children.append(child_result)
            if child_result["created"] is False:
                artifacts["errors"].append({
                    "job": job["id"], "target": target, "kind": "trigger",
                    "message": '"' + jw["name"] + '" calls ' + target
                    + " which cannot run (" + child_result["reason"]
                    + ") — GitHub fails the caller job"})
        elif uses.get("kind") == "remote":
            artifacts["notes"].append({
                "job": job["id"], "kind": "downstream",
                "message": '"' + jw["name"] + '" calls the reusable '
                + "workflow " + uses["raw"] + " in another repository — "
                + "its config is not available offline"})
        elif uses.get("kind") == "local":
            artifacts["notes"].append({
                "job": job["id"], "kind": "downstream",
                "message": '"' + jw["name"] + '" calls ' + uses["raw"]
                + " which is not in this report"})

    creation_fails = created is not False \
        and any(e["kind"] != "trigger" for e in artifacts["errors"])

    return {
        "id": candidate["id"], "label": candidate["label"],
        "source": candidate["source"], "ref": candidate["ref"],
        "refType": candidate["refType"], "target": candidate.get("target"),
        "workflow": wf_key,
        "workflowName": wf.get("name"),
        "childOf": candidate.get("childOf"),
        "parentJob": candidate.get("parentJob"),
        "parentConditional": candidate.get("parentConditional") or False,
        "created": created, "reason": reason,
        "creationFails": creation_fails,
        "workflowTrace": trace,
        "workflowVariables": {},
        "forwardedVars": {k: (None if v is None else _to_str(v))
                          for k, v in inputs_map.items()},
        "env": env, "controlled": controlled,
        "jobs": results, "jobOrder": [j["id"] for j in ordered],
        "artifacts": artifacts, "children": children,
        "separate": candidate.get("separate") or False,
    }


# ---------------- candidate enumeration ----------------

def _wf_label(wf_key, whatif):
    wf = (whatif.get("workflows") or {}).get(wf_key) or {}
    return wf.get("name") or wf_key


def _build_candidates(config, whatif):
    """events × workflows. Every workflow that subscribes to a fired event
    gets a candidate run — including the same workflow twice when a push
    also fires pull_request (the duplicate-run scenario)."""
    scenario = config.get("scenario")
    branch = config.get("branch") or whatif["default_branch"]
    tag = config.get("tag") or "v1.0.0"
    target = config.get("target") or whatif["default_branch"]
    workflows = whatif.get("workflows") or {}

    events: list[dict] = []
    if scenario == "push_branch":
        events.append({"name": "push", "refType": "branch", "ref": branch})
        if config.get("openPR"):
            action = config.get("prAction") or "synchronize"
            events.append({"name": "pull_request", "refType": "pull_request",
                           "ref": branch, "target": target,
                           "action": action, "headBranch": branch})
            events.append({"name": "pull_request_target",
                           "refType": "branch", "ref": branch,
                           "target": target, "action": action,
                           "headBranch": branch})
    elif scenario == "push_tag":
        events.append({"name": "push", "refType": "tag", "ref": tag})
    elif scenario == "pr":
        action = config.get("prAction") or "opened"
        events.append({"name": "pull_request", "refType": "pull_request",
                       "ref": branch, "target": target, "action": action,
                       "headBranch": branch})
        events.append({"name": "pull_request_target", "refType": "branch",
                       "ref": branch, "target": target, "action": action,
                       "headBranch": branch})
    elif scenario == "schedule":
        events.append({"name": "schedule", "refType": "branch",
                       "ref": whatif["default_branch"]})
    elif scenario == "workflow_dispatch":
        ref = tag if config.get("refKind") == "tag" else branch
        events.append({"name": "workflow_dispatch",
                       "refType": "tag" if config.get("refKind") == "tag"
                       else "branch", "ref": ref, "separate": True})
    elif scenario == "release":
        events.append({"name": "release", "refType": "tag", "ref": tag,
                       "action": config.get("releaseAction") or "published"})
    else:
        events.append({"name": "push", "refType": "branch", "ref": branch})

    out = []
    for event in events:
        for wf_key, wf in workflows.items():
            on = wf.get("on") or {}
            if event["name"] not in on:
                continue
            if scenario == "workflow_dispatch" \
                    and config.get("dispatchWorkflow") \
                    and wf_key != config["dispatchWorkflow"]:
                continue
            label = _wf_label(wf_key, {"workflows": workflows}) \
                + " — " + event["name"]
            if event.get("action"):
                label += " (" + event["action"] + ")"
            out.append({
                "id": wf_key + ":" + event["name"],
                "workflow": wf_key,
                "source": event["name"],
                "refType": event["refType"],
                "ref": event["ref"],
                "target": event.get("target"),
                "action": event.get("action"),
                "headBranch": event.get("headBranch"),
                "label": label,
                "childOf": None,
                "separate": bool(event.get("separate")),
            })
    return out


def _workflow_run_cascade(candidates, all_jobs, config, whatif, report,
                          depth=0):
    """Workflows with on: workflow_run fire when a named workflow's run
    completes — attach them as children of the triggering candidate. They
    always run the default branch's version of themselves."""
    if depth >= 3:
        return
    workflows = whatif.get("workflows") or {}
    for cand in candidates:
        if cand["created"] is False:
            continue
        trigger_name = cand.get("workflowName") \
            or _wf_label(cand["workflow"], whatif)
        for wf_key, wf in workflows.items():
            wr = (wf.get("on") or {}).get("workflow_run")
            if wr is None or wf_key == cand["workflow"]:
                continue
            names = wr.get("workflows") or []
            if trigger_name not in names \
                    and cand["workflow"] not in names:
                continue
            types = wr.get("types") or ["completed"]
            if "completed" not in types and "requested" not in types \
                    and "in_progress" not in types:
                continue
            verdict: bool | None = True
            note = ("runs when workflow '" + trigger_name + "' completes; "
                    + "always uses the workflow version on the default "
                    + "branch")
            b, bi = wr.get("branches"), wr.get("branches_ignore")
            if cand["refType"] == "branch" and b is not None:
                verdict = match_pattern_list(cand["ref"], b)
            elif cand["refType"] == "branch" and bi is not None:
                verdict = tri_not(match_pattern_list(cand["ref"], bi))
            elif cand["refType"] != "branch" and (b is not None
                                                  or bi is not None):
                verdict = None
            child = {
                "id": cand["id"] + ">workflow_run>" + wf_key,
                "workflow": wf_key,
                "source": "workflow_run",
                "refType": "branch",
                "ref": whatif["default_branch"],
                "target": None,
                "label": _wf_label(wf_key, whatif) + " — workflow_run",
                "childOf": wf_key,
                "parentJob": None,
                "parentConditional": cand["created"] is None,
                "triggerVerdict": verdict,
                "triggerReason": note,
                "lineage": [cand["workflow"]],
            }
            result = _evaluate_candidate(child, all_jobs, config, whatif,
                                         report)
            cand["children"].append(result)
            _workflow_run_cascade([result], all_jobs, config, whatif,
                                  report, depth + 1)


# ---------------- event evaluation (entry point) ----------------

def evaluate_event(report: dict, config: dict) -> dict | None:
    """Evaluate one What-If configuration against a report dict
    (Report.to_dict() shape) whose what-if program is GitHub-flavored.
    Returns the same structure whatif_github.js's evaluateEvent produces,
    or None when the report has no GitHub what-if program."""
    whatif = (report.get("annotations") or {}).get("whatif")
    if not whatif or whatif.get("provider") != "github":
        return None
    version = whatif.get("version")
    if version != WHATIF_VERSION:
        raise WhatifVersionError(
            f"report carries what-if program version {version!r}; this "
            f"evaluator speaks version {WHATIF_VERSION} — regenerate the "
            f"report with this pipeview")
    all_jobs = _job_index(report)
    candidates = [_evaluate_candidate(c, all_jobs, config, whatif, report)
                  for c in _build_candidates(config, whatif)]
    _workflow_run_cascade(candidates, all_jobs, config, whatif, report)

    # duplicates: the same job might run in >= 2 simultaneous candidates —
    # for GitHub that means the same WORKFLOW triggered by two events of
    # one push (job ids are namespaced per workflow)
    duplicates: list[dict] = []
    simultaneous = [c for c in candidates if not c.get("separate")]
    if len(simultaneous) > 1:
        seen: dict[str, list[str]] = {}
        for c in simultaneous:
            if c["created"] is False or c["creationFails"]:
                continue
            for job_id in c["jobOrder"]:
                if might_run(c["jobs"][job_id]):
                    seen.setdefault(job_id, []).append(c["id"])
        for job_id, cand_ids in seen.items():
            if len(cand_ids) > 1:
                entry: dict[str, Any] = {"job": job_id,
                                         "candidates": cand_ids}
                c0 = next((c for c in simultaneous if job_id in c["jobs"]),
                          None)
                if c0 and c0["jobs"][job_id].get("matrixCount"):
                    entry["instances"] = c0["jobs"][job_id]["matrixCount"]
                duplicates.append(entry)

    return {
        "candidates": candidates,
        "duplicates": duplicates,
        "crossPipelineArtifacts": False,
        "lint": whatif.get("lint") or [],
        "fatal": whatif.get("fatal") or [],
    }
