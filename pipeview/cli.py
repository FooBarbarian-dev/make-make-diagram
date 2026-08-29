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

    # `pipeview gitlab …` / `pipeview github …` route to the remote
    # subcommands — the only parts of pipeview that perform network access.
    # A local directory literally named "gitlab" is still reachable as
    # `pipeview ./gitlab`.
    if argv and argv[0] == "gitlab":
        from pipeview.gitlab.cli import main as gitlab_main
        return gitlab_main(argv[1:])
    if argv and argv[0] == "github":
        from pipeview.github.cli import main as github_main
        return github_main(argv[1:])

    # `pipeview scenarios …` routes to the trigger-docs scenario helpers —
    # offline, like everything outside `gitlab`. A local directory literally
    # named "scenarios" is still reachable as `pipeview ./scenarios`.
    if argv and argv[0] == "scenarios":
        from pipeview.scenarios_cli import main as scenarios_main
        return scenarios_main(argv[1:])

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
        "--no-bundled-templates",
        action="store_true",
        help=(
            "Leave GitLab include:template entries unresolved instead of "
            "reading pipeview's bundled snapshot of GitLab's built-in "
            "templates (still offline either way)"
        ),
    )
    parser.add_argument(
        "--trigger-docs",
        metavar="FILE",
        help=(
            "Scenarios file (start one with `pipeview scenarios init`): also "
            "write per-trigger markdown docs for each GitLab CI root, to "
            "<outdir>/<name>.trigger-docs/"
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

    trigger_scenarios = None
    trigger_skipped: list[str] = []
    docs_floor = 0
    if args.trigger_docs:
        from pipeview.scenarios import load_scenarios
        trigger_scenarios, sdiags = load_scenarios(args.trigger_docs)
        for d in sdiags:
            print(f"  {args.trigger_docs}: [{d.severity}] {d.message}",
                  file=sys.stderr)
        trigger_skipped = [d.message for d in sdiags if d.severity == "error"]
        if sdiags:
            docs_floor = 1
        if not trigger_scenarios:
            print(f"No usable scenarios in {args.trigger_docs} — "
                  "trigger docs skipped", file=sys.stderr)
            trigger_scenarios = None

    any_diagnostics = False
    max_severity = None

    for root_path, root_kind in roots:
        report = _parse_root(root_path, root_kind,
                             bundled_templates=not args.no_bundled_templates)
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

        if trigger_scenarios:
            if root_kind in ("gitlab_yaml", "github_workflows"):
                docs_floor = max(docs_floor, _emit_trigger_docs(
                    report, trigger_scenarios, trigger_skipped,
                    os.path.join(outdir, f"{basename}.trigger-docs"),
                    root_path, args))
            else:
                print(f"  {root_path}: trigger docs apply to GitLab CI and "
                      "GitHub Actions configurations — skipped",
                      file=sys.stderr)

        if report.diagnostics:
            any_diagnostics = True
            sev = report.max_severity()
            if sev:
                sev_order = {"info": 0, "warning": 1, "error": 2}
                if max_severity is None or sev_order.get(sev, 0) > sev_order.get(max_severity, 0):
                    max_severity = sev

            _print_diagnostics_summary(report, root_path)

        print(f"Report generated: {outdir}/{basename}.*")

    if docs_floor or (any_diagnostics and max_severity in ("warning", "error")):
        return 1
    return 0


def _emit_trigger_docs(report: Report, scenarios, skipped: list[str],
                       docdir: str, root_path: str, args) -> int:
    """Write the trigger-docs folder for one GitLab root. Returns the exit
    floor (docs problems never block report generation)."""
    from pipeview.render.trigger_docs import (
        generate_trigger_docs,
        write_docs_folder,
    )
    project = os.path.basename(root_path)
    norm = os.path.abspath(root_path).replace(os.sep, "/")
    if norm.endswith("/.github/workflows"):
        # the workflows dir is the root; the repo directory is the name
        project = os.path.basename(norm[: -len("/.github/workflows")]) \
            or project
    provenance = {"project": project, "ref": "",
                  "commit": "", "version": pipeview.__version__}
    cmd = f"pipeview {args.path} --trigger-docs {args.trigger_docs} -o {args.output}"
    files = generate_trigger_docs(report.to_dict(), scenarios, skipped,
                                  provenance, cmd)
    if files is None:
        print(f"  {root_path}: no what-if program — trigger docs skipped",
              file=sys.stderr)
        return 0
    floor = 0
    for d in write_docs_folder(docdir, files):
        print(f"  {docdir}: [{d.severity}] {d.message}", file=sys.stderr)
        floor = 1
    print(f"Trigger docs generated: {docdir}/")
    return floor


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

        workflows_dir = os.path.join(path, ".github", "workflows")
        if os.path.isdir(workflows_dir):
            roots.append((workflows_dir, "github_workflows"))

    return roots


def _classify_file(path: str) -> str | None:
    basename = os.path.basename(path)
    if basename in _MAKEFILE_NAMES or path.endswith(".mk"):
        return "makefile"
    if basename == ".gitlab-ci.yml" or (path.endswith(".yml") and "gitlab" in basename.lower()):
        return "gitlab_yaml"
    if path.endswith(".yml") or path.endswith(".yaml"):
        norm = os.path.abspath(path).replace(os.sep, "/")
        if "/.github/workflows/" in norm or _looks_like_github_workflow(path):
            return "github_workflows"
        return "gitlab_yaml"
    return None


def _looks_like_github_workflow(path: str) -> bool:
    """Content sniff for a workflow file outside .github/workflows: a
    top-level mapping with jobs: and on: (unquoted `on` reads as YAML
    True) and no GitLab-only markers."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            data = yaml.safe_load(f.read())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    keys = {("on" if k is True else k) for k in data}
    return "jobs" in keys and "on" in keys and "stages" not in keys


def _parse_root(path: str, kind: str, bundled_templates: bool = True) -> Report:
    if kind == "makefile":
        return parse_makefile(path)
    elif kind == "gitlab_yaml":
        return parse_gitlab(path, bundled_templates=bundled_templates)
    elif kind == "github_workflows":
        from pipeview.parsers.github_parser import parse_github
        return parse_github(path)
    else:
        return Report(
            root=path,
            format=kind,
        )


def _output_basename(path: str, kind: str) -> str:
    if kind == "github_workflows":
        if os.path.isdir(path):
            return "github-actions"
        return os.path.basename(path).replace(".", "_")
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
