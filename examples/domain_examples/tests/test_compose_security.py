"""
Regression guards for domain-example deployment manifests.

The domain example servers (`chemistry`, `energysystems`, `earthscience`,
…) all use ``create_noop_auth_config()`` for local development, which
accepts any bearer token without validation. To keep that safe, their
``docker-compose.yml`` files MUST publish their port to the loopback
interface only (``127.0.0.1:HOST_PORT:CONTAINER_PORT``) so the
unauthenticated ``execute_*_code`` tool is not reachable off-host.

This test fails if any domain example accidentally drops the
``127.0.0.1:`` prefix (which would expose arbitrary remote code execution
to anything that can reach the host's published port).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

DOMAIN_EXAMPLES_ROOT = Path(__file__).resolve().parent.parent


def _discover_compose_files() -> list[Path]:
    return sorted(DOMAIN_EXAMPLES_ROOT.glob("*/docker-compose.yml"))


@pytest.mark.parametrize(
    "compose_path",
    _discover_compose_files(),
    ids=lambda p: p.parent.name,
)
def test_domain_compose_publishes_to_loopback_only(compose_path: Path) -> None:
    """Every domain example must bind its published port to 127.0.0.1.

    The domain servers use ``create_noop_auth_config()`` which accepts any
    bearer token. Binding to ``0.0.0.0`` (the Docker default when no host
    address is given) would expose unauthenticated remote code execution.
    """
    spec = yaml.safe_load(compose_path.read_text())
    services = spec.get("services") or {}
    assert services, f"{compose_path} has no services"

    offenders: list[str] = []
    for service_name, service in services.items():
        for entry in service.get("ports") or []:
            # Only string short-syntax entries are checked here; long-syntax
            # entries (dicts) must set host_ip explicitly to "127.0.0.1".
            if isinstance(entry, dict):
                host_ip = entry.get("host_ip")
                if host_ip != "127.0.0.1":
                    offenders.append(f"{service_name}: long-syntax port host_ip={host_ip!r} (must be '127.0.0.1')")
                continue

            port_str = str(entry)
            # Short syntax forms that are safe:
            #   "127.0.0.1:HOST:CONTAINER"  ✅
            # Anything else (e.g. "8021:8000", "0.0.0.0:8021:8000",
            # "[::]:8021:8000") publishes on all interfaces and is rejected.
            if not port_str.startswith("127.0.0.1:"):
                offenders.append(f"{service_name}: port {port_str!r}")

    assert not offenders, (
        f"{compose_path.relative_to(DOMAIN_EXAMPLES_ROOT.parent)} publishes ports off-loopback "
        f"while the server uses noop auth — this exposes unauthenticated remote code execution. "
        f"Offending entries: {offenders}"
    )
