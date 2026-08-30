#!/usr/bin/env python3
"""Refresh pipeview's bundled snapshot of GitLab's built-in CI templates.

Maintainer tool — this is the ONE place in the repository that downloads
GitLab's template tree (lib/gitlab/ci/templates from gitlab-org/gitlab) so
the shipped package never has to. Run it to move the snapshot to a newer
GitLab release, then commit the result:

    python scripts/update_gitlab_templates.py             # newest stable tag
    python scripts/update_gitlab_templates.py --ref v19.3.0-ee

It rewrites pipeview/data/gitlab_ci_templates/ with:
- the `*.gitlab-ci.yml` tree exactly as GitLab ships it (MIT-licensed:
  the gitlab repo's LICENSE covers everything outside doc/, ee/ and jh/),
- LICENSE — the gitlab repository's license text at that ref,
- README.md — provenance notes,
- _meta.json — source, ref, version, file count (read at runtime by
  pipeview.gitlab_templates to say which snapshot resolved a template).

Stdlib only, like the rest of pipeview.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_REPO = "https://gitlab.com/gitlab-org/gitlab"
TEMPLATE_PATH = "lib/gitlab/ci/templates"
STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-ee$")   # no -rc suffix


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pipeview-maint"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def latest_stable_tag(repo_url: str) -> str:
    host_and_path = repo_url.split("://", 1)[-1]
    host, _, project = host_and_path.partition("/")
    api = (f"https://{host}/api/v4/projects/"
           f"{urllib.parse.quote(project, safe='')}/repository/tags?per_page=50")
    tags = json.loads(_get(api).decode("utf-8"))
    for tag in tags:   # newest first; skip release candidates
        if STABLE_TAG.match(tag.get("name", "")):
            return tag["name"]
    raise SystemExit("No stable vX.Y.Z-ee tag in the newest 50 tags")


def fetch_tree(repo_url: str, ref: str) -> dict[str, bytes]:
    """{relative template path: content} for every *.gitlab-ci.yml at ref."""
    archive = (f"{repo_url}/-/archive/{urllib.parse.quote(ref)}/x.tar.gz"
               f"?path={urllib.parse.quote(TEMPLATE_PATH)}")
    print(f"Downloading {archive}")
    blob = _get(archive)
    out: dict[str, bytes] = {}
    marker = f"/{TEMPLATE_PATH}/"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".gitlab-ci.yml"):
                continue
            _, _, rel = member.name.partition(marker)
            rel = posixpath.normpath(rel)
            if not rel or rel.startswith(("..", "/")):
                continue   # refuse anything that would escape the dest dir
            fobj = tar.extractfile(member)
            if fobj is not None:
                out[rel] = fobj.read()
    if not out:
        raise SystemExit(f"Archive for {ref} contained no *.gitlab-ci.yml files")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", help="gitlab-org/gitlab tag to snapshot "
                                      "(default: newest stable vX.Y.Z-ee tag)")
    parser.add_argument("--repo-url", default=DEFAULT_REPO)
    parser.add_argument(
        "--dest",
        default=os.path.join(os.path.dirname(__file__), "..",
                             "pipeview", "data", "gitlab_ci_templates"),
    )
    args = parser.parse_args()

    ref = args.ref or latest_stable_tag(args.repo_url)
    m = STABLE_TAG.match(ref)
    version = ".".join(m.groups()) if m else ref.lstrip("v")
    print(f"Snapshotting {TEMPLATE_PATH} at {ref} (GitLab {version})")

    tree = fetch_tree(args.repo_url, ref)
    license_text = _get(f"{args.repo_url}/-/raw/{urllib.parse.quote(ref)}/LICENSE")

    dest = os.path.abspath(args.dest)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for rel, content in sorted(tree.items()):
        path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)

    with open(os.path.join(dest, "LICENSE"), "wb") as f:
        f.write(license_text)
    meta = {
        "source": args.repo_url,
        "ref": ref,
        "gitlab_version": version,
        "path": TEMPLATE_PATH,
        "template_count": len(tree),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "license": "MIT Expat (see LICENSE in this directory)",
    }
    with open(os.path.join(dest, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    with open(os.path.join(dest, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"""# Bundled GitLab CI templates

Verbatim snapshot of GitLab's built-in `include:template` files
(`{TEMPLATE_PATH}` in [gitlab-org/gitlab]({args.repo_url})) at
**{ref}** (GitLab {version}), {len(tree)} templates.

pipeview uses these as an offline fallback when resolving
`include: template:` entries, because GitLab's REST template API cannot
serve most of them (it only exposes the flattened "dropdown" keys — see
`docs/agents/specs/2026-08-25-gitlab-template-fallback-design.md`).

The files are MIT-licensed (the gitlab repository's LICENSE covers
everything outside `doc/`, `ee/` and `jh/`); the license text at this ref
sits alongside in `LICENSE`. Do not edit these files by hand — refresh the
snapshot with `python scripts/update_gitlab_templates.py`.
""")

    print(f"Wrote {len(tree)} templates to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
