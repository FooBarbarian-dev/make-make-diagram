"""Trigger-docs markdown renderer.

Turns evaluated What-If scenarios into per-scenario markdown docs plus a
`pipeline-triggers.md` index — GitLab-flavored markdown with native
mermaid blocks, written to be committed into a project's own repo (see
docs/superpowers/specs/2026-08-27-trigger-docs-design.md).

Honesty and determinism rules:
- *depends* verdicts stay unknown, with the missing fact named — never
  resolved optimistically;
- verdicts are encoded in node shape and label, never color (GitLab
  renders mermaid in its own light/dark themes);
- no timestamps anywhere: identical inputs give byte-identical output;
- everything here is pure string-building except write_docs_folder, the
  one function that touches the filesystem — and the only files it will
  ever delete or overwrite are ones carrying this module's provenance
  marker.
"""

from __future__ import annotations

import os
import re

import pipeview
from pipeview.model import Diagnostic
from pipeview.parsers.gitlab_whatif_eval import evaluate_event, might_run
from pipeview.render.mmd import escape_label
from pipeview.scenarios import Scenario, to_whatif_config

MARKER = "pipeview-trigger-doc"
INDEX_NAME = "pipeline-triggers.md"

# Past this many in-pipeline jobs a flowchart stops being readable and
# GitLab's mermaid renderer starts to choke — collapse to stage summaries.
GRAPH_JOB_LIMIT = 60

_LEGEND = ("*Hexagon = manual gate · ⏱ = delayed · dashed `?` = depends "
           "(unknown) · ▶ = spawns downstream. Jobs without an incoming "
           "arrow start when the previous stage finishes.*")


# ---------------- small text helpers ----------------

def _md_cell(s: str) -> str:
    """Make text safe inside a markdown table cell."""
    return s.replace("\n", " ").replace("|", "\\|")


def _code(s: str) -> str:
    """Wrap text in a code span, surviving embedded backticks."""
    if "`" in s:
        return "`` " + s + " ``"
    return "`" + s + "`"


def _plain(s: str) -> str:
    """One-line plain text (for sequence diagrams and summaries)."""
    return re.sub(r"\s+", " ", s).strip()


def _candidate_ref(cand: dict) -> str:
    if cand["refType"] == "merge_request" and cand.get("target"):
        return f"{cand['ref']} → {cand['target']}"
    return cand["ref"]


def _event_text(scenario: Scenario, cand_ref: str | None = None) -> str:
    """Human description of the event, from the scenario definition."""
    c = scenario.config
    branch = c.get("branch")
    tag = c.get("tag")
    if scenario.event == "push_branch":
        text = f"push to branch `{branch}`" if branch \
            else "push to the default branch"
        if c.get("new_branch"):
            text += " (first push of the branch)"
        if "open_mr" in c:
            target = c["open_mr"].get("target")
            text += " with an open MR" + (f" → `{target}`" if target else "")
        return text
    if scenario.event == "push_tag":
        return f"push tag `{tag}`" if tag else "push a tag"
    if scenario.event == "mr":
        src = f"`{branch}`" if branch else "the source branch"
        dst = f"`{c['target']}`" if c.get("target") else "the default branch"
        text = f"merge request {src} → {dst}"
        if c.get("draft"):
            text += " (draft)"
        return text
    on = f" on tag `{tag or 'v1.0.0'}`" if c.get("ref_kind") == "tag" \
        else (f" on branch `{branch}`" if branch else "")
    return {
        "schedule": "scheduled pipeline",
        "web": "manual pipeline (web UI)",
        "api": "pipeline created via the API",
        "trigger": "pipeline created by a trigger token",
    }[scenario.event] + on


def _scenario_line(scenario: Scenario) -> str:
    parts = [_event_text(scenario)]
    variables = scenario.config.get("variables") or {}
    if variables:
        parts.append("variables: " + ", ".join(
            f"`{k}={v}`" for k, v in sorted(variables.items())))
    changed = scenario.config.get("changed_files")
    if changed is None:
        parts.append("changed files: not specified")
    elif changed == "all":
        parts.append("changed files: every pattern matches")
    elif changed:
        parts.append("changed files: " + ", ".join(f"`{p}`" for p in changed))
    else:
        parts.append("changed files: none match")
    return "**Scenario:** " + " · ".join(parts)


# ---------------- report metadata ----------------

def _job_meta(report: dict) -> dict:
    jobs = {}
    for n in report.get("nodes") or []:
        w = (n.get("annotations") or {}).get("whatif")
        if n.get("kind") == "job" and w:
            jobs[n["id"]] = w
    whatif = (report.get("annotations") or {}).get("whatif") or {}
    return {"jobs": jobs, "stages": whatif.get("stages") or [],
            "whatif": whatif}


def _stage_sorted(ids: list[str], meta: dict) -> list[str]:
    # GitLab presents pipelines in stage order; so do we. YAML definition
    # order within a stage, unknown stages last, stably.
    pos = {job_id: i for i, job_id in enumerate(ids)}
    stages = meta["stages"]

    def rank(job_id: str) -> int:
        stage = meta["jobs"].get(job_id, {}).get("stage")
        try:
            return stages.index(stage)
        except ValueError:
            return len(stages)

    return sorted(ids, key=lambda i: (rank(i), pos[i]))


# ---------------- verdict + why ----------------

def _matrix_suffix(outcome: dict) -> str:
    if outcome.get("matrixPartial") and outcome.get("matrix"):
        n = sum(1 for m in outcome["matrix"]
                if m["state"] in ("runs", "manual", "delayed", "conditional"))
        return f" [{n}/{len(outcome['matrix'])} matrix instances]"
    if outcome.get("matrixCount"):
        return f" ×{outcome['matrixCount']}"
    return ""


def _verdict_text(outcome: dict, is_trigger: bool) -> str:
    state = outcome["state"]
    if is_trigger and might_run(outcome):
        t = "▶ trigger"
        if state == "manual":
            t += " (manual gate)"
        elif state == "conditional":
            t += " (*depends*)"
        return t + _matrix_suffix(outcome)
    if state == "runs":
        when = outcome.get("when")
        t = "runs only after an earlier job fails" if when == "on_failure" \
            else "runs (when: always)" if when == "always" else "runs"
    elif state == "manual":
        t = "**manual gate**" if outcome.get("allow_failure") is False \
            else "manual (optional)"
    elif state == "delayed":
        t = "delayed" + (f" {outcome['start_in']}" if outcome.get("start_in") else "")
    elif state == "conditional":
        t = "*depends*"
    elif state == "skipped":
        t = "skipped (when: never)"
    else:
        t = "not added"
    return t + _matrix_suffix(outcome)


def _matched_desc(outcome: dict) -> str | None:
    for entry in outcome.get("trace") or []:
        if entry.get("verdict") == "matched":
            return entry.get("desc")
    return None


def _why_text(outcome: dict) -> str:
    """One literal line from the deciding rule — never a paraphrase."""
    state = outcome["state"]
    if state == "conditional":
        why = "depends on " + _code(outcome.get("condition") or "an unknown condition")
        notes = outcome.get("conditionNotes") or []
        if notes:
            why += " — " + notes[0]
        return why
    if state == "skipped":
        desc = _matched_desc(outcome)
        return ("rule " + _code(desc) + " says `when: never`") \
            if desc and desc != "(unconditional)" else "`when: never`"
    if state == "not-added":
        if outcome.get("collapsed"):
            return outcome.get("reason") or "excluded"
        reason = outcome.get("reason") or "no rule matched"
        return reason
    desc = _matched_desc(outcome)
    if desc is None or desc == "(unconditional)" \
            or desc.startswith("implicit default only"):
        return "—"
    return _code(desc)


def _trigger_targets(whatif_job: dict) -> list[str]:
    """Boundary descriptions for a trigger job — never expanded (docs stop
    at the boundary by design)."""
    trig = whatif_job.get("trigger") or {}
    out = []
    for rel in trig.get("children") or []:
        out.append(f"child pipeline `{rel}`")
    if trig.get("project"):
        out.append(f"downstream pipeline in `{trig['project']}`")
    for entry in trig.get("unresolved") or []:
        out.append(f"a pipeline not resolvable offline ({entry})")
    fwd = trig.get("forward")
    if fwd and fwd.get("yaml_variables") is False:
        out.append("no yaml variables forwarded")
    return out


def _trigger_why(whatif_job: dict) -> str:
    targets = _trigger_targets(whatif_job)
    return "spawns " + "; ".join(targets) if targets else "trigger job"


# ---------------- mermaid: per-candidate DAG ----------------

def _unique_nids(keys: list[str], prefix: str) -> dict[str, str]:
    """Sanitize arbitrary strings into unique mermaid identifiers. The
    prefix keeps them clear of mermaid keywords (`end`, `graph`, …) and of
    each other's namespaces."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for key in keys:
        base = prefix + re.sub(r"[^0-9A-Za-z_]", "_", key)
        nid, n = base, 2
        while nid in used:
            nid = f"{base}_{n}"
            n += 1
        used.add(nid)
        out[key] = nid
    return out


def _dag_node(nid: str, outcome: dict, whatif_job: dict) -> str:
    name = escape_label(whatif_job["name"])
    if outcome.get("matrixCount"):
        name += f" ×{outcome['matrixCount']}"
    if whatif_job.get("trigger"):
        return f'{nid}(["▶ {name}"])'
    state = outcome["state"]
    if state == "manual":
        return f'{nid}{{{{"{name} (manual)"}}}}'
    if state == "delayed":
        start = outcome.get("start_in")
        return f'{nid}["{name} ⏱{" " + escape_label(start) if start else ""}"]'
    if state == "conditional":
        return f'{nid}["{name}?"]:::depends'
    return f'{nid}["{name}"]'


def _effective_needs(outcome: dict, whatif_job: dict) -> list[dict]:
    if outcome.get("needsUncertain"):
        return []
    if "needsOverride" in outcome:
        return outcome["needsOverride"] or []
    return whatif_job.get("needs") or []


def _dag_lines(cand: dict, meta: dict) -> tuple[list[str], bool]:
    """The candidate's job graph. Returns (lines, collapsed_to_stages)."""
    in_ids = [i for i in _stage_sorted(cand["jobOrder"], meta)
              if might_run(cand["jobs"][i])]
    stages = meta["stages"]

    def stage_of(job_id):
        return meta["jobs"][job_id].get("stage") or ""

    if len(in_ids) > GRAPH_JOB_LIMIT:
        # stage-summary fallback: one node per stage, complete table below
        lines = ["flowchart LR"]
        counts: dict[str, int] = {}
        for job_id in in_ids:
            counts[stage_of(job_id)] = counts.get(stage_of(job_id), 0) + 1
        ordered = [s for s in stages if s in counts] \
            + [s for s in counts if s not in stages]
        stage_nids = _unique_nids(ordered, "s_")
        prev = None
        for stage in ordered:
            nid = stage_nids[stage]
            lines.append(f'  {nid}["{escape_label(stage)} — {counts[stage]} jobs"]')
            if prev:
                lines.append(f"  {prev} --> {nid}")
            prev = nid
        return lines, True

    lines = ["flowchart LR"]
    by_stage: dict[str, list[str]] = {}
    for job_id in in_ids:
        by_stage.setdefault(stage_of(job_id), []).append(job_id)
    ordered = [s for s in stages if s in by_stage] \
        + [s for s in by_stage if s not in stages]
    nids = _unique_nids(in_ids, "j_")
    stage_nids = _unique_nids(ordered, "s_")
    has_depends = False
    for stage in ordered:
        lines.append(f'  subgraph {stage_nids[stage]}["{escape_label(stage)}"]')
        for job_id in by_stage[stage]:
            outcome = cand["jobs"][job_id]
            if outcome["state"] == "conditional":
                has_depends = True
            lines.append("    " + _dag_node(nids[job_id], outcome,
                                            meta["jobs"][job_id]))
        lines.append("  end")

    # boundary nodes for trigger jobs — the chain is not followed
    for job_id in in_ids:
        targets = _trigger_targets(meta["jobs"][job_id])
        targets = [t for t in targets if not t.startswith("no yaml variables")]
        if targets:
            nid = nids[job_id]
            label = escape_label("; ".join(_plain(t.replace("`", "")) for t in targets))
            lines.append(f'  b_{nid}(["▶ spawns {label}"])')
            lines.append(f"  {nid} --> b_{nid}")

    # only real needs: edges; stage columns carry the implicit ordering
    in_set = set(in_ids)
    for job_id in in_ids:
        whatif_job = meta["jobs"][job_id]
        prefix = (whatif_job.get("child_of") + "::") \
            if whatif_job.get("child_of") else ""
        for need in _effective_needs(cand["jobs"][job_id], whatif_job):
            if need.get("kind") in ("cross_pipeline", "cross_project"):
                continue
            target_name = re.sub(r":\s*\[.*\]$", "", need.get("job") or "")
            target_id = prefix + target_name
            if target_id in in_set:
                lines.append(f"  {nids[target_id]} --> {nids[job_id]}")

    if has_depends:
        lines.append("  classDef depends stroke-dasharray: 5 5;")
    return lines, False


def _fanout_lines(scenario: Scenario, cands: list[dict]) -> list[str]:
    lines = ["flowchart LR",
             f'  event(["{escape_label(_plain(_event_text(scenario).replace("`", "")))}"])']
    for cand in cands:
        nid = "pipe_" + re.sub(r"[^0-9A-Za-z_]", "_", cand["id"])
        if cand["created"] is False:
            label = f"{cand['label']} — not created"
        else:
            n = sum(1 for i in cand["jobOrder"] if might_run(cand["jobs"][i]))
            label = f"{cand['label']} — {n} jobs"
            if cand["creationFails"]:
                label = f"{cand['label']} — creation fails"
        lines.append(f'  event --> {nid}["{escape_label(label)}"]')
    return lines


# ---------------- mermaid: lifecycle sequence diagram ----------------

def _lifecycle_lines(scenario: Scenario, cand: dict, meta: dict) -> list[str]:
    in_ids = [i for i in _stage_sorted(cand["jobOrder"], meta)
              if might_run(cand["jobs"][i])]
    has_downstream = any(
        t for i in in_ids
        for t in _trigger_targets(meta["jobs"][i])
        if not t.startswith("no yaml variables"))
    lines = ["sequenceDiagram", "  actor Dev as Developer",
             "  participant GL as GitLab"]
    if has_downstream:
        lines.append("  participant DS as Downstream")
    event = _plain(_event_text(scenario).replace("`", ""))
    lines.append(f"  Dev->>GL: {event}")
    lines.append(f"  GL->>GL: create {cand['label']}")
    by_stage: dict[str, list[str]] = {}
    for job_id in in_ids:
        by_stage.setdefault(meta["jobs"][job_id].get("stage") or "", []).append(job_id)
    ordered = [s for s in meta["stages"] if s in by_stage] \
        + [s for s in by_stage if s not in meta["stages"]]
    for stage in ordered:
        auto = []
        for job_id in by_stage[stage]:
            outcome = cand["jobs"][job_id]
            name = _plain(meta["jobs"][job_id]["name"])
            if outcome["state"] == "manual":
                lines.append(f"  GL-->>Dev: wait — manual gate {name}")
                lines.append(f"  Dev->>GL: start {name}")
            elif outcome["state"] == "delayed":
                start = outcome.get("start_in")
                lines.append(f"  Note over GL: {name} starts after "
                             f"{start or 'its delay'}")
            else:
                auto.append(name + ("?" if outcome["state"] == "conditional" else ""))
        if auto:
            lines.append(f"  Note over GL: {stage}: {', '.join(auto)}")
        for job_id in by_stage[stage]:
            targets = [t for t in _trigger_targets(meta["jobs"][job_id])
                       if not t.startswith("no yaml variables")]
            if targets:
                spawn = _plain("; ".join(targets).replace("`", ""))
                lines.append(f"  GL->>DS: spawn {spawn}")
    return lines


# ---------------- per-candidate section ----------------

def _outcome_counts(cand: dict) -> dict:
    counts = {"runs": 0, "manual": 0, "delayed": 0, "depends": 0, "out": 0}
    for job_id in cand["jobOrder"]:
        state = cand["jobs"][job_id]["state"]
        if state == "runs":
            counts["runs"] += 1
        elif state == "manual":
            counts["manual"] += 1
        elif state == "delayed":
            counts["delayed"] += 1
        elif state == "conditional":
            counts["depends"] += 1
        else:
            counts["out"] += 1
    return counts


def _counts_text(counts: dict) -> str:
    bits = [f"**{counts['runs']} run**"]
    if counts["manual"]:
        bits.append(f"{counts['manual']} manual gate"
                    + ("s" if counts["manual"] != 1 else ""))
    if counts["delayed"]:
        bits.append(f"{counts['delayed']} delayed")
    if counts["depends"]:
        bits.append(f"{counts['depends']} depends")
    text = ", ".join(bits)
    if counts["out"]:
        text += f"; {counts['out']} not in this pipeline"
    return text


def _candidate_section(scenario: Scenario, cand: dict, meta: dict) -> list[str]:
    lines: list[str] = []
    ref = _candidate_ref(cand)
    lines.append(f"## {cand['label']} (`{ref}`)")
    lines.append("")

    if cand["created"] is False:
        lines.append(f"> **Not created** — {cand['reason']}.")
        lines.append("")
        return lines
    if cand["created"] is None:
        lines.append(f"> ⚠ **Creation uncertain** — {cand['reason']}.")
        lines.append("")
    if cand["creationFails"]:
        lines.append("> ⚠ **Pipeline creation fails** — GitLab refuses to create "
                     "this pipeline:")
        for err in cand["artifacts"]["errors"]:
            if err["kind"] != "trigger":
                lines.append(f"> - {_md_cell(err['message'])}")
        lines.append("")

    dag, collapsed = _dag_lines(cand, meta)
    lines.append("```mermaid")
    lines.extend(dag)
    lines.append("```")
    if collapsed:
        lines.append(f"*More than {GRAPH_JOB_LIMIT} jobs — the graph shows one "
                     f"node per stage; the table below lists every job.*")
    else:
        lines.append(_LEGEND)
    lines.append("")

    lines.append("| Job | Stage | Verdict | Why |")
    lines.append("|---|---|---|---|")
    in_rows = []
    out_rows = []
    for job_id in _stage_sorted(cand["jobOrder"], meta):
        outcome = cand["jobs"][job_id]
        whatif_job = meta["jobs"][job_id]
        is_trigger = bool(whatif_job.get("trigger"))
        verdict = _verdict_text(outcome, is_trigger)
        why = _trigger_why(whatif_job) if is_trigger and might_run(outcome) \
            else _why_text(outcome)
        row = (f"| {_md_cell(_code(whatif_job['name']))} "
               f"| {_md_cell(whatif_job.get('stage') or '')} "
               f"| {_md_cell(verdict)} | {_md_cell(why)} |")
        (in_rows if might_run(outcome) else out_rows).append(row)
    lines.extend(in_rows)
    lines.append("")

    if out_rows:
        lines.append("<details><summary>Jobs not in this pipeline "
                     f"({len(out_rows)})</summary>")
        lines.append("")
        lines.append("| Job | Stage | Verdict | Why not |")
        lines.append("|---|---|---|---|")
        lines.extend(out_rows)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # trigger-kind errors fail the trigger job, not the pipeline — but the
    # reader still needs them
    trigger_errors = [e for e in cand["artifacts"]["errors"]
                      if e["kind"] == "trigger"]
    for err in trigger_errors:
        lines.append(f"> ⚠ {_md_cell(err['message'])}")
    if trigger_errors:
        lines.append("")

    if "lifecycle" in scenario.diagrams:
        lines.append("```mermaid")
        lines.extend(_lifecycle_lines(scenario, cand, meta))
        lines.append("```")
        lines.append("")
    return lines


# ---------------- documents ----------------

def _provenance_comment(provenance: dict, scenario_id: str,
                        scenario_hash: str) -> str:
    return (f"<!-- {MARKER}: project={provenance.get('project') or '-'} "
            f"ref={provenance.get('ref') or '-'} "
            f"commit={provenance.get('commit') or '-'} "
            f"scenario={scenario_id} scenario-hash={scenario_hash or '-'} "
            f"pipeview={provenance.get('version') or pipeview.__version__} -->")


def _provenance_line(provenance: dict) -> str:
    origin = provenance.get("project") or "this configuration"
    if provenance.get("ref"):
        origin += f" @ {provenance['ref']}"
    commit = provenance.get("commit")
    short = f" ({commit[:10]})" if commit else ""
    return (f"*Generated by pipeview from `{origin}`{short} — "
            f"do not edit by hand.*")


def render_scenario_doc(report: dict, scenario: Scenario, evaluation: dict,
                        provenance: dict) -> str:
    meta = _job_meta(report)
    lines: list[str] = [f"# {scenario.title}", ""]
    lines.append(_provenance_comment(provenance, scenario.id,
                                     scenario.scenario_hash))
    lines.append(_provenance_line(provenance))
    lines.append("")
    if scenario.intro:
        lines.append(scenario.intro.strip())
        lines.append("")
    lines.append(_scenario_line(scenario))
    lines.append("")

    if evaluation.get("fatal"):
        lines.append("> ⚠ **Invalid configuration — GitLab refuses to create "
                     "any pipeline for any trigger:**")
        for entry in evaluation["fatal"]:
            message = entry.get("message") if isinstance(entry, dict) else str(entry)
            lines.append(f"> - {_md_cell(message)}")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    cands = evaluation["candidates"]
    lines.append("## Outcome")
    lines.append("")
    for cand in cands:
        ref = _candidate_ref(cand)
        if cand["created"] is False:
            lines.append(f"- **{cand['label']}** (`{ref}`): not created — "
                         f"{cand['reason']}.")
        else:
            counts = _outcome_counts(cand)
            total = len(cand["jobOrder"])
            lines.append(f"- **{cand['label']}** (`{ref}`): {total} jobs — "
                         + _counts_text(counts) + ".")
    dups = evaluation.get("duplicates") or []
    if dups:
        names = ", ".join(_code(meta["jobs"].get(d["job"], {}).get("name")
                                or d["job"]) for d in dups)
        lines.append("")
        lines.append(f"{len(dups)} job{'s' if len(dups) != 1 else ''} would run "
                     f"in more than one of these pipelines for the same event: "
                     f"{names}.")
    lines.append("")

    if len(cands) > 1:
        lines.append("```mermaid")
        lines.extend(_fanout_lines(scenario, cands))
        lines.append("```")
        lines.append("")

    for cand in cands:
        lines.extend(_candidate_section(scenario, cand, meta))

    return "\n".join(lines).rstrip() + "\n"


def render_index(report: dict, entries: list[tuple[Scenario, dict]],
                 skipped: list[str], provenance: dict,
                 regenerate_cmd: str) -> str:
    meta = _job_meta(report)
    whatif = meta["whatif"]
    lines = ["# Pipeline trigger docs", ""]
    lines.append(_provenance_comment(provenance, "-index-", ""))
    lines.append(_provenance_line(provenance))
    lines.append("")
    lines.append("What this project's pipelines do for each trigger scenario, "
                 "one doc per scenario.")
    lines.append("")
    default = whatif.get("default_branch")
    protected = whatif.get("protected_refs") or []
    if default:
        lines.append(f"*The simulated world assumes default branch `{default}` "
                     f"with protected branches "
                     + ", ".join(f"`{r}`" for r in protected)
                     + "; every other branch is a generic unprotected feature "
                       "branch.*")
        lines.append("")
    lines.append("| Scenario | Event | Pipelines | Jobs that run | Gates | Doc |")
    lines.append("|---|---|---|---|---|---|")
    for scenario, evaluation in entries:
        if evaluation.get("fatal"):
            lines.append(f"| {_md_cell(scenario.title)} | {scenario.event} "
                         f"| — | — | — | [{scenario.id}.md]({scenario.id}.md) "
                         f"(invalid configuration) |")
            continue
        cands = [c for c in evaluation["candidates"] if c["created"] is not False]
        running_ids = {job_id for c in cands for job_id in c["jobOrder"]
                       if might_run(c["jobs"][job_id])}
        gates = sum(1 for c in cands for job_id in c["jobOrder"]
                    if c["jobs"][job_id]["state"] == "manual")
        lines.append(f"| {_md_cell(scenario.title)} | {scenario.event} "
                     f"| {len(cands)} | {len(running_ids)} | {gates} "
                     f"| [{scenario.id}.md]({scenario.id}.md) |")
    lines.append("")
    if skipped:
        lines.append("## Skipped scenarios")
        lines.append("")
        lines.append("These scenario definitions could not be used — no doc "
                     "was generated for them:")
        lines.append("")
        for message in skipped:
            lines.append(f"- {_md_cell(message)}")
        lines.append("")
    lines.append("Regenerate these docs (they are never edited by hand):")
    lines.append("")
    lines.append("```")
    lines.append(regenerate_cmd)
    lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def generate_trigger_docs(report: dict, scenarios: list[Scenario],
                          skipped: list[str], provenance: dict,
                          regenerate_cmd: str) -> dict[str, str] | None:
    """Evaluate every scenario against the report and render the full doc
    set: {filename: content}. Returns None when the report carries no
    what-if program (not a GitLab CI configuration)."""
    entries: list[tuple[Scenario, dict]] = []
    for scenario in scenarios:
        evaluation = evaluate_event(report, to_whatif_config(scenario))
        if evaluation is None:
            return None
        entries.append((scenario, evaluation))
    files = {f"{scenario.id}.md": render_scenario_doc(report, scenario,
                                                      evaluation, provenance)
             for scenario, evaluation in entries}
    files[INDEX_NAME] = render_index(report, entries, skipped, provenance,
                                     regenerate_cmd)
    return files


def write_docs_folder(dirpath: str, files: dict[str, str]) -> list[Diagnostic]:
    """Write the doc set, replacing only what pipeview itself generated.

    Stale ``.md`` files carrying the provenance marker are deleted (a
    removed scenario must not leave a zombie doc); files without the
    marker are never deleted or overwritten — they get a warning and, on a
    name collision, win over the generated file."""
    diags: list[Diagnostic] = []
    os.makedirs(dirpath, exist_ok=True)
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(dirpath, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        if MARKER in head:
            if name not in files:
                os.remove(path)
        else:
            diags.append(Diagnostic(
                severity="warning",
                message=f"{path} is not a pipeview-generated doc — left "
                        f"untouched" + (" (a generated file wanted this name; "
                                        "rename one of them)"
                                        if name in files else "")))
            files = {k: v for k, v in files.items() if k != name}
    for name in sorted(files):
        with open(os.path.join(dirpath, name), "w", encoding="utf-8") as f:
            f.write(files[name])
    return diags
