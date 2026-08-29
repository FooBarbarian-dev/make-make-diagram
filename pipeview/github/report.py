"""Fetched GitHub workflows → the ordinary offline pipeline.

Mirrors ``pipeview.gitlab.report``: fetch, materialize under
``<outdir>/fetched/<owner-repo>@<ref>/``, parse with the offline GitHub
parser (cross-repo reusable calls resolved through the materialized
files), merge the fetch notes into the report diagnostics, write the
requested formats. Network access ends before the parse step.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timezone

import pipeview
from pipeview.github.api import GitHubClient
from pipeview.github.fetch import fetch_config, make_github_resolver
from pipeview.gitlab.fetch import FetchResult, materialize, slugify
from pipeview.model import Diagnostic, Report
from pipeview.parsers.github_parser import parse_github
from pipeview.render.exports import (
    export_dot,
    export_json,
    export_mermaid,
    export_svg,
)
from pipeview.render.html import render_html

log = logging.getLogger(__name__)

DEFAULT_FORMATS = ("html", "json")


def report_slug(full_name: str, ref: str) -> str:
    return f"{slugify(full_name)}@{slugify(ref)}"


def generate_report(
    client: GitHubClient,
    full_name: str,
    *,
    ref: str | None = None,
    outdir: str = "./pipeview-out",
    formats=DEFAULT_FORMATS,
    strategy: str = "files",   # accepted for TUI compat; only one exists
    bundled_templates: bool = True,   # accepted for compat; not used
    trigger_docs: dict | None = None,
) -> tuple[Report, list[str]]:
    log.info("Generating report for %s (ref=%s, outdir=%s)",
             full_name, ref, outdir)
    repo = client.get_repo(full_name)
    ref = ref or repo.get("default_branch") or "main"
    result = fetch_config(client, repo, ref)

    slug = report_slug(full_name, ref)
    outdir = os.path.abspath(outdir)
    workdir = os.path.join(outdir, "fetched", slug)

    _, _, _ = materialize(result, workdir)
    resolver = make_github_resolver(workdir, result.external_map) \
        if result.external_map else None

    report = parse_github(
        os.path.join(workdir, ".github", "workflows"),
        repo_root=workdir,
        external_resolver=resolver,
    )
    _annotate(report, result)
    counts = Counter(d.severity for d in report.diagnostics)
    log.info("Parsed: %d node(s), %d edge(s), %d file(s); diagnostics: %s",
             len(report.nodes), len(report.edges), len(report.files),
             dict(counts) or "none")
    for d in report.diagnostics:
        log.debug("diagnostic [%s] %s", d.severity, d.message)
    report.generated_at = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    report.tool_version = pipeview.__version__

    written: list[str] = []
    if "html" in formats:
        path = os.path.join(outdir, f"{slug}.report.html")
        render_html(report, path)
        written.append(path)
    if "json" in formats:
        path = os.path.join(outdir, f"{slug}.model.json")
        export_json(report, path)
        written.append(path)
    if "dot" in formats:
        path = os.path.join(outdir, f"{slug}.graph.dot")
        export_dot(report, path)
        written.append(path)
    if "mmd" in formats:
        path = os.path.join(outdir, f"{slug}.graph.mmd")
        export_mermaid(report, path)
        written.append(path)
    if "svg" in formats:
        path = os.path.join(outdir, f"{slug}.graph.svg")
        export_svg(report, path)
        written.append(path)

    # Trigger docs are additive: problems there never block the report.
    if trigger_docs and trigger_docs.get("scenarios"):
        from pipeview.render.trigger_docs import (
            generate_trigger_docs,
            write_docs_folder,
        )
        provenance = {"project": full_name, "ref": ref, "commit": "",
                      "version": pipeview.__version__}
        docs = generate_trigger_docs(
            report.to_dict(), trigger_docs["scenarios"],
            trigger_docs.get("skipped") or [], provenance,
            trigger_docs.get("cmd") or "pipeview github sync")
        if docs is None:
            report.diagnostics.append(Diagnostic(
                severity="info",
                message="No what-if program in this configuration — "
                        "trigger docs skipped"))
        else:
            docdir = os.path.join(outdir, f"{slug}.trigger-docs")
            report.diagnostics.extend(write_docs_folder(docdir, docs))
            written.append(docdir)

    for path in written:
        log.info("Wrote %s", path)
    return report, written


def _annotate(report: Report, result: FetchResult) -> None:
    """Merge fetch provenance and notes into the parsed report."""
    repo = result.project
    report.annotations["github_remote"] = {
        "host": result.host,
        "project": repo.get("full_name")
        or repo.get("path_with_namespace") or "",
        "project_name": repo.get("name") or "",
        "web_url": repo.get("html_url") or repo.get("web_url") or "",
        "ref": result.ref,
        "strategy": result.strategy,
        "lint_valid": None,
        "includes": [],
        "include_projects": _include_projects(result),
    }
    for severity, message in result.notes:
        report.diagnostics.append(
            Diagnostic(severity=severity, message=message))
    report.diagnostics.append(Diagnostic(
        severity="info",
        message=(
            f"Fetched from "
            f"{report.annotations['github_remote']['project']}@{result.ref}"
            " via the GitHub contents API; cross-repository reusable "
            "workflows are materialized under `_external/`"
        ),
    ))


def _include_projects(result: FetchResult) -> list[dict]:
    """Typed cross-repo references for the rollup, from the external_map
    keys (shape: uses:owner/repo/path@ref)."""
    out = []
    seen = set()
    for key in result.external_map:
        if not key.startswith("uses:"):
            continue
        ref_str = key[len("uses:"):]
        try:
            owner, repo_name, rest = ref_str.split("/", 2)
            path, at_ref = rest.rsplit("@", 1)
        except ValueError:
            continue
        ident = (f"{owner}/{repo_name}".lower(), at_ref, path)
        if ident in seen:
            continue
        seen.add(ident)
        out.append({"project": f"{owner}/{repo_name}", "ref": at_ref,
                    "file": path})
    return sorted(out, key=lambda e: (e["project"], e["file"]))
