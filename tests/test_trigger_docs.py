"""Trigger-docs renderer: golden docs, determinism, hostile inputs, the
graph guardrail, marker-based folder hygiene, and the offline guarantee.

Regenerate the golden files after an intended output change with:
    UPDATE_GOLDEN=1 python -m pytest tests/test_trigger_docs.py -k golden
then review the diff like any other code change.
"""

import os
import re
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif_eval import evaluate_event
from pipeview.render.trigger_docs import (
    GRAPH_JOB_LIMIT,
    MARKER,
    generate_trigger_docs,
    render_scenario_doc,
    write_docs_folder,
)
from pipeview.scenarios import load_scenarios, to_whatif_config

TESTS = Path(__file__).parent
REPO = TESTS.parent
FIXDIR = TESTS / "fixtures" / "trigger_docs"
EXPECTED = FIXDIR / "expected"

PROV = {"project": "group/app", "ref": "main", "commit": "abc1234def5678",
        "version": "0.0.0-test"}
CMD = ("pipeview examples/gitlab-whatif-project --trigger-docs "
       "tests/fixtures/trigger_docs/scenarios.yaml")


def _generate() -> dict[str, str]:
    scenarios, diags = load_scenarios(str(FIXDIR / "scenarios.yaml"))
    assert not [d for d in diags if d.severity == "error"]
    report = parse_gitlab(
        str(REPO / "examples" / "gitlab-whatif-project" / ".gitlab-ci.yml")
    ).to_dict()
    files = generate_trigger_docs(report, scenarios, [], PROV, CMD)
    assert files is not None
    return files


def test_golden_docs():
    files = _generate()
    if os.environ.get("UPDATE_GOLDEN"):
        EXPECTED.mkdir(parents=True, exist_ok=True)
        for stale in EXPECTED.glob("*.md"):
            stale.unlink()
        for name, text in files.items():
            (EXPECTED / name).write_text(text, encoding="utf-8")
        pytest.skip("golden files rewritten — review the diff")
    expected_names = sorted(p.name for p in EXPECTED.glob("*.md"))
    assert sorted(files) == expected_names
    for name in expected_names:
        assert files[name] == (EXPECTED / name).read_text(encoding="utf-8"), \
            f"{name} drifted from its golden file (UPDATE_GOLDEN=1 regenerates)"


def test_generation_is_deterministic():
    assert _generate() == _generate()


def test_no_network_references():
    # committed docs must honor the offline guarantee like the HTML reports
    for name, text in _generate().items():
        assert "http://" not in text and "https://" not in text, name


def test_every_doc_carries_the_marker():
    for name, text in _generate().items():
        assert MARKER in text.splitlines()[2], name


def _table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines()
            if line.startswith("|") and not set(line) <= {"|", "-", " "}]


def test_hostile_job_names_survive():
    report = parse_gitlab(
        str(TESTS / "fixtures" / "gitlab" / "hostile_names" / ".gitlab-ci.yml")
    ).to_dict()
    scenarios, diags = load_scenarios(str(FIXDIR / "scenarios.yaml"))
    scenario = next(s for s in scenarios if s.id == "push-main")
    evaluation = evaluate_event(report, to_whatif_config(scenario))
    text = render_scenario_doc(report, scenario, evaluation, PROV)

    # markdown tables keep their column count — raw pipes must be escaped
    for row in _table_rows(text):
        assert len(re.findall(r"(?<!\\)\|", row)) == 5, row

    in_mermaid = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_mermaid = line == "```mermaid"
            continue
        if not in_mermaid:
            continue
        # mermaid's `end` keyword may appear only as the subgraph closer
        stripped = line.strip()
        if stripped == "end":
            continue
        # every node/edge identifier is sanitized: no bare brackets/quotes
        # outside a quoted label, no identifier that is just `end`
        for ident in re.findall(r"^\s*([A-Za-z_][\w]*)", line):
            assert ident not in ("end", "graph", "subgraph") or \
                stripped.startswith("subgraph "), line
        for label in re.findall(r'"([^"]*)"', line):
            assert "[" not in label and "]" not in label, line


def test_graph_guardrail_collapses_to_stages(tmp_path):
    jobs = "\n".join(
        f"job_{i:02d}:\n  stage: {'build' if i % 2 else 'test'}\n"
        f"  script: [echo {i}]"
        for i in range(GRAPH_JOB_LIMIT + 1))
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(f"stages: [build, test]\n{jobs}\n", encoding="utf-8")
    report = parse_gitlab(str(ci)).to_dict()
    scenarios, _ = load_scenarios(str(FIXDIR / "scenarios.yaml"))
    scenario = next(s for s in scenarios if s.id == "push-main")
    evaluation = evaluate_event(report, to_whatif_config(scenario))
    text = render_scenario_doc(report, scenario, evaluation, PROV)
    assert "the graph shows one node per stage" in text
    assert "subgraph" not in text
    assert re.search(r'"build — \d+ jobs"', text)
    # the table still lists every job
    assert sum(1 for r in _table_rows(text) if "job_" in r) == GRAPH_JOB_LIMIT + 1


def test_fatal_configuration_renders_honestly():
    report = parse_gitlab(
        str(TESTS / "fixtures" / "gitlab" / "whatif_invalid" / ".gitlab-ci.yml")
    ).to_dict()
    scenarios, _ = load_scenarios(str(FIXDIR / "scenarios.yaml"))
    scenario = next(s for s in scenarios if s.id == "push-main")
    evaluation = evaluate_event(report, to_whatif_config(scenario))
    if not evaluation.get("fatal"):
        pytest.skip("fixture no longer produces a fatal configuration")
    text = render_scenario_doc(report, scenario, evaluation, PROV)
    assert "Invalid configuration" in text
    assert "## Outcome" not in text


def test_write_docs_folder_hygiene(tmp_path):
    docdir = tmp_path / "trigger-docs"
    docdir.mkdir()
    (docdir / "stale.md").write_text(
        f"<!-- {MARKER}: scenario=stale -->\nold\n", encoding="utf-8")
    (docdir / "human.md").write_text("my notes\n", encoding="utf-8")
    (docdir / "collide.md").write_text("more notes\n", encoding="utf-8")

    files = {"fresh.md": f"<!-- {MARKER}: scenario=fresh -->\nnew\n",
             "collide.md": f"<!-- {MARKER}: scenario=collide -->\nnew\n"}
    diags = write_docs_folder(str(docdir), files)

    assert not (docdir / "stale.md").exists()          # zombie removed
    assert (docdir / "fresh.md").read_text() == files["fresh.md"]
    assert (docdir / "human.md").read_text() == "my notes\n"
    assert (docdir / "collide.md").read_text() == "more notes\n"  # human wins
    warnings = [d for d in diags if d.severity == "warning"]
    assert any("human.md" in d.message for d in warnings)
    assert any("collide.md" in d.message and "wanted this name" in d.message
               for d in warnings)


def test_rerun_is_idempotent_on_disk(tmp_path):
    docdir = tmp_path / "trigger-docs"
    files = _generate()
    assert write_docs_folder(str(docdir), files) == []
    assert write_docs_folder(str(docdir), files) == []
    on_disk = sorted(p.name for p in docdir.glob("*.md"))
    assert on_disk == sorted(files)
