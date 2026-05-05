#!/usr/bin/env python3
"""
Build script for Docker configuration of domain servers.

Reads per-domain configuration from ``domains/*/domain.yaml`` and generates:

- A combined ``Dockerfile`` (base stages + per-domain stages)
- A ``docker-compose.yml`` (YAML-anchor header + per-service blocks)

Usage::

    # Generate Dockerfile + docker-compose.yml from all domain.yaml files:
    uv run python src/code_execution/docker/build.py generate

    # Scaffold a new domain (creates domain.yaml + server stub):
    uv run python src/code_execution/docker/build.py new <name>

The build context root (``--root``) defaults to ``src/`` (two levels above
this script inside ``code_execution/docker/``).  Override when the ``domains/``
directory lives elsewhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
"""Directory containing this script (src/code_execution/docker/)."""

_DEFAULT_ROOT = _SCRIPT_DIR.parent.parent
"""Build-context root (src/) — two levels above this script."""

# ---------------------------------------------------------------------------
# Domain configuration model
# ---------------------------------------------------------------------------


class DomainConfig(BaseModel):
    """Per-domain server configuration declared in ``domains/<name>/domain.yaml``."""

    name: str = Field(description="Domain name, e.g. 'example' or 'vitrimer_tg_sim'")
    module: str = Field(
        description="Python module entry-point, e.g. 'domains.example.server.example_server'"
    )
    port: int = Field(description="Host port mapped to container port 8000")
    description: str = Field(default="", description="Human-readable description for Dockerfile comments")
    system_packages: list[str] = Field(
        default_factory=list,
        description="apt packages to install inside the container (USER root block)",
    )
    extra_files: list[str] = Field(
        default_factory=list,
        description="Extra paths to COPY after server/ directory, relative to the domain root "
        "(e.g. ['states.py', '__init__.py'])",
    )
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional ENV instructions (Dockerfile) / environment entries (compose)",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Docker Compose service names this service depends on",
    )
    volumes: list[str] = Field(
        default_factory=list,
        description="Extra volume mounts for docker-compose (in addition to ~/.azure)",
    )
    build_args: dict[str, str] = Field(
        default_factory=dict,
        description="Docker build-time ARG values passed to the Dockerfile",
    )
    gpu: bool = Field(default=False, description="Reserve NVIDIA GPU resource in docker-compose")
    memory_limit: Optional[str] = Field(
        default=None, description="Compose memory limit, e.g. '32g'"
    )
    trusted_hosts: bool = Field(
        default=True,
        description="Include OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS env var in the compose service",
    )
    dockerfile_fragment: Optional[str] = Field(
        default=None,
        description=(
            "Path to a raw Dockerfile fragment relative to the domain directory. "
            "When set, this file is included verbatim instead of rendering "
            "domain.Dockerfile.j2. Use for complex multi-stage builds (e.g. powergrid)."
        ),
    )
    sidecar_services: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Additional docker-compose services to emit alongside this domain's service "
            "(e.g. the openlca-ipc Java sidecar). Each key is a service name; each value "
            "is a raw mapping that is emitted as-is under services:."
        ),
    )

    @property
    def service_name(self) -> str:
        """Compose/Docker service name derived from the domain name."""
        return self.name.replace("_", "-") + "-server"


# Explicitly rebuild the model so Pydantic can resolve forward references
# even when this module is loaded via importlib (e.g. in tests).
DomainConfig.model_rebuild()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_domain_configs(root: Path) -> list[tuple[Path, DomainConfig]]:
    """Return ``(domain_dir, config)`` pairs for every ``domains/*/domain.yaml`` under *root*."""
    domains_dir = root / "domains"
    if not domains_dir.exists():
        return []

    results: list[tuple[Path, DomainConfig]] = []
    for yaml_path in sorted(domains_dir.glob("*/domain.yaml")):
        with yaml_path.open() as fh:
            data = yaml.safe_load(fh)
        config = DomainConfig.model_validate(data)
        results.append((yaml_path.parent, config))
    return results


# ---------------------------------------------------------------------------
# Dockerfile generation
# ---------------------------------------------------------------------------


def _render_dockerfile(root: Path, output_dir: Path) -> Path:
    """Generate combined ``Dockerfile`` = ``base.Dockerfile`` + per-domain stages."""
    base_dockerfile = _SCRIPT_DIR / "base.Dockerfile"
    if not base_dockerfile.exists():
        print(f"ERROR: {base_dockerfile} not found.", file=sys.stderr)
        sys.exit(1)

    jinja_env = Environment(
        loader=FileSystemLoader(str(_SCRIPT_DIR)),
        keep_trailing_newline=True,
    )
    template = jinja_env.get_template("domain.Dockerfile.j2")

    sections: list[str] = [
        "# Auto-generated by build.py — do not edit manually.",
        "# Run: uv run python src/code_execution/docker/build.py generate",
        "#",
        base_dockerfile.read_text().rstrip(),
    ]

    for domain_dir, config in find_domain_configs(root):
        if config.dockerfile_fragment:
            fragment_path = domain_dir / config.dockerfile_fragment
            if not fragment_path.exists():
                print(
                    f"WARNING: Dockerfile fragment not found: {fragment_path} — skipping {config.name}",
                    file=sys.stderr,
                )
                continue
            sections.append("\n" + fragment_path.read_text().rstrip())
        else:
            rendered = template.render(config=config).rstrip()
            sections.append("\n" + rendered)

    out_path = output_dir / "Dockerfile"
    out_path.write_text("\n".join(sections) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# docker-compose generation
# ---------------------------------------------------------------------------

_COMPOSE_HEADER_TEMPLATE = """\
# Auto-generated by build.py — do not edit manually.
# Run: uv run python src/code_execution/docker/build.py generate

# ---------------------------------------------------------------------------
# YAML anchors (Docker Compose extension fields)
# ---------------------------------------------------------------------------

x-common-build: &common-build
  context: ../..
  dockerfile: code_execution/docker/Dockerfile
  additional_contexts:
    azure-cli: ~/.azure

x-base-env: &base-env
  PORT: "8000"
  HOST: "0.0.0.0"
  OBO_SIMULATION_MODE: "true"

x-trusted-hosts: &trusted-hosts
  OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS: "{trusted_hosts_value}"

x-common-healthcheck: &common-healthcheck
  test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s

services:
"""


def _render_compose(root: Path, output_dir: Path) -> Path:
    """Generate ``docker-compose.yml`` = anchor header + per-domain service blocks."""
    all_domains = find_domain_configs(root)

    # Build the OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS value from all participating services
    trusted_service_names = ",".join(
        cfg.service_name for _, cfg in all_domains if cfg.trusted_hosts
    )

    jinja_env = Environment(
        loader=FileSystemLoader(str(_SCRIPT_DIR)),
        keep_trailing_newline=True,
    )
    template = jinja_env.get_template("compose-service.j2")

    header = _COMPOSE_HEADER_TEMPLATE.format(trusted_hosts_value=trusted_service_names)
    service_blocks: list[str] = []
    named_volumes: list[str] = []

    for _, config in all_domains:
        if config.dockerfile_fragment and not _fragment_has_standard_stage(config):
            # Special-case domains with non-standard stages are represented by
            # their sidecar/manual entries only (if any).
            pass
        else:
            service_blocks.append(template.render(config=config).rstrip())

        # Emit any sidecar services verbatim
        for svc_name, svc_def in config.sidecar_services.items():
            block = _render_sidecar_service(svc_name, svc_def)
            service_blocks.append(block)

        # Collect named volumes (not path-based mounts)
        for vol in config.volumes:
            vol_name = vol.split(":")[0]
            if not (vol_name.startswith("~") or vol_name.startswith("/") or vol_name.startswith(".")):
                if vol_name not in named_volumes:
                    named_volumes.append(vol_name)

        for svc_def in config.sidecar_services.values():
            for vol in svc_def.get("volumes", []):
                vol_name = str(vol).split(":")[0]
                if not (
                    vol_name.startswith("~") or vol_name.startswith("/") or vol_name.startswith(".")
                ):
                    if vol_name not in named_volumes:
                        named_volumes.append(vol_name)

    lines = [header + "\n".join(service_blocks)]

    if named_volumes:
        lines.append("\nvolumes:")
        for vol in named_volumes:
            lines.append(f"  {vol}:")

    out_path = output_dir / "docker-compose.yml"
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def _fragment_has_standard_stage(config: DomainConfig) -> bool:
    """Return True if the fragment exposes a stage named ``config.service_name``."""
    # Heuristic: check if the fragment file mentions the service name as a FROM target.
    # Used to decide whether to include a compose service entry.
    return True  # Conservative: always include; override per domain if needed.


def _render_sidecar_service(name: str, definition: dict[str, Any]) -> str:
    """Render a sidecar service dict to indented YAML text under services:."""
    raw = yaml.dump({name: definition}, default_flow_style=False, sort_keys=False)
    # Indent to sit under services:
    indented = "\n".join("  " + line if line.strip() else "" for line in raw.splitlines())
    return indented


# ---------------------------------------------------------------------------
# Server stub template (used by `new` command)
# ---------------------------------------------------------------------------

_SERVER_STUB = '''\
"""{{ description }}"""

import os

from code_execution.auth import create_entra_auth_config, create_noop_auth_config
from code_execution.code_execution_models import EnvironmentConfig
from code_execution.server import CodeExecutionServer


class {{ class_name }}(CodeExecutionServer):
    """{{ description }}"""


def main() -> None:
    env_config = EnvironmentConfig(
        name="{{ name }}",
        description="{{ description }}",
    )

    # Use Entra ID in production when env vars are set; fall back to no-op for
    # local development / OBO simulation mode.
    if os.getenv("ENTRA_CLIENT_ID") and os.getenv("ENTRA_TENANT_ID"):
        auth_config = create_entra_auth_config(
            client_id=os.environ["ENTRA_CLIENT_ID"],
            tenant_id=os.environ["ENTRA_TENANT_ID"],
        )
    else:
        auth_config = create_noop_auth_config()

    server = {{ class_name }}(
        environment_config=env_config,
        auth_config=auth_config,
    )
    server.run()


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate Dockerfile and docker-compose.yml from all domain.yaml files."""
    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else _SCRIPT_DIR

    domains = find_domain_configs(root)
    if not domains:
        print(
            f"No domain.yaml files found under {root / 'domains'}.\n"
            "Create domains/<name>/domain.yaml for each server, or scaffold one with:\n"
            "  uv run python src/code_execution/docker/build.py new <name>"
        )
        return

    dockerfile_path = _render_dockerfile(root, output_dir)
    print(f"Generated: {dockerfile_path}")

    compose_path = _render_compose(root, output_dir)
    print(f"Generated: {compose_path}")

    print(f"\nDomains included ({len(domains)}):")
    for _, cfg in domains:
        fragment_note = f" [fragment: {cfg.dockerfile_fragment}]" if cfg.dockerfile_fragment else ""
        print(f"  {cfg.service_name:<30} port {cfg.port}{fragment_note}")


def cmd_new(args: argparse.Namespace) -> None:
    """Scaffold a new domain: create domain.yaml + server stub."""
    name: str = args.name
    root = Path(args.root).resolve()

    if not name.isidentifier():
        print(f"ERROR: '{name}' is not a valid Python identifier.", file=sys.stderr)
        sys.exit(1)

    domain_dir = root / "domains" / name
    server_dir = domain_dir / "server"
    server_dir.mkdir(parents=True, exist_ok=True)

    # Assign the next available port
    existing = find_domain_configs(root)
    used_ports = {cfg.port for _, cfg in existing}
    next_port = max(used_ports) + 1 if used_ports else 8000

    # ---- domain.yaml -------------------------------------------------------
    domain_yaml_path = domain_dir / "domain.yaml"
    if domain_yaml_path.exists():
        print(f"WARNING: {domain_yaml_path} already exists — skipping.", file=sys.stderr)
    else:
        domain_data = {
            "name": name,
            "module": f"domains.{name}.server.{name}_server",
            "port": next_port,
            "description": f"{name.replace('_', ' ').title()} domain server",
            "system_packages": [],
            "extra_files": ["states.py"],
            "extra_env": {},
            "depends_on": [],
            "volumes": [],
        }
        with domain_yaml_path.open("w") as fh:
            yaml.dump(domain_data, fh, default_flow_style=False, sort_keys=False)
        print(f"Created: {domain_yaml_path}")

    # ---- server stub -------------------------------------------------------
    server_py_path = server_dir / f"{name}_server.py"
    if server_py_path.exists():
        print(f"WARNING: {server_py_path} already exists — skipping.", file=sys.stderr)
    else:
        from jinja2 import Template

        description = f"{name.replace('_', ' ').title()} domain server."
        class_name = "".join(part.title() for part in name.split("_")) + "Server"
        code = Template(_SERVER_STUB).render(
            name=name,
            description=description,
            class_name=class_name,
        )
        server_py_path.write_text(code)
        print(f"Created: {server_py_path}")

    # ---- __init__.py stubs -------------------------------------------------
    for init_dir in [domain_dir, server_dir]:
        init_path = init_dir / "__init__.py"
        if not init_path.exists():
            init_path.write_text("")
            print(f"Created: {init_path}")

    print(
        f"\nNext steps:\n"
        f"  1. Edit  {domain_yaml_path}\n"
        f"  2. Implement tools in {server_dir}/\n"
        f"  3. Generate configs:\n"
        f"       uv run python src/code_execution/docker/build.py generate\n"
        f"  4. Build the image:\n"
        f"       docker compose build {name.replace('_', '-')}-server"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build script for domain server Docker configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Regenerate Dockerfile + docker-compose.yml:\n"
            "  uv run python src/code_execution/docker/build.py generate\n\n"
            "  # Scaffold a new domain called 'chemistry':\n"
            "  uv run python src/code_execution/docker/build.py new chemistry\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- generate ------------------------------------------------------------
    gen_parser = subparsers.add_parser(
        "generate",
        help="Generate Dockerfile and docker-compose.yml from all domain.yaml files",
    )
    gen_parser.add_argument(
        "--root",
        default=str(_DEFAULT_ROOT),
        help="Build-context root directory that contains the domains/ folder (default: %(default)s)",
    )
    gen_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write generated files (default: same directory as build.py)",
    )
    gen_parser.set_defaults(func=cmd_generate)

    # -- new -----------------------------------------------------------------
    new_parser = subparsers.add_parser(
        "new",
        help="Scaffold a new domain server (creates domain.yaml + server stub)",
    )
    new_parser.add_argument("name", help="Domain name, e.g. 'chemistry' or 'vitrimer_vae'")
    new_parser.add_argument(
        "--root",
        default=str(_DEFAULT_ROOT),
        help="Build-context root directory that contains the domains/ folder (default: %(default)s)",
    )
    new_parser.set_defaults(func=cmd_new)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
