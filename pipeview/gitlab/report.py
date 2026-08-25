"""FetchResult -> materialized workdir -> offline parser -> report files.

Network access ends before this module's parse step: everything GitLab
served is on disk first, then the ordinary offline pipeline runs, so the
generated reports keep the offline guarantee.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timezone

import pipeview
from pipeview.gitlab.fetch import FetchResult, fetch_config, materialize, slugify
from pipeview.model import Diagnostic, Report, SourceFile
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.render.exports import export_dot, export_json, export_mermaid, export_svg
from pipeview.render.html import render_html

log = logging.getLogger(__name__)

DEFAULT_FORMATS = ("html", "json")


def report_slug(project_path: str, ref: str) -> str:
    return f"{slugify(project_path)}@{slugify(ref)}"


def generate_report(
    client,
    project_path: str,
    *,
    ref: str | None = None,
    outdir: str = "./pipeview-out",
    formats=DEFAULT_FORMATS,
    strategy: str = "auto",
) -> tuple[Report, list[str]]:
    """Fetch, parse, and render one project's pipeline report.

    Returns (report, written_paths). The fetched files stay under
    `<outdir>/fetched/<slug>/` for inspection and re-runs.
    """
    log.info("Generating report for %s (ref=%s, strategy=%s, outdir=%s)",
             project_path, ref or "<default branch>", strategy, outdir)
    project = client.get_project(project_path)
    ref = ref or project.get("default_branch") or "main"
    result = fetch_config(client, project, ref, strategy=strategy)

    proj_full = project.get("path_with_namespace") or str(project_path)
    slug = report_slug(proj_full, ref)
    outdir = os.path.abspath(outdir)
    workdir = os.path.join(outdir, "fetched", slug)

    root_abs, resolver, local_roots = materialize(result, workdir)
    log.info("Parsing %s", root_abs)
    report = parse_gitlab(
        root_abs,
        repo_root=workdir,
        external_resolver=resolver,
        local_roots=local_roots,
    )
    _annotate(report, result)
    counts = Counter(d.severity for d in report.diagnostics)
    log.info("Parsed: %d node(s), %d edge(s), %d file(s); diagnostics: %s",
             len(report.nodes), len(report.edges), len(report.files),
             dict(counts) or "none")
    for d in report.diagnostics:
        loc = f" ({d.source.file}:{d.source.line})" if d.source else ""
        log.debug("diagnostic [%s] %s%s", d.severity, d.message, loc)
    report.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report.tool_version = pipeview.__version__

    os.makedirs(outdir, exist_ok=True)
    fmts = set(formats)
    written: list[str] = []
    if "html" in fmts:
        p = os.path.join(outdir, f"{slug}.report.html")
        render_html(report, p)
        written.append(p)
    if "json" in fmts:
        p = os.path.join(outdir, f"{slug}.model.json")
        export_json(report, p)
        written.append(p)
    if "dot" in fmts:
        p = os.path.join(outdir, f"{slug}.graph.dot")
        export_dot(report, p)
        written.append(p)
    if "mmd" in fmts:
        p = os.path.join(outdir, f"{slug}.graph.mmd")
        export_mermaid(report, p)
        written.append(p)
    if "svg" in fmts:
        p = os.path.join(outdir, f"{slug}.graph.svg")
        export_svg(report, p)
        written.append(p)
    for p in written:
        log.info("Wrote %s", p)
    return report, written


def _annotate(report: Report, result: FetchResult) -> None:
    project = result.project
    lint = result.lint or {}

    report.annotations["gitlab_remote"] = {
        "host": result.host,
        "project": project.get("path_with_namespace"),
        "project_name": project.get("name"),
        "web_url": project.get("web_url"),
        "ref": result.ref,
        "strategy": result.strategy,
        "lint_valid": lint.get("valid"),
        "includes": lint.get("includes") or [],
    }

    for severity, message in result.notes:
        report.diagnostics.append(Diagnostic(severity=severity, message=message))

    proj_at_ref = f"{project.get('path_with_namespace')}@{result.ref}"

    if result.strategy == "lint":
        report.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Configuration fetched from GitLab ({proj_at_ref}) via the "
                "CI Lint API — the report shows GitLab's own merged view with "
                "every include expanded; line numbers refer to the merged file"
            ),
        ))
        # File Map provenance: the merged file is the only real file on
        # disk, but the lint response says which files went into it.
        existing = {f.path for f in report.files}
        for entry in _lint_include_displays(lint):
            if entry not in existing:
                report.files.append(
                    SourceFile(path=entry, kind="gitlab_yaml", status="ok")
                )
                existing.add(entry)
    else:
        report.diagnostics.append(Diagnostic(
            severity="info",
            message=(
                f"Configuration fetched from GitLab ({proj_at_ref}) file by "
                "file; cross-repository includes are materialized under "
                "_external/"
            ),
        ))

    if lint:
        if lint.get("valid") is True:
            report.diagnostics.append(Diagnostic(
                severity="info",
                message="GitLab CI Lint: configuration is valid",
            ))
        elif lint.get("valid") is False:
            report.diagnostics.append(Diagnostic(
                severity="error",
                message="GitLab CI Lint: configuration is INVALID "
                        "(GitLab would refuse to create pipelines)",
            ))
        for err in lint.get("errors") or []:
            report.diagnostics.append(Diagnostic(
                severity="error", message=f"GitLab CI Lint: {err}",
            ))
        for warn in lint.get("warnings") or []:
            report.diagnostics.append(Diagnostic(
                severity="warning", message=f"GitLab CI Lint: {warn}",
            ))


def _lint_include_displays(lint: dict) -> list[str]:
    """Human-readable File Map entries from the lint response's `includes`
    provenance array (shape varies a little across versions — be tolerant)."""
    out: list[str] = []
    for entry in lint.get("includes") or []:
        if not isinstance(entry, dict):
            continue
        typ = entry.get("type") or "include"
        location = entry.get("location") or "?"
        extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
        ctx = ""
        proj = extra.get("project")
        ref = extra.get("ref")
        if proj:
            ctx = f"{proj}@{ref}" if ref else str(proj)
        if ctx:
            out.append(f"[{typ}:{ctx}] {location}")
        else:
            out.append(f"[{typ}] {location}")
    return out
