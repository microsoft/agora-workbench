"""
Domain Registry — maps domain/server names to domain-specific metadata.

Provides a clean separation between the generic MCP tools layer
(which handles server connectivity) and domain-specific concerns
(tool registries and prompt templates).

Architecture:
    domains/domain_registry.yaml  →  DomainRegistry (this module)
                                         ↑
                                    agent_bot/  reads from here
                                         ↓
                                    tools/ stays domain-agnostic

The tools/ layer should NOT import this module.  It is intended
for use by the agent layer (agent_bot/) which sits above both.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Dict

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DomainConfig:
    """Configuration for a domain's tools and prompts.

    Attributes:
        name: Domain/server name (matches the key in domain_registry.yaml
              and the ``name`` field in server_registry.yaml).
        tool_registry_module: Python module path containing the tool registry
              factory (e.g. ``domains.powergrid.server.tool_registry``).
        tool_registry_function: Name of the factory function that returns a
              ``ToolRegistry`` (e.g. ``create_powergrid_tool_registry``).
        domain_prompt_path: Relative path (from AgoraAgentMAF root) to a
              Jinja prompt template with domain-specific LLM instructions.
    """

    name: str
    tool_registry_module: Optional[str] = None
    tool_registry_function: Optional[str] = None
    domain_prompt_path: Optional[str] = None


class DomainRegistry:
    """Registry mapping domain/server names to their domain-specific configuration."""

    def __init__(self) -> None:
        self._domains: Dict[str, DomainConfig] = {}

    def register(self, config: DomainConfig) -> None:
        """Register a domain configuration."""
        self._domains[config.name] = config

    def get(self, name: str) -> Optional[DomainConfig]:
        """Get domain config by name (returns None if not found)."""
        return self._domains.get(name)

    def list_domains(self) -> Dict[str, DomainConfig]:
        """Return a copy of all registered domain configs."""
        return self._domains.copy()

    def get_domain_prompt_path(self, server_name: str) -> Optional[str]:
        """Convenience: resolve server name → domain_prompt_path (or None)."""
        config = self._domains.get(server_name)
        return config.domain_prompt_path if config else None


def _load_domain_registry() -> DomainRegistry:
    """Load domain configurations from ``domains/domain_registry.yaml``."""
    registry = DomainRegistry()

    yaml_path = Path(__file__).parent / "domain_registry.yaml"
    if not yaml_path.exists():
        LOGGER.warning(f"Domain registry YAML not found at {yaml_path}")
        return registry

    try:
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        if not data or "domains" not in data:
            LOGGER.warning(f"No 'domains' key found in {yaml_path}")
            return registry

        for name, config_data in data["domains"].items():
            config = DomainConfig(
                name=name,
                tool_registry_module=config_data.get("tool_registry_module"),
                tool_registry_function=config_data.get("tool_registry_function"),
                domain_prompt_path=config_data.get("domain_prompt_path"),
            )
            registry.register(config)

        LOGGER.info(f"Loaded {len(registry._domains)} domain config(s) from {yaml_path}")
    except yaml.YAMLError as e:
        LOGGER.error(f"Failed to parse {yaml_path}: {e}")
    except Exception as e:
        LOGGER.error(f"Failed to load domain registry: {e}")

    return registry


@lru_cache(maxsize=1)
def get_domain_registry() -> DomainRegistry:
    """Get the global domain registry (singleton, loaded once from YAML)."""
    return _load_domain_registry()


def reset_domain_registry() -> None:
    """Reset the cached registry (useful for testing)."""
    get_domain_registry.cache_clear()
