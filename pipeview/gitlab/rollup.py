"""Static cross-project linking across one sync run's reports.

Pure functions over already-parsed Reports — no network, no filesystem.
The rollup answers "which tracked projects *can* trigger which" from
configuration alone; upstream edges are the same downstream edges seen
from the other side, valid only within the tracked set and labeled as
such. Anything that cannot be resolved statically (untracked targets,
CI-variable project/ref values, dynamic child pipelines) degrades to an
explicit external entry or caveat — never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pipeview.model import Report

ROLLUP_SCHEMA_VERSION = 1

# Reports generated in one sync pass are seconds apart; a spread beyond
# this means the rollup mixes meaningfully different snapshots.
_SKEW_WARN_SECONDS = 3600


@dataclass
class RollupSource:
    """One successfully generated tracked entry."""
    entry: str               # tracked entry string ("group/app" or "group/app@ref")
    report: Report
    report_html: str | None  # basename of the written HTML, if any


def build_rollup(host: str, sources: list[RollupSource],
                 missing_entries: list[str] | None = None) -> dict:
    """Resolve cross-project references between `sources` into a rollup
    document (see the 2026-08-25 design spec for the shape)."""
    projects = [_project_summary(s) for s in sources]

    by_path: dict[str, list[int]] = {}
    for i, p in enumerate(projects):
        by_path.setdefault(p["project"].lower(), []).append(i)

    # A bare entry follows the default branch, so its resolved ref IS the
    # default branch — the target of every ref-less trigger at it.
    default_branch: dict[str, str] = {}
    for i, p in enumerate(projects):
        if "@" not in sources[i].entry and p["ref"]:
            default_branch[p["project"].lower()] = p["ref"]

    links: list[dict] = []
    externals: dict[str, dict] = {}
    diagnostics: list[dict] = []

    for i, src in enumerate(sources):
        for node in src.report.nodes:
            info = node.annotations.get("trigger_info")
            if isinstance(info, dict) and info.get("mode") == "multi_project":
                _collect_reference(
                    links, externals, kind="trigger", src_index=i,
                    node=node, path=info.get("project") or "?",
                    ref=info.get("ref"),
                    base_caveats=list(info.get("unresolved") or []),
                    extra={"strategy": info.get("strategy"),
                           "forward": info.get("forward")},
                    by_path=by_path, projects=projects,
                    default_branch=default_branch,
                )
            for rec in node.annotations.get("cross_project_needs") or []:
                _collect_reference(
                    links, externals, kind="needs_project", src_index=i,
                    node=node, path=rec.get("project") or "?",
                    ref=rec.get("ref"), base_caveats=[],
                    extra={"job": rec.get("job"), "ghost": rec.get("ghost")},
                    by_path=by_path, projects=projects,
                    default_branch=default_branch,
                )
        for inc in _include_references(src.report):
            _collect_reference(
                links, externals, kind="include", src_index=i,
                node=None, path=inc["project"], ref=inc.get("ref"),
                base_caveats=[], extra={"file": inc.get("file")},
                by_path=by_path, projects=projects,
                default_branch=default_branch,
            )

    if missing_entries:
        diagnostics.append({
            "severity": "warning",
            "message": ("Rollup covers the successful entries only; "
                        "missing: " + ", ".join(sorted(missing_entries))),
        })
    unresolved = sorted(externals)
    if unresolved:
        diagnostics.append({
            "severity": "info",
            "message": (f"{len(unresolved)} referenced project(s) are not "
                        "tracked: " + ", ".join(unresolved)
                        + " — track them to link their pipelines"),
        })
    skew = _generated_at_skew(projects)
    if skew is not None and skew > _SKEW_WARN_SECONDS:
        diagnostics.append({
            "severity": "warning",
            "message": (f"Reports in this rollup were generated up to "
                        f"{skew // 3600}h apart — the fleet view may mix "
                        "different snapshots"),
        })

    return {
        "schema_version": ROLLUP_SCHEMA_VERSION,
        "host": host,
        "generated_at": max((p["generated_at"] for p in projects
                             if p["generated_at"]), default=""),
        "tool_version": projects[0]["tool_version"] if projects else "",
        "projects": projects,
        "links": links,
        "externals": [externals[k] for k in unresolved],
        "diagnostics": diagnostics,
    }


def annotate_reports(rollup: dict, sources: list[RollupSource],
                     rollup_html: str) -> set[int]:
    """Write rollup_link annotations back onto the source reports so their
    detail panels can point at the rollup. Returns the indices of reports
    that changed (callers re-render those)."""
    touched: set[int] = set()
    for link in rollup["links"]:
        j = link["dst"]["project"]
        if j is None:
            continue
        i = link["src"]["project"]
        report = sources[i].report
        target = {
            "project": rollup["projects"][j]["project"],
            "entry": rollup["projects"][j]["entry"],
            "rollup": rollup_html,
        }
        for node_id in filter(None, (link["src"].get("node"),
                                     link["src"].get("ghost"))):
            node = report.node_by_id(node_id)
            if node is not None and node.annotations.get("rollup_link") != target:
                node.annotations["rollup_link"] = target
                touched.add(i)
    for i, src in enumerate(sources):
        marker = {"file": rollup_html}
        if src.report.annotations.get("rollup") != marker:
            src.report.annotations["rollup"] = marker
            touched.add(i)
    return touched


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _project_summary(src: RollupSource) -> dict:
    report = src.report
    remote = report.annotations.get("gitlab_remote") or {}
    sev: dict[str, int] = {}
    for d in report.diagnostics:
        sev[d.severity] = sev.get(d.severity, 0) + 1
    jobs = [n for n in report.nodes
            if n.kind == "job" and not n.name.startswith(".")]
    return {
        "entry": src.entry,
        "project": remote.get("project") or src.entry.split("@", 1)[0],
        "ref": remote.get("ref") or "",
        "report_html": src.report_html,
        "web_url": remote.get("web_url"),
        "lint_valid": remote.get("lint_valid"),
        "generated_at": report.generated_at,
        "tool_version": report.tool_version,
        "counts": {
            "jobs": len(jobs),
            "stages": sum(1 for n in report.nodes if n.kind == "stage"),
            "diagnostics": sev,
        },
        "model": report.to_dict(),
    }


def _include_references(report: Report) -> list[dict]:
    """include:project provenance from either fetch strategy, deduplicated
    to (project, ref, file)."""
    remote = report.annotations.get("gitlab_remote") or {}
    own = (remote.get("project") or "").lower()
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(project: str, ref: str | None, file: str | None) -> None:
        if not project or project.lower() == own:
            return
        key = (project.lower(), ref, file)
        if key not in seen:
            seen.add(key)
            out.append({"project": project, "ref": ref, "file": file})

    for entry in remote.get("include_projects") or []:   # files strategy
        if isinstance(entry, dict):
            add(str(entry.get("project") or ""), entry.get("ref"),
                entry.get("file"))
    for entry in remote.get("includes") or []:           # lint provenance
        if not isinstance(entry, dict):
            continue
        extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
        proj = extra.get("project")
        if proj:
            add(str(proj), extra.get("ref"), entry.get("location"))
    return out


def _collect_reference(links: list, externals: dict, *, kind: str,
                       src_index: int, node, path: str, ref: str | None,
                       base_caveats: list[str], extra: dict,
                       by_path: dict, projects: list,
                       default_branch: dict) -> None:
    src: dict[str, Any] = {"project": src_index}
    if node is not None:
        src["node"] = node.id
        if node.source is not None:
            src["file"] = node.source.file
            src["line"] = node.source.line
        ghost = extra.pop("ghost", None) if kind == "needs_project" else None
        if ghost is None and kind == "trigger":
            ghost = f"downstream:{path}"
        if ghost:
            src["ghost"] = ghost

    project_unresolvable = "$" in path
    candidates = [] if project_unresolvable else by_path.get(path.lower(), [])

    if not candidates:
        ext = externals.setdefault(path, {
            "path": path, "kinds": [], "refs": [], "references": 0,
        })
        ext["references"] += 1
        if kind not in ext["kinds"]:
            ext["kinds"].append(kind)
        if ref and ref not in ext["refs"]:
            ext["refs"].append(ref)
        caveats = list(base_caveats)
        if not project_unresolvable:
            caveats.append("project is not tracked")
        links.append({
            "kind": kind, "src": src,
            "dst": {"project": None, "path": path, "ref": ref},
            "caveats": caveats, **extra,
        })
        return

    for j in candidates:
        caveats = list(base_caveats)
        tracked_ref = projects[j]["ref"]
        if ref is not None and "$" in ref:
            pass  # already flagged "ref uses CI variables"; no comparison
        elif ref is not None:
            if ref != tracked_ref:
                caveats.append(f"ref mismatch: targets '{ref}', "
                               f"tracked at '{tracked_ref}'")
        else:
            # ref-less references target the project's default branch
            default = default_branch.get(path.lower())
            if default is None:
                if "@" in projects[j]["entry"]:
                    caveats.append(
                        "targets the default branch; tracked pinned at "
                        f"'{tracked_ref}'")
            elif default != tracked_ref:
                caveats.append(
                    f"targets the default branch ('{default}'), "
                    f"tracked at '{tracked_ref}'")
        links.append({
            "kind": kind, "src": src,
            "dst": {"project": j, "path": projects[j]["project"], "ref": ref},
            "caveats": caveats, **extra,
        })


def _generated_at_skew(projects: list[dict]) -> int | None:
    stamps = []
    for p in projects:
        try:
            stamps.append(datetime.strptime(
                p["generated_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError, KeyError):
            pass
    if len(stamps) < 2:
        return None
    return int((max(stamps) - min(stamps)).total_seconds())
