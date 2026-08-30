"""Token resolution and interactive setup for GitHub.

Resolution order (first hit wins):
  1. --token flag
  2. $PIPEVIEW_GITHUB_TOKEN
  3. $GITHUB_TOKEN
  4. $GH_TOKEN
  5. stored token in ~/.config/pipeview/github.json for the host

A token cannot be created through the API (the API needs one to
authenticate), so `interactive_setup` opens GitHub's new-token form —
prefilled with a name where the form supports it — and the user pastes
the result back. Fine-grained or classic tokens both work; read access
to the repositories to analyze is all pipeview needs.
"""

from __future__ import annotations

import getpass
import os
import sys
import urllib.parse

from pipeview.github.api import GitHubAuthError, GitHubClient, GitHubError
from pipeview.github.config import GitHubConfig

_ENV_VARS = ("PIPEVIEW_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

TOKEN_SCOPE = "repo"


def resolve_token(host: str, cli_token: str | None, config: GitHubConfig,
                  environ: dict | None = None) -> tuple[str | None, str]:
    """Returns (token, source_description)."""
    env = environ if environ is not None else os.environ
    if cli_token:
        return cli_token, "--token flag"
    for var in _ENV_VARS:
        if env.get(var):
            return env[var], f"${var}"
    stored = config.stored_token(host)
    if stored:
        return stored, config.path
    return None, "not found"


def token_creation_url(host: str) -> str:
    """GitHub's classic-token form, prefilled with a description and the
    repo scope (read access to private repositories)."""
    host = GitHubConfig.normalize_host(host)
    params = urllib.parse.urlencode({"description": "pipeview",
                                     "scopes": TOKEN_SCOPE})
    return f"{host}/settings/tokens/new?{params}"


def verify_token(host: str, token: str, *, ca_bundle: str | None = None,
                 insecure: bool = False) -> dict:
    """Raises GitHubError if the token doesn't authenticate; else the user."""
    client = GitHubClient(host, token, ca_bundle=ca_bundle, insecure=insecure)
    return client.current_user()


def interactive_setup(host: str, config: GitHubConfig, *,
                      ca_bundle: str | None = None, insecure: bool = False,
                      open_browser: bool = True) -> str | None:
    """Walk the user through creating and storing a token. Returns the
    verified token, or None if the user gave up."""
    host = GitHubConfig.normalize_host(host)
    url = token_creation_url(host)

    print(f"pipeview needs a personal access token for {host}")
    print(f"(scope: {TOKEN_SCOPE} — read access to your repositories).\n")
    print("Create one here (form is prefilled), then paste it below:")
    print(f"  {url}\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass  # headless boxes: URL is printed above

    for attempt in range(3):
        try:
            token = getpass.getpass("Token (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not token:
            return None
        try:
            user = verify_token(host, token, ca_bundle=ca_bundle,
                                insecure=insecure)
        except GitHubAuthError:
            print("That token did not authenticate — check it was copied "
                  "whole.", file=sys.stderr)
            continue
        except GitHubError as e:
            print(f"Could not verify against {host}: {e}", file=sys.stderr)
            continue

        print(f"Authenticated as {user.get('login', '?')} "
              f"({user.get('name') or ''})")
        try:
            answer = input(f"Store this token in {config.path} (0600)? "
                           "[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            config.set_token(host, token)
            if not config.default_host:
                config.default_host = host
            config.save()
            print(f"Saved. ({config.path})")
        else:
            print("Not stored — export PIPEVIEW_GITHUB_TOKEN to avoid "
                  "re-pasting.")
        return token

    return None
