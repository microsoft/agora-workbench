# Releasing Agora Workbench

Agora Workbench uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version in
`pyproject.toml` is the authoritative package version, release tags use the matching `vMAJOR.MINOR.PATCH`
format, and documentation versions use `MAJOR.MINOR.PATCH`.

While the project is below `1.0.0`, minor releases may contain breaking changes. Patch releases should remain
backward compatible within their minor release.

## Preparing a release

1. Choose the next version according to Semantic Versioning.
2. Update `project.version` in `pyproject.toml` and run `uv lock`.
3. Move the relevant entries under `Unreleased` in `CHANGELOG.md` into a section for the new version and date.
4. Run the release checks:

   ```bash
   uv build
   uv run ruff check .
   uv run pyright --level error src
   uv run pytest -m "not live"
   uv run mkdocs build --strict
   ```

5. Merge the release changes into `main`.
6. Create and push an annotated tag from the release commit:

   ```bash
   git switch main
   git pull --ff-only
   git tag -a v0.1.0 -m "Agora Workbench v0.1.0"
   git push origin v0.1.0
   ```

7. Create a GitHub Release from the tag using the matching changelog entry as the release notes. Publishing the
   release triggers the PyPI workflow.
8. Approve the deployment to the protected `pypi` GitHub environment.
9. Confirm the release is available and installable:

   ```bash
   version=$(python -c \
     'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
   python -m venv /tmp/agora-workbench-release
   /tmp/agora-workbench-release/bin/pip install "agora-workbench==$version"
   /tmp/agora-workbench-release/bin/python -c "import agora_workbench"
   ```

10. Confirm that the documentation for the new version was published. The PyPI workflow deploys it automatically
    after the upload succeeds, so no manual step is needed.

The PyPI workflow builds from the immutable release tag, verifies that the tag and package versions match, checks
the distribution contents, installs the wheel in a clean environment, and publishes through PyPI Trusted Publishing.
It does not use a long-lived API token. Pull requests that affect packaging run the same build, content validation,
and installation checks through the `package.yml` workflow.

### Trusted Publisher configuration

Configure pending publishers on both [TestPyPI](https://test.pypi.org/manage/account/publishing/) and
[PyPI](https://pypi.org/manage/account/publishing/):

| Index | Project | Owner | Repository | Workflow | Environment |
| --- | --- | --- | --- | --- | --- |
| TestPyPI | `agora-workbench` | `microsoft` | `agora-workbench` | `publish-testpypi.yml` | `testpypi` |
| PyPI | `agora-workbench` | `microsoft` | `agora-workbench` | `publish-pypi.yml` | `pypi` |

The GitHub `testpypi` and `pypi` environments should require approval from a repository maintainer. A pending
publisher does not reserve the project name until the first successful upload.

### Testing a release candidate

After the release preparation changes merge, run the TestPyPI workflow against `main`:

```bash
gh workflow run publish-testpypi.yml -f source_ref=main
```

The workflow appends a unique `.dev<run-id>` suffix to the package version, so it can be rerun after fixes without
consuming the intended production version. It builds and validates the distributions, publishes them through the
protected `testpypi` environment, then installs the uploaded wheel from TestPyPI and exercises its imports and CLIs.

Only create the final Git tag and GitHub Release after the TestPyPI workflow succeeds.

The workflow can be started manually for a release whose GitHub Release was published before the workflow existed:

```bash
version=$(python -c \
  'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
gh workflow run publish-pypi.yml -f tag="v$version"
```

The manual input must name an existing release tag. Published files on PyPI are immutable, so never move or recreate
a tag after publishing it.

The PyPI workflow and distribution cleanup were introduced after `v0.1.0` was created. The first PyPI release is
therefore `v0.1.1`; the existing `v0.1.0` tag remains unchanged and installable directly from Git.

## Publishing versioned documentation

The documentation workflow publishes changes from `main` as `dev`. Release documentation is published
automatically: after the PyPI upload succeeds, `publish-pypi.yml` calls the documentation workflow with the
release tag, so `/0.1.2/` and the matching aliases appear without any manual step.

Aliases are resolved from the published GitHub Releases:

- The `MAJOR.MINOR` alias moves to the release only when it is the newest patch in its own series.
- The `latest` alias, and the site default, move only when the release is the newest release overall.

A patch for an older series therefore publishes its version-specific documentation and updates its `MAJOR.MINOR`
alias without dragging `latest` backwards. Because the workflow runs from the release tag, the automation only
applies to tags created after it was introduced.

### Deploying release documentation manually

Manual deployment is still available for backfilling a release that predates the automation, or for repairing an
alias:

1. Open **Actions > Deploy Documentation > Run workflow**.
2. Select the matching tag, such as `v0.1.0`, in **Use workflow from**.
3. Enter the release version without the `v` prefix, such as `0.1.0`.
4. Enter aliases such as `0.1 latest`.
5. Select **Set the latest alias as the site default** for the newest supported release.

The same deployment can be started with the GitHub CLI:

```bash
gh workflow run docs.yml \
  --ref v0.1.0 \
  -f version=0.1.0 \
  -f aliases="0.1 latest" \
  -f set_latest_default=true
```

When backfilling several releases, deploy them oldest first so the aliases finish on the newest version.

The resulting documentation is available under version-specific paths such as `/0.1.0/`, while `/latest/`
tracks the newest release. The version selector is provided by `mike`.

To test a versioned documentation build locally without pushing it:

```bash
uv run mike deploy --branch docs-preview 0.1.0
uv run mike serve --branch docs-preview
```

Delete the local `docs-preview` branch after testing if it is no longer needed.

## Consuming a release

Downstream projects should pin a compatible PyPI version:

```toml
dependencies = [
    "agora-workbench>=0.1.1,<0.2.0",
]
```

Downstream projects should commit their `uv.lock` file so the selected distribution and hashes remain reproducible.
