"""Rollup resolution pass: pure linking across parsed reports."""

from pipeview.gitlab.rollup import RollupSource, annotate_reports, build_rollup
from pipeview.parsers.gitlab_parser import parse_gitlab

HOST = "https://gitlab.example.com"


def _report(tmp_path, name, yaml_text, *, project, ref, entry=None,
            include_projects=None, lint_includes=None):
    d = tmp_path / name
    d.mkdir()
    root = d / ".gitlab-ci.yml"
    root.write_text(yaml_text)
    r = parse_gitlab(str(root))
    r.generated_at = "2026-08-25T12:00:00Z"
    r.tool_version = "test"
    r.annotations["gitlab_remote"] = {
        "host": HOST,
        "project": project,
        "project_name": project.rsplit("/", 1)[-1],
        "web_url": f"{HOST}/{project}",
        "ref": ref,
        "strategy": "lint",
        "lint_valid": True,
        "includes": lint_includes or [],
        "include_projects": include_projects or [],
    }
    return r


def _src(report, entry, html="x.report.html"):
    return RollupSource(entry=entry, report=report, report_html=html)


class TestTriggerLinks:
    def test_clean_link_to_bare_entry(self, tmp_path):
        up = _report(tmp_path, "up", (
            "fan-out:\n  trigger: group/infra\n"
        ), project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="master")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(down, "group/infra")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        assert link["src"] == {"project": 0, "node": "fan-out",
                               "file": ".gitlab-ci.yml", "line": 1,
                               "ghost": "downstream:group/infra"}
        assert link["dst"] == {"project": 1, "path": "group/infra",
                               "ref": None}
        assert link["caveats"] == []

    def test_explicit_branch_matching_pinned_entry(self, tmp_path):
        up = _report(tmp_path, "up", (
            "fan-out:\n"
            "  trigger:\n"
            "    project: group/infra\n"
            "    branch: prod\n"
            "    strategy: mirror\n"
        ), project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="prod")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(down, "group/infra@prod")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        assert link["dst"]["project"] == 1
        assert link["strategy"] == "mirror"
        assert link["caveats"] == []

    def test_ref_mismatch_carries_caveat(self, tmp_path):
        up = _report(tmp_path, "up", (
            "fan-out:\n"
            "  trigger:\n"
            "    project: group/infra\n"
            "    branch: prod\n"
        ), project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="v2")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(down, "group/infra@v2")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        assert link["dst"]["project"] == 1
        assert link["caveats"] == ["ref mismatch: targets 'prod', "
                                   "tracked at 'v2'"]

    def test_refless_trigger_at_pinned_entry_warns(self, tmp_path):
        up = _report(tmp_path, "up", "fan-out:\n  trigger: group/infra\n",
                     project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="v2")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(down, "group/infra@v2")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        assert link["dst"]["project"] == 1
        assert link["caveats"] == ["targets the default branch; "
                                   "tracked pinned at 'v2'"]

    def test_refless_trigger_with_known_default_branch(self, tmp_path):
        # infra tracked twice: bare (resolves the default branch) + pinned.
        up = _report(tmp_path, "up", "fan-out:\n  trigger: group/infra\n",
                     project="group/app", ref="main")
        down_main = _report(tmp_path, "dm", "build:\n  script: [echo]\n",
                            project="group/infra", ref="master")
        down_pin = _report(tmp_path, "dp", "build:\n  script: [echo]\n",
                           project="group/infra", ref="v2")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"),
            _src(down_main, "group/infra"),
            _src(down_pin, "group/infra@v2")])
        trigger_links = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        by_dst = {ln["dst"]["project"]: ln for ln in trigger_links}
        assert by_dst[1]["caveats"] == []
        assert by_dst[2]["caveats"] == [
            "targets the default branch ('master'), tracked at 'v2'"]

    def test_untracked_target_is_external(self, tmp_path):
        up = _report(tmp_path, "up", "fan-out:\n  trigger: group/other\n",
                     project="group/app", ref="main")
        rollup = build_rollup(HOST, [_src(up, "group/app")])
        [link] = rollup["links"]
        assert link["dst"] == {"project": None, "path": "group/other",
                               "ref": None}
        assert "project is not tracked" in link["caveats"]
        [ext] = rollup["externals"]
        assert ext["path"] == "group/other"
        assert ext["kinds"] == ["trigger"]
        assert any("not tracked" in d["message"]
                   for d in rollup["diagnostics"])

    def test_variable_project_stays_unresolved(self, tmp_path):
        up = _report(tmp_path, "up", (
            "fan-out:\n"
            "  trigger:\n"
            "    project: $GROUP/app\n"
        ), project="group/app", ref="main")
        rollup = build_rollup(HOST, [_src(up, "group/app")])
        [link] = rollup["links"]
        assert link["dst"]["project"] is None
        assert "project uses CI variables" in link["caveats"]
        assert "project is not tracked" not in link["caveats"]

    def test_case_insensitive_project_match(self, tmp_path):
        up = _report(tmp_path, "up", "fan-out:\n  trigger: Group/Infra\n",
                     project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="master")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(down, "group/infra")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "trigger"]
        assert link["dst"]["project"] == 1


class TestOtherLinkKinds:
    def test_needs_project_link(self, tmp_path):
        up = _report(tmp_path, "up", (
            "use:\n"
            "  script: [echo]\n"
            "  needs:\n"
            "    - project: group/lib\n"
            "      job: build\n"
            "      ref: master\n"
            "      artifacts: true\n"
        ), project="group/app", ref="main")
        lib = _report(tmp_path, "lib", "build:\n  script: [echo]\n",
                      project="group/lib", ref="master")
        rollup = build_rollup(HOST, [
            _src(up, "group/app"), _src(lib, "group/lib")])
        [link] = [ln for ln in rollup["links"] if ln["kind"] == "needs_project"]
        assert link["dst"] == {"project": 1, "path": "group/lib",
                               "ref": "master"}
        assert link["job"] == "build"
        assert link["src"]["ghost"] == "group/lib::build"
        assert link["caveats"] == []

    def test_include_links_from_both_strategies(self, tmp_path):
        a = _report(tmp_path, "a", "j:\n  script: [echo]\n",
                    project="group/a", ref="main",
                    include_projects=[{"project": "group/lib", "ref": None,
                                       "file": "ci/shared.yml"}])
        b = _report(tmp_path, "b", "j:\n  script: [echo]\n",
                    project="group/b", ref="main",
                    lint_includes=[{"type": "file",
                                    "location": "ci/shared.yml",
                                    "extra": {"project": "group/lib",
                                              "ref": "main"}}])
        lib = _report(tmp_path, "lib", "j:\n  script: [echo]\n",
                      project="group/lib", ref="main")
        rollup = build_rollup(HOST, [
            _src(a, "group/a"), _src(b, "group/b"), _src(lib, "group/lib")])
        inc = [ln for ln in rollup["links"] if ln["kind"] == "include"]
        assert {(ln["src"]["project"], ln["dst"]["project"]) for ln in inc} \
            == {(0, 2), (1, 2)}
        assert all(ln["file"] == "ci/shared.yml" for ln in inc)

    def test_self_include_ignored(self, tmp_path):
        a = _report(tmp_path, "a", "j:\n  script: [echo]\n",
                    project="group/a", ref="main",
                    include_projects=[{"project": "group/a", "ref": None,
                                       "file": "ci/own.yml"}])
        rollup = build_rollup(HOST, [_src(a, "group/a")])
        assert [ln for ln in rollup["links"] if ln["kind"] == "include"] == []


class TestRollupDocument:
    def test_project_summaries(self, tmp_path):
        a = _report(tmp_path, "a", (
            "stages: [s1]\n"
            ".tmpl:\n  script: [echo]\n"
            "j:\n  stage: s1\n  script: [echo]\n"
        ), project="group/a", ref="main")
        rollup = build_rollup(HOST, [_src(a, "group/a", html="a.report.html")])
        [p] = rollup["projects"]
        assert p["project"] == "group/a"
        assert p["ref"] == "main"
        assert p["report_html"] == "a.report.html"
        assert p["counts"]["jobs"] == 1        # template excluded
        assert p["counts"]["stages"] >= 1
        assert p["model"]["nodes"]             # full model embedded
        assert rollup["host"] == HOST
        assert rollup["schema_version"] == 1

    def test_missing_entries_diagnostic(self, tmp_path):
        a = _report(tmp_path, "a", "j:\n  script: [echo]\n",
                    project="group/a", ref="main")
        rollup = build_rollup(HOST, [_src(a, "group/a")],
                              missing_entries=["group/broken"])
        assert any("group/broken" in d["message"]
                   for d in rollup["diagnostics"])


class TestAnnotateReports:
    def test_resolved_nodes_gain_rollup_link(self, tmp_path):
        up = _report(tmp_path, "up", "fan-out:\n  trigger: group/infra\n",
                     project="group/app", ref="main")
        down = _report(tmp_path, "down", "build:\n  script: [echo]\n",
                       project="group/infra", ref="master")
        sources = [_src(up, "group/app"), _src(down, "group/infra")]
        rollup = build_rollup(HOST, sources)
        touched = annotate_reports(rollup, sources, "rollup.report.html")
        assert touched == {0, 1}   # both get the report-level marker
        trig = up.node_by_id("fan-out")
        ghost = up.node_by_id("downstream:group/infra")
        expected = {"project": "group/infra", "entry": "group/infra",
                    "rollup": "rollup.report.html"}
        assert trig.annotations["rollup_link"] == expected
        assert ghost.annotations["rollup_link"] == expected
        assert up.annotations["rollup"] == {"file": "rollup.report.html"}

    def test_unresolved_nodes_untouched(self, tmp_path):
        up = _report(tmp_path, "up", "fan-out:\n  trigger: group/other\n",
                     project="group/app", ref="main")
        sources = [_src(up, "group/app")]
        rollup = build_rollup(HOST, sources)
        annotate_reports(rollup, sources, "rollup.report.html")
        assert "rollup_link" not in up.node_by_id("fan-out").annotations
