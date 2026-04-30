# External Dependencies

This directory contains external packages managed as Git submodules that are installed
as editable dependencies in the code execution server environments.

## Current External Packages

### pypsa-usa
Git submodule pointing to the PyPSA-USA repository.

PyPSA-USA is a power system model for the United States that provides Snakemake
workflows for building and optimizing electricity networks.

**Usage in servers:**
- `powergrid` server includes pypsa-usa in its environment
- Tools in `domains/powergrid/tools/` can import and use pypsa-usa modules
- Snakemake workflows are executed from the pypsa-usa installation

**Installation:**
The submodule allows the code execution environment to install pypsa-usa as an
editable package via `-e ../../external/pypsa-usa` in requirements.txt.

## Adding New External Packages

To add a new external dependency as a submodule:

1. Add the submodule:
   ```bash
   cd domains/powergrid/server/external
   git submodule add https://github.com/org/package-name package-name
   ```

2. Add to server requirements.txt:
   ```txt
   -e ../../external/package-name
   ```

3. The package will be installed when the server environment is built

## Working with Submodules

**Clone with submodules:**
```bash
git clone --recurse-submodules https://github.com/your-org/AgoraAgentMAF.git
```

**Initialize submodules after clone:**
```bash
git submodule update --init --recursive
```

**Update submodules to latest:**
```bash
git submodule update --remote
```
