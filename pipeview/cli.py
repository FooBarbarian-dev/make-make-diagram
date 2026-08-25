from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import pipeview
from pipeview.model import Report
from pipeview.parsers.enrich import enrich_make_report
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.make_parser import parse_makefile
from pipeview.render.exports import export_dot, export_json, export_mermaid, export_svg
from pipeview.render.html import render_html

_MAKEFILE_NAMES = {"Makefile", "makefile", "GNUmakefile"}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # `pipeview gitlab …` routes to the remote subcommand — the only part of
    # pipeview that performs network access. A local directory literally
    # named "gitlab" is still reachable as `pipeview ./gitlab`.
    if argv and argv[0] == "gitlab":
        from pipeview.gitlab.cli import main as gitlab_main
        return gitlab_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="pipeview",
        description=(
            "Generate offline interactive HTML reports "
            "for GNU Make and GitLab CI pipelines."
        ),
        epilog=(
            "examples:\n"
            "  pipeview Makefile                  Analyze a Makefile\n"
            "  pipeview .                         Discover Makefile and .gitlab-ci.yml in cwd\n"
            "  pipeview .gitlab-ci.yml -o report  Analyze GitLab CI, output to report/\n"
            "  pipeview src/ --no-enrich          Skip make -pqn enrichment pass\n"
            "  pipeview Makefile --format html,svg Export as HTML and SVG\n"
            "  pipeview gitlab                    Browse a GitLab instance (see\n"
            "                                     pipeview gitlab --help)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        help="File (Makefile, *.mk, *.yml) or directory to analyze",
    )
    parser.add_argument(
        "-o", "--output",
        default="./pipeview-out",
        help="Output directory (default: ./pipeview-out)",
    )
    parser.add_argument(
        "--format",
        default="html,json",
        help="Comma-separated output formats: html, json, svg, dot, mmd (default: html,json)",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help=(
            "Skip the Make enrichment pass "
            "(which runs 'make -pqn' and may execute shell snippets)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pipeview {pipeview.__version__}",
    )

    args = parser.parse_args(argv)
    formats = {f.strip() for f in args.format.split(",")}
    path = os.path.abspath(args.path)
    outdir = os.path.abspath(args.output)

    roots = _discover_roots(path)
    if not roots:
        print(f"Error: No analyzable files found at {args.path}", file=sys.stderr)
        return 2

    os.makedirs(outdir, exist_ok=True)

    any_diagnostics = False
    max_severity = None

    for root_path, root_kind in roots:
        report = _parse_root(root_path, root_kind)
        report.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report.tool_version = pipeview.__version__

        if root_kind == "makefile" and not args.no_enrich:
            enrich_make_report(report, root_path)

        basename = _output_basename(root_path, root_kind)

        if "html" in formats:
            render_html(report, os.path.join(outdir, f"{basename}.report.html"))
        if "json" in formats:
            export_json(report, os.path.join(outdir, f"{basename}.model.json"))
        if "dot" in formats:
            export_dot(report, os.path.join(outdir, f"{basename}.graph.dot"))
        if "mmd" in formats:
            export_mermaid(report, os.path.join(outdir, f"{basename}.graph.mmd"))
        if "svg" in formats:
            export_svg(report, os.path.join(outdir, f"{basename}.graph.svg"))

        if report.diagnostics:
            any_diagnostics = True
            sev = report.max_severity()
            if sev:
                sev_order = {"info": 0, "warning": 1, "error": 2}
                if max_severity is None or sev_order.get(sev, 0) > sev_order.get(max_severity, 0):
                    max_severity = sev

            _print_diagnostics_summary(report, root_path)

        print(f"Report generated: {outdir}/{basename}.*")

    if any_diagnostics and max_severity in ("warning", "error"):
        return 1
    return 0


def _discover_roots(path: str) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []

    if os.path.isfile(path):
        kind = _classify_file(path)
        if kind:
            roots.append((path, kind))
        return roots

    if os.path.isdir(path):
        for name in _MAKEFILE_NAMES:
            p = os.path.join(path, name)
            if os.path.isfile(p):
                roots.append((p, "makefile"))
                break

        gitlab_path = os.path.join(path, ".gitlab-ci.yml")
        if os.path.isfile(gitlab_path):
            roots.append((gitlab_path, "gitlab_yaml"))

    return roots


def _classify_file(path: str) -> str | None:
    basename = os.path.basename(path)
    if basename in _MAKEFILE_NAMES or path.endswith(".mk"):
        return "makefile"
    if basename == ".gitlab-ci.yml" or (path.endswith(".yml") and "gitlab" in basename.lower()):
        return "gitlab_yaml"
    if path.endswith(".yml") or path.endswith(".yaml"):
        return "gitlab_yaml"
    return None


def _parse_root(path: str, kind: str) -> Report:
    if kind == "makefile":
        return parse_makefile(path)
    elif kind == "gitlab_yaml":
        return parse_gitlab(path)
    else:
        return Report(
            root=path,
            format=kind,
        )


def _output_basename(path: str, kind: str) -> str:
    basename = os.path.basename(path)
    if basename == ".gitlab-ci.yml":
        return "gitlab-ci"
    return basename.replace(".", "_")


def _print_diagnostics_summary(report: Report, root_path: str) -> None:
    counts = {"info": 0, "warning": 0, "error": 0}
    for d in report.diagnostics:
        counts[d.severity] += 1

    parts = []
    if counts["error"]:
        parts.append(f"{counts['error']} error(s)")
    if counts["warning"]:
        parts.append(f"{counts['warning']} warning(s)")
    if counts["info"]:
        parts.append(f"{counts['info']} info")

    if parts:
        print(f"  {root_path}: {', '.join(parts)}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
