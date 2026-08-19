"""Curated documentation for GitLab CI/CD predefined variables.

One catalog, embedded whole into ``report.annotations["predefined_var_docs"]``
by ``parse_gitlab`` and rendered by the HTML report (What-If tooltips, the
Variable Explorer detail panel, and the Variables-tab reference section).

Honesty rules (see docs/superpowers/specs/2026-08-19-gitlab-predefined-
variable-docs-design.md):

- Every name the What-If simulator sets or controls (``buildEnv`` /
  ``CONTROLLED`` in ``whatif.js``) MUST have an entry, and the entry's
  set/unset text must restate the fact the simulator implements. A test
  scans the shipped templates and fails on any uncovered name.
- ``example`` values are illustrations, never simulation output; the UI
  always labels them "e.g.".
- No URLs anywhere (offline reports; URL-shaped examples are scheme-less).
- A predefined name with no entry here keeps the report's generic honest
  wording — never invent a description.

Entry fields: ``summary`` (one sentence, what it is), ``example`` (one
realistic value), ``set_when`` (when GitLab sets it), optional ``unset_when``
(when it is notably absent — usually the gotcha), optional ``note`` (one
documented surprise).
"""

from __future__ import annotations

_EVERY_JOB = "every job"
_MR_ONLY = "merge request pipelines only"
_OUTSIDE_MR = "outside merge request pipelines"

_SHA = "1ecfd275763eff1d6b4844ea3168962458c9f27a"
_ZERO_SHA = "0" * 40


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
        "Marks that the script runs in a CI/CD environment.",
        "true",
        _EVERY_JOB,
    ),
    "GITLAB_CI": _e(
        "Marks that the script runs under GitLab CI/CD specifically "
        "(as opposed to another CI system).",
        "true",
        _EVERY_JOB,
    ),
    # ---- commit / ref ------------------------------------------------------
    "CI_COMMIT_SHA": _e(
        "The full 40-character SHA of the commit the pipeline runs on.",
        _SHA,
        _EVERY_JOB,
    ),
    "CI_COMMIT_SHORT_SHA": _e(
        "The first eight characters of CI_COMMIT_SHA.",
        _SHA[:8],
        _EVERY_JOB,
    ),
    "CI_COMMIT_BEFORE_SHA": _e(
        "The SHA the ref pointed at before the push event.",
        _ZERO_SHA,
        _EVERY_JOB,
        note=(
            "All zeros except in ordinary branch-push pipelines: merge request, "
            "tag, scheduled, manual, API/trigger, and first-push-of-a-new-branch "
            "pipelines all get the zero SHA."
        ),
    ),
    "CI_COMMIT_MESSAGE": _e(
        "The full commit message.",
        "Fix login redirect",
        _EVERY_JOB,
    ),
    "CI_COMMIT_TITLE": _e(
        "The first line of the commit message.",
        "Fix login redirect",
        _EVERY_JOB,
    ),
    "CI_COMMIT_DESCRIPTION": _e(
        "The commit message without its first line (the whole message when the "
        "title is longer than 100 characters).",
        "Adds a regression test for the redirect loop.",
        _EVERY_JOB,
    ),
    "CI_COMMIT_REF_NAME": _e(
        "The branch or tag name the pipeline runs for.",
        "feature/widget",
        _EVERY_JOB,
        note=(
            "In merge request pipelines this is the SOURCE branch name; the "
            "merge-request git ref is CI_MERGE_REQUEST_REF_PATH."
        ),
    ),
    "CI_COMMIT_REF_SLUG": _e(
        "CI_COMMIT_REF_NAME lowercased, shortened to 63 characters, with "
        "everything except 0-9 and a-z replaced by - and stripped of leading and "
        "trailing dashes — safe for URLs, host names, and paths.",
        "feature-widget",
        _EVERY_JOB,
    ),
    "CI_COMMIT_REF_PROTECTED": _e(
        '"true" when the branch or tag the pipeline runs for is protected.',
        "false",
        _EVERY_JOB,
    ),
    "CI_COMMIT_BRANCH": _e(
        "The branch name the pipeline runs on.",
        "main",
        "branch pipelines, including scheduled, manual, and API/trigger "
        "pipelines on a branch",
        unset_when="merge request pipelines and tag pipelines",
    ),
    "CI_COMMIT_TAG": _e(
        "The tag name the pipeline runs for.",
        "v1.0.0",
        "tag pipelines only",
        unset_when="branch and merge request pipelines",
    ),
    "CI_COMMIT_TAG_MESSAGE": _e(
        "The annotation message of the tag (empty for lightweight tags).",
        "Release 1.0.0",
        "tag pipelines only",
        unset_when="branch and merge request pipelines",
    ),
    "CI_COMMIT_TIMESTAMP": _e(
        "The commit timestamp, in ISO 8601 format.",
        "2026-08-19T09:10:00+02:00",
        _EVERY_JOB,
    ),
    "CI_COMMIT_AUTHOR": _e(
        "The commit author, in 'Name <email>' form.",
        "Jane Doe <jdoe@example.com>",
        _EVERY_JOB,
    ),
    # ---- pipeline ----------------------------------------------------------
    "CI_PIPELINE_SOURCE": _e(
        "How the pipeline was triggered.",
        "push",
        _EVERY_JOB,
        note=(
            'Values: push, merge_request_event, schedule, web, api, trigger, '
            "pipeline, parent_pipeline, chat, external, "
            'external_pull_request_event. It is "push" for BOTH branch and tag '
            "pushes — tell them apart with CI_COMMIT_BRANCH / CI_COMMIT_TAG."
        ),
    ),
    "CI_PIPELINE_ID": _e(
        "The instance-wide numeric ID of the pipeline.",
        "1000",
        _EVERY_JOB,
    ),
    "CI_PIPELINE_IID": _e(
        "The per-project number of the pipeline.",
        "42",
        _EVERY_JOB,
    ),
    "CI_PIPELINE_URL": _e(
        "The URL of the pipeline's page.",
        "gitlab.example.com/group/project/-/pipelines/1000",
        _EVERY_JOB,
    ),
    "CI_PIPELINE_CREATED_AT": _e(
        "The UTC timestamp the pipeline was created, in ISO 8601 format.",
        "2026-08-19T09:14:00Z",
        _EVERY_JOB,
    ),
    "CI_PIPELINE_NAME": _e(
        "The pipeline's display name, from workflow:name.",
        "Nightly build",
        "pipelines whose configuration sets workflow:name",
        unset_when="pipelines without workflow:name",
    ),
    "CI_PIPELINE_SCHEDULE_DESCRIPTION": _e(
        "The description of the pipeline schedule that started this pipeline.",
        "nightly",
        "scheduled pipelines only",
        unset_when="every other pipeline type",
    ),
    "CI_PIPELINE_TRIGGERED": _e(
        '"true" when the pipeline was started with a trigger token.',
        "true",
        "trigger-token pipelines only",
        unset_when="every other pipeline type",
    ),
    # ---- merge request -----------------------------------------------------
    "CI_OPEN_MERGE_REQUESTS": _e(
        "A comma-separated list of up to four open merge requests that use the "
        "current branch and project as source, in group/project!iid form.",
        "group/project!1",
        "merge request pipelines, and branch pipelines whose branch is the "
        "source of an open MR",
        unset_when="when no open merge request uses the branch as source",
        note=(
            "Set in EVERY branch pipeline with an open MR — push, schedule, web, "
            "api, trigger — which is what the documented duplicate-pipeline "
            "dedup patterns key on."
        ),
    ),
    "CI_MERGE_REQUEST_ID": _e(
        "The instance-wide ID of the merge request.",
        "1000",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_IID": _e(
        "The per-project number of the merge request (the !N shown in the UI).",
        "1",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_EVENT_TYPE": _e(
        "The flavor of the merge request pipeline.",
        "detached",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
        note="Values: detached, merged_result, merge_train.",
    ),
    "CI_MERGE_REQUEST_REF_PATH": _e(
        "The git ref path of the merge request.",
        "refs/merge-requests/1/head",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": _e(
        "The source branch of the merge request.",
        "feature/widget",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": _e(
        "The target branch of the merge request.",
        "main",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED": _e(
        '"true" when the merge request\'s source branch is protected.',
        "false",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED": _e(
        '"true" when the merge request\'s target branch is protected.',
        "true",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_TITLE": _e(
        "The title of the merge request.",
        "Add widget support",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_DRAFT": _e(
        '"true" when the merge request is marked as a draft.',
        "false",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
        note=(
            'Always "true" or "false" in MR pipelines, never unset — GitLab '
            "does not skip drafts unless your rules say so."
        ),
    ),
    "CI_MERGE_REQUEST_PROJECT_ID": _e(
        "The ID of the project the merge request targets.",
        "1",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_PROJECT_PATH": _e(
        "The full path of the project the merge request targets.",
        "group/project",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_SQUASH_ON_MERGE": _e(
        '"true" when squash-on-merge is enabled for the merge request.',
        "false",
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    "CI_MERGE_REQUEST_LABELS": _e(
        "Comma-separated labels of the merge request.",
        "backend,urgent",
        "merge request pipelines when the MR has at least one label",
        unset_when="outside MR pipelines, and when the MR has no labels",
    ),
    "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA": _e(
        "The HEAD SHA of the merge request's source branch.",
        _SHA,
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
        note=(
            "An empty string (set but empty) in detached MR pipelines; a real "
            "SHA only in merged-results and merge-train pipelines. Empty-but-set "
            "matters: bare $VAR is falsy on empty."
        ),
    ),
    "CI_MERGE_REQUEST_TARGET_BRANCH_SHA": _e(
        "The HEAD SHA of the merge request's target branch.",
        _SHA,
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
        note=(
            "An empty string (set but empty) in detached MR pipelines; a real "
            "SHA only in merged-results and merge-train pipelines."
        ),
    ),
    "CI_MERGE_REQUEST_ASSIGNEES": _e(
        "Comma-separated usernames of the merge request's assignees.",
        "jdoe",
        "merge request pipelines when the MR has assignees",
        unset_when="outside MR pipelines, and when the MR has no assignees",
    ),
    "CI_MERGE_REQUEST_MILESTONE": _e(
        "The milestone title of the merge request.",
        "18.3",
        "merge request pipelines when the MR has a milestone",
        unset_when="outside MR pipelines, and when the MR has no milestone",
    ),
    "CI_MERGE_REQUEST_DIFF_BASE_SHA": _e(
        "The base SHA of the merge request diff.",
        _SHA,
        _MR_ONLY,
        unset_when=_OUTSIDE_MR,
    ),
    # ---- external pull requests (GitHub-mirrored repositories) -------------
    "CI_EXTERNAL_PULL_REQUEST_IID": _e(
        "The pull request number from the external provider (GitHub).",
        "7",
        "external pull request pipelines only (repositories mirrored from "
        "GitHub)",
        unset_when="every other pipeline type",
    ),
    "CI_EXTERNAL_PULL_REQUEST_SOURCE_BRANCH_NAME": _e(
        "The source branch of the external pull request.",
        "feature/widget",
        "external pull request pipelines only",
        unset_when="every other pipeline type",
    ),
    "CI_EXTERNAL_PULL_REQUEST_TARGET_BRANCH_NAME": _e(
        "The target branch of the external pull request.",
        "main",
        "external pull request pipelines only",
        unset_when="every other pipeline type",
    ),
    # ---- project -----------------------------------------------------------
    "CI_PROJECT_ID": _e(
        "The instance-wide numeric ID of the project.",
        "217",
        _EVERY_JOB,
    ),
    "CI_PROJECT_NAME": _e(
        "The project's directory name (the last component of its path).",
        "project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_PATH": _e(
        "The project's full path, including its namespace.",
        "group/project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_PATH_SLUG": _e(
        "CI_PROJECT_PATH lowercased with everything except 0-9 and a-z "
        "replaced by - — safe for URLs and host names.",
        "group-project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_NAMESPACE": _e(
        "The project's namespace (group path or username).",
        "group",
        _EVERY_JOB,
    ),
    "CI_PROJECT_ROOT_NAMESPACE": _e(
        "The top-level group or username the project lives under.",
        "group",
        _EVERY_JOB,
    ),
    "CI_PROJECT_TITLE": _e(
        "The project's human-readable name as shown in the GitLab UI.",
        "My Project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_DIR": _e(
        "The absolute path the repository is cloned to and the job runs in.",
        "/builds/group/project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_URL": _e(
        "The project's web URL.",
        "gitlab.example.com/group/project",
        _EVERY_JOB,
    ),
    "CI_PROJECT_VISIBILITY": _e(
        "The project's visibility: private, internal, or public.",
        "private",
        _EVERY_JOB,
    ),
    "CI_DEFAULT_BRANCH": _e(
        "The project's default branch name.",
        "main",
        _EVERY_JOB,
    ),
    "CI_REPOSITORY_URL": _e(
        "The URL to clone the repository over HTTP(S), with a CI token "
        "embedded.",
        "gitlab-ci-token:[masked]@gitlab.example.com/group/project.git",
        _EVERY_JOB,
    ),
    # ---- job ---------------------------------------------------------------
    "CI_JOB_NAME": _e(
        "The name of the job.",
        "build_job",
        _EVERY_JOB,
    ),
    "CI_JOB_STAGE": _e(
        "The stage the job belongs to.",
        "test",
        _EVERY_JOB,
    ),
    "CI_JOB_ID": _e(
        "The unique, instance-wide numeric ID of the job.",
        "50829",
        _EVERY_JOB,
    ),
    "CI_JOB_URL": _e(
        "The URL of the job's page.",
        "gitlab.example.com/group/project/-/jobs/50829",
        _EVERY_JOB,
    ),
    "CI_JOB_TOKEN": _e(
        "A short-lived token authenticating the running job against the GitLab "
        "API, container registry, and package registries.",
        "[masked]",
        _EVERY_JOB,
        note=(
            "Masked in job logs. It acts with the triggering user's "
            "permissions, limited by the project's job-token allowlist."
        ),
    ),
    "CI_JOB_STATUS": _e(
        "The status of the job as it runs — mainly useful in after_script.",
        "success",
        _EVERY_JOB,
        note="Values: success, failed, canceled.",
    ),
    "CI_JOB_MANUAL": _e(
        '"true" when the job was started manually.',
        "true",
        "manually started jobs only",
        unset_when="jobs not started manually",
    ),
    "CI_JOB_STARTED_AT": _e(
        "The UTC timestamp the job started, in ISO 8601 format.",
        "2026-08-19T09:15:00Z",
        _EVERY_JOB,
    ),
    "CI_JOB_IMAGE": _e(
        "The container image the job runs in.",
        "alpine:3.20",
        "jobs that run in a container image",
        unset_when="jobs without an image (for example shell-executor runners)",
    ),
    "CI_NODE_INDEX": _e(
        "This instance's index within a parallel: job, starting at 1.",
        "2",
        "jobs using parallel:",
        unset_when="jobs without parallel:",
    ),
    "CI_NODE_TOTAL": _e(
        "The total number of parallel instances of the job; 1 when parallel: "
        "is not used.",
        "5",
        _EVERY_JOB,
    ),
    "CI_CONCURRENT_ID": _e(
        "A unique ID for the build slot on the runner, used to separate "
        "concurrent working directories.",
        "0",
        _EVERY_JOB,
    ),
    "CI_CONCURRENT_PROJECT_ID": _e(
        "A unique ID for the build slot on the runner, per project.",
        "0",
        _EVERY_JOB,
    ),
    # ---- registry / server / API -------------------------------------------
    "CI_REGISTRY": _e(
        "The address of the GitLab container registry.",
        "registry.example.com",
        "every job, when the container registry is enabled",
        unset_when="instances without a container registry",
    ),
    "CI_REGISTRY_IMAGE": _e(
        "The container-image address for the project in the registry.",
        "registry.example.com/group/project",
        "every job, when the container registry is enabled",
        unset_when="instances without a container registry",
    ),
    "CI_REGISTRY_USER": _e(
        "The username for pushing and pulling registry images as the running "
        "job.",
        "gitlab-ci-token",
        "every job, when the container registry is enabled",
        unset_when="instances without a container registry",
    ),
    "CI_REGISTRY_PASSWORD": _e(
        "The password for the registry — in practice the value of "
        "CI_JOB_TOKEN.",
        "[masked]",
        "every job, when the container registry is enabled",
        unset_when="instances without a container registry",
    ),
    "CI_SERVER_URL": _e(
        "The base URL of the GitLab instance.",
        "gitlab.example.com",
        _EVERY_JOB,
    ),
    "CI_SERVER_HOST": _e(
        "The GitLab instance's host name, without protocol or port.",
        "gitlab.example.com",
        _EVERY_JOB,
    ),
    "CI_SERVER_PORT": _e(
        "The port used to reach the GitLab instance.",
        "443",
        _EVERY_JOB,
    ),
    "CI_SERVER_PROTOCOL": _e(
        "The protocol used to reach the GitLab instance.",
        "https",
        _EVERY_JOB,
    ),
    "CI_SERVER_VERSION": _e(
        "The full version of the GitLab instance.",
        "18.2.1",
        _EVERY_JOB,
    ),
    "CI_API_V4_URL": _e(
        "The base URL of the GitLab REST API (v4).",
        "gitlab.example.com/api/v4",
        _EVERY_JOB,
    ),
    "CI_API_GRAPHQL_URL": _e(
        "The base URL of the GitLab GraphQL API.",
        "gitlab.example.com/api/graphql",
        _EVERY_JOB,
    ),
    "CI_PAGES_DOMAIN": _e(
        "The instance's configured domain for GitLab Pages sites.",
        "example.io",
        _EVERY_JOB,
    ),
    "CI_PAGES_URL": _e(
        "The URL of the project's GitLab Pages site.",
        "group.example.io/project",
        _EVERY_JOB,
    ),
    # ---- environments / deployments ---------------------------------------
    "CI_ENVIRONMENT_NAME": _e(
        "The name of the job's environment.",
        "production",
        "jobs with environment: set",
        unset_when="jobs without an environment",
    ),
    "CI_ENVIRONMENT_SLUG": _e(
        "The simplified, URL/DNS-safe form of the environment name.",
        "production",
        "jobs with environment: set",
        unset_when="jobs without an environment",
    ),
    "CI_ENVIRONMENT_URL": _e(
        "The environment's URL, when one is configured.",
        "app.example.com",
        "jobs with environment:url set",
        unset_when="jobs without an environment URL",
    ),
    "CI_ENVIRONMENT_ACTION": _e(
        "The environment action of the job: start, prepare, stop, verify, or "
        "access.",
        "start",
        "jobs with environment:action set",
        unset_when="jobs without an environment action",
    ),
    "CI_ENVIRONMENT_TIER": _e(
        "The deployment tier of the environment: production, staging, testing, "
        "development, or other.",
        "production",
        "jobs with an environment",
        unset_when="jobs without an environment",
    ),
    "CI_DEPLOY_FREEZE": _e(
        '"true" when the pipeline runs during a configured deploy-freeze '
        "window.",
        "true",
        "only during a deploy freeze",
        unset_when="outside deploy-freeze windows",
    ),
    "CI_KUBERNETES_ACTIVE": _e(
        '"true" when the job can use a configured Kubernetes cluster.',
        "true",
        "jobs with an available Kubernetes cluster",
        unset_when="jobs without a cluster",
    ),
    # ---- runner ------------------------------------------------------------
    "CI_RUNNER_ID": _e(
        "The numeric ID of the runner executing the job.",
        "17",
        _EVERY_JOB,
    ),
    "CI_RUNNER_DESCRIPTION": _e(
        "The description the runner was registered with.",
        "shared-linux-runner",
        _EVERY_JOB,
    ),
    "CI_RUNNER_TAGS": _e(
        "The runner's tag list, as a JSON array of strings.",
        '["docker", "linux"]',
        _EVERY_JOB,
    ),
    "CI_RUNNER_VERSION": _e(
        "The GitLab Runner version executing the job.",
        "18.2.0",
        _EVERY_JOB,
    ),
    # ---- triggering user ---------------------------------------------------
    "GITLAB_USER_LOGIN": _e(
        "The username of the user who started the pipeline (for manual jobs, "
        "the user who started the job).",
        "jdoe",
        _EVERY_JOB,
    ),
    "GITLAB_USER_NAME": _e(
        "The display name of the user who started the pipeline or job.",
        "Jane Doe",
        _EVERY_JOB,
    ),
    "GITLAB_USER_EMAIL": _e(
        "The email address of the user who started the pipeline or job.",
        "jdoe@example.com",
        _EVERY_JOB,
    ),
    "GITLAB_USER_ID": _e(
        "The numeric ID of the user who started the pipeline or job.",
        "42",
        _EVERY_JOB,
    ),
}
