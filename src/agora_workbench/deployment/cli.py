"""CLI entrypoint for deployment scaffolding.

Usage:
    agora-workbench-deploy init [--target docker|azure|activity-ui|all] [--output-dir DIR]
    agora-workbench-deploy skill [--name NAME] [--output-dir DIR] [--force]
    agora-workbench-deploy skill --list

``init`` copies deployment templates into the specified directory so they can be
customized for the user's server. ``skill`` installs a bundled agent skill into
an agent's skills directory.
"""

import argparse
import shutil
import sys
from importlib.resources import files
from pathlib import Path


DEPLOYMENT_TEMPLATES = files("agora_workbench.deployment.templates")
ACTIVITY_UI_TEMPLATES = files("activity_ui")
SKILLS = files("agora_workbench.skills")

DEFAULT_SKILL = "agora-workbench"

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


def _copy_template(name: str, dest_dir: Path) -> Path:
    """Copy a single template file to the destination, preserving subdirectories."""
    source = _template_source(name)
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
        target: Which template set to scaffold: "docker", "azure", "activity-ui", or "all".
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
        choices = ", ".join([*TEMPLATE_SETS, "all"])
        raise ValueError(f"Unknown target: {target!r}. Choose from: {choices}")

    created = []
    for set_name in sets_to_copy:
        for template_name in TEMPLATE_SETS[set_name]:
            path = _copy_template(template_name, dest)
            created.append(str(path))

    return created


def available_skills() -> list[str]:
    """Return the names of the agent skills bundled with the package."""
    return sorted(entry.name for entry in SKILLS.iterdir() if entry.is_dir() and not entry.name.startswith(("_", ".")))


def _skill_files(name: str) -> list[str]:
    """Return every file in a bundled skill tree, relative to the skill root."""
    root = SKILLS.joinpath(name)
    if not root.is_dir():
        choices = ", ".join(available_skills()) or "none"
        raise ValueError(f"Unknown skill: {name!r}. Available: {choices}")

    collected: list[str] = []

    def walk(node, prefix: str) -> None:
        for entry in node.iterdir():
            if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                continue
            relative = f"{prefix}{entry.name}"
            if entry.is_dir():
                walk(entry, f"{relative}/")
            else:
                collected.append(relative)

    walk(root, "")
    return sorted(collected)


def install_skill(name: str = DEFAULT_SKILL, output_dir: str = "skills", force: bool = False) -> list[str]:
    """Copy a bundled agent skill into an agent's skills directory.

    The skill is written to ``<output_dir>/<name>/`` so the result follows the
    Agent Skills layout (``skills/<name>/SKILL.md`` plus nested sub-skills).

    Args:
        name: Which bundled skill to install (see :func:`available_skills`).
        output_dir: Skills directory to install into, e.g. ``~/.claude/skills``.
        force: Replace an existing installation. The destination is removed
            first, so files dropped or renamed in a newer package version do
            not linger alongside the current ones.

    Returns:
        List of created file paths.

    Raises:
        ValueError: If no bundled skill matches ``name``.
        FileExistsError: If the destination exists and ``force`` is False.
    """
    relative_paths = _skill_files(name)
    skill_root = SKILLS.joinpath(name)
    dest = Path(output_dir).expanduser() / name

    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists. Pass force=True to replace it.")
        shutil.rmtree(dest)

    created = []
    for relative in relative_paths:
        source = skill_root.joinpath(*relative.split("/"))
        target = dest.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Skill prose contains non-ASCII (em dashes), so the encoding is
        # pinned rather than left to the platform default.
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(str(target))

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

    skill_parser = subparsers.add_parser(
        "skill",
        help="Install a bundled agent skill into your agent's skills directory.",
    )
    skill_parser.add_argument(
        "--name",
        default=DEFAULT_SKILL,
        help=f"Which bundled skill to install (default: {DEFAULT_SKILL}).",
    )
    skill_parser.add_argument(
        "--output-dir",
        "-o",
        default="skills",
        help="Skills directory to install into (default: ./skills). E.g. ~/.claude/skills.",
    )
    skill_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing skill directory (removes it first, so stale files are not left behind).",
    )
    skill_parser.add_argument(
        "--list",
        action="store_true",
        help="List the bundled skills and exit.",
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

    if args.command == "skill":
        if args.list:
            print("Bundled skills:")
            for skill_name in available_skills():
                print(f"  {skill_name}")
            return

        dest = Path(args.output_dir).expanduser() / args.name

        if dest.exists() and not args.force:
            print(f"⚠️  {dest}/ already exists.", file=sys.stderr)
            print("\nUse --force to overwrite.", file=sys.stderr)
            sys.exit(1)

        try:
            created = install_skill(name=args.name, output_dir=args.output_dir, force=args.force)
        except ValueError as exc:
            print(f"⚠️  {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"✓ Installed skill '{args.name}' ({len(created)} file(s)) into {dest}/:")
        for path in sorted(created):
            print(f"  {path}")
        print("\nNext steps:")
        print("  1. Point your agent client at the skills directory, or restart it to pick the skill up")
        print("  2. Connect the agent to your workbench server over Streamable HTTP at /mcp")


if __name__ == "__main__":
    main()
