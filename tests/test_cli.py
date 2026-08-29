import json
import os
import tempfile
from pathlib import Path

import pytest

from pipeview.cli import main
from pipeview.model import SCHEMA_VERSION

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"
GITHUB_FIXTURES = Path(__file__).parent / "fixtures" / "github"
EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestCliBasic:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        assert "pipeview" in capsys.readouterr().out

    def test_no_args(self):
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_nonexistent_path(self, tmpdir):
        code = main(["/nonexistent/path", "-o", tmpdir])
        assert code == 2


class TestCliMake:
    def test_single_makefile(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "minimal" / "Makefile"),
            "-o", tmpdir,
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.report.html"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.model.json"))

    def test_directory_discovery(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "minimal"),
            "-o", tmpdir,
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.report.html"))

    def test_all_formats(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "minimal" / "Makefile"),
            "-o", tmpdir,
            "--format", "html,json,svg,dot,mmd",
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.report.html"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.model.json"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.graph.svg"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.graph.dot"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.graph.mmd"))

    def test_broken_exits_1(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "broken" / "Makefile"),
            "-o", tmpdir,
        ])
        assert code == 1

    def test_no_enrich(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "minimal" / "Makefile"),
            "-o", tmpdir,
            "--no-enrich",
        ])
        assert code == 0

    def test_json_output_valid(self, tmpdir):
        main([
            str(MAKE_FIXTURES / "minimal" / "Makefile"),
            "-o", tmpdir,
        ])
        json_path = os.path.join(tmpdir, "Makefile.model.json")
        with open(json_path) as f:
            data = json.load(f)
        assert data["schema_version"] == SCHEMA_VERSION
        assert len(data["nodes"]) > 0
        assert data["format"] == "makefile"


class TestCliGitlab:
    def test_single_gitlab_file(self, tmpdir):
        code = main([
            str(GITLAB_FIXTURES / "minimal" / ".gitlab-ci.yml"),
            "-o", tmpdir,
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "gitlab-ci.report.html"))
        assert os.path.isfile(os.path.join(tmpdir, "gitlab-ci.model.json"))

    def test_directory_discovery_gitlab(self, tmpdir):
        code = main([
            str(GITLAB_FIXTURES / "minimal"),
            "-o", tmpdir,
        ])
        assert code == 0

    def test_yaml_error_exits_1(self, tmpdir):
        code = main([
            str(GITLAB_FIXTURES / "yaml_error" / ".gitlab-ci.yml"),
            "-o", tmpdir,
        ])
        assert code == 1

    def test_extends_ghost_exits_1(self, tmpdir):
        code = main([
            str(GITLAB_FIXTURES / "extends_chain" / ".gitlab-ci.yml"),
            "-o", tmpdir,
        ])
        assert code == 1


class TestExamples:
    def test_make_example(self, tmpdir):
        code = main([
            str(EXAMPLES / "make-project"),
            "-o", tmpdir,
            "--no-enrich",
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.report.html"))
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.model.json"))

    def test_gitlab_example(self, tmpdir):
        code = main([
            str(EXAMPLES / "gitlab-project"),
            "-o", tmpdir,
        ])
        assert code in (0, 1)
        assert os.path.isfile(os.path.join(tmpdir, "gitlab-ci.report.html"))
        assert os.path.isfile(os.path.join(tmpdir, "gitlab-ci.model.json"))

    def test_python_m_pipeview(self, tmpdir):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "pipeview",
             str(EXAMPLES / "make-project"), "-o", tmpdir, "--no-enrich"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert os.path.isfile(os.path.join(tmpdir, "Makefile.report.html"))


class TestCliGithub:
    def test_directory_discovery_github(self, tmpdir):
        code = main([
            str(GITHUB_FIXTURES / "minimal"),
            "-o", tmpdir,
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "github-actions.report.html"))
        data = json.loads(
            Path(tmpdir, "github-actions.model.json").read_text())
        assert data["format"] == "github_actions"

    def test_single_workflow_file(self, tmpdir):
        code = main([
            str(GITHUB_FIXTURES / "minimal" / ".github" / "workflows" / "ci.yml"),
            "-o", tmpdir,
        ])
        assert code == 0
        assert os.path.isfile(os.path.join(tmpdir, "ci_yml.report.html"))

    def test_workflow_outside_dot_github_is_sniffed(self, tmpdir):
        src = (GITHUB_FIXTURES / "minimal" / ".github" / "workflows"
               / "ci.yml").read_text()
        loose = Path(tmpdir, "loose.yml")
        loose.write_text(src)
        out = os.path.join(tmpdir, "out")
        code = main([str(loose), "-o", out])
        assert code == 0
        data = json.loads(Path(out, "loose_yml.model.json").read_text())
        assert data["format"] == "github_actions"

    def test_invalid_workflows_exit_1(self, tmpdir):
        code = main([
            str(GITHUB_FIXTURES / "invalid"),
            "-o", tmpdir,
        ])
        assert code == 1


class TestExitCodes:
    def test_clean_exit_0(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "minimal" / "Makefile"),
            "-o", tmpdir,
            "--no-enrich",
        ])
        assert code == 0

    def test_diagnostics_exit_1(self, tmpdir):
        code = main([
            str(MAKE_FIXTURES / "broken" / "Makefile"),
            "-o", tmpdir,
        ])
        assert code == 1

    def test_no_roots_exit_2(self, tmpdir):
        empty = tempfile.mkdtemp()
        code = main([empty, "-o", tmpdir])
        assert code == 2
