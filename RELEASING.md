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

7. Create a GitHub Release from the tag using the matching changelog entry as the release notes.

Do not publish Agora Workbench to PyPI while the repository is private. PyPI publishing will be added
separately after the repository becomes public.

## Publishing versioned documentation

The documentation workflow publishes changes from `main` as `dev`. Release documentation must be deployed
manually from the matching release tag:

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

The resulting documentation is available under version-specific paths such as `/0.1.0/`, while `/latest/`
tracks the newest release. The version selector is provided by `mike`.

To test a versioned documentation build locally without pushing it:

```bash
uv run mike deploy --branch docs-preview 0.1.0 -- --strict
uv run mike serve --branch docs-preview
```

Delete the local `docs-preview` branch after testing if it is no longer needed.

## Consuming a release

Until PyPI publishing is enabled, downstream projects should pin an immutable release tag:

```toml
dependencies = [
    "agora-workbench @ git+https://github.com/microsoft/agora-workbench.git@v0.1.0",
]
```

Downstream projects should also commit their `uv.lock` file so the tag resolves to a recorded commit.
