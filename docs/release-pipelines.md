# Release Pipelines

This project uses `release-please` to automate versioning and releases.

When commits are pushed to the `main` branch, the `.github/workflows/release.yml` GitHub Actions workflow runs. `release-please` analyzes the commit history (using Conventional Commits) and automatically creates or updates a "Release PR".

Once the Release PR is approved and merged into `main`, `release-please` tags the release, updates the `CHANGELOG.md`, and creates a GitHub Release. Subsequently, the `publish` job in the workflow attaches the built `sdist` and `wheel` packages to the GitHub release.

## GitHub Token Permissions
Because `release-please` automates the creation of Pull Requests, the default `GITHUB_TOKEN` must have the appropriate permissions. In your repository settings (Settings -> Actions -> General -> Workflow permissions), ensure that you have checked:
- "Read and write permissions"
- "Allow GitHub Actions to create and approve pull requests"

Alternatively, you can set the `token:` parameter in the `release-please` step to use a Personal Access Token (PAT) with `repo` scope if you prefer not to enable this repository-wide setting.
