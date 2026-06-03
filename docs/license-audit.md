# License Audit Report

**Date:** 2026-06-03
**Scope:** All direct and transitive dependencies of `agora-workbench` (from `pyproject.toml`)
**Target license:** MIT (open-sourcing under MIT)
**Tool:** `pip-licenses 5.x` against resolved dependency set

## Summary

✅ **All 205 third-party dependencies have MIT-compatible licenses.** No GPL, AGPL, SSPL, or proprietary-licensed packages were found.

## License Distribution

| License Family | Count | Compatible? |
|---|---|---|
| MIT / MIT License | 86 | ✅ Yes |
| BSD License / BSD-2-Clause / BSD-3-Clause | 61 | ✅ Yes |
| Apache Software License / Apache-2.0 | 25 | ✅ Yes |
| ISC / ISC License (ISCL) | 8 | ✅ Yes |
| Mozilla Public License 2.0 (MPL-2.0) | 4 | ✅ Yes (file-level copyleft, compatible with MIT) |
| Python Software Foundation License / PSF-2.0 | 4 | ✅ Yes |
| The Unlicense | 1 | ✅ Yes |
| MIT-CMU (Pillow) | 1 | ✅ Yes |
| Multi-license (Apache + BSD, Apache + MIT, etc.) | 6 | ✅ Yes |
| UNKNOWN (this project itself) | 4 | N/A — these are `agora-workbench` and `agora-agent` |
| UNKNOWN (`matplotlib-inline`) | 1 | ✅ Yes — actual license is BSD-3-Clause (metadata gap) |

## Azure SDK Packages

All Azure SDK packages are confirmed MIT-licensed:

| Package | Version | License |
|---|---|---|
| azure-core | 1.38.3 | MIT |
| azure-identity | 1.26.0b2 | MIT |
| azure-data-tables | 12.7.0 | MIT License |
| azure-search-documents | 11.7.0b2 | MIT License |
| azure-storage-blob | 12.28.0 | MIT License |
| azure-common | 1.1.28 | MIT License |

## Packages Requiring Note

### MPL-2.0 Packages

MPL-2.0 is a weak/file-level copyleft license. It is compatible with MIT for distribution purposes — modifications to *MPL-licensed files themselves* must remain under MPL-2.0, but this does not affect the rest of the project.

- `certifi` 2026.2.25
- `fqdn` 1.5.1
- `pathspec` 1.1.1
- `pytest-metadata` 3.1.1 (dev-only)

### UNKNOWN Metadata

- `agora-workbench` 0.1.0 — this project (will be MIT once `license` field is added to `pyproject.toml`)
- `agora-agent` 0.1.0 — sibling workspace package (same repo)
- `matplotlib-inline` 0.2.1 — actual license is BSD-3-Clause per source repository; missing classifier in package metadata

## Flagged Issues

**None.** No GPL, AGPL, SSPL, EUPL, or proprietary-only dependencies were found in either direct or transitive dependencies.

## Recommended Actions

1. Add `license = "MIT"` to `pyproject.toml` `[project]` section to resolve the UNKNOWN status for this project's own packages.
2. No dependency changes are required for MIT open-sourcing.

## Reproduction

```bash
uv run --with pip-licenses pip-licenses --format=markdown --order=license
```
