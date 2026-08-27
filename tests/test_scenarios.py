"""Scenario file loader: schema validation, degradation, and check lint.

The scenarios file is the input to trigger-docs generation (see
docs/superpowers/specs/2026-08-27-trigger-docs-design.md). The loader's
failure philosophy matches the parsers': one bad scenario degrades one
scenario, never the file; only file-level problems empty the result.
"""

import textwrap

from pipeview.scenarios import Scenario, load_scenarios

GOOD = textwrap.dedent("""\
    version: 1
    scenarios:
      - id: push-main
        title: Push to main
        intro: |
          What runs on every merge to main.
        event: push_branch
        branch: main
        variables: { DEPLOY: "1" }
      - id: release-tag
        event: push_tag
        tag: v1.2.3
      - id: mr-to-main
        event: mr
        target: main
        draft: false
        mr_flavor: merged_result
      - id: nightly
        event: schedule
        ref_kind: branch
        changed_files: [src/app.py]
        diagrams: [dag, lifecycle]
""")


def _load(tmp_path, text):
    p = tmp_path / "scenarios.yaml"
    p.write_text(text, encoding="utf-8")
    return load_scenarios(str(p))


def _errors(diags):
    return [d for d in diags if d.severity == "error"]


def _warnings(diags):
    return [d for d in diags if d.severity == "warning"]


def test_good_file_parses(tmp_path):
    scenarios, diags = _load(tmp_path, GOOD)
    assert [s.id for s in scenarios] == [
        "push-main", "release-tag", "mr-to-main", "nightly"]
    assert not _errors(diags)
    by_id = {s.id: s for s in scenarios}
    assert by_id["push-main"].title == "Push to main"
    assert "merge to main" in by_id["push-main"].intro
    assert by_id["push-main"].event == "push_branch"
    assert by_id["push-main"].config["branch"] == "main"
    assert by_id["push-main"].config["variables"] == {"DEPLOY": "1"}
    assert by_id["release-tag"].title == "release-tag"  # defaults from id
    assert by_id["nightly"].config["changed_files"] == ["src/app.py"]
    assert by_id["nightly"].diagrams == ["dag", "lifecycle"]
    assert by_id["push-main"].diagrams == ["dag"]  # the default


def test_scenario_source_points_at_its_stanza(tmp_path):
    scenarios, _ = _load(tmp_path, GOOD)
    by_id = {s.id: s for s in scenarios}
    assert by_id["push-main"].source.line < by_id["release-tag"].source.line
    assert by_id["push-main"].source.file.endswith("scenarios.yaml")


def test_hash_is_stable_and_tracks_definition(tmp_path):
    a, _ = _load(tmp_path, GOOD)
    b, _ = _load(tmp_path, GOOD)
    assert [s.scenario_hash for s in a] == [s.scenario_hash for s in b]
    changed, _ = _load(tmp_path, GOOD.replace('DEPLOY: "1"', 'DEPLOY: "2"'))
    assert changed[0].scenario_hash != a[0].scenario_hash
    assert changed[1].scenario_hash == a[1].scenario_hash


def test_missing_file_is_one_error(tmp_path):
    scenarios, diags = load_scenarios(str(tmp_path / "absent.yaml"))
    assert scenarios == []
    assert len(_errors(diags)) == 1


def test_yaml_error_is_one_error(tmp_path):
    scenarios, diags = _load(tmp_path, "version: 1\nscenarios: [unclosed")
    assert scenarios == []
    assert len(_errors(diags)) == 1


def test_version_is_required_and_checked(tmp_path):
    for text in ("scenarios: []\n", "version: 2\nscenarios: []\n"):
        scenarios, diags = _load(tmp_path, text)
        assert scenarios == []
        assert any("version" in d.message for d in _errors(diags))


def test_scenarios_must_be_a_list(tmp_path):
    scenarios, diags = _load(tmp_path, "version: 1\nscenarios: {a: 1}\n")
    assert scenarios == []
    assert _errors(diags)


def test_one_bad_scenario_degrades_only_itself(tmp_path):
    text = GOOD + "  - id: broken\n    event: no_such_event\n"
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == [
        "push-main", "release-tag", "mr-to-main", "nightly"]
    assert any("no_such_event" in d.message for d in _errors(diags))


def test_duplicate_id_skips_the_second(tmp_path):
    text = GOOD + "  - id: push-main\n    event: schedule\n"
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios].count("push-main") == 1
    assert any("duplicate" in d.message.lower() for d in _errors(diags))


def test_bad_id_and_missing_id_are_skipped(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: Bad_Id!
            event: schedule
          - event: schedule
          - id: fine
            event: schedule
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["fine"]
    assert len(_errors(diags)) == 2


def test_unknown_key_is_an_error_skip(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: typo
            event: push_branch
            brnach: main
    """)
    scenarios, diags = _load(tmp_path, text)
    assert scenarios == []
    assert any("brnach" in d.message for d in _errors(diags))


def test_inapplicable_key_warns_and_is_ignored(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: nightly
            event: schedule
            target: main
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["nightly"]
    assert "target" not in scenarios[0].config
    assert any("target" in d.message for d in _warnings(diags))
    assert not _errors(diags)


def test_mr_flavor_is_validated(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: mr
            event: mr
            mr_flavor: sideways
    """)
    scenarios, diags = _load(tmp_path, text)
    assert scenarios == []
    assert any("sideways" in d.message for d in _errors(diags))


def test_variable_map_types_are_checked(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: bad-vars
            event: schedule
            variables: [DEPLOY]
    """)
    scenarios, diags = _load(tmp_path, text)
    assert scenarios == []
    assert any("variables" in d.message for d in _errors(diags))


def test_variable_values_are_coerced_to_strings(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: coerce
            event: schedule
            variables: { COUNT: 3, FLAG: true }
    """)
    scenarios, diags = _load(tmp_path, text)
    assert scenarios[0].config["variables"] == {"COUNT": "3", "FLAG": "true"}


def test_open_mr_block_is_validated(tmp_path):
    good = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: push-mr
            event: push_branch
            branch: feature/x
            open_mr: { target: main, draft: true }
    """)
    scenarios, diags = _load(tmp_path, good)
    assert scenarios[0].config["open_mr"] == {"target": "main", "draft": True}
    bad = good.replace("target: main", "targt: main")
    scenarios, diags = _load(tmp_path, bad)
    assert scenarios == []
    assert any("targt" in d.message for d in _errors(diags))


def test_open_mr_carries_mr_flavor_and_labels(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: train
            event: push_branch
            branch: feature/x
            open_mr: { target: main, mr_flavor: merge_train,
                       mr_labels: [urgent, backend] }
          - id: bad-flavor
            event: push_branch
            open_mr: { mr_flavor: sideways }
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["train"]
    assert scenarios[0].config["open_mr"] == {
        "draft": False, "target": "main", "mr_flavor": "merge_train",
        "mr_labels": "urgent,backend"}
    from pipeview.scenarios import to_whatif_config
    config = to_whatif_config(scenarios[0])
    assert config["mrFlavor"] == "merge_train"
    assert config["mrLabels"] == "urgent,backend"
    assert any("sideways" in d.message for d in _errors(diags))


def test_branchy_tag_warns(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: odd-tag
            event: push_tag
            tag: main
          - id: fine-tag
            event: push_tag
            tag: v2.0.0
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["odd-tag", "fine-tag"]
    warns = _warnings(diags)
    assert len(warns) == 1 and "odd-tag" in warns[0].message


def test_predefined_variable_shadowing_warns(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: shadow
            event: schedule
            variables: { CI_COMMIT_BRANCH: main }
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["shadow"]
    assert any("CI_COMMIT_BRANCH" in d.message for d in _warnings(diags))


def test_unknown_diagram_warns_and_is_dropped(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: diag
            event: schedule
            diagrams: [dag, gantt]
    """)
    scenarios, diags = _load(tmp_path, text)
    assert scenarios[0].diagrams == ["dag"]
    assert any("gantt" in d.message for d in _warnings(diags))


def test_changed_files_all(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: everything
            event: push_branch
            changed_files: all
          - id: typo
            event: push_branch
            changed_files: alll
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["everything"]
    assert scenarios[0].config["changed_files"] == "all"
    assert any("literal `all`" in d.message for d in _errors(diags))
    from pipeview.scenarios import to_whatif_config
    assert to_whatif_config(scenarios[0])["changedFiles"] == "all"


def test_open_mr_on_refless_events(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: nightly-mr
            event: schedule
            branch: feature/x
            open_mr: { target: main }
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["nightly-mr"]
    assert not _warnings(diags) and not _errors(diags)
    assert scenarios[0].config["open_mr"] == {"target": "main", "draft": False}


def test_commit_message_key(tmp_path):
    text = textwrap.dedent("""\
        version: 1
        scenarios:
          - id: skip-ci
            event: push_branch
            commit_message: "chore: bump [skip ci]"
          - id: bad
            event: push_branch
            commit_message: [a, b]
    """)
    scenarios, diags = _load(tmp_path, text)
    assert [s.id for s in scenarios] == ["skip-ci"]
    assert scenarios[0].config["commit_message"] == "chore: bump [skip ci]"
    assert any("commit_message" in d.message for d in _errors(diags))


def test_scenario_records_are_plain_data(tmp_path):
    scenarios, _ = _load(tmp_path, GOOD)
    s = scenarios[0]
    assert isinstance(s, Scenario)
    assert isinstance(s.config, dict)
    assert len(s.scenario_hash) == 8
