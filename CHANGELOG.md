# Changelog

All notable changes to Agora Workbench are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

In addition to the standard Keep a Changelog sections, a release may open with a `Breaking` section listing
changes that require action from existing users. Each entry there states who is affected and how to migrate.

## [Unreleased]

### Breaking

- The connector CLI no longer falls back to no-op auth when no backend is configured. Starting without
  authentication is now an explicit opt-in via `CONNECTOR_ALLOW_NOOP_AUTH=1`; otherwise the connector exits with
  a configuration error, so a missing or misspelled `ENTRA_CLIENT_ID` fails the deployment instead of quietly
  starting it unprotected. Setting only one of `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` is likewise now an error
  rather than silently selecting no-op auth
  ([#287](https://github.com/microsoft/agora-workbench/issues/287)).

  **Who is affected:** anyone starting `mcp-connector-server` without both `ENTRA_CLIENT_ID` and
  `ENTRA_TENANT_ID` set — typically local development and CI. A connector that previously started with a
  `No ENTRA_CLIENT_ID/ENTRA_TENANT_ID set` warning now exits with status 1.

  **To migrate:** set `CONNECTOR_ALLOW_NOOP_AUTH=1` to keep the old unauthenticated behaviour, or configure a
  real backend (`ENTRA_CLIENT_ID` + `ENTRA_TENANT_ID`, or `CONNECTOR_AUTH_FACTORY`). Deployments using the
  bundled Azure scripts are unaffected: `_deploy-common.sh` already requires both Entra variables.

### Added

- `SessionConfig.data_manager_factory` for supplying a customized `DataLakeDataManager` per session, and an
  optional `data_manager` argument on `Session`. A deployment needing a different credential, a custom artifact
  resolver, extra fetchers, or configuration derived from the session's `user_identity` / `user_token` no longer
  has to subclass `SessionManager` and replace `session.data_manager` after the fact — a workaround that leaked a
  temp cache directory per session, since `DataLakeDataManager` allocates one eagerly in its constructor. The
  factory receives a `SessionContext` and must return a fresh instance per session, because the session owns the
  manager and cleans it up ([#307](https://github.com/microsoft/agora-workbench/issues/307)).
- `CONNECTOR_AUTH_FACTORY` for pointing the connector CLI at a `"module.path:factory"` that returns a custom
  `AuthConfig`, and an optional `auth_config_factory` argument on `connector.cli.main()` so a downstream package
  can ship its own console script that reuses the CLI's environment parsing and server selection. Operators with
  an identity provider other than Entra ID no longer have to fork the CLI to use
  `mcp-connector-server` ([#287](https://github.com/microsoft/agora-workbench/issues/287)).

### Fixed

- `{name}_check_batch` and `{name}_cancel_batch` failed with `404: Batch '<id>' not found` for every valid batch,
  leaving results from `{name}_parallel_execute` unreachable. Both tools resolve the owning session from the batch
  status payload, which never carried `parent_session_id`, so the ownership check always failed. Because that
  payload is built before the check runs, a finished batch had its results computed and its child sessions and
  batch state cleaned up, then discarded rather than returned — so a second call reported the batch as genuinely
  missing ([#304](https://github.com/microsoft/agora-workbench/pull/304)).
- `{name}_check_batch` and `{name}_cancel_batch` now settle authorization before building a status payload.
  Building one retires a terminal batch, so a caller who supplied a batch id they did not own could close its
  child sessions and prune its state before being refused, destroying results the owner had not yet read
  ([#304](https://github.com/microsoft/agora-workbench/pull/304)).

## [0.1.2] - 2026-08-05

### Added

- `agora-workbench-deploy skill` for installing the bundled `agora-workbench` agent skill into an agent's skills
  directory from the installed package, so consumers no longer need a source checkout to obtain it.
- `AuthConfig.protected_resource_metadata` for publishing RFC 9728 protected-resource metadata through the public
  auth contract, so any identity provider can describe itself instead of the server composing an Entra-shaped
  document. `create_entra_auth_config()` populates it automatically.

### Changed

- Moved the `agora-workbench` agent skill from the repository root into the `agora_workbench.skills` package so that
  it ships in the published wheel and source distribution.
- The scaffolded `docker/base.Dockerfile` installs `agora-workbench` from PyPI by default, so it builds from a
  consumer project root instead of requiring a workbench source checkout. Build against a checkout with
  `--build-arg AGORA_WORKBENCH_SOURCE=local`, and pin a release with `--build-arg AGORA_WORKBENCH_VERSION=<version>`.
- Servers now log a startup warning when authorization is required but no OAuth protected-resource metadata can be
  resolved, replacing a silent 404 from `/.well-known/oauth-protected-resource`.
- Reading `_client_id` / `_tenant_id` off a `TokenValidator` is deprecated in favour of
  `AuthConfig.protected_resource_metadata`. The fallback is retained for existing validators and is now documented
  on the `TokenValidator` ABC.
- The scaffolded `docker/base.Dockerfile` installs `uv` from a version-pinned PyPI release rather than piping an
  unversioned remote install script into a shell, so image builds of a given commit are reproducible and auditable.
  Override with `--build-arg UV_VERSION=<version>` ([#288](https://github.com/microsoft/agora-workbench/issues/288)).

### Fixed

- Included dotfile templates in the published wheel, so `agora-workbench-deploy init --target docker` no longer
  fails with `FileNotFoundError` on the missing `docker/.env.server.example` ([#286](https://github.com/microsoft/agora-workbench/issues/286)).
- Scaffolding now skips a template that is missing from the installed package with a warning instead of aborting
  partway through and leaving a half-written output directory.
- Corrected the scaffolded `docker/docker-compose.yml` `env_file` path, which pointed one directory above the
  `.env.server` the CLI and docs tell you to create.
- The Azure deploy scripts now locate the base image at its scaffolded `docker/base.Dockerfile` path (falling back
  to the legacy location), instead of silently skipping the base build and failing any server image that extends it.
- The private `_client_id` / `_tenant_id` probe on `TokenValidator` is now symmetric, so a validator exposing
  only `_tenant_id` contributes it instead of being silently ignored ([#289](https://github.com/microsoft/agora-workbench/issues/289)).

## [0.1.1] - 2026-07-30

### Added

- Trusted Publishing workflow for building validated release distributions and publishing them to PyPI.
- Pull request packaging checks and repeatable TestPyPI candidate deployments with clean-install smoke tests.
- `agora-workbench-deploy init --target activity-ui` for scaffolding the standalone monitoring service from the
  installed package.

### Changed

- Excluded tests, bytecode, and cache artifacts from wheel and source distributions.
- Updated installation guidance to use the released PyPI package while identifying bundled examples that require a
  source checkout.

## [0.1.0] - 2026-07-28

### Added

- `CodeExecutionServer` for running session-isolated Python kernels with uv, conda, or pip environments.
- Typed domain tool registration, searchable tool catalogs, reusable agent skills, and state-based workflow planning.
- Adaptive tool execution, explicit session reconnects, persistent session state, and helpers for publishing large outputs.
- Data catalogs with keyword and vector search across local files and Azure Blob Storage.
- File, object, and artifact transfer between kernels, agents, and trusted peer servers.
- `ConnectorServer` router and gateway modes for composing multiple MCP servers behind a unified endpoint.
- Unified cross-server state graphs with configurable bridge edges for multi-server workflow discovery.
- Sidecar process support for sharing expensive models and other process-global resources across kernel sessions.
- Streamable HTTP hosting with Bearer token middleware, Azure Entra ID authentication, and no-op development auth.
- `agora-workbench-deploy` scaffolding for Docker and Azure Container Apps deployments.
- A real-time activity UI with session-grouped events, artifact previews, and output downloads.
- Reference chemistry, geospatial, and energy-system servers demonstrating domain tools and skills.
- Tutorials for Microsoft Agent Framework, OpenAI Agents SDK, and GitHub Copilot SDK clients.
- User guides and generated API reference documentation for server construction, data access, deployment, and extension.
