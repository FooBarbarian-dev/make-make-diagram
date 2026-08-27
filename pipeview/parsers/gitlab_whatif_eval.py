"""Python what-if evaluator — the CLI-side twin of templates/whatif.js.

A DOM-free, tri-state (True / False / None=unknown) interpreter over the
programs compiled by gitlab_whatif.py, used where no browser exists:
trigger-docs generation and pytest. It is a literal port of whatif.js —
same functions, same output keys (camelCase, as the report JS consumes
them), same note/error message strings.

PARITY CONTRACT: tests/whatif_vectors.json is the semantics table both
interpreters answer to — whatif.js under node (tests/test_whatif_evaluator
.py) and this module natively (tests/test_whatif_eval_py.py). Behavior
changes land as vectors first, then in both interpreters; never edit one
interpreter without the other.

Value model for variables (three different "missing" cases):
  - a name in the env             → that string value
  - deliberately unset for this pipeline type (CI_COMMIT_BRANCH in an MR
    pipeline, CI_MERGE_REQUEST_* in a branch pipeline, …) → unset (None)
  - any other CI_ or GITLAB_ name → UNKNOWN: real GitLab sets it at
    runtime, the simulation doesn't — verdicts touching it are tri-state
    unknown, never a confident guess
  - any other custom name         → unset (defined nowhere)

Entry point: evaluate_event(report_dict, config)
  config = {
    'scenario': 'push_branch'|'push_tag'|'mr'|'schedule'|'web'|'api'|'trigger',
    'branch', 'tag', 'refKind', 'openMR', 'target', 'draft', 'mrFlavor',
    'mrLabels', 'tagProtected', 'newBranch',
    'changedFiles': None | 'all' | [paths], 'commitMessage',
    'overrides': {NAME: value}
  }
"""

from __future__ import annotations

import re
from typing import Any

from pipeview.parsers.gitlab_whatif import WHATIF_VERSION, glob_to_regex


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


# ---------------- variable lookup ----------------

class _Unknown:
    """Sentinel for runtime-only variables; never leaks as a string."""


UNKNOWN = _Unknown()
_PREDEFINED_RE = re.compile(r"^(CI|GITLAB)_")


def _lookup_var(name, env, controlled, notes):
    if name in env:
        return env[name]
    if controlled and _PREDEFINED_RE.match(name):
        decided = name in controlled["names"] \
            or any(name.startswith(p) for p in controlled["prefixes"])
        if not decided:
            if notes is not None:
                notes.append('$' + name + ' is set by the pipeline at runtime — not '
                             + 'simulated; add it in the variables panel to pin a value')
            return UNKNOWN
    return None  # deliberately unset, or a custom variable defined nowhere


def _term_value(term, env, controlled, notes):
    if term.get("t") == "var":
        return _lookup_var(term["name"], env, controlled, notes)
    if term.get("t") == "str":
        return term["value"]
    if term.get("t") == "null":
        return None
    return None  # regex terms have no scalar value


# ---------------- expression evaluation ----------------

def _compile_regex(source, flags, notes):
    py_flags = 0
    dropped = ""
    for f in (flags or ""):
        if f == "i":
            py_flags |= re.IGNORECASE
        elif f == "m":
            py_flags |= re.MULTILINE
        elif f == "s":
            py_flags |= re.DOTALL
        else:
            dropped += f
    if dropped and notes is not None:
        notes.append('regex flags "' + dropped + '" are not supported')
    try:
        return re.compile(source, py_flags)
    except re.error:
        return None


_RE_LITERAL = re.compile(r"^/(.*)/([a-z]*)$", re.DOTALL)


def _match_against(left_val, right, env, controlled, notes):
    # Right side of =~ / !~: a /regex/ literal, a variable holding /regex/,
    # or (documented-but-discouraged fallback) a plain string → substring
    # test "left is contained in right".
    if left_val is UNKNOWN:
        return None
    # GitLab scans text.to_s — an unset left side behaves as "" (so a
    # pattern that matches the empty string, like /.*/, DOES match)
    if left_val is None:
        left_val = ""
    if right.get("t") == "re":
        if right.get("non_re2"):
            notes.append('/' + right["source"] + '/ uses lookaround or backreferences — '
                         + "GitLab's RE2 engine rejects it and the configuration is invalid")
            return None
        rx = _compile_regex(right["source"], right.get("flags"), notes)
        if rx is None:
            notes.append('invalid regex /' + right["source"] + '/')
            return None
        return rx.search(left_val) is not None
    rv = _term_value(right, env, controlled, notes)
    if rv is UNKNOWN:
        return None
    if rv is None:
        # GitLab: `return false unless regexp` — a definite no-match
        notes.append('the pattern variable is unset — GitLab returns no-match')
        return False
    m = _RE_LITERAL.match(rv)
    if m:
        rx2 = _compile_regex(m.group(1), m.group(2), notes)
        if rx2 is None:
            notes.append('invalid regex in variable: ' + rv)
            return None
        return rx2.search(left_val) is not None
    notes.append('right side "' + rv + '" is not /regex/ — GitLab falls back to a '
                 + 'substring check (undocumented behavior)')
    return left_val in rv


def eval_expr(ast, env, notes=None, controlled=None):
    if notes is None:
        notes = []
    if not ast:
        return True
    op = ast.get("op")
    if op == "opaque":
        notes.append('expression could not be parsed: ' + (ast.get("src") or ""))
        return None
    if op == "invalid":
        notes.append('GitLab rejects this expression (invalid expression syntax): '
                     + (ast.get("src") or ""))
        return None
    if op == "and":
        return tri_and([eval_expr(a, env, notes, controlled) for a in ast["args"]])
    if op == "or":
        return tri_or([eval_expr(a, env, notes, controlled) for a in ast["args"]])
    if op == "not":
        return tri_not(eval_expr(ast["arg"], env, notes, controlled))
    if op == "truthy":
        v = _term_value(ast["term"], env, controlled, notes)
        if v is UNKNOWN:
            return None
        return v is not None and v != ""   # "false" and "0" are truthy
    if op == "cmp":
        # chained comparisons: the left side may itself be a comparison
        # (left-associative, GitLab's shunting-yard behavior)
        left_is_expr = bool(ast["left"].get("op"))
        left = eval_expr(ast["left"], env, notes, controlled) if left_is_expr \
            else _term_value(ast["left"], env, controlled, notes)
        if ast["cmp"] in ("==", "!="):
            right_is_expr = bool(ast["right"].get("op"))
            right = eval_expr(ast["right"], env, notes, controlled) if right_is_expr \
                else _term_value(ast["right"], env, controlled, notes)
            if left_is_expr != right_is_expr:
                # a boolean result never equals a string/null, unknown or not
                return False if ast["cmp"] == "==" else True
            if left_is_expr:
                if left is None or right is None:
                    return None
                return (left == right) if ast["cmp"] == "==" else (left != right)
            if left is UNKNOWN or right is UNKNOWN:
                return None
            eq = left == right             # None == None is true (both unset)
            return eq if ast["cmp"] == "==" else not eq
        if left_is_expr:
            notes.append('matching a boolean result with =~ is not modeled')
            return None
        matched = _match_against(left, ast["right"], env, controlled, notes)
        if matched is None:
            return None
        return matched if ast["cmp"] == "=~" else not matched
    return None


def _collect_ast_vars(ast, out: dict) -> None:
    if not isinstance(ast, dict):
        return
    if ast.get("t") == "var":
        out[ast["name"]] = True
        return
    for k in ("left", "right", "arg", "term"):
        if ast.get(k):
            _collect_ast_vars(ast[k], out)
    for a in ast.get("args") or []:
        _collect_ast_vars(a, out)


# ---------------- glob matching (shared with the compiler) ----------------

def _match_changed_files(paths, changed_files):
    for pattern in paths:
        rx = glob_to_regex(pattern)
        if rx is None:
            return None
        for f in changed_files:
            if rx.search(f):
                return True
    return False


# ---------------- environment construction ----------------

FAKE_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0"
FAKE_BEFORE_SHA = "900dcafe900dcafe900dcafe900dcafe900dcafe"
ZERO_SHA = "0000000000000000000000000000000000000000"


def _slugify(ref: str) -> str:
    out = re.sub(r"[^0-9a-z]", "-", ref.lower())
    return out.strip("-")[:63]


def _is_protected_ref(ref, ref_type, whatif, config):
    if ref_type == "tag":
        return bool(config and config.get("tagProtected"))
    return ref in (whatif.get("protected_refs") or [])


# Names the env-builder decides about even when it leaves them unset —
# so the evaluator can tell "deliberately unset" from "not simulated".
_CONTROLLED = {
    "names": ["CI_COMMIT_BRANCH", "CI_COMMIT_TAG", "CI_COMMIT_TAG_MESSAGE",
              "CI_OPEN_MERGE_REQUESTS", "CI_PIPELINE_SCHEDULE_DESCRIPTION",
              "CI_PIPELINE_TRIGGERED"],
    "prefixes": ["CI_MERGE_REQUEST_"],
}


def _controlled_for(env):
    names = list(env.keys())
    for n in _CONTROLLED["names"]:
        if n not in names:
            names.append(n)
    return {"names": names, "prefixes": list(_CONTROLLED["prefixes"])}


def _build_env(candidate, config, whatif):
    # The predefined-variable matrix, per candidate — see whatif.js buildEnv
    # for the sourced facts this mirrors.
    msg = config.get("commitMessage") or "Update code"
    nl = msg.find("\n")
    is_plain_branch_push = candidate["source"] == "push" \
        and candidate["refType"] == "branch" and not config.get("newBranch")
    env = {
        "CI": "true",
        "GITLAB_CI": "true",
        "CI_DEFAULT_BRANCH": whatif["default_branch"],
        "CI_PROJECT_PATH": "group/project",
        "CI_PROJECT_NAME": "project",
        "CI_PROJECT_NAMESPACE": "group",
        "CI_PIPELINE_SOURCE": candidate["source"],
        "CI_COMMIT_SHA": FAKE_SHA,
        "CI_COMMIT_SHORT_SHA": FAKE_SHA[:8],
        "CI_COMMIT_MESSAGE": msg,
        "CI_COMMIT_TITLE": msg[:nl] if nl >= 0 else msg,
        "CI_COMMIT_DESCRIPTION": msg[nl + 1:] if nl >= 0 else "",
        "CI_COMMIT_REF_NAME": candidate["ref"],
        "CI_COMMIT_REF_SLUG": _slugify(candidate["ref"]),
        "CI_COMMIT_REF_PROTECTED":
            "true" if _is_protected_ref(candidate["ref"], candidate["refType"],
                                        whatif, config) else "false",
        "CI_COMMIT_BEFORE_SHA": FAKE_BEFORE_SHA if is_plain_branch_push else ZERO_SHA,
    }

    if candidate["refType"] == "branch":
        env["CI_COMMIT_BRANCH"] = candidate["ref"]
        if config.get("openMR"):
            env["CI_OPEN_MERGE_REQUESTS"] = "group/project!1"
    elif candidate["refType"] == "tag":
        env["CI_COMMIT_TAG"] = candidate["ref"]
        env["CI_COMMIT_TAG_MESSAGE"] = ""
    elif candidate["refType"] == "merge_request":
        target = candidate.get("target") or whatif["default_branch"]
        flavor = config.get("mrFlavor") or "detached"
        env["CI_OPEN_MERGE_REQUESTS"] = "group/project!1"
        env["CI_MERGE_REQUEST_ID"] = "1000"
        env["CI_MERGE_REQUEST_IID"] = "1"
        env["CI_MERGE_REQUEST_EVENT_TYPE"] = flavor
        env["CI_MERGE_REQUEST_REF_PATH"] = "refs/merge-requests/1/head"
        env["CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"] = candidate["ref"]
        env["CI_MERGE_REQUEST_TARGET_BRANCH_NAME"] = target
        env["CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED"] = \
            "true" if _is_protected_ref(candidate["ref"], "branch", whatif, config) \
            else "false"
        env["CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED"] = \
            "true" if _is_protected_ref(target, "branch", whatif, config) else "false"
        env["CI_MERGE_REQUEST_TITLE"] = "Example merge request"
        # always "true"/"false" in MR pipelines, never unset (source-verified)
        env["CI_MERGE_REQUEST_DRAFT"] = "true" if config.get("draft") else "false"
        env["CI_MERGE_REQUEST_PROJECT_ID"] = "1"
        env["CI_MERGE_REQUEST_PROJECT_PATH"] = "group/project"
        env["CI_MERGE_REQUEST_SQUASH_ON_MERGE"] = "false"
        if config.get("mrLabels"):
            env["CI_MERGE_REQUEST_LABELS"] = config["mrLabels"]
        # Empty in detached MR pipelines; real SHAs only in merged results /
        # merge trains. Empty-but-set matters: bare $VAR is falsy on "".
        merged = flavor in ("merged_result", "merge_train")
        env["CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"] = FAKE_SHA if merged else ""
        env["CI_MERGE_REQUEST_TARGET_BRANCH_SHA"] = FAKE_SHA if merged else ""

    if candidate["source"] == "schedule":
        env["CI_PIPELINE_SCHEDULE_DESCRIPTION"] = "nightly"
    if candidate["source"] == "trigger":
        env["CI_PIPELINE_TRIGGERED"] = "true"
    return env


# ---------------- rule / program evaluation ----------------

def _describe_condition(rule):
    bits = []
    if rule.get("raw_if"):
        bits.append(rule["raw_if"])
    if rule.get("changes"):
        bits.append("changes: " + ", ".join(rule["changes"]["paths"]))
    if rule.get("exists"):
        bits.append("exists: " + ", ".join(rule["exists"]["paths"]))
    return " AND ".join(bits) or "(unconditional)"


def _display_value(name, env, controlled):
    v = _lookup_var(name, env, controlled, None)
    if v is UNKNOWN:
        return {"name": name, "value": None, "runtime": True}
    return {"name": name, "value": v, "runtime": False}


def _eval_rule_condition(rule, ctx):
    """One rule's condition → {'v': tri-state, 'notes': [...], 'vars': [...]}"""
    parts = []
    notes: list[str] = []
    vars_: list[dict] = []
    if rule.get("if"):
        parts.append(eval_expr(rule["if"], ctx["env"], notes, ctx["controlled"]))
        names: dict[str, bool] = {}
        _collect_ast_vars(rule["if"], names)
        for n in sorted(names):
            vars_.append(_display_value(n, ctx["env"], ctx["controlled"]))
    if rule.get("changes"):
        changes = rule["changes"]
        if changes.get("compare_to") or changes.get("regexp"):
            parts.append(None)
            notes.append('changes:' + ('compare_to' if changes.get("compare_to")
                                       else 'regexp')
                         + ' is not resolvable offline')
        elif ctx["changesAlwaysTrue"]:
            parts.append(True)
            notes.append('rules:changes is always true in pipelines with no push event '
                         + '(tag, schedule, manual, api/trigger, first push of a new branch)')
        elif ctx["changedFiles"] == "all":
            parts.append(True)
            notes.append('assuming every changes: pattern matches')
        elif ctx["changedFiles"] is None:
            parts.append(None)
            notes.append('depends on which files changed — fill in the changed-files list')
        else:
            m = _match_changed_files(changes["paths"], ctx["changedFiles"])
            parts.append(m)
            if m is None:
                notes.append('unsupported glob pattern')
    if rule.get("exists"):
        exists = rule["exists"]
        if exists.get("result") is None:
            parts.append(None)
            notes.append(exists.get("reason") or 'exists result unknown')
        else:
            parts.append(exists["result"])
            notes.append('exists checked against the repo at report generation time: '
                         + ('found' if exists["result"] else 'not found'))
    return {"v": tri_and(parts) if parts else True, "notes": notes, "vars": vars_}


def _rule_outcome(rule, defaults, has_rules):
    when = rule.get("when") or defaults.get("when") or "on_success"
    if when == "never":
        out = {"included": False, "state": "skipped", "when": "never"}
    else:
        allow_failure = rule.get("allow_failure")
        if allow_failure is None:
            if defaults.get("allow_failure") is not None:
                # an explicit job-level allow_failure always applies
                allow_failure = defaults["allow_failure"]
            elif when == "manual":
                # GitLab: a manual job defaults to BLOCKING whenever the job
                # uses rules (manual_action? && !has_rules?); only legacy
                # rule-less manual jobs default to optional (allow_failure: true)
                allow_failure = False if has_rules else True
            else:
                allow_failure = False
        out = {
            "included": True,
            "state": "manual" if when == "manual"
                     else "delayed" if when == "delayed" else "runs",
            "when": when,
            "allow_failure": allow_failure,
            "start_in": rule.get("start_in") or defaults.get("start_in") or None,
            "variables": rule.get("variables") or None,
        }
    if rule.get("needs") is not None:
        out["needsOverride"] = rule["needs"]   # rules:needs replaces the job's needs
    return out


def _walk_rules(rules, idx, defaults, ctx, trace):
    """First-match-wins walk producing either a definite outcome or a
    conditional tree when an unknown condition forks the trace."""
    if idx >= len(rules):
        trace.append({"rule": None, "desc": "no rule matched", "verdict": "end"})
        return {"included": False, "state": "not-added", "reason": "no rule matched"}
    rule = rules[idx]
    cond = _eval_rule_condition(rule, ctx)
    desc = _describe_condition(rule)
    if cond["v"] is True:
        out = _rule_outcome(rule, defaults, True)
        trace.append({"rule": idx, "desc": desc, "verdict": "matched",
                      "notes": cond["notes"], "vars": cond["vars"],
                      "when": out.get("when")})
        return out
    if cond["v"] is False:
        trace.append({"rule": idx, "desc": desc, "verdict": "no match",
                      "notes": cond["notes"], "vars": cond["vars"]})
        return _walk_rules(rules, idx + 1, defaults, ctx, trace)
    trace.append({"rule": idx, "desc": desc, "verdict": "unknown",
                  "notes": cond["notes"], "vars": cond["vars"]})
    then_out = _rule_outcome(rule, defaults, True)
    else_out = _walk_rules(rules, idx + 1, defaults, ctx, trace)
    if not then_out["included"] and not else_out["included"] \
            and else_out["state"] != "conditional":
        # the unknown condition cannot change the answer — stay definite
        return {
            "included": False,
            "state": "skipped" if then_out["state"] == "skipped"
                     or else_out["state"] == "skipped" else "not-added",
            "when": then_out.get("when"),
            "collapsed": True,
            "reason": "excluded whichever way the unknown conditions go",
        }
    conditional = {
        "state": "conditional",
        "condition": desc,
        "conditionNotes": cond["notes"],
        "then": then_out,
        "otherwise": else_out,
        "included": then_out["included"] or else_out["included"],
    }
    if "needsOverride" in then_out or "needsOverride" in else_out \
            or else_out.get("needsUncertain"):
        conditional["needsUncertain"] = True
    return conditional


# GitLab matches both the plural keywords and the singular sanitized
# source names; `pipelines`/`pipeline` match multi-project downstreams
# ONLY — parent_pipeline (child pipelines) does not pluralize to it.
_LEGACY_KEYWORDS = {
    "branches": lambda c: c["refType"] == "branch",
    "tags": lambda c: c["refType"] == "tag",
    "merge_requests": lambda c: c["source"] == "merge_request_event",
    "merge_request": lambda c: c["source"] == "merge_request_event",
    "pushes": lambda c: c["source"] == "push",
    "push": lambda c: c["source"] == "push",
    "schedules": lambda c: c["source"] == "schedule",
    "schedule": lambda c: c["source"] == "schedule",
    "triggers": lambda c: c["source"] == "trigger",
    "trigger": lambda c: c["source"] == "trigger",
    "api": lambda c: c["source"] == "api",
    "web": lambda c: c["source"] == "web",
    "pipelines": lambda c: c["source"] == "pipeline",
    "pipeline": lambda c: c["source"] == "pipeline",
    "parent_pipelines": lambda c: c["source"] == "parent_pipeline",
    "parent_pipeline": lambda c: c["source"] == "parent_pipeline",
    "chat": lambda c: c["source"] == "chat",
    "external": lambda c: c["source"] == "external",
    "external_pull_requests": lambda c: c["source"] == "external_pull_request_event",
    "external_pull_request": lambda c: c["source"] == "external_pull_request_event",
}


def _legacy_refs_match(refs, candidate, notes):
    results = []
    for entry in refs:
        if entry in _LEGACY_KEYWORDS:
            results.append(_LEGACY_KEYWORDS[entry](candidate))
        elif "@" in entry:
            results.append(None)
            notes.append('ref@project form "' + entry + '" is not simulated')
        elif candidate["refType"] == "merge_request":
            # ref name/regex patterns match branch and tag names only — never
            # merge request pipelines
            results.append(False)
        else:
            m = _RE_LITERAL.match(entry)
            if m:
                rx = _compile_regex(m.group(1), m.group(2), notes)
                if rx is None:
                    results.append(None)
                    notes.append('invalid ref regex ' + entry)
                else:
                    results.append(rx.search(candidate["ref"]) is not None)
            else:
                results.append(entry == candidate["ref"])
    return tri_or(results)


def _legacy_half(spec, candidate, ctx, notes, is_only):
    # GitLab's documented combination (verified against source): `only`
    # includes when ALL clause kinds match (AND); `except` excludes when ANY
    # clause kind matches (OR). Inside every array it is OR. An absent `only`
    # defaults to branches+tags. (`is None`, not truthiness: JS treats an
    # empty spec object as present, and so must we.)
    if spec is None:
        return _legacy_refs_match(["branches", "tags"], candidate, notes) \
            if is_only else False
    parts = []
    refs = spec.get("refs")
    if is_only and not refs:
        refs = ["branches", "tags"]
    if refs:
        parts.append(_legacy_refs_match(refs, candidate, notes))
    if spec.get("variables"):
        parts.append(tri_or([eval_expr(ast, ctx["env"], notes, ctx["controlled"])
                             for ast in spec["variables"]]))
    if spec.get("changes"):
        # only:changes with a no-push-event ref is documented "always true"
        # (only → job runs; except → job never runs)
        if ctx["changesAlwaysTrue"] or ctx["changedFiles"] == "all":
            parts.append(True)
        elif ctx["changedFiles"] is None:
            parts.append(None)
            notes.append('only/except:changes depends on the changed-files list')
        else:
            parts.append(_match_changed_files(spec["changes"], ctx["changedFiles"]))
    if spec.get("unsupported"):
        parts.append(None)
        notes.append('unsupported only/except keys: ' + ", ".join(spec["unsupported"]))
    if not parts:
        return False
    return tri_and(parts) if is_only else tri_or(parts)


def _eval_legacy(program, defaults, candidate, ctx, trace):
    notes: list[str] = []
    only_v = _legacy_half(program.get("only"), candidate, ctx, notes, True)
    except_v = _legacy_half(program["except"], candidate, ctx, notes, False) \
        if program.get("except") else False
    included = tri_and([only_v, tri_not(except_v)])
    desc = 'implicit default only: [branches, tags] (job has no rules/only/except)' \
        if program.get("implicit_default") else 'only/except'
    if included is True:
        out = _rule_outcome({"when": None}, defaults, False)
        trace.append({"rule": 0, "desc": desc, "verdict": "matched",
                      "notes": notes, "when": out.get("when")})
        return out
    if included is False:
        trace.append({"rule": 0, "desc": desc, "verdict": "no match", "notes": notes})
        return {"included": False, "state": "not-added",
                "reason": desc + " did not match"}
    trace.append({"rule": 0, "desc": desc, "verdict": "unknown", "notes": notes})
    return {
        "state": "conditional", "condition": desc, "conditionNotes": notes,
        "then": _rule_outcome({"when": None}, defaults, False),
        "otherwise": {"included": False, "state": "not-added"},
        "included": True,
    }


def _evaluate_job_program(job_whatif, candidate, ctx):
    defaults = {
        "when": job_whatif.get("when") or "on_success",
        "allow_failure": job_whatif.get("allow_failure"),
        "start_in": job_whatif.get("start_in"),
    }
    trace: list[dict] = []
    program = job_whatif["program"]
    if program["kind"] == "rules":
        outcome = _walk_rules(program["rules"], 0, defaults, ctx, trace)
    elif program["kind"] == "legacy":
        outcome = _eval_legacy(program, defaults, candidate, ctx, trace)
    else:
        trace.append({"rule": None, "desc": program.get("reason") or "rules unknown",
                      "verdict": "unknown"})
        outcome = {
            "state": "conditional",
            "condition": program.get("reason") or "rules not analyzable",
            "then": _rule_outcome({"when": None}, defaults, True),
            "otherwise": {"included": False, "state": "not-added"},
            "included": True,
        }
    outcome["trace"] = trace
    return outcome


_MATRIX_ORDER = ["runs", "delayed", "manual", "conditional", "skipped", "not-added"]


def _evaluate_job_matrix(job_whatif, candidate, ctx):
    # parallel:matrix — GitLab expands instances BEFORE rules evaluate, each
    # instance seeing its own axis variables (at job-variable precedence, so
    # forwarded/override values are re-applied on top)
    par = job_whatif.get("parallel")
    if not par:
        return _evaluate_job_program(job_whatif, candidate, ctx)
    if par["kind"] == "count":
        single = _evaluate_job_program(job_whatif, candidate, ctx)
        single["matrixCount"] = par["count"]
        return single
    reapply = ctx.get("reapply") or {}
    per = []
    for c in par["combos"]:
        sub_ctx = {
            "env": {**ctx["env"], **c["vars"], **reapply},
            "controlled": ctx["controlled"],
            "changedFiles": ctx["changedFiles"],
            "changesAlwaysTrue": ctx["changesAlwaysTrue"],
            "reapply": reapply,
        }
        per.append({"name": c["name"],
                    "outcome": _evaluate_job_program(job_whatif, candidate, sub_ctx)})
    if not per:
        return _evaluate_job_program(job_whatif, candidate, ctx)

    def order(state):
        return _MATRIX_ORDER.index(state) if state in _MATRIX_ORDER \
            else len(_MATRIX_ORDER)

    pick = per[0]
    for p in per:
        if order(p["outcome"]["state"]) < order(pick["outcome"]["state"]):
            pick = p
    base = dict(pick["outcome"])
    base["matrix"] = [{"name": p["name"], "state": p["outcome"]["state"],
                       "when": p["outcome"].get("when")} for p in per]
    base["matrixCount"] = len(per)
    base["matrixPartial"] = any(p["outcome"]["state"] != per[0]["outcome"]["state"]
                                for p in per)
    base["included"] = any(might_run(p["outcome"]) for p in per)
    if any(p["outcome"].get("needsUncertain") for p in per):
        base["needsUncertain"] = True
    return base


def _walk_include_gate(rules, ctx):
    # include:rules — the file's jobs exist only when the include gate lets
    # the file in (no matching rule → the include is NOT processed)
    def walk(idx):
        if idx >= len(rules):
            return False
        cond = _eval_rule_condition(rules[idx], ctx)
        then_v = rules[idx].get("when") != "never"
        if cond["v"] is True:
            return then_v
        if cond["v"] is False:
            return walk(idx + 1)
        rest = walk(idx + 1)
        return rest if rest == then_v else None
    return walk(0)


def _evaluate_job(job_whatif, candidate, ctx):
    gate_rules = job_whatif.get("include_gate")
    if not gate_rules and gate_rules != []:
        return _evaluate_job_matrix(job_whatif, candidate, ctx)
    gv = _walk_include_gate(gate_rules, ctx)
    gdesc = " | ".join(_describe_condition(r) for r in gate_rules)
    if gv is False:
        return {"included": False, "state": "not-added",
                "reason": "its include is not processed for this pipeline",
                "trace": [{"rule": None, "verdict": "no match",
                           "desc": 'from a conditional include gated on: ' + gdesc
                           + ' — the include is not processed, the job does not exist here'}]}
    inner = _evaluate_job_matrix(job_whatif, candidate, ctx)
    if gv is True:
        inner["trace"].insert(0, {"rule": None, "verdict": "matched",
                                  "desc": 'from a conditional include (gate matched): '
                                  + gdesc})
        return inner
    if not inner.get("included") and inner["state"] != "conditional":
        inner["trace"].insert(0, {"rule": None, "verdict": "unknown",
                                  "desc": 'include gate unknown (' + gdesc
                                  + ') — the job is excluded either way'})
        return inner
    wrapped = {
        "state": "conditional",
        "condition": 'include gated on: ' + gdesc,
        "then": inner["then"] if inner["state"] == "conditional" else inner,
        "otherwise": {"included": False, "state": "not-added"},
        "included": inner.get("included"),
        "trace": inner["trace"],
    }
    if inner.get("needsUncertain") or "needsOverride" in inner:
        wrapped["needsUncertain"] = True
    wrapped["trace"].insert(0, {"rule": None, "verdict": "unknown",
                                "desc": 'from a conditional include gated on: ' + gdesc})
    return wrapped


def might_run(outcome) -> bool:
    """The honest might-this-job-run test: conditional counts only when some
    branch of the unknown actually includes the job."""
    if not outcome:
        return False
    if outcome["state"] == "conditional":
        return bool(outcome.get("included"))
    return outcome["state"] in ("runs", "manual", "delayed")


# ---------------- workflow gate ----------------

def _eval_workflow(workflow, ctx):
    if not workflow or not workflow.get("rules"):
        return {"created": True,
                "reason": 'no workflow:rules — every pipeline type is allowed',
                "variables": {}, "trace": []}
    trace: list[dict] = []
    rules = workflow["rules"]

    def walk(idx):
        if idx >= len(rules):
            trace.append({"rule": None, "desc": "no workflow rule matched",
                          "verdict": "end"})
            return {"created": False,
                    "reason": 'no workflow rule matched — the pipeline is not created',
                    "variables": {}}
        rule = rules[idx]
        cond = _eval_rule_condition(rule, ctx)
        desc = _describe_condition(rule)
        if cond["v"] is True:
            never = rule.get("when") == "never"
            trace.append({"rule": idx, "desc": desc, "verdict": "matched",
                          "notes": cond["notes"], "vars": cond["vars"],
                          "when": "never" if never else "always"})
            return {
                "created": not never,
                "reason": 'workflow rule ' + str(idx + 1)
                          + (' says when: never' if never else ' allows it')
                          + ': ' + desc,
                "variables": rule.get("variables") or {},
            }
        if cond["v"] is False:
            trace.append({"rule": idx, "desc": desc, "verdict": "no match",
                          "notes": cond["notes"], "vars": cond["vars"]})
            return walk(idx + 1)
        trace.append({"rule": idx, "desc": desc, "verdict": "unknown",
                      "notes": cond["notes"], "vars": cond["vars"]})
        rest = walk(idx + 1)
        uncertain_vars = list(rule["variables"].keys()) \
            if rule.get("variables") else []
        if rest.get("variablesUncertain"):
            uncertain_vars = uncertain_vars + rest["variablesUncertain"]
        then_created = rule.get("when") != "never"
        if rest["created"] == then_created:
            # the unknown rule cannot change whether the pipeline is created
            collapsed = {
                "created": rest["created"],
                "reason": 'the pipeline is created whichever way the unknown rule goes'
                          if rest["created"] else
                          'the pipeline is not created whichever way the unknown rule goes',
                "variables": rest.get("variables") or {},
            }
            if uncertain_vars:
                collapsed["variablesUncertain"] = uncertain_vars
            return collapsed
        result = {
            "created": None,
            "reason": 'depends on: ' + desc,
            "conditional": {"then": then_created, "otherwise": rest["created"]},
            # an unknown rule's variables are NOT applied as fact — jobs are
            # evaluated with the definite path's variables plus a caveat
            "variables": rest.get("variables") or {},
        }
        if uncertain_vars:
            result["variablesUncertain"] = uncertain_vars
        return result

    result = walk(0)
    result["trace"] = trace
    return result


# ---------------- candidates ----------------

def _build_candidates(config, whatif):
    out = []
    branch = config.get("branch") or whatif["default_branch"]
    scenario = config.get("scenario")
    if scenario == "push_branch":
        out.append({"id": "branch", "source": "push", "refType": "branch",
                    "ref": branch, "label": "Branch pipeline",
                    "noPushEvent": False, "childOf": None})
        if config.get("openMR"):
            out.append({"id": "mr", "source": "merge_request_event",
                        "refType": "merge_request", "ref": branch,
                        "target": config.get("target") or whatif["default_branch"],
                        "label": "Merge request pipeline",
                        "noPushEvent": False, "childOf": None})
    elif scenario == "push_tag":
        out.append({"id": "tag", "source": "push", "refType": "tag",
                    "ref": config.get("tag") or "v1.0.0",
                    "label": "Tag pipeline", "noPushEvent": True, "childOf": None})
    elif scenario == "mr":
        out.append({"id": "mr", "source": "merge_request_event",
                    "refType": "merge_request", "ref": branch,
                    "target": config.get("target") or whatif["default_branch"],
                    "label": "Merge request pipeline",
                    "noPushEvent": False, "childOf": None})
    elif scenario in ("schedule", "web", "api", "trigger"):
        labels = {"schedule": "Scheduled pipeline", "web": "Manual pipeline (web)",
                  "api": "API pipeline", "trigger": "Trigger-token pipeline"}
        if config.get("refKind") == "tag":
            out.append({"id": scenario, "source": scenario, "refType": "tag",
                        "ref": config.get("tag") or "v1.0.0",
                        "label": labels[scenario] + " on a tag",
                        "noPushEvent": True, "childOf": None})
        else:
            out.append({"id": scenario, "source": scenario, "refType": "branch",
                        "ref": branch, "label": labels[scenario],
                        "noPushEvent": True, "childOf": None})
    else:
        out.append({"id": "branch", "source": "push", "refType": "branch",
                    "ref": branch, "label": "Branch pipeline",
                    "noPushEvent": False, "childOf": None})
    return out


# ---------------- job indexing ----------------

def _job_index(report):
    jobs = []
    for n in report.get("nodes") or []:
        w = (n.get("annotations") or {}).get("whatif")
        if n.get("kind") == "job" and w:
            jobs.append({"id": n["id"], "node": n, "whatif": w})
    return jobs


# ---------------- artifact / consumption analysis ----------------

def _stage_idx(stages, stage):
    try:
        return stages.index(stage)
    except ValueError:
        return len(stages)


def _analyze_artifacts(candidate, jobs, results, whatif, report):
    notes: list[dict] = []
    errors: list[dict] = []
    by_id = {j["id"]: j for j in jobs}
    node_ids = {n["id"]: n for n in report.get("nodes") or []}
    unresolved_includes = whatif.get("unresolved_includes") or []
    included = [j for j in jobs if might_run(results[j["id"]])]

    for job in included:
        w = job["whatif"]
        outcome = results[job["id"]]
        dotenv_in: list[str] = []   # producers whose dotenv reaches this job

        def check_target(name, optional, want_artifacts, kind_label,
                         job=job, w=w, outcome=outcome, dotenv_in=dotenv_in):
            # "job: [v1, v2]" addresses one matrix instance by its expanded name
            lookup_name = name
            instance_vals = None
            m_inst = re.match(r"^(.+?):\s*\[(.+)\]$", name)
            if m_inst:
                lookup_name = m_inst.group(1)
                instance_vals = m_inst.group(2)
            target_id = ((w.get("child_of") + "::") if w.get("child_of") else "") \
                + lookup_name
            target = by_id.get(target_id)
            if not target:
                node = node_ids.get(target_id) or node_ids.get(lookup_name)
                if node and node.get("kind") == "job":
                    is_template = lookup_name.startswith(".") \
                        or "template" in (node.get("flags") or [])
                    errors.append({"job": job["id"], "target": name, "kind": kind_label,
                                   "message":
                                   '"' + w["name"] + '" ' + kind_label + ' "' + name
                                   + '", a hidden/template job that is never added to '
                                   + 'pipelines — GitLab fails to create the pipeline'
                                   if is_template else
                                   '"' + w["name"] + '" ' + kind_label + ' "' + name
                                   + '", a job in a different pipeline scope '
                                   + '(parent/child) — needs cannot cross pipelines; '
                                   + 'GitLab fails to create the pipeline'})
                elif unresolved_includes:
                    notes.append({"job": job["id"], "kind": "external",
                                  "message": kind_label + ' "' + name
                                  + '" is not in the local files — probably defined '
                                  + 'in an unresolved include ('
                                  + ", ".join(unresolved_includes)
                                  + '), not simulated'})
                elif not optional:
                    errors.append({"job": job["id"], "target": name, "kind": kind_label,
                                   "message": '"' + w["name"] + '" ' + kind_label
                                   + ' "' + name + '", which is not defined anywhere '
                                   + '— GitLab fails to create the pipeline'})
                return
            st = results[target["id"]]
            if instance_vals is not None:
                tpar = target["whatif"].get("parallel")
                full_name = lookup_name + ": [" + instance_vals + "]"
                inst = next((mm for mm in (st.get("matrix") or [])
                             if mm["name"] == full_name), None)
                if not tpar or tpar.get("kind") != "matrix" \
                        or ((st.get("matrix") or []) and not inst):
                    errors.append({"job": job["id"], "target": target["id"],
                                   "kind": kind_label,
                                   "message": '"' + w["name"] + '" ' + kind_label
                                   + ' "' + name + '", which is not a valid matrix '
                                   + 'instance of "' + lookup_name + '" — GitLab '
                                   + 'fails to create the pipeline'})
                    return
                if inst:
                    st = {"state": inst["state"], "when": inst.get("when")}
                    if inst["state"] == "conditional":
                        st["included"] = True
            if not might_run(st):
                if not optional:
                    errors.append({"job": job["id"], "target": target["id"],
                                   "kind": kind_label,
                                   "message": '"' + w["name"] + '" ' + kind_label
                                   + ' "' + name + '", but "' + name + '" is not in '
                                   + 'this pipeline (' + st["state"] + ') — GitLab '
                                   + 'would probably fail to create the pipeline'})
                return
            if kind_label == "depends on" \
                    and _stage_idx(whatif["stages"], target["whatif"]["stage"]) \
                    >= _stage_idx(whatif["stages"], w["stage"]):
                errors.append({"job": job["id"], "target": target["id"],
                               "kind": kind_label,
                               "message": '"' + w["name"] + '" lists "' + name
                               + '" in dependencies, but it is not in an earlier '
                               + 'stage — GitLab rejects this'})
                return
            if st["state"] == "conditional" and not optional \
                    and outcome["state"] != "conditional":
                notes.append({"job": job["id"], "kind": "conditional-need",
                              "message": '"' + w["name"] + '" ' + kind_label + ' "'
                              + name + '", whose inclusion is conditional — if it '
                              + 'is dropped, pipeline creation probably fails'})
            if st["state"] == "manual":
                notes.append({"job": job["id"], "kind": "manual-producer",
                              "message": '"' + w["name"] + '" consumes artifacts '
                              + 'from "' + name + '", a manual job — the artifacts '
                              + 'exist only after its gate is run'})
            if st.get("when") == "on_failure":
                notes.append({"job": job["id"], "kind": "on-failure-chain",
                              "message": '"' + w["name"] + '" needs "' + name
                              + '", which runs only after an earlier failure — "'
                              + w["name"] + '" is effectively skipped in green '
                              + 'pipelines'})
            if want_artifacts and target["whatif"]["artifacts"].get("when") == "on_failure":
                notes.append({"job": job["id"], "kind": "artifacts-when",
                              "message": '"' + w["name"] + '" consumes artifacts '
                              + 'from "' + name + '", which uploads them only when '
                              + 'it FAILS (artifacts:when: on_failure) — on success '
                              + 'there is nothing to download'})
            if want_artifacts and target["whatif"]["artifacts"].get("dotenv"):
                dotenv_in.append(target["id"])

        effective_needs = w.get("needs")
        if outcome.get("needsUncertain"):
            notes.append({"job": job["id"], "kind": "needs-uncertain",
                          "message": '"' + w["name"] + '" has rules:needs on a rule '
                          + 'whose match is uncertain — artifact flow is not '
                          + 'analyzed for it'})
            effective_needs = None
        elif "needsOverride" in outcome:
            effective_needs = outcome["needsOverride"]   # rules:needs replaced them

        if effective_needs is not None:
            for need in effective_needs:
                if need.get("kind") in ("cross_pipeline", "cross_project"):
                    notes.append({"job": job["id"], "kind": "cross-pipeline",
                                  "message": '"' + w["name"] + '" fetches artifacts '
                                  + 'from another '
                                  + ('pipeline' if need["kind"] == "cross_pipeline"
                                     else 'project')
                                  + ' (' + need["ref"] + ') by ref — when duplicate '
                                  + 'pipelines run on the same ref, which artifacts '
                                  + 'it gets is ambiguous'})
                    continue
                check_target(need["job"], need.get("optional"),
                             need.get("artifacts"), "needs")
        elif outcome.get("needsUncertain"):
            pass  # skipped above
        elif w.get("dependencies") is not None:
            for dep in w["dependencies"]:
                check_target(dep, False, True, "depends on")
        else:
            # GitLab default: artifacts from every included earlier-stage job
            my_stage = _stage_idx(whatif["stages"], w["stage"])
            for other in included:
                if other["id"] == job["id"]:
                    continue
                if _stage_idx(whatif["stages"], other["whatif"]["stage"]) < my_stage \
                        and other["whatif"]["artifacts"].get("dotenv"):
                    dotenv_in.append(other["id"])

        for producer in dotenv_in:
            notes.append({"job": job["id"], "kind": "dotenv", "producer": producer,
                          "message": '"' + w["name"] + '" runtime env is extended by '
                          + 'variables from "' + by_id[producer]["whatif"]["name"]
                          + '"’s dotenv report — dotenv variables can never affect '
                          + 'rules (rules evaluate before any job runs)'})

    producers = [j["id"] for j in included
                 if j["whatif"]["artifacts"].get("paths")
                 or j["whatif"]["artifacts"].get("dotenv")]
    return {"notes": notes, "errors": errors, "producers": producers}


# ---------------- event evaluation (entry point) ----------------

_MAX_CHILD_DEPTH = 3


def _evaluate_candidate(candidate, all_jobs, config, whatif, report):
    env = _build_env(candidate, config, whatif)
    controlled = _controlled_for(env)
    overrides = config.get("overrides") or {}
    # yaml defaults visible in this candidate: the child file's own globals
    # for a child pipeline, the main pipeline's globals otherwise
    yaml_view = (whatif.get("child_globals") or {}).get(candidate["childOf"]) or {} \
        if candidate.get("childOf") else (whatif.get("globals") or {})
    # trigger:forward-ed variables arrive at pipeline-variable precedence
    # (above the child's own yaml values)
    pipeline_view = candidate.get("forwardedVars") or {}
    changes_always_true = candidate["noPushEvent"] \
        or bool(config.get("newBranch") and candidate["source"] == "push"
                and candidate["refType"] == "branch")

    ctx0 = {"env": {**env, **yaml_view, **pipeline_view, **overrides},
            "controlled": controlled,
            "changedFiles": config.get("changedFiles"),
            "changesAlwaysTrue": changes_always_true}
    # the child's own workflow gates the child (never the parent's)
    wf = (whatif.get("child_workflows") or {}).get(candidate["childOf"]) \
        if candidate.get("childOf") else whatif.get("workflow")
    gate = _eval_workflow(wf, ctx0)

    jobs = [j for j in all_jobs
            if (j["whatif"].get("child_of") or None) == (candidate.get("childOf") or None)]
    results: dict[str, dict] = {}
    for job in jobs:
        # inherit:variables filters the yaml defaults BEFORE rules evaluate
        inh = job["whatif"].get("inherit_variables")
        globals_part = yaml_view
        if inh is False:
            globals_part = {}
        elif isinstance(inh, list):
            globals_part = {n: yaml_view[n] for n in inh if n in yaml_view}
        job_env = {**env, **globals_part, **(gate.get("variables") or {}),
                   **(job["whatif"].get("variables") or {}), **pipeline_view,
                   **overrides}
        ctx = {"env": job_env, "controlled": controlled,
               "changedFiles": config.get("changedFiles"),
               "changesAlwaysTrue": changes_always_true,
               # matrix axis vars slot in at job-variable precedence;
               # these layers re-apply on top of them
               "reapply": {**pipeline_view, **overrides}}
        results[job["id"]] = _evaluate_job(job["whatif"], candidate, ctx)

    included_jobs = [j for j in jobs if might_run(results[j["id"]])]
    # .pre/.post alone are not a pipeline (verified GitLab behavior)
    real_jobs = [j for j in included_jobs
                 if j["whatif"]["stage"] not in (".pre", ".post")]
    created = gate["created"]
    reason = gate["reason"]
    if created is not False and not real_jobs:
        created = False
        reason = ('only .pre/.post jobs were added — GitLab does not consider that '
                  'a complete pipeline and does not create it') if included_jobs \
            else 'no jobs were added — GitLab does not create an empty pipeline'

    artifacts = _analyze_artifacts(candidate, jobs, results, whatif, report)
    # a started environment whose on_stop job is excluded here can never be
    # auto-stopped — the docs tell users to keep both jobs' rules in sync
    for job in jobs:
        envc = job["whatif"].get("environment")
        if not envc or not envc.get("on_stop"):
            continue
        if envc.get("action") and envc["action"] != "start":
            continue
        if not might_run(results[job["id"]]):
            continue
        stop_id = ((job["whatif"].get("child_of") + "::")
                   if job["whatif"].get("child_of") else "") + envc["on_stop"]
        stop = results.get(stop_id)
        if stop and not might_run(stop):
            artifacts["notes"].append({
                "job": job["id"], "kind": "env-stop",
                "message": '"' + job["whatif"]["name"] + '" starts environment "'
                + envc["name"] + '" but its on_stop job "' + envc["on_stop"]
                + '" is not in this pipeline (' + stop["state"] + ') — the '
                + 'environment can never be auto-stopped; keep both jobs’ rules '
                + 'in sync'})
    if gate.get("variablesUncertain"):
        artifacts["notes"].insert(0, {
            "job": None, "kind": "workflow-vars",
            "message": 'workflow variables ' + ", ".join(gate["variablesUncertain"])
            + ' come from a rule whose match is uncertain — they were NOT applied '
            + 'to this evaluation'})

    # child pipelines spawned by included trigger jobs (recursive)
    children: list[dict] = []
    lineage = candidate.get("lineage") or []
    for job in jobs:
        trig = job["whatif"].get("trigger")
        if not trig or not might_run(results[job["id"]]) or created is False:
            continue
        for child_rel in trig.get("children") or []:
            if child_rel == (candidate.get("childOf") or None) or child_rel in lineage:
                artifacts["notes"].append({
                    "job": job["id"], "kind": "downstream",
                    "message": '"' + job["whatif"]["name"] + '" re-triggers '
                    + child_rel + ' which is already in this chain — cycle not '
                    + 'expanded'})
                continue
            if len(lineage) >= _MAX_CHILD_DEPTH:
                artifacts["notes"].append({
                    "job": job["id"], "kind": "downstream",
                    "message": 'child pipelines nested deeper than '
                    + str(_MAX_CHILD_DEPTH) + ' levels are not expanded'})
                continue
            # trigger:forward — by default the trigger job's yaml variables
            # (over the parent's yaml defaults) are forwarded to the child
            fwd = trig.get("forward") or {"yaml_variables": True}
            forwarded_vars = {} if fwd.get("yaml_variables") is False \
                else {**yaml_view, **(job["whatif"].get("variables") or {})}
            child_candidate = {
                "id": candidate["id"] + ">" + job["id"] + ">" + child_rel,
                "source": "parent_pipeline",
                "refType": candidate["refType"],
                "ref": candidate["ref"],
                "target": candidate.get("target"),
                "label": "Child pipeline: " + child_rel,
                "noPushEvent": True,
                "childOf": child_rel,
                "parentJob": job["id"],
                "parentConditional": results[job["id"]]["state"] == "conditional",
                "forwardedVars": forwarded_vars,
                "lineage": lineage + [candidate.get("childOf") or "(root)"],
            }
            child_result = _evaluate_candidate(child_candidate, all_jobs, config,
                                               whatif, report)
            children.append(child_result)
            if child_result["created"] is False:
                artifacts["errors"].append({
                    "job": job["id"], "target": child_rel, "kind": "trigger",
                    "message": '"' + job["whatif"]["name"] + '"’s child pipeline '
                    + child_rel + ' would have no jobs (' + child_result["reason"]
                    + ') — GitLab fails the trigger job: "downstream pipeline can '
                    + 'not be created, the resulting pipeline would have been empty"'})
        for ref in trig.get("unresolved") or []:
            artifacts["notes"].append({
                "job": job["id"], "kind": "downstream",
                "message": '"' + job["whatif"]["name"] + '" triggers a child '
                + 'pipeline whose config is not available offline (' + ref
                + ') — not simulated'})
        if trig.get("project"):
            artifacts["notes"].append({
                "job": job["id"], "kind": "downstream",
                "message": '"' + job["whatif"]["name"] + '" triggers a pipeline in '
                + 'project "' + trig["project"] + '" — its config is not available '
                + 'offline'})

    # needs/dependencies problems make GitLab refuse to create THIS pipeline
    # (trigger-kind errors fail only the trigger job, not the pipeline)
    creation_fails = created is not False \
        and any(e["kind"] != "trigger" for e in artifacts["errors"])

    return {
        "id": candidate["id"], "label": candidate["label"],
        "source": candidate["source"], "ref": candidate["ref"],
        "refType": candidate["refType"], "target": candidate.get("target"),
        "childOf": candidate.get("childOf"),
        "parentJob": candidate.get("parentJob"),
        "parentConditional": candidate.get("parentConditional") or False,
        "created": created, "reason": reason, "creationFails": creation_fails,
        "workflowTrace": gate.get("trace") or [],
        "workflowVariables": gate.get("variables") or {},
        "forwardedVars": pipeline_view,
        "env": env, "controlled": controlled,
        "jobs": results, "jobOrder": [j["id"] for j in jobs],
        "artifacts": artifacts, "children": children,
    }


def evaluate_event(report: dict, config: dict) -> dict | None:
    """Evaluate one What-If configuration against a report dict
    (Report.to_dict() shape). Returns the same structure whatif.js's
    evaluateEvent produces, or None when the report has no what-if program."""
    whatif = (report.get("annotations") or {}).get("whatif")
    if not whatif:
        return None
    version = whatif.get("version")
    if version != WHATIF_VERSION:
        raise WhatifVersionError(
            f"report carries what-if program version {version!r}; this evaluator "
            f"speaks version {WHATIF_VERSION} — regenerate the report with this "
            f"pipeview")
    all_jobs = _job_index(report)
    candidates = [_evaluate_candidate(c, all_jobs, config, whatif, report)
                  for c in _build_candidates(config, whatif)]

    # duplicates: a job that might run in >= 2 top-level candidates
    duplicates: list[dict] = []
    if len(candidates) > 1:
        seen: dict[str, list[str]] = {}
        for c in candidates:
            if c["created"] is False or c["creationFails"]:
                continue
            for job_id in c["jobOrder"]:
                if might_run(c["jobs"][job_id]):
                    seen.setdefault(job_id, []).append(c["id"])
        job_by_id = {j["id"]: j for j in all_jobs}
        for job_id, cand_ids in seen.items():
            if len(cand_ids) > 1:
                entry: dict[str, Any] = {"job": job_id, "candidates": cand_ids}
                c0 = next((c for c in candidates if job_id in c["jobs"]), None)
                if c0 and c0["jobs"][job_id].get("matrixCount"):
                    entry["instances"] = c0["jobs"][job_id]["matrixCount"]
                # a shared resource_group serializes the duplicated runs
                if job_by_id.get(job_id, {}).get("whatif", {}).get("resource_group"):
                    entry["resource_group"] = \
                        job_by_id[job_id]["whatif"]["resource_group"]
                duplicates.append(entry)

    producing = [c for c in candidates
                 if c["created"] is not False and c["artifacts"]["producers"]]

    return {
        "candidates": candidates,
        "duplicates": duplicates,
        "crossPipelineArtifacts": len(producing) >= 2,
        "lint": whatif.get("lint") or [],
        "fatal": whatif.get("fatal") or [],
    }
