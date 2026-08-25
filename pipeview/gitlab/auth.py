"""Token resolution and interactive setup.

Resolution order (first hit wins):
  1. --token flag
  2. $PIPEVIEW_GITLAB_TOKEN
  3. $GITLAB_TOKEN
  4. $GITLAB_PRIVATE_TOKEN
  5. stored token in ~/.config/pipeview/gitlab.json for the host

A *first* personal access token cannot be created through the API — the
API needs a token to authenticate, and POST /user/personal_access_tokens
is admin-only. The supported creation path is GitLab's prefilled form,
which `interactive_setup` opens in a browser:

    <host>/-/user_settings/personal_access_tokens?name=pipeview&scopes=read_api
"""

from __future__ import annotations

import getpass
import os
import sys
import urllib.parse

from pipeview.gitlab.api import GitLabAuthError, GitLabClient, GitLabError
from pipeview.gitlab.config import GitLabConfig

_ENV_VARS = ("PIPEVIEW_GITLAB_TOKEN", "GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN")

TOKEN_SCOPE = "read_api"


def resolve_token(host: str, cli_token: str | None, config: GitLabConfig,
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
    """GitLab's PAT form, prefilled with a name and the read_api scope."""
    host = GitLabConfig.normalize_host(host)
    params = urllib.parse.urlencode({"name": "pipeview", "scopes": TOKEN_SCOPE})
    return f"{host}/-/user_settings/personal_access_tokens?{params}"


def verify_token(host: str, token: str, *, ca_bundle: str | None = None,
                 insecure: bool = False) -> dict:
    """Raises GitLabError if the token doesn't authenticate; else the user."""
    client = GitLabClient(host, token, ca_bundle=ca_bundle, insecure=insecure)
    return client.current_user()


def interactive_setup(host: str, config: GitLabConfig, *,
                      ca_bundle: str | None = None, insecure: bool = False,
                      open_browser: bool = True) -> str | None:
    """Walk the user through creating and storing a token. Returns the
    verified token, or None if the user gave up."""
    host = GitLabConfig.normalize_host(host)
    url = token_creation_url(host)

    print(f"pipeview needs a personal access token for {host}")
    print(f"(scope: {TOKEN_SCOPE} — read-only API access).\n")
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
            user = verify_token(host, token, ca_bundle=ca_bundle, insecure=insecure)
        except GitLabAuthError:
            print("That token did not authenticate — check it was copied whole.",
                  file=sys.stderr)
            continue
        except GitLabError as e:
            print(f"Could not verify against {host}: {e}", file=sys.stderr)
            continue

        print(f"Authenticated as {user.get('username', '?')} ({user.get('name', '')})")
        try:
            answer = input(f"Store this token in {config.path} (0600)? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            config.set_token(host, token)
            if not config.default_host:
                config.default_host = host
            config.save()
            print(f"Saved. ({config.path})")
        else:
            print("Not stored — export PIPEVIEW_GITLAB_TOKEN to avoid re-pasting.")
        return token

    return None
