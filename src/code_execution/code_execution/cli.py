"""
CLI for code_execution environment management.

Usage:
    python -m code_execution.cli warm --config config.yaml

Commands:
    warm    Build environment and provision assets without starting the server.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .asset_provisioner import provision_assets
from .code_execution_models import EnvironmentConfig

LOGGER = logging.getLogger(__name__)


def _load_config(config_path: Path) -> EnvironmentConfig:
    """Load EnvironmentConfig from a JSON or YAML file."""
    content = config_path.read_text(encoding="utf-8")

    if config_path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            print("ERROR: PyYAML is required for YAML config files. Install with: pip install pyyaml", file=sys.stderr)
            sys.exit(1)
        data = yaml.safe_load(content)
    elif config_path.suffix == ".json":
        data = json.loads(content)
    else:
        # Try JSON first, then YAML
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            try:
                import yaml

                data = yaml.safe_load(content)
            except ImportError:
                print(
                    f"ERROR: Cannot parse {config_path} — not valid JSON and PyYAML not installed.",
                    file=sys.stderr,
                )
                sys.exit(1)

    return EnvironmentConfig(**data)


async def _warm(config: EnvironmentConfig) -> None:
    """Build environment and provision assets."""
    from . import environment_builders

    # Build environment
    build_dir = config.get_build_dir()
    expected_python = config.get_python_path()

    if expected_python.exists():
        LOGGER.info(f"Environment already exists: {expected_python}")
    elif config.auto_build:
        LOGGER.info(f"Building {config.type} environment: {config.name}")
        parent_dir = build_dir.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        # Write dependency file
        if config.type == "conda":
            filename = "environment.yml"
        else:
            filename = "requirements.txt"

        dep_file_path = parent_dir / filename
        dep_file_path.write_text(config.dependency_file)

        if config.type == "uv":
            await environment_builders.build_uv_environment(config)
        elif config.type == "conda":
            await environment_builders.build_conda_environment(config)
        elif config.type == "pip":
            await environment_builders.build_pip_environment(config)
        else:
            print(f"ERROR: Unsupported environment type: {config.type}", file=sys.stderr)
            sys.exit(1)

        LOGGER.info(f"Environment built: {expected_python}")
    else:
        print(
            f"ERROR: Environment not found at {expected_python} and auto_build is disabled.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Provision assets
    if config.assets:
        LOGGER.info(f"Provisioning {len(config.assets)} asset(s)...")
        await provision_assets(config)
        LOGGER.info("All assets provisioned.")
    else:
        LOGGER.info("No assets to provision.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="code_execution.cli",
        description="Code execution environment management CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # warm subcommand
    warm_parser = subparsers.add_parser(
        "warm",
        help="Build environment and provision assets without starting the server",
    )
    warm_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to environment config file (JSON or YAML)",
    )
    warm_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "warm":
        if not args.config.exists():
            print(f"ERROR: Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)

        config = _load_config(args.config)
        asyncio.run(_warm(config))
        print(f"✓ Environment '{config.name}' is warm and ready.")


if __name__ == "__main__":
    main()
