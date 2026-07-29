# Changelog

All notable changes to Agora Workbench are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Trusted Publishing workflow for building validated release distributions and publishing them to PyPI.

### Changed

- Excluded tests, bytecode, and cache artifacts from wheel and source distributions.

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
