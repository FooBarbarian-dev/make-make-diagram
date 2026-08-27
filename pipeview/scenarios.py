"""Trigger-docs scenario file: schema and loader.

A scenario is a named What-If configuration — the same knobs the report's
What-If tab exposes, spelled in snake_case, keyed by an id that becomes the
generated doc's filename (see
docs/superpowers/specs/2026-08-27-trigger-docs-design.md).

Failure philosophy matches the parsers: one bad scenario degrades one
scenario (a diagnostic plus a skip), never the file; only file-level
problems (unreadable, YAML error, bad version) empty the result. Keys that
exist but don't apply to the chosen event are warned about and ignored —
they read like copy-paste slips, not typos — while keys unknown to every
event are treated as typos and fail the scenario.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from pipeview.model import Diagnostic, SourceLocation
from pipeview.parsers.gitlab_predefined import PREDEFINED_VAR_DOCS

SCENARIOS_SCHEMA_VERSION = 1

# The whatif.js scenario ids, verbatim.
EVENTS = frozenset({
    "push_branch", "push_tag", "mr", "schedule", "web", "api", "trigger",
})

_COMMON_KEYS = frozenset({
    "id", "title", "intro", "event", "variables", "changed_files", "diagrams",
    "commit_message",
})
# schedule/web/api/trigger run on a branch or tag; open_mr is meaningful
# there too — CI_OPEN_MERGE_REQUESTS is set in EVERY branch pipeline whose
# branch has an open MR (the documented dedup pattern relies on it)
_REFLESS_EVENT_KEYS = frozenset({"ref_kind", "branch", "tag", "open_mr"})
_EVENT_KEYS: dict[str, frozenset[str]] = {
    "push_branch": frozenset({"branch", "open_mr", "new_branch"}),
    "push_tag": frozenset({"tag", "tag_protected"}),
    "mr": frozenset({"branch", "target", "draft", "mr_flavor", "mr_labels"}),
    "schedule": _REFLESS_EVENT_KEYS,
    "web": _REFLESS_EVENT_KEYS,
    "api": _REFLESS_EVENT_KEYS,
    "trigger": _REFLESS_EVENT_KEYS,
}
_ALL_KEYS = _COMMON_KEYS.union(*_EVENT_KEYS.values())

_OPEN_MR_KEYS = frozenset({"target", "draft"})
_MR_FLAVORS = frozenset({"detached", "merged_result", "merge_train"})
_REF_KINDS = frozenset({"branch", "tag"})
_DIAGRAMS = ("dag", "lifecycle")

_ID_RE = re.compile(r"^[a-z0-9-]+$")

# Tags that read like branch names — the What-If simulator's well-known
# branches plus the usual long-lived suspects; a `/` is a branch-path smell.
_BRANCHY_TAGS = frozenset({"main", "master", "dev", "develop", "trunk"})


@dataclass
class Scenario:
    id: str
    event: str
    title: str
    intro: str
    config: dict[str, Any]
    diagrams: list[str] = field(default_factory=lambda: ["dag"])
    scenario_hash: str = ""
    source: SourceLocation | None = None


def to_whatif_config(scenario: Scenario) -> dict[str, Any]:
    """Spell a Scenario as the What-If evaluator's config object — the
    camelCase knobs whatif.js and gitlab_whatif_eval share."""
    c = scenario.config
    out: dict[str, Any] = {"scenario": scenario.event}
    if "branch" in c:
        out["branch"] = c["branch"]
    if "tag" in c:
        out["tag"] = c["tag"]
    if "ref_kind" in c:
        out["refKind"] = c["ref_kind"]
    if "new_branch" in c:
        out["newBranch"] = bool(c["new_branch"])
    if "tag_protected" in c:
        out["tagProtected"] = bool(c["tag_protected"])
    if "target" in c:
        out["target"] = c["target"]
    if "draft" in c:
        out["draft"] = bool(c["draft"])
    if "mr_flavor" in c:
        out["mrFlavor"] = c["mr_flavor"]
    if "mr_labels" in c:
        labels = c["mr_labels"]
        out["mrLabels"] = ",".join(str(v) for v in labels) \
            if isinstance(labels, list) else str(labels)
    if "open_mr" in c:
        out["openMR"] = True
        if c["open_mr"].get("target"):
            out["target"] = c["open_mr"]["target"]
        out["draft"] = c["open_mr"].get("draft", False)
    if "changed_files" in c:
        out["changedFiles"] = "all" if c["changed_files"] == "all" \
            else list(c["changed_files"])
    if "commit_message" in c:
        out["commitMessage"] = c["commit_message"]
    if "variables" in c:
        out["overrides"] = dict(c["variables"])
    return out


def _hash_stanza(stanza: dict) -> str:
    canonical = json.dumps(stanza, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _stanza_lines(text: str) -> list[int]:
    """1-based start line of each entry in the top-level `scenarios:` list,
    via a compose pass over the same text safe_load parsed."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    for key_node, value_node in root.value:
        if getattr(key_node, "value", None) == "scenarios" and \
                isinstance(value_node, yaml.SequenceNode):
            return [item.start_mark.line + 1 for item in value_node.value]
    return []


def _as_string_map(value: Any) -> dict[str, str] | None:
    """Coerce a YAML mapping of scalars into {str: str}; None if it isn't one.
    Booleans become GitLab-style lowercase true/false."""
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for k, v in value.items():
        if isinstance(v, (dict, list)):
            return None
        if isinstance(v, bool):
            v = "true" if v else "false"
        out[str(k)] = "" if v is None else str(v)
    return out


def _as_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(isinstance(v, (dict, list)) for v in value):
        return None
    return [str(v) for v in value]


def load_scenarios(path: str) -> tuple[list[Scenario], list[Diagnostic]]:
    """Parse a scenarios file into records plus diagnostics."""
    diags: list[Diagnostic] = []
    file_loc = SourceLocation(file=path, line=1)

    def file_error(message: str) -> tuple[list[Scenario], list[Diagnostic]]:
        diags.append(Diagnostic(severity="error", message=message, source=file_loc))
        return [], diags

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return file_error(f"Cannot read scenarios file {path}: {e}")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return file_error(f"Scenarios file {path} is not valid YAML: {e}")

    if not isinstance(data, dict):
        return file_error(f"Scenarios file {path} must be a mapping at the top level")
    version = data.get("version")
    if version != SCENARIOS_SCHEMA_VERSION:
        return file_error(
            f"Scenarios file {path} needs `version: {SCENARIOS_SCHEMA_VERSION}` "
            f"(found {version!r})")
    raw_list = data.get("scenarios")
    if not isinstance(raw_list, list):
        return file_error(f"Scenarios file {path} needs a `scenarios:` list")

    lines = _stanza_lines(text)
    scenarios: list[Scenario] = []
    seen_ids: set[str] = set()

    for index, stanza in enumerate(raw_list):
        line = lines[index] if index < len(lines) else 1
        loc = SourceLocation(file=path, line=line)
        label = f"scenario #{index + 1}"

        def error(message: str, loc=loc) -> None:
            diags.append(Diagnostic(severity="error", message=message, source=loc))

        def warn(message: str, loc=loc) -> None:
            diags.append(Diagnostic(severity="warning", message=message, source=loc))

        if not isinstance(stanza, dict):
            error(f"{label}: each scenario must be a mapping")
            continue

        sid = stanza.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            error(f"{label}: `id` is required and must match [a-z0-9-]+ "
                  f"(found {sid!r})")
            continue
        label = f"scenario '{sid}'"
        if sid in seen_ids:
            error(f"{label}: duplicate id — this stanza is skipped")
            continue

        event = stanza.get("event")
        if event not in EVENTS:
            error(f"{label}: `event` must be one of {', '.join(sorted(EVENTS))} "
                  f"(found {event!r})")
            continue

        unknown = sorted(set(stanza) - _ALL_KEYS)
        if unknown:
            error(f"{label}: unknown key(s): {', '.join(unknown)}")
            continue
        inapplicable = sorted(set(stanza) - _COMMON_KEYS - _EVENT_KEYS[event])
        for key in inapplicable:
            warn(f"{label}: `{key}` does not apply to {event} scenarios — ignored")

        config: dict[str, Any] = {}
        for key in sorted(set(stanza) & _EVENT_KEYS[event]):
            value = stanza[key]
            if key in ("branch", "tag", "target") and value is not None \
                    and not isinstance(value, (dict, list)):
                value = str(value)
            config[key] = value

        if "mr_flavor" in config and config["mr_flavor"] not in _MR_FLAVORS:
            error(f"{label}: `mr_flavor` must be one of "
                  f"{', '.join(sorted(_MR_FLAVORS))} (found {config['mr_flavor']!r})")
            continue
        if "ref_kind" in config and config["ref_kind"] not in _REF_KINDS:
            error(f"{label}: `ref_kind` must be `branch` or `tag` "
                  f"(found {config['ref_kind']!r})")
            continue
        if "open_mr" in config:
            open_mr = config["open_mr"]
            if not isinstance(open_mr, dict):
                error(f"{label}: `open_mr` must be a mapping")
                continue
            bad_keys = sorted(set(open_mr) - _OPEN_MR_KEYS)
            if bad_keys:
                error(f"{label}: unknown key(s) in `open_mr`: {', '.join(bad_keys)}")
                continue
            config["open_mr"] = {
                "target": str(open_mr["target"]) if "target" in open_mr else None,
                "draft": bool(open_mr.get("draft", False)),
            }
            if config["open_mr"]["target"] is None:
                del config["open_mr"]["target"]

        if "variables" in stanza:
            variables = _as_string_map(stanza["variables"])
            if variables is None:
                error(f"{label}: `variables` must be a mapping of scalars")
                continue
            config["variables"] = variables
            for name in sorted(set(variables) & set(PREDEFINED_VAR_DOCS)):
                warn(f"{label}: variable `{name}` shadows a GitLab predefined "
                     f"variable — the scenario value wins in the simulation")
        if "changed_files" in stanza:
            if stanza["changed_files"] == "all":
                # the What-If tab's third state: every changes: pattern matches
                config["changed_files"] = "all"
            else:
                changed = _as_string_list(stanza["changed_files"])
                if changed is None:
                    error(f"{label}: `changed_files` must be a list of paths, "
                          f"or the literal `all`")
                    continue
                config["changed_files"] = changed
        if "commit_message" in stanza:
            message = stanza["commit_message"]
            if isinstance(message, (dict, list)):
                error(f"{label}: `commit_message` must be a string")
                continue
            config["commit_message"] = "" if message is None else str(message)

        diagrams = ["dag"]
        if "diagrams" in stanza:
            raw = stanza["diagrams"]
            if not isinstance(raw, list):
                error(f"{label}: `diagrams` must be a list")
                continue
            diagrams = []
            for entry in raw:
                if entry in _DIAGRAMS and entry not in diagrams:
                    diagrams.append(entry)
                elif entry not in _DIAGRAMS:
                    warn(f"{label}: unknown diagram kind {entry!r} — "
                         f"expected one of {', '.join(_DIAGRAMS)}; dropped")
            if not diagrams:
                diagrams = ["dag"]

        if event == "push_tag":
            tag = config.get("tag")
            if isinstance(tag, str) and ("/" in tag or tag in _BRANCHY_TAGS):
                warn(f"{label}: tag {tag!r} looks like a branch name — "
                     f"push_tag simulates a tag push")

        title = stanza.get("title")
        scenarios.append(Scenario(
            id=sid,
            event=event,
            title=title if isinstance(title, str) and title else sid,
            intro=stanza.get("intro") or "",
            config=config,
            diagrams=diagrams,
            scenario_hash=_hash_stanza(stanza),
            source=loc,
        ))
        seen_ids.add(sid)

    return scenarios, diags
