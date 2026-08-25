"""`pipeview gitlab …` — the only part of pipeview that touches a network.

Subcommands:
  (none)/browse  interactive project browser (curses TUI)
  auth           create/verify/store an API token for a GitLab host
  projects       list projects the token can see
  report         fetch one project's CI config and generate the report
  track/untrack/tracked   manage the tracked-projects list
  sync           generate reports for every tracked project
"""

from __future__ import annotations

import argparse
import os
import sys

from pipeview.gitlab import auth as auth_mod
from pipeview.gitlab.api import GitLabClient, GitLabError
from pipeview.gitlab.config import GitLabConfig

_HOST_ENV_VARS = ("PIPEVIEW_GITLAB_HOST", "GITLAB_HOST", "CI_SERVER_URL")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = GitLabConfig(os.environ.get("PIPEVIEW_GITLAB_CONFIG") or None)

    command = args.command or "browse"
    try:
        if command == "auth":
            return _cmd_auth(args, config)
        host = _resolve_host(args, config)
        if not host:
            print(
                "No GitLab host configured. Pass --host https://gitlab.example.com "
                "(or set PIPEVIEW_GITLAB_HOST), or run `pipeview gitlab auth --host …` "
                "once to store it.",
                file=sys.stderr,
            )
            return 2

        if command == "tracked":
            return _cmd_tracked(config, host)
        if command == "track":
            return _cmd_track(args, config, host, add=True)
        if command == "untrack":
            return _cmd_track(args, config, host, add=False)

        client = _make_client(args, config, host)
        if client is None:
            return 2

        if command == "browse":
            return _cmd_browse(args, config, host, client)
        if command == "projects":
            return _cmd_projects(args, client)
        if command == "report":
            return _cmd_report(args, client)
        if command == "sync":
            return _cmd_sync(args, config, host, client)
    except GitLabError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 2

    parser.error(f"unknown command {command!r}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeview gitlab",
        description=(
            "Browse a GitLab instance and generate pipeline reports straight "
            "from what it serves. This subcommand is the only part of "
            "pipeview that performs network access."
        ),
        epilog=(
            "examples:\n"
            "  pipeview gitlab auth --host https://gitlab.example.com\n"
            "  pipeview gitlab                       Interactive project browser\n"
            "  pipeview gitlab projects --search api\n"
            "  pipeview gitlab report group/app --ref main -o report\n"
            "  pipeview gitlab track group/app        Track (default branch)\n"
            "  pipeview gitlab track group/app@dev    Track a specific branch\n"
            "  pipeview gitlab sync                   Reports for all tracked\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", nargs="?",
        choices=["browse", "auth", "projects", "report", "track", "untrack",
                 "tracked", "sync"],
        help="Subcommand (default: browse — the interactive TUI)",
    )
    parser.add_argument(
        "project", nargs="?",
        help=(
            "Project path for report/track/untrack: group/app, or "
            "group/app@ref to name a branch/tag (equivalent to --ref)"
        ),
    )
    parser.add_argument("--host", help="GitLab base URL, e.g. https://gitlab.example.com")
    parser.add_argument("--token", help="API token (else env vars / stored config)")
    parser.add_argument("--ref", help="Branch/tag/SHA (default: project's default branch)")
    parser.add_argument(
        "-o", "--output", default="./pipeview-out",
        help="Output directory (default: ./pipeview-out)",
    )
    parser.add_argument(
        "--format", default="html,json",
        help="Comma-separated output formats: html, json, svg, dot, mmd",
    )
    parser.add_argument(
        "--strategy", choices=["auto", "lint", "files"], default="auto",
        help=(
            "How to fetch the config: 'lint' asks GitLab's CI Lint API for "
            "the fully merged view (one call, includes resolved server-side); "
            "'files' walks include: across repositories file by file (real "
            "per-file line numbers); 'auto' tries lint, falls back to files"
        ),
    )
    parser.add_argument("--search", help="projects: server-side search filter")
    parser.add_argument("--ca-bundle", help="Custom CA bundle for TLS verification")
    parser.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS verification (NOT recommended; never stored)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    return parser


def _split_project_ref(args) -> tuple[str | None, str | None]:
    """(project, ref) from the positional arg. `group/app@ref` names a ref
    inline (project paths cannot contain '@'); an explicit --ref wins."""
    project = args.project
    ref = None
    if project and "@" in project:
        project, ref = GitLabConfig.parse_entry(project)
    return project, (args.ref or ref)


def _resolve_host(args, config: GitLabConfig) -> str | None:
    if getattr(args, "host", None):
        return GitLabConfig.normalize_host(args.host)
    for var in _HOST_ENV_VARS:
        if os.environ.get(var):
            return GitLabConfig.normalize_host(os.environ[var])
    if config.default_host:
        return config.default_host
    hosts = config.known_hosts()
    if len(hosts) == 1:
        return hosts[0]
    return None


def _make_client(args, config: GitLabConfig, host: str) -> GitLabClient | None:
    token, source = auth_mod.resolve_token(host, args.token, config)
    if token is None:
        if sys.stdin.isatty() and sys.stderr.isatty():
            token = auth_mod.interactive_setup(
                host, config, ca_bundle=args.ca_bundle, insecure=args.insecure)
        if token is None:
            print(
                f"No API token for {host}. Run `pipeview gitlab auth --host {host}`, "
                "or export PIPEVIEW_GITLAB_TOKEN / GITLAB_TOKEN.",
                file=sys.stderr,
            )
            return None
    if args.insecure:
        print(f"WARNING: TLS verification disabled for {host}", file=sys.stderr)
    return GitLabClient(host, token, ca_bundle=args.ca_bundle,
                        insecure=args.insecure, timeout=args.timeout)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_auth(args, config: GitLabConfig) -> int:
    host = _resolve_host(args, config)
    if not host:
        try:
            raw = input("GitLab host (e.g. https://gitlab.example.com): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
        if not raw:
            return 2
        host = GitLabConfig.normalize_host(raw)

    token, source = auth_mod.resolve_token(host, args.token, config)
    if token:
        try:
            user = auth_mod.verify_token(host, token, ca_bundle=args.ca_bundle,
                                         insecure=args.insecure)
            print(f"Token from {source} works: authenticated as "
                  f"{user.get('username', '?')} on {host}")
            if not config.default_host:
                config.default_host = host
                config.save()
            return 0
        except GitLabError as e:
            print(f"Existing token ({source}) failed: {e}", file=sys.stderr)

    token = auth_mod.interactive_setup(host, config, ca_bundle=args.ca_bundle,
                                       insecure=args.insecure)
    return 0 if token else 2


def _cmd_browse(args, config: GitLabConfig, host: str, client) -> int:
    from pipeview.gitlab.tui import run_tui
    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    return run_tui(client, config, host, outdir=args.output,
                   formats=formats, strategy=args.strategy)


def _cmd_projects(args, client) -> int:
    page: int | None = 1
    shown = 0
    while page is not None:
        items, page = client.list_projects(search=args.search or None, page=page)
        for p in items:
            path = p.get("path_with_namespace") or "?"
            activity = (p.get("last_activity_at") or "")[:10]
            print(f"{path:50}  {activity}")
            shown += 1
        if page is not None and shown >= 200:
            print("… (more results — narrow with --search)", file=sys.stderr)
            break
    if shown == 0:
        print("No projects visible to this token"
              + (f" matching {args.search!r}" if args.search else ""),
              file=sys.stderr)
    return 0


def _cmd_report(args, client) -> int:
    project, ref = _split_project_ref(args)
    if not project:
        print("Usage: pipeview gitlab report <group/project>[@ref] [--ref R]",
              file=sys.stderr)
        return 2
    from pipeview.gitlab.report import generate_report
    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    report, written = generate_report(
        client, project, ref=ref, outdir=args.output,
        formats=formats, strategy=args.strategy)
    for path in written:
        print(f"Report generated: {path}")
    _print_diagnostics(report)
    return 1 if report.max_severity() in ("warning", "error") else 0


def _cmd_track(args, config: GitLabConfig, host: str, add: bool) -> int:
    project, ref = _split_project_ref(args)
    if not project:
        verb = "track" if add else "untrack"
        print(f"Usage: pipeview gitlab {verb} <group/project>[@ref] [--ref R]",
              file=sys.stderr)
        return 2
    entry = GitLabConfig.make_entry(project, ref)
    if add:
        changed = config.track(host, project, ref)
        what = f"{entry}" + ("" if ref else " (default branch)")
        print(f"{'Tracking' if changed else 'Already tracking'} {what}")
    else:
        if config.untrack(host, project, ref):
            print(f"Untracked {entry}")
        elif ref is None and (removed := config.untrack_all(host, project)):
            # Bare untrack with only ref-pinned entries stored: remove them all.
            plural = "y" if removed == 1 else "ies"
            print(f"Untracked {project} ({removed} ref entr{plural})")
        else:
            print(f"Was not tracking {entry}")
    config.save()
    return 0


def _cmd_tracked(config: GitLabConfig, host: str) -> int:
    tracked = config.tracked(host)
    if not tracked:
        print(f"No tracked projects for {host} — "
              "`pipeview gitlab track group/app[@ref]` or press t in the browser")
        return 0
    for path in tracked:
        print(path)
    return 0


def _cmd_sync(args, config: GitLabConfig, host: str, client) -> int:
    from pipeview.gitlab.report import generate_report
    tracked = config.tracked(host)
    if not tracked:
        print(f"No tracked projects for {host} — nothing to sync",
              file=sys.stderr)
        return 0
    if args.ref:
        print("Note: --ref is ignored by sync — each tracked entry carries "
              "its own ref (track group/app@ref to pin one)", file=sys.stderr)
    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    worst = 0
    for entry in tracked:
        path, ref = GitLabConfig.parse_entry(entry)
        try:
            report, written = generate_report(
                client, path, ref=ref, outdir=args.output,
                formats=formats, strategy=args.strategy)
        except GitLabError as e:
            print(f"{entry}: FAILED — {e}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        sev = report.max_severity()
        marker = f" [{sev}]" if sev in ("warning", "error") else ""
        html = next((p for p in written if p.endswith(".html")),
                    written[0] if written else "?")
        print(f"{entry}: {html}{marker}")
        if sev in ("warning", "error"):
            worst = max(worst, 1)
    return worst


def _print_diagnostics(report) -> None:
    for d in report.diagnostics:
        loc = f" ({d.source.file}:{d.source.line})" if d.source else ""
        print(f"  {d.severity}: {d.message}{loc}", file=sys.stderr)
