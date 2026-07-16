from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

from pipeview.model import Diagnostic, Report, SourceLocation


_VAR_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*(?::=|=)\s*(.*)")
_DEFAULT_GOAL_RE = re.compile(r"^\.DEFAULT_GOAL\s*:=\s*(.*)")

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
            timeout=TIMEOUT_SECONDS,
            env={**os.environ, "LANG": "C"},
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


def _parse_database(output: str, report: Report) -> None:
    var_map = {v.name: v for v in report.variables}
    resolved_vars: dict[str, str] = {}

    in_variables_section = False
    for line in output.splitlines():
        if line.startswith("# Variables"):
            in_variables_section = True
            continue
        if line.startswith("# Files") or line.startswith("# Directories"):
            in_variables_section = False
            continue

        if in_variables_section:
            if line.startswith("#") or not line.strip():
                continue
            m = _VAR_LINE_RE.match(line)
            if m:
                name = m.group(1)
                value = m.group(2).strip()
                resolved_vars[name] = value

        dgm = _DEFAULT_GOAL_RE.match(line)
        if dgm:
            goal = dgm.group(1).strip()
            if goal and report.default_goal is None:
                report.default_goal = goal

    for vname, resolved in resolved_vars.items():
        if vname in var_map:
            var = var_map[vname]
            if var.events:
                var.events[-1].resolved_value = resolved
