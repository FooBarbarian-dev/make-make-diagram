"""`pipeview scenarios` — helpers for the trigger-docs scenarios file.

Offline like the rest of the local CLI: `init` writes a commented starter
file, `check` validates one, `preview` renders scenario docs for a local
checkout to stdout. Routed from pipeview.cli the same way `gitlab` is.

Exit codes follow pipeview's convention: 0 clean, 1 diagnostics,
2 nothing usable.
"""

from __future__ import annotations

import argparse
import os
import sys

import pipeview
from pipeview.scenarios import load_scenarios

DEFAULT_FILENAME = "pipeview-scenarios.yaml"

INIT_TEMPLATE = """\
# Trigger-docs scenarios for pipeview.
#
# Each scenario is a named What-If configuration — the same knobs the HTML
# report's What-If tab exposes. `pipeview <path> --trigger-docs THIS-FILE`
# (or `pipeview gitlab sync --trigger-docs THIS-FILE`) renders one markdown
# doc per scenario, for every project in the run.
#
# Validate with:  pipeview scenarios check THIS-FILE
# Iterate with:   pipeview scenarios preview THIS-FILE path/to/checkout
#
# Every scenario takes:
#   id:       required — [a-z0-9-]+, becomes the doc filename <id>.md
#   title:    optional — the doc's heading (defaults to the id)
#   intro:    optional — prose copied verbatim under the title
#   event:    required — one of:
#               push_branch  push to a branch      (knobs: branch, open_mr,
#                                                   new_branch)
#               push_tag     push a tag            (knobs: tag, tag_protected)
#               mr           merge request         (knobs: branch, target,
#                                                   draft, mr_flavor, mr_labels)
#               schedule | web | api | trigger     (knobs: ref_kind, branch, tag)
#   variables:     optional — simulated project-level variables {NAME: value}
#   changed_files: optional — list of changed paths, or the literal `all`
#                  (assume every changes: pattern matches); omit it to leave
#                  the changed-files question open (rules:changes reports
#                  *depends* instead of guessing)
#   commit_message: optional — the simulated commit message, for rules on
#                  CI_COMMIT_MESSAGE / CI_COMMIT_TITLE ("[skip ci]"-style)
#   diagrams:      optional — [dag] (default) or [dag, lifecycle]
version: 1
scenarios:
  - id: push-main
    title: Push to main
    intro: |
      What runs on every merge to main.
    event: push_branch
    branch: main

  - id: release-tag
    title: Release tag
    event: push_tag
    tag: v1.2.3            # an example ref; each project's rules decide

  - id: nightly
    title: Nightly schedule
    event: schedule
    # variables: { NIGHTLY: "1" }

  # - id: feature-mr
  #   title: Push to a feature branch with an open MR
  #   event: push_branch
  #   branch: feature/example
  #   open_mr: { target: main, draft: false }
  #   diagrams: [dag, lifecycle]
"""


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "verify":
        return _cmd_verify(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeview scenarios",
        description="Author and validate trigger-docs scenario files "
                    "(see the generated file's comments for the schema).",
    )
    parser.add_argument(
        "--version", action="version", version=f"pipeview {pipeview.__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="write a commented starter file")
    p_init.add_argument("path", nargs="?", default=DEFAULT_FILENAME,
                        help=f"file to create (default: ./{DEFAULT_FILENAME})")

    p_check = sub.add_parser("check", help="validate a scenarios file")
    p_check.add_argument("path", help="scenarios file to validate")

    p_preview = sub.add_parser(
        "preview", help="render scenario docs for a local checkout to stdout")
    p_preview.add_argument("path", help="scenarios file")
    p_preview.add_argument("repo", help="local checkout (or .gitlab-ci.yml path)")
    p_preview.add_argument("--scenario", metavar="ID",
                           help="render just this scenario")

    p_verify = sub.add_parser(
        "verify",
        help="check committed docs against fresh generation (read-only)",
        description=(
            "Regenerate the docs for a local checkout and compare with the "
            "committed copies — provenance lines are masked, so a newer "
            "pipeview regenerating identical content is not drift. Exit 0 in "
            "sync, 1 on drift (each file named), 2 when inputs are unusable. "
            "Note: generation here parses the local checkout; docs generated "
            "by `gitlab sync` for configs that rely on cross-repo includes "
            "may legitimately differ."
        ),
    )
    p_verify.add_argument("path", help="scenarios file")
    p_verify.add_argument("repo", help="local checkout (or .gitlab-ci.yml path)")
    p_verify.add_argument("docs_dir",
                          help="the committed docs folder (e.g. docs/ci)")
    return parser


def _print_diags(diags) -> None:
    for d in diags:
        print(f"{d.severity}: {d.message}", file=sys.stderr)


def _cmd_init(args) -> int:
    path = args.path
    if os.path.exists(path):
        print(f"Error: {path} already exists — not overwriting", file=sys.stderr)
        return 2
    with open(path, "w", encoding="utf-8") as f:
        f.write(INIT_TEMPLATE)
    print(f"Wrote {path} — edit it, then validate with "
          f"`pipeview scenarios check {path}`")
    return 0


def _cmd_check(args) -> int:
    scenarios, diags = load_scenarios(args.path)
    _print_diags(diags)
    if not scenarios:
        print(f"{args.path}: no usable scenarios", file=sys.stderr)
        return 2
    ids = ", ".join(s.id for s in scenarios)
    print(f"{args.path}: {len(scenarios)} scenario(s) usable: {ids}")
    return 1 if diags else 0


def _cmd_preview(args) -> int:
    # local import: pipeview.cli routes to this module, so import lazily
    from pipeview.cli import _discover_roots, _parse_root
    from pipeview.render.trigger_docs import generate_trigger_docs

    scenarios, diags = load_scenarios(args.path)
    _print_diags(diags)
    if not scenarios:
        print(f"{args.path}: no usable scenarios", file=sys.stderr)
        return 2
    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
        if not scenarios:
            print(f"Error: no scenario with id {args.scenario!r}", file=sys.stderr)
            return 2

    repo = os.path.abspath(args.repo)
    roots = [(p, k) for p, k in _discover_roots(repo) if k == "gitlab_yaml"]
    if not roots:
        print(f"Error: no GitLab CI configuration found at {args.repo}",
              file=sys.stderr)
        return 2
    root_path, root_kind = roots[0]
    report = _parse_root(root_path, root_kind)
    skipped = [d.message for d in diags if d.severity == "error"]
    provenance = {"project": os.path.basename(root_path), "ref": "",
                  "commit": "", "version": pipeview.__version__}
    files = generate_trigger_docs(
        report.to_dict(), scenarios, skipped, provenance,
        f"pipeview scenarios preview {args.path} {args.repo}")
    if files is None:
        print(f"Error: {root_path} carries no what-if program", file=sys.stderr)
        return 2
    for scenario in scenarios:
        name = f"{scenario.id}.md"
        print(f"===== {name} =====")
        print(files[name])
    # preview's exit code is about the scenarios file — the repo's own
    # diagnostics belong to report generation, not to this iteration loop
    sev = report.max_severity()
    if sev in ("warning", "error"):
        print(f"note: this configuration has {sev} diagnostics — "
              f"run `pipeview {args.repo}` for details", file=sys.stderr)
    return 1 if diags else 0


def _cmd_verify(args) -> int:
    # local import: pipeview.cli routes to this module, so import lazily
    from pipeview.cli import _discover_roots, _parse_root
    from pipeview.render.trigger_docs import (
        compare_docs_folder,
        generate_trigger_docs,
    )

    scenarios, diags = load_scenarios(args.path)
    _print_diags(diags)
    if not scenarios:
        print(f"{args.path}: no usable scenarios", file=sys.stderr)
        return 2
    repo = os.path.abspath(args.repo)
    roots = [(p, k) for p, k in _discover_roots(repo) if k == "gitlab_yaml"]
    if not roots:
        print(f"Error: no GitLab CI configuration found at {args.repo}",
              file=sys.stderr)
        return 2
    if not os.path.isdir(args.docs_dir):
        print(f"Error: {args.docs_dir} is not a directory", file=sys.stderr)
        return 2
    root_path, root_kind = roots[0]
    report = _parse_root(root_path, root_kind)
    skipped = [d.message for d in diags if d.severity == "error"]
    provenance = {"project": os.path.basename(root_path), "ref": "",
                  "commit": "", "version": pipeview.__version__}
    files = generate_trigger_docs(
        report.to_dict(), scenarios, skipped, provenance,
        f"pipeview scenarios verify {args.path} {args.repo} {args.docs_dir}")
    if files is None:
        print(f"Error: {root_path} carries no what-if program", file=sys.stderr)
        return 2

    result = compare_docs_folder(args.docs_dir, files)
    for name in result["missing"]:
        print(f"missing: {name} — expected but not present "
              f"(or not pipeview-generated)", file=sys.stderr)
    for name in result["stale"]:
        print(f"stale: {name} — content differs from fresh generation",
              file=sys.stderr)
    for name in result["orphaned"]:
        print(f"orphaned: {name} — no current scenario produces it",
              file=sys.stderr)
    if result["missing"] or result["stale"] or result["orphaned"]:
        print(f"{args.docs_dir}: drift — {len(result['ok'])}/{len(files)} "
              f"docs up to date")
        return 1
    print(f"{args.docs_dir}: up to date ({len(files)} docs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
