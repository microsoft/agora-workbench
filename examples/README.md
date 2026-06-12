# Examples

This directory contains working examples that demonstrate how to build and use Agora Workbench servers.

## Contents

| Directory | Description |
|-----------|-------------|
| [`servers/`](servers/) | Example MCP server implementations (chemistry, earth science, energy systems) with Docker Compose for local deployment |
| [`agent_free_getting_started/`](agent_free_getting_started/) | Quickstart showing how to run a `CodeExecutionServer` and call it directly over MCP — no agent framework required |

## Getting Started

If you're new to Agora Workbench, start with [`agent_free_getting_started/`](agent_free_getting_started/) to see the simplest possible server + client setup using raw MCP calls or `curl`.

For production-style servers with tool registries, state graphs, and containerized environments, explore the implementations in [`servers/`](servers/).
