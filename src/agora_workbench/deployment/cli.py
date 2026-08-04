"""CLI entrypoint for deployment scaffolding.

Usage:
    agora-workbench-deploy init [--target docker|azure|activity-ui|all] [--output-dir DIR]

Copies deployment templates into the specified directory so they can be
customized for the user's server.
"""

import argparse
import sys
from importlib.resources import files
from pathlib import Path
from typing import Optional


DEPLOYMENT_TEMPLATES = files("agora_workbench.deployment.templates")
ACTIVITY_UI_TEMPLATES = files("activity_ui")

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
    "activity-ui": [
        "activity_ui/Dockerfile",
        "activity_ui/docker-compose.yml",
        "activity_ui/requirements.txt",
        "activity_ui/__init__.py",
        "activity_ui/auth.py",
        "activity_ui/models.py",
        "activity_ui/server.py",
        "activity_ui/static/index.html",
        "activity_ui/README.md",
    ],
}


def _template_source(name: str):
    if not name.startswith("activity_ui/"):
        return DEPLOYMENT_TEMPLATES.joinpath(name)

    relative_name = name.removeprefix("activity_ui/")
    if relative_name == "README.md":
        return DEPLOYMENT_TEMPLATES.joinpath(name)
    return ACTIVITY_UI_TEMPLATES.joinpath(relative_name)


def _copy_template(name: str, dest_dir: Path) -> Optional[Path]:
    """Copy a single template file to the destination, preserving subdirectories.

    Returns the created path, or ``None`` if the template is missing from the
    installed package. A missing template is reported as a warning rather than
    raising, so one absent file cannot abort a scaffold that has already
    partially written to the user's directory.
    """
    source = _template_source(name)

    try:
        content = source.read_text()
    except OSError:
        print(
            f"⚠️  Skipping {name}: template not found in the installed package.",
            file=sys.stderr,
        )
        return None

    target = dest_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    # Preserve executable bit for shell scripts
    if name.endswith(".sh"):
        target.chmod(target.stat().st_mode | 0o755)

    return target


def init(target: str = "all", output_dir: str = "deployment") -> list[str]:
    """Copy deployment templates into the target directory.

    Args:
        target: Which template set to scaffold: "docker", "azure", "activity-ui", or "all".
        output_dir: Destination directory (created if needed).

    Returns:
        List of created file paths (relative to output_dir). Templates missing
        from the installed package are skipped with a warning and omitted.
    """
    dest = Path(output_dir)

    if target == "all":
        sets_to_copy = list(TEMPLATE_SETS.keys())
    elif target in TEMPLATE_SETS:
        sets_to_copy = [target]
    else:
        choices = ", ".join([*TEMPLATE_SETS, "all"])
        raise ValueError(f"Unknown target: {target!r}. Choose from: {choices}")

    created = []
    for set_name in sets_to_copy:
        for template_name in TEMPLATE_SETS[set_name]:
            path = _copy_template(template_name, dest)
            if path is not None:
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
        choices=[*TEMPLATE_SETS, "all"],
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
            step = 5 if args.target == "all" else 1
            print(f"  {step}. Edit azure/parameters/server.bicepparam for your server")
            print(f"  {step + 1}. Run azure/setup.sh to provision Azure infrastructure")
            print(f"  {step + 2}. Run azure/deploy-server.sh --server <name> to deploy")
        if args.target in ("activity-ui", "all"):
            step = 8 if args.target == "all" else 1
            print(f"  {step}. Create the shared network: docker network create agora-activity")
            print(f"  {step + 1}. Start the UI: docker compose -f activity_ui/docker-compose.yml up -d --build")


if __name__ == "__main__":
    main()
