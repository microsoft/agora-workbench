"""
FunctionTools for registering the generated domain in AgoraAgentMAF's config files.

After the tool passes testing, these tools add entries to:
  - domains/domain_registry.yaml
  - server_registry.yaml
  - code_execution/docker/docker-compose.yml
  - code_execution/docker/Dockerfile (new stage)
"""

import logging
from pathlib import Path

import yaml

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

# AgoraAgentMAF root
_AGORA_MAF_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ── Pydantic input models ────────────────────────────────────────────────


class RegisterDomainInput(BaseModel):
    domain_name: str = Field(description="Domain name (e.g. 'stamp')")
    port: int = Field(description="Port number for the MCP server (choose an unused port, e.g. 8010)")
    tool_registry_module: str = Field(
        description="Python module path to the tool registry (e.g. 'domains.stamp.server.tool_registry')"
    )
    tool_registry_function: str = Field(
        description="Factory function name in the module (e.g. 'create_stamp_tool_registry')"
    )
    domain_prompt_path: str | None = Field(
        default=None,
        description="Optional path to Jinja2 domain prompt (e.g. 'domains/stamp/domain_prompt/stamp.jinja')",
    )


class ViewRegistrationStatusInput(BaseModel):
    domain_name: str = Field(description="Domain name to check registration status for")


# ── Tool factory ─────────────────────────────────────────────────────────


def create_registration_tools() -> list[FunctionTool]:
    """Create FunctionTools for domain registration."""

    async def register_domain(
        domain_name: str,
        port: int,
        tool_registry_module: str,
        tool_registry_function: str,
        domain_prompt_path: str | None = None,
    ) -> str:
        """Register the domain in all AgoraAgentMAF config files."""
        results = []

        # 0. Auto-generate a minimal domain prompt if none provided
        if not domain_prompt_path:
            try:
                prompt_dir = _AGORA_MAF_ROOT / "domains" / domain_name / "domain_prompt"
                prompt_dir.mkdir(parents=True, exist_ok=True)
                prompt_file = prompt_dir / f"{domain_name}.jinja"
                if not prompt_file.exists():
                    prompt_file.write_text(
                        f"You have access to the '{domain_name}' domain tools.\n"
                        f"Use search_tools to discover tools from this domain when the user's request "
                        f"relates to {domain_name} capabilities.\n"
                    )
                    results.append(f"✓ domain_prompt: generated {prompt_file.relative_to(_AGORA_MAF_ROOT)}")
                domain_prompt_path = str(prompt_file.relative_to(_AGORA_MAF_ROOT))
            except Exception as e:
                results.append(f"⊘ domain_prompt: auto-generation failed ({e}), continuing without")

        # 1. Update domain_registry.yaml
        try:
            domain_registry_path = _AGORA_MAF_ROOT / "domains" / "domain_registry.yaml"
            if domain_registry_path.exists():
                with open(domain_registry_path) as f:
                    registry = yaml.safe_load(f) or {}
            else:
                registry = {"domains": {}}

            domains = registry.setdefault("domains", {})
            entry = {
                "tool_registry_module": tool_registry_module,
                "tool_registry_function": tool_registry_function,
            }
            if domain_prompt_path:
                entry["domain_prompt_path"] = domain_prompt_path

            domains[domain_name] = entry

            with open(domain_registry_path, "w") as f:
                yaml.dump(registry, f, default_flow_style=False, sort_keys=False)

            results.append(f"✓ domain_registry.yaml: added '{domain_name}'")
        except Exception as e:
            results.append(f"✗ domain_registry.yaml: {e}")

        # 2. Update server_registry.yaml
        try:
            server_registry_path = _AGORA_MAF_ROOT / "server_registry.yaml"
            if server_registry_path.exists():
                with open(server_registry_path) as f:
                    srv_registry = yaml.safe_load(f) or {}
            else:
                srv_registry = {"servers": []}

            servers = srv_registry.setdefault("servers", [])
            # Check if already registered
            existing = [s for s in servers if s.get("name") == domain_name]
            if existing:
                results.append(f"⊘ server_registry.yaml: '{domain_name}' already registered")
            else:
                server_module = f"domains.{domain_name}.server.{domain_name}_server"
                servers.append(
                    {
                        "name": domain_name,
                        "module": server_module,
                        "config_function": f"create_{domain_name}_config",
                        "port": port,
                    }
                )

                with open(server_registry_path, "w") as f:
                    yaml.dump(srv_registry, f, default_flow_style=False, sort_keys=False)

                results.append(f"✓ server_registry.yaml: added '{domain_name}' on port {port}")
        except Exception as e:
            results.append(f"✗ server_registry.yaml: {e}")

        # 3. Add Dockerfile stage
        stage_name = f"{domain_name}-server"
        try:
            dockerfile_path = _AGORA_MAF_ROOT / "code_execution" / "docker" / "Dockerfile"
            if dockerfile_path.exists():
                content = dockerfile_path.read_text()
                if f"FROM base AS {stage_name}" in content:
                    results.append(f"⊘ Dockerfile: stage '{stage_name}' already exists")
                else:
                    new_stage = f"""
# ── {domain_name} domain ─────────────────────────────────
FROM base AS {stage_name}
COPY domains/{domain_name}/server/ /app/domains/{domain_name}/server/
RUN if [ -f /app/domains/{domain_name}/server/requirements.txt ]; then \\
        uv pip install --system -r /app/domains/{domain_name}/server/requirements.txt; \\
    fi
ENV PORT={port}
CMD ["python", "-m", "domains.{domain_name}.server.{domain_name}_server"]
"""
                    content += new_stage
                    dockerfile_path.write_text(content)
                    results.append(f"✓ Dockerfile: added stage '{stage_name}'")
            else:
                results.append("⊘ Dockerfile: not found (skipped)")
        except Exception as e:
            results.append(f"✗ Dockerfile: {e}")

        # 4. Add docker-compose service
        try:
            compose_path = _AGORA_MAF_ROOT / "code_execution" / "docker" / "docker-compose.yml"
            if compose_path.exists():
                with open(compose_path) as f:
                    compose = yaml.safe_load(f) or {}

                services = compose.setdefault("services", {})
                service_name = f"{domain_name}-server"
                if service_name in services:
                    results.append(f"⊘ docker-compose.yml: service '{service_name}' already exists")
                else:
                    services[service_name] = {
                        "build": {
                            "context": "../..",
                            "dockerfile": "code_execution/docker/Dockerfile",
                            "target": stage_name,
                        },
                        "ports": [f"{port}:{port}"],
                        "environment": [
                            "OBO_SIMULATION_MODE=true",
                            f"PORT={port}",
                        ],
                        "env_file": "../../.env",
                        "command": f"python -m domains.{domain_name}.server.{domain_name}_server",
                        "healthcheck": {
                            "test": f"curl -sf http://localhost:{port}/health || exit 1",
                            "interval": "30s",
                            "timeout": "10s",
                            "retries": 3,
                            "start_period": "30s",
                        },
                    }

                    with open(compose_path, "w") as f:
                        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

                    results.append(f"✓ docker-compose.yml: added service '{service_name}'")
            else:
                results.append("⊘ docker-compose.yml: not found (skipped)")
        except Exception as e:
            results.append(f"✗ docker-compose.yml: {e}")

        return "\n".join(results)

    async def view_registration_status(domain_name: str) -> str:
        """Check if a domain is registered in all config files."""
        lines = [f"Registration status for '{domain_name}':"]

        # domain_registry.yaml
        dr_path = _AGORA_MAF_ROOT / "domains" / "domain_registry.yaml"
        if dr_path.exists():
            with open(dr_path) as f:
                dr = yaml.safe_load(f) or {}
            if domain_name in dr.get("domains", {}):
                lines.append("  ✓ domain_registry.yaml: registered")
            else:
                lines.append("  ✗ domain_registry.yaml: NOT registered")
        else:
            lines.append("  ⊘ domain_registry.yaml: file not found")

        # server_registry.yaml
        sr_path = _AGORA_MAF_ROOT / "server_registry.yaml"
        if sr_path.exists():
            with open(sr_path) as f:
                sr = yaml.safe_load(f) or {}
            servers = sr.get("servers", [])
            match = [s for s in servers if s.get("name") == domain_name]
            if match:
                lines.append(f"  ✓ server_registry.yaml: registered on port {match[0].get('port', '?')}")
            else:
                lines.append("  ✗ server_registry.yaml: NOT registered")
        else:
            lines.append("  ⊘ server_registry.yaml: file not found")

        # Dockerfile
        df_path = _AGORA_MAF_ROOT / "code_execution" / "docker" / "Dockerfile"
        if df_path.exists():
            content = df_path.read_text()
            if f"{domain_name}-server" in content:
                lines.append("  ✓ Dockerfile: stage exists")
            else:
                lines.append("  ✗ Dockerfile: no stage found")
        else:
            lines.append("  ⊘ Dockerfile: file not found")

        # docker-compose.yml
        dc_path = _AGORA_MAF_ROOT / "code_execution" / "docker" / "docker-compose.yml"
        if dc_path.exists():
            with open(dc_path) as f:
                dc = yaml.safe_load(f) or {}
            if f"{domain_name}-server" in dc.get("services", {}):
                lines.append("  ✓ docker-compose.yml: service exists")
            else:
                lines.append("  ✗ docker-compose.yml: no service found")
        else:
            lines.append("  ⊘ docker-compose.yml: file not found")

        # Domain files
        domain_dir = _AGORA_MAF_ROOT / "domains" / domain_name / "server"
        if domain_dir.exists():
            files = [str(f.relative_to(domain_dir)) for f in domain_dir.rglob("*") if f.is_file()]
            lines.append(f"  ✓ Domain directory: {len(files)} files")
            for f in files[:10]:
                lines.append(f"      {f}")
        else:
            lines.append("  ✗ Domain directory: not found")

        return "\n".join(lines)

    return [
        FunctionTool(
            name="register_domain",
            description=(
                "Register the domain in all AgoraAgentMAF config files: "
                "domain_registry.yaml, server_registry.yaml, Dockerfile, and docker-compose.yml. "
                "Call this after the domain server is verified working."
            ),
            func=register_domain,
            input_model=RegisterDomainInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="view_registration_status",
            description="Check if a domain is registered in all config files.",
            func=view_registration_status,
            input_model=ViewRegistrationStatusInput,
            approval_mode="never_require",
        ),
    ]
