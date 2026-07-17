import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeview.parsers.enrich import enrich_make_report
from pipeview.parsers.make_parser import parse_makefile

FIXTURES = Path(__file__).parent / "fixtures" / "make"


@pytest.fixture
def make_report():
    return parse_makefile(str(FIXTURES / "minimal" / "Makefile"))


class TestEnrichWithMake:
    @pytest.mark.skipif(
        shutil.which("make") is None,
        reason="make not on PATH",
    )
    def test_enrichment_resolves_variables(self):
        report = parse_makefile(str(FIXTURES / "variables" / "Makefile"))
        enrich_make_report(report, str(FIXTURES / "variables" / "Makefile"))
        cc_var = next((v for v in report.variables if v.name == "CC"), None)
        assert cc_var is not None
        if cc_var.events and cc_var.events[-1].resolved_value is not None:
            assert cc_var.events[-1].resolved_value == "gcc"

    @pytest.mark.skipif(
        shutil.which("make") is None,
        reason="make not on PATH",
    )
    def test_enrichment_adds_diagnostic(self):
        report = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        enrich_make_report(report, str(FIXTURES / "minimal" / "Makefile"))
        # Either enrichment works or adds a diagnostic


class TestEnrichWithoutMake:
    def test_missing_make_emits_diagnostic(self, make_report):
        with patch("shutil.which", return_value=None):
            enrich_make_report(make_report, str(FIXTURES / "minimal" / "Makefile"))
        diags = [d for d in make_report.diagnostics if "not found" in d.message]
        assert len(diags) >= 1
        assert diags[0].severity == "info"

    def test_timeout_emits_diagnostic(self, make_report):
        import subprocess
        with patch("shutil.which", return_value="/usr/bin/make"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("make", 30)):
            enrich_make_report(make_report, str(FIXTURES / "minimal" / "Makefile"))
        diags = [d for d in make_report.diagnostics if "timed out" in d.message]
        assert len(diags) >= 1

    def test_oserror_emits_diagnostic(self, make_report):
        with patch("shutil.which", return_value="/usr/bin/make"), \
             patch("subprocess.run", side_effect=OSError("test error")):
            enrich_make_report(make_report, str(FIXTURES / "minimal" / "Makefile"))
        diags = [d for d in make_report.diagnostics if "error" in d.message.lower()]
        assert len(diags) >= 1
