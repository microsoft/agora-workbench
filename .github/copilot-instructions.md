The project uses `uv` for dependency management with `pyproject.toml` at the repo root. Source code lives under `src/`. Run `uv run` from the repo root to run scripts (e.g. `uv run python src/script.py`) and `uv add` to add packages. This automatically uses the correct virtual environment.

Always use `uv run pytest` to run tests.