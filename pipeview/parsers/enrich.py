from __future__ import annotations

import os
import re
import shutil
import subprocess

from pipeview.model import Diagnostic, Report

_VAR_LINE_RE = re.compile(r"^([^\s:#=]+)\s*(?::{1,3}=|\?=|\+=|!=|=)\s*(.*)")
_DEFAULT_GOAL_RE = re.compile(r"^\.DEFAULT_GOAL\s*:?=\s*(.*)")

# make -p precedes each variable in its database with an origin comment:
#   # makefile (from 'Makefile', line 3)   /  # environment  /  # default
#   # automatic  /  # command line  /  # 'override' directive
_ORIGIN_MAP = [
    ("# makefile", "makefile"),
    ("# environment", "environment"),
    ("# command line", "command line"),
    ("# automatic", "automatic"),
    ("# default", "default"),
    ("# 'override' directive", "override"),
]

TIMEOUT_SECONDS = 30


def enrich_make_report(report: Report, makefile_path: str) -> None:
    make_bin = shutil.which("make")
    if make_bin is None:
        report.diagnostics.append(
            Diagnostic(
                severity="info",
                message="Enrichment skipped: 'make' not found on PATH",
            )
        )
        return

    abs_path = os.path.abspath(makefile_path)
    work_dir = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)

    try:
        result = subprocess.run(
            [make_bin, "-pqn", "-f", filename],
            cwd=work_dir,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            # The parser matches make's English database comments
            # ("# Variables", "# makefile", …); LC_ALL outranks LANG and
            # LC_MESSAGES, and LANGUAGE outranks both for gettext, so all
            # three must be pinned or a localized desktop gets translated
            # output and silently no enrichment.
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"},
        )
        output = result.stdout
    except subprocess.TimeoutExpired:
        report.diagnostics.append(
            Diagnostic(
                severity="info",
                message=f"Enrichment skipped: 'make -pqn' timed out after {TIMEOUT_SECONDS}s",
            )
        )
        return
    except OSError as e:
        report.diagnostics.append(
            Diagnostic(
                severity="info",
                message=f"Enrichment skipped: error running make: {e}",
            )
        )
        return

    if not output:
        report.diagnostics.append(
            Diagnostic(severity="info", message="Enrichment: make -pqn produced no output")
        )
        return

    _parse_database(output, report)


def _origin_for(comment: str) -> str | None:
    for prefix, origin in _ORIGIN_MAP:
        if comment.startswith(prefix):
            return origin
    return None


def _parse_database(output: str, report: Report) -> None:
    var_map = {v.name: v for v in report.variables}
    resolved_vars: dict[str, str] = {}
    origins: dict[str, str] = {}

    in_variables_section = False
    last_origin: str | None = None
    for line in output.splitlines():
        if line.startswith("# Variables"):
            in_variables_section = True
            continue
        if line.startswith("# Files") or line.startswith("# Directories"):
            in_variables_section = False
            continue

        if in_variables_section:
            if line.startswith("#"):
                origin = _origin_for(line)
                if origin:
                    last_origin = origin
                continue
            if not line.strip():
                last_origin = None
                continue
            m = _VAR_LINE_RE.match(line)
            if m:
                name = m.group(1)
                resolved_vars[name] = m.group(2).strip()
                if last_origin:
                    origins[name] = last_origin
                last_origin = None

        dgm = _DEFAULT_GOAL_RE.match(line)
        if dgm:
            goal = dgm.group(1).strip()
            if goal and report.default_goal is None:
                report.default_goal = goal

    for vname, resolved in resolved_vars.items():
        var = var_map.get(vname)
        if var is None:
            continue
        if var.events:
            var.events[-1].resolved_value = resolved
        origin = origins.get(vname)
        if origin:
            # Ground truth beats the static guess ("built-in default" → the
            # database's own word for it).
            var.origin = origin
