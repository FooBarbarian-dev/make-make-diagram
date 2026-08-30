"""Curated documentation for GitHub Actions predefined variables.

One catalog, embedded whole into ``report.annotations["predefined_var_docs"]``
by ``parse_github`` and rendered by the HTML report (What-If tooltips, the
Variable Explorer detail panel, and the Variables-tab reference section) —
the GitHub twin of ``gitlab_predefined.py``, under the same honesty rules:

- Every name the What-If simulator sets or controls MUST have an entry, and
  the entry's set/unset text must restate the fact the simulator implements.
  A test scans the shipped templates and fails on any uncovered name.
- ``example`` values are illustrations, never simulation output; the UI
  always labels them "e.g.".
- No URLs anywhere (offline reports; URL-shaped examples are scheme-less).
- A predefined name with no entry here keeps the report's generic honest
  wording — never invent a description.

Two catalogs ship: ``PREDEFINED_VAR_DOCS`` documents the default environment
variables (``GITHUB_*`` / ``RUNNER_*`` / ``CI``) that job steps see, and
``CONTEXT_FIELD_DOCS`` documents the ``github.*`` expression-context fields
that ``if:`` conditions read. Where an env var mirrors a context field
(``GITHUB_REF`` / ``github.ref``) the wording is kept in sync by hand.

Entry fields: ``summary`` (one sentence, what it is), ``example`` (one
realistic value), ``set_when`` (when GitHub sets it), optional ``unset_when``
(when it is notably absent — usually the gotcha), optional ``note`` (one
documented surprise).
"""

from __future__ import annotations

_EVERY_JOB = "every job"
_EVERY_RUN = "every workflow run"
_PR_ONLY = "pull_request / pull_request_target runs only"
_OUTSIDE_PR = "outside pull request runs"

_SHA = "1ecfd275763eff1d6b4844ea3168962458c9f27a"


def _e(
    summary: str,
    example: str,
    set_when: str,
    unset_when: str | None = None,
    note: str | None = None,
) -> dict[str, str]:
    entry = {"summary": summary, "example": example, "set_when": set_when}
    if unset_when is not None:
        entry["unset_when"] = unset_when
    if note is not None:
        entry["note"] = note
    return entry


PREDEFINED_VAR_DOCS: dict[str, dict[str, str]] = {
    # ---- CI environment markers -------------------------------------------
    "CI": _e(
        "Marks that the script runs in a CI environment.",
        "true",
        _EVERY_JOB,
    ),
    "GITHUB_ACTIONS": _e(
        "Marks that the script runs under GitHub Actions specifically "
        "(as opposed to another CI system).",
        "true",
        _EVERY_JOB,
    ),
    # ---- repository / actor ------------------------------------------------
    "GITHUB_REPOSITORY": _e(
        "The owner and repository name the workflow runs in.",
        "octo-org/widget",
        _EVERY_JOB,
    ),
    "GITHUB_REPOSITORY_OWNER": _e(
        "The owner (user or organization) of the repository.",
        "octo-org",
        _EVERY_JOB,
    ),
    "GITHUB_REPOSITORY_ID": _e(
        "The numeric ID of the repository.",
        "123456789",
        _EVERY_JOB,
    ),
    "GITHUB_REPOSITORY_OWNER_ID": _e(
        "The numeric ID of the repository owner.",
        "1024",
        _EVERY_JOB,
    ),
    "GITHUB_ACTOR": _e(
        "The username of the account that initiated the workflow run.",
        "octocat",
        _EVERY_JOB,
        note=(
            "For a scheduled run this is the user who last modified the "
            "workflow's cron schedule, not a pusher."
        ),
    ),
    "GITHUB_ACTOR_ID": _e(
        "The numeric ID of the account that initiated the workflow run.",
        "583231",
        _EVERY_JOB,
    ),
    "GITHUB_TRIGGERING_ACTOR": _e(
        "The username of the account that caused THIS run attempt — differs "
        "from GITHUB_ACTOR when someone re-runs another person's workflow.",
        "hubber",
        _EVERY_JOB,
    ),
    # ---- event / ref -------------------------------------------------------
    "GITHUB_EVENT_NAME": _e(
        "The name of the event that triggered the run.",
        "push",
        _EVERY_JOB,
    ),
    "GITHUB_EVENT_PATH": _e(
        "Path of the file on the runner holding the full webhook event "
        "payload as JSON.",
        "/github/workflow/event.json",
        _EVERY_JOB,
    ),
    "GITHUB_SHA": _e(
        "The commit SHA that triggered the workflow.",
        _SHA,
        _EVERY_JOB,
        note=(
            "In pull_request runs this is the SHA of a synthetic MERGE commit "
            "of head into base, not the head commit — the head commit is "
            "github.event.pull_request.head.sha."
        ),
    ),
    "GITHUB_REF": _e(
        "The fully-formed git ref that triggered the run: refs/heads/<branch> "
        "for branch pushes, refs/tags/<tag> for tag pushes, "
        "refs/pull/<number>/merge for pull requests.",
        "refs/heads/feature/widget",
        _EVERY_JOB,
        note=(
            "Branch or tag filters in on: match the SHORT name (main, v1.0), "
            "but GITHUB_REF keeps the refs/ prefix — comparing it to a bare "
            "branch name is a classic if: bug."
        ),
    ),
    "GITHUB_REF_NAME": _e(
        "The short ref name that triggered the run: the branch or tag name, "
        "or <pr_number>/merge for pull requests.",
        "feature/widget",
        _EVERY_JOB,
    ),
    "GITHUB_REF_TYPE": _e(
        'The type of ref that triggered the run: "branch" or "tag".',
        "branch",
        _EVERY_JOB,
    ),
    "GITHUB_REF_PROTECTED": _e(
        '"true" when branch protection or a ruleset applies to the ref that '
        "triggered the run.",
        "false",
        _EVERY_JOB,
    ),
    "GITHUB_BASE_REF": _e(
        "The TARGET branch of the pull request (base), as a short name.",
        "main",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
    "GITHUB_HEAD_REF": _e(
        "The SOURCE branch of the pull request (head), as a short name.",
        "feature/widget",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
    # ---- workflow / run identity ------------------------------------------
    "GITHUB_WORKFLOW": _e(
        "The name of the running workflow (the name: key, or the file path "
        "when the workflow has no name).",
        "CI",
        _EVERY_JOB,
    ),
    "GITHUB_WORKFLOW_REF": _e(
        "The path of the workflow file inside its repository, with the ref "
        "it was taken from.",
        "octo-org/widget/.github/workflows/ci.yml@refs/heads/main",
        _EVERY_JOB,
    ),
    "GITHUB_WORKFLOW_SHA": _e(
        "The commit SHA of the workflow file version being run.",
        _SHA,
        _EVERY_JOB,
    ),
    "GITHUB_JOB": _e(
        "The job_id (YAML key) of the current job.",
        "build",
        _EVERY_JOB,
    ),
    "GITHUB_RUN_ID": _e(
        "A unique number for each workflow run in the repository — stable "
        "across re-runs of the same run.",
        "9134567890",
        _EVERY_JOB,
    ),
    "GITHUB_RUN_NUMBER": _e(
        "A unique number for each run of a particular workflow, starting at "
        "1 and incrementing per new run — NOT re-incremented on re-runs.",
        "212",
        _EVERY_JOB,
    ),
    "GITHUB_RUN_ATTEMPT": _e(
        "How many times this run has been attempted, starting at 1.",
        "1",
        _EVERY_JOB,
    ),
    "GITHUB_RETENTION_DAYS": _e(
        "How many days workflow run logs and artifacts are kept.",
        "90",
        _EVERY_JOB,
    ),
    # ---- action being executed --------------------------------------------
    "GITHUB_ACTION": _e(
        "The id of the current step, or a generated ordinal id for steps "
        "without one.",
        "__run",
        "every step",
    ),
    "GITHUB_ACTION_REPOSITORY": _e(
        "For a step running a published action, the owner/name of the "
        "action's repository.",
        "actions/checkout",
        "steps that run an action via uses:",
        unset_when="run: steps",
    ),
    "GITHUB_ACTION_PATH": _e(
        "The directory where a composite action's files live — lets a "
        "composite action address its own bundled scripts.",
        "/home/runner/work/_actions/octo-org/setup/v2",
        "composite action steps only",
        unset_when="ordinary workflow steps",
    ),
    # ---- communication files -----------------------------------------------
    "GITHUB_ENV": _e(
        "Path of the file a step appends NAME=value lines to, to export "
        "environment variables to later steps in the same job.",
        "/home/runner/work/_temp/_runner_file_commands/set_env_abc",
        "every step",
        note="Variables written here reach LATER steps only, never the writing step.",
    ),
    "GITHUB_OUTPUT": _e(
        "Path of the file a step appends name=value lines to, to declare "
        "step outputs consumable via steps.<id>.outputs.",
        "/home/runner/work/_temp/_runner_file_commands/set_output_abc",
        "every step",
    ),
    "GITHUB_PATH": _e(
        "Path of the file a step appends directories to, to prepend them to "
        "PATH for later steps in the same job.",
        "/home/runner/work/_temp/_runner_file_commands/add_path_abc",
        "every step",
    ),
    "GITHUB_STEP_SUMMARY": _e(
        "Path of the file a step appends markdown to; the content renders on "
        "the run's summary page.",
        "/home/runner/work/_temp/_runner_file_commands/step_summary_abc",
        "every step",
    ),
    "GITHUB_STATE": _e(
        "Path of the file an action appends name=value lines to, to share "
        "state with its own pre: and post: hooks.",
        "/home/runner/work/_temp/_runner_file_commands/save_state_abc",
        "every step",
    ),
    # ---- server / workspace ------------------------------------------------
    "GITHUB_SERVER_URL": _e(
        "Base URL of the GitHub server the run belongs to (github.com or a "
        "GitHub Enterprise Server host).",
        "github.com (scheme omitted here; the real value carries one)",
        _EVERY_JOB,
    ),
    "GITHUB_API_URL": _e(
        "Base URL of the GitHub REST API for this server.",
        "api.github.com (scheme omitted here; the real value carries one)",
        _EVERY_JOB,
    ),
    "GITHUB_GRAPHQL_URL": _e(
        "Base URL of the GitHub GraphQL API for this server.",
        "api.github.com/graphql (scheme omitted here; the real value carries one)",
        _EVERY_JOB,
    ),
    "GITHUB_WORKSPACE": _e(
        "The default working directory on the runner — where actions/checkout "
        "places the repository.",
        "/home/runner/work/widget/widget",
        _EVERY_JOB,
        note="Empty until a checkout step actually populates it.",
    ),
    "GITHUB_TOKEN": _e(
        "The installation access token GitHub mints for the run — in "
        "expressions it is secrets.GITHUB_TOKEN or github.token.",
        "ghs_16C7e42F292c6912E7710c838347Ae178B4a",
        "available to every run as a secret",
        unset_when=(
            "never exported as an environment variable automatically — a "
            "step sees it only when the workflow maps it into env: or an "
            "action input explicitly"
        ),
    ),
    # ---- runner ------------------------------------------------------------
    "RUNNER_OS": _e(
        'The operating system of the runner: "Linux", "Windows", or "macOS".',
        "Linux",
        _EVERY_JOB,
    ),
    "RUNNER_ARCH": _e(
        'The architecture of the runner: "X86", "X64", "ARM", or "ARM64".',
        "X64",
        _EVERY_JOB,
    ),
    "RUNNER_NAME": _e(
        "The name of the runner executing the job.",
        "GitHub Actions 17",
        _EVERY_JOB,
    ),
    "RUNNER_ENVIRONMENT": _e(
        '"github-hosted" or "self-hosted", per the runner executing the job.',
        "github-hosted",
        _EVERY_JOB,
    ),
    "RUNNER_TEMP": _e(
        "A temporary directory on the runner, emptied at the start and end "
        "of each job.",
        "/home/runner/work/_temp",
        _EVERY_JOB,
    ),
    "RUNNER_TOOL_CACHE": _e(
        "The directory containing preinstalled tools on GitHub-hosted "
        "runners.",
        "/opt/hostedtoolcache",
        _EVERY_JOB,
    ),
    "RUNNER_DEBUG": _e(
        '"1" when debug logging is enabled for the run.',
        "1",
        "runs with debug logging enabled (re-run with debug, or the "
        "ACTIONS_RUNNER_DEBUG secret/variable set)",
        unset_when="ordinary runs — not set to 0, simply absent",
    ),
}


# ---------------------------------------------------------------------------
# Expression-context fields (`if:` conditions read these, not env vars)
# ---------------------------------------------------------------------------

CONTEXT_FIELD_DOCS: dict[str, dict[str, str]] = {
    "github.event_name": _e(
        "The name of the event that triggered the run — the context twin of "
        "GITHUB_EVENT_NAME.",
        "push",
        _EVERY_RUN,
    ),
    "github.ref": _e(
        "The fully-formed git ref of the run: refs/heads/<branch>, "
        "refs/tags/<tag>, or refs/pull/<number>/merge.",
        "refs/heads/main",
        _EVERY_RUN,
        note=(
            "Keeps the refs/ prefix — github.ref == 'main' never matches; "
            "compare github.ref_name, or the full refs/heads/main."
        ),
    ),
    "github.ref_name": _e(
        "The short ref name of the run: branch or tag name, or "
        "<pr_number>/merge for pull requests.",
        "main",
        _EVERY_RUN,
    ),
    "github.ref_type": _e(
        'The type of ref of the run: "branch" or "tag".',
        "branch",
        _EVERY_RUN,
    ),
    "github.ref_protected": _e(
        "true when branch protection or a ruleset applies to the ref of "
        "the run.",
        "false",
        _EVERY_RUN,
    ),
    "github.sha": _e(
        "The commit SHA that triggered the workflow (the merge commit for "
        "pull_request runs).",
        _SHA,
        _EVERY_RUN,
    ),
    "github.event": _e(
        "The full webhook event payload as an object — event-specific fields "
        "live under it (github.event.pull_request.draft, "
        "github.event.head_commit.message, …).",
        "{…}",
        _EVERY_RUN,
        note="Its shape depends entirely on the triggering event.",
    ),
    "github.base_ref": _e(
        "The base (target) branch of the pull request, as a short name.",
        "main",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
    "github.head_ref": _e(
        "The head (source) branch of the pull request, as a short name.",
        "feature/widget",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
    "github.actor": _e(
        "The username of the account that initiated the workflow run.",
        "octocat",
        _EVERY_RUN,
    ),
    "github.repository": _e(
        "The owner and repository name the workflow runs in.",
        "octo-org/widget",
        _EVERY_RUN,
    ),
    "github.repository_owner": _e(
        "The owner (user or organization) of the repository.",
        "octo-org",
        _EVERY_RUN,
    ),
    "github.workflow": _e(
        "The name of the running workflow (or its file path when unnamed).",
        "CI",
        _EVERY_RUN,
    ),
    "github.run_id": _e(
        "A unique number for each workflow run in the repository.",
        "9134567890",
        _EVERY_RUN,
    ),
    "github.run_number": _e(
        "A unique number for each run of this workflow, starting at 1.",
        "212",
        _EVERY_RUN,
    ),
    "github.run_attempt": _e(
        "How many times this run has been attempted, starting at 1.",
        "1",
        _EVERY_RUN,
    ),
    "github.job": _e(
        "The job_id (YAML key) of the current job.",
        "build",
        _EVERY_JOB,
    ),
    "github.token": _e(
        "The installation access token GitHub mints for the run — the "
        "expression twin of secrets.GITHUB_TOKEN.",
        "ghs_16C7e42F292c6912E7710c838347Ae178B4a",
        _EVERY_RUN,
    ),
    "github.event.pull_request.draft": _e(
        "true while the pull request is a draft.",
        "false",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
    "github.event.pull_request.number": _e(
        "The pull request number.",
        "1234",
        _PR_ONLY,
        unset_when=_OUTSIDE_PR,
    ),
}
