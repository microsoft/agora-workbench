"""CLI entrypoint for deployment scaffolding.

Usage:
    agora-workbench-deploy init [--target docker|azure|all] [--output-dir DIR]

Copies deployment templates into the specified directory so they can be
customized for the user's server.
"""

import argparse
import sys
from importlib.resources import files
from pathlib import Path


TEMPLATES = files("agora_workbench.deployment.templates")

TEMPLATE_SETS = {
    "docker": [
        "docker/base.Dockerfile",
        "docker/Dockerfile",
        "docker/docker-compose.yml",
        "docker/.env.server.example",
    ],
    "azure": [
        "azure/main.bicep",
        "azure/activity-ui.bicep",
        "azure/deploy.sh",
        "azure/deploy-server.sh",
        "azure/deploy-network.sh",
        "azure/_deploy-common.sh",
        "azure/setup.sh",
        "azure/setup-app-registrations.sh",
        "azure/README.md",
        "azure/parameters/server.bicepparam",
        "azure/networks/router.yaml",
    ],
}


def _copy_template(name: str, dest_dir: Path) -> Path:
    """Copy a single template file to the destination, preserving subdirectories."""
    source = TEMPLATES.joinpath(name)
    target = dest_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)

    content = source.read_text()
    target.write_text(content)

    # Preserve executable bit for shell scripts
    if name.endswith(".sh"):
        target.chmod(target.stat().st_mode | 0o755)

    return target


def init(target: str = "all", output_dir: str = "deployment") -> list[str]:
    """Copy deployment templates into the target directory.

    Args:
        target: Which template set to scaffold — "docker", "azure", or "all".
        output_dir: Destination directory (created if needed).

    Returns:
        List of created file paths (relative to output_dir).
    """
    dest = Path(output_dir)

    if target == "all":
        sets_to_copy = list(TEMPLATE_SETS.keys())
    elif target in TEMPLATE_SETS:
        sets_to_copy = [target]
    else:
        raise ValueError(f"Unknown target: {target!r}. Choose from: docker, azure, all")

    created = []
    for set_name in sets_to_copy:
        for template_name in TEMPLATE_SETS[set_name]:
            path = _copy_template(template_name, dest)
            created.append(str(path))

    return created


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="agora-workbench-deploy",
        description="Scaffold deployment files for an agora-workbench server.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init",
        help="Copy deployment templates into your project.",
    )
    init_parser.add_argument(
        "--target",
        choices=["docker", "azure", "all"],
        default="all",
        help="Which deployment templates to scaffold (default: all).",
    )
    init_parser.add_argument(
        "--output-dir",
        "-o",
        default="deployment",
        help="Destination directory (default: ./deployment).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files without prompting.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "init":
        dest = Path(args.output_dir)

        # Check for existing files unless --force
        if dest.exists() and not args.force:
            existing = [
                f
                for set_name in (list(TEMPLATE_SETS.keys()) if args.target == "all" else [args.target])
                for f in TEMPLATE_SETS[set_name]
                if (dest / f).exists()
            ]
            if existing:
                print(f"⚠️  The following files already exist in {dest}/:", file=sys.stderr)
                for f in existing:
                    print(f"   {f}", file=sys.stderr)
                print("\nUse --force to overwrite.", file=sys.stderr)
                sys.exit(1)

        created = init(target=args.target, output_dir=args.output_dir)
        print(f"✓ Scaffolded {len(created)} deployment file(s) into {dest}/:")
        for path in sorted(created):
            print(f"  {path}")
        print("\nNext steps:")
        if args.target in ("docker", "all"):
            print("  1. Edit docker/Dockerfile to COPY your server code")
            print("  2. Build the base image: docker build -f docker/base.Dockerfile -t mcp-server-base:local .")
            print("  3. Copy docker/.env.server.example → docker/.env.server and fill in values")
            print("  4. docker compose -f docker/docker-compose.yml up --build")
        if args.target in ("azure", "all"):
            print("  5. Edit azure/parameters/server.bicepparam for your server")
            print("  6. Run azure/setup.sh to provision Azure infrastructure")
            print("  7. Run azure/deploy-server.sh --server <name> to deploy")


if __name__ == "__main__":
    main()
