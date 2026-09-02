"""Tests for pipeview.browser — opening reports/URLs per platform, and
the WSL interop fallback. No test launches a real browser."""

from __future__ import annotations

import os
import subprocess

import pytest

from pipeview import browser


@pytest.fixture
def launches(monkeypatch):
    """Record subprocess.run calls and script their results."""
    calls: list[list[str]] = []
    results: dict[str, int] = {}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        exe = os.path.basename(argv[0])
        rc = results.get(exe, 0)
        out = "\\\\wsl.localhost\\Ubuntu\\tmp\\x.report.html" \
            if exe == "wslpath" else ""
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    calls_results = (calls, results)
    return calls_results


def test_to_uri_paths_and_urls(tmp_path):
    p = tmp_path / "a b#c.report.html"
    uri = browser.to_uri(str(p))
    assert uri.startswith("file:///")
    assert "a%20b%23c.report.html" in uri
    assert browser.to_uri("https://x.example/y?z=1") == "https://x.example/y?z=1"
    assert browser.to_uri("file:///already/a/uri") == "file:///already/a/uri"


def test_is_wsl_detection(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)

    class U:
        release = "5.15.167.4-microsoft-standard-WSL2"
    monkeypatch.setattr(browser.platform, "uname", lambda: U())
    assert browser.is_wsl()

    U.release = "6.8.0-45-generic"
    assert not browser.is_wsl()
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert browser.is_wsl()
    # never on native Windows, whatever the env says
    monkeypatch.setattr(os, "name", "nt")
    assert not browser.is_wsl()


def test_plain_platform_uses_webbrowser(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "is_wsl", lambda: False)
    opened = []
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url: opened.append(url) or True)
    p = tmp_path / "r.report.html"
    assert browser.open_in_browser(str(p))
    assert opened == [p.resolve().as_uri()]


def test_webbrowser_failure_is_false_not_exception(monkeypatch):
    monkeypatch.setattr(browser, "is_wsl", lambda: False)
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: False)
    assert browser.open_in_browser("https://x.example") is False

    def boom(url):
        raise RuntimeError("no display")
    monkeypatch.setattr(browser.webbrowser, "open", boom)
    assert browser.open_in_browser("https://x.example") is False


def test_wsl_prefers_wslview(monkeypatch, launches, tmp_path):
    calls, _ = launches
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.shutil, "which",
                        lambda n: "/usr/bin/wslview" if n == "wslview" else None)
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url: pytest.fail("webbrowser used on WSL"))
    p = tmp_path / "r.report.html"
    assert browser.open_in_browser(str(p))
    assert calls == [["wslview", str(p)]]


def test_wsl_without_wslu_goes_through_powershell(monkeypatch, launches,
                                                  tmp_path):
    calls, _ = launches
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda n: None)
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url: pytest.fail("webbrowser used on WSL"))
    p = tmp_path / "r.report.html"
    assert browser.open_in_browser(str(p))
    assert calls[0] == ["wslpath", "-w", str(p)]
    ps = calls[1]
    assert ps[0].endswith("powershell.exe")
    assert ps[-1] == ("Start-Process -FilePath "
                      "'\\\\wsl.localhost\\Ubuntu\\tmp\\x.report.html'")


def test_wsl_url_needs_no_path_conversion(monkeypatch, launches):
    calls, results = launches
    results["powershell.exe"] = 1          # powershell refused; cmd start works
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda n: None)
    assert browser.open_in_browser("https://gitlab.example/-/tokens")
    assert not any(c[0] == "wslpath" for c in calls)
    assert calls[-1][-3:] == ["start", "", "https://gitlab.example/-/tokens"]


def test_wsl_everything_fails_falls_back_to_webbrowser(monkeypatch, launches):
    calls, results = launches
    results["powershell.exe"] = 1
    results["cmd.exe"] = 1
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda n: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: False)
    assert browser.open_in_browser("https://x.example") is False


def test_wsl_honors_explicit_browser_env(monkeypatch, launches):
    calls, _ = launches
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    monkeypatch.setenv("BROWSER", "firefox")
    opened = []
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url: opened.append(url) or True)
    assert browser.open_in_browser("https://x.example")
    assert opened == ["https://x.example"] and calls == []
