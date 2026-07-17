import json
import os
import tempfile
from pathlib import Path

import pytest

from pipeview.cli import main

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"
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
        assert data["schema_version"] == 2
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
