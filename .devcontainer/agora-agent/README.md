# Earthshots Dev Container

This dev container provides a comprehensive development environment for all Earthshots projects with the following conda environments:

- **earthshots-orfb** (default) - Organic Redox Flow Battery environment
- **earthshots-sorbent** - Sorbent capture environment  
- **earthshots-dac** - Direct Air Capture environment

## Features

- All earthshots conda environments installed and ready to use
- VS Code extensions for Python development:
  - Python
  - Jupyter
  - Azure Resources
  - GitHub Copilot Chat
  - isort
  - black
- Pre-commit hooks configured
- Shared AI4S packages installed in all environments
- Zsh with Oh My Zsh and helpful plugins

## Usage

1. Open this repository in VS Code
2. When prompted, click "Reopen in Container" or use Command Palette: "Dev Containers: Reopen in Container"
3. Select the "earthshots" dev container
4. Wait for the container to build (first time will take longer)

## Switching Between Environments

The default environment is `earthshots-orfb`. To switch environments:

```bash
conda activate earthshots-sorbent
# or
conda activate earthshots-dac
# or back to default
conda activate earthshots-orfb
```

## Available Packages

Each environment includes the necessary dependencies for its respective earthshots project:

- **orfb**: Azure services, quantum computing, molecular simulation tools
- **sorbent**: PyTorch, molecular ML frameworks, quantum chemistry tools  
- **dac**: Process simulation and lifecycle assessment tools
