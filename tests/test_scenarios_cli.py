"""`pipeview scenarios` sub-CLI: init / check / preview, and the routing
from the main entry point."""

import os
from pathlib import Path

from pipeview.cli import main
from pipeview.scenarios_cli import DEFAULT_FILENAME

TESTS = Path(__file__).parent
FIXDIR = TESTS / "fixtures" / "trigger_docs"
EXAMPLES = TESTS.parent / "examples"
SCENARIOS = str(FIXDIR / "scenarios.yaml")


class TestInit:
    def test_writes_default_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["scenarios", "init"]) == 0
        assert (tmp_path / DEFAULT_FILENAME).is_file()
        assert DEFAULT_FILENAME in capsys.readouterr().out

    def test_refuses_overwrite(self, tmp_path, capsys):
        target = tmp_path / "s.yaml"
        target.write_text("mine\n", encoding="utf-8")
        assert main(["scenarios", "init", str(target)]) == 2
        assert target.read_text() == "mine\n"
        assert "not overwriting" in capsys.readouterr().err

    def test_template_passes_check(self, tmp_path):
        target = str(tmp_path / "s.yaml")
        assert main(["scenarios", "init", target]) == 0
        assert main(["scenarios", "check", target]) == 0


class TestCheck:
    def test_clean_file(self, capsys):
        assert main(["scenarios", "check", SCENARIOS]) == 0
        assert "4 scenario(s) usable" in capsys.readouterr().out

    def test_warnings_exit_1(self, tmp_path, capsys):
        f = tmp_path / "s.yaml"
        f.write_text("version: 1\nscenarios:\n"
                     "  - id: odd\n    event: push_tag\n    tag: main\n",
                     encoding="utf-8")
        assert main(["scenarios", "check", str(f)]) == 1
        assert "looks like a branch name" in capsys.readouterr().err

    def test_unusable_exit_2(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text("scenarios: []\n", encoding="utf-8")
        assert main(["scenarios", "check", str(f)]) == 2
        assert main(["scenarios", "check", str(tmp_path / "absent.yaml")]) == 2


class TestPreview:
    def test_renders_all_docs(self, capsys):
        code = main(["scenarios", "preview", SCENARIOS,
                     str(EXAMPLES / "gitlab-whatif-project")])
        out = capsys.readouterr().out
        assert code == 0
        assert "===== push-main.md =====" in out
        assert "===== nightly.md =====" in out
        assert "## Outcome" in out

    def test_single_scenario(self, capsys):
        code = main(["scenarios", "preview", SCENARIOS,
                     str(EXAMPLES / "gitlab-whatif-project"),
                     "--scenario", "release-tag"])
        out = capsys.readouterr().out
        assert code == 0
        assert "===== release-tag.md =====" in out
        assert "push-main.md" not in out

    def test_unknown_scenario_exit_2(self, capsys):
        code = main(["scenarios", "preview", SCENARIOS,
                     str(EXAMPLES / "gitlab-whatif-project"),
                     "--scenario", "no-such"])
        assert code == 2
        assert "no scenario with id" in capsys.readouterr().err

    def test_non_gitlab_repo_exit_2(self, capsys):
        code = main(["scenarios", "preview", SCENARIOS,
                     str(TESTS / "fixtures" / "make" / "minimal")])
        assert code == 2
        assert "no GitLab CI configuration" in capsys.readouterr().err


class TestLocalTriggerDocs:
    def test_generates_docs_folder(self, tmp_path):
        outdir = str(tmp_path / "out")
        code = main([str(TESTS / "fixtures" / "gitlab" / "minimal"),
                     "-o", outdir, "--trigger-docs", SCENARIOS])
        assert code == 0
        docdir = os.path.join(outdir, "gitlab-ci.trigger-docs")
        names = sorted(os.listdir(docdir))
        assert names == ["nightly.md", "pipeline-triggers.md",
                         "push-feature-mr.md", "push-main.md",
                         "release-tag.md"]
        text = Path(docdir, "push-main.md").read_text(encoding="utf-8")
        assert "pipeview-trigger-doc" in text
        assert "```mermaid" in text
        # the report itself was still generated
        assert os.path.isfile(os.path.join(outdir, "gitlab-ci.report.html"))

    def test_rerun_removes_stale_scenario_docs(self, tmp_path):
        outdir = str(tmp_path / "out")
        fixture = str(TESTS / "fixtures" / "gitlab" / "minimal")
        assert main([fixture, "-o", outdir, "--trigger-docs", SCENARIOS]) == 0
        trimmed = tmp_path / "one.yaml"
        trimmed.write_text(
            "version: 1\nscenarios:\n  - id: push-main\n"
            "    event: push_branch\n    branch: main\n", encoding="utf-8")
        assert main([fixture, "-o", outdir,
                     "--trigger-docs", str(trimmed)]) == 0
        docdir = os.path.join(outdir, "gitlab-ci.trigger-docs")
        assert sorted(os.listdir(docdir)) == ["pipeline-triggers.md",
                                              "push-main.md"]

    def test_make_root_is_skipped_politely(self, tmp_path, capsys):
        outdir = str(tmp_path / "out")
        code = main([str(TESTS / "fixtures" / "make" / "minimal"), "-o", outdir,
                     "--trigger-docs", SCENARIOS])
        assert code == 0
        assert "apply to GitLab CI configurations" in capsys.readouterr().err
        assert not any(n.endswith(".trigger-docs")
                       for n in os.listdir(outdir))

    def test_unusable_scenarios_exit_1_but_report_generated(self, tmp_path,
                                                            capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("nope: [", encoding="utf-8")
        outdir = str(tmp_path / "out")
        code = main([str(EXAMPLES / "gitlab-whatif-project"), "-o", outdir,
                     "--trigger-docs", str(bad)])
        assert code == 1
        assert "trigger docs skipped" in capsys.readouterr().err
        assert os.path.isfile(os.path.join(outdir, "gitlab-ci.report.html"))
