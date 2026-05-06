"""
Tests for the Docker build script (src/deployment/mcp_server/build.py).

These tests validate:
- DomainConfig model parsing and validation
- Template rendering via the generate command
- The new-domain scaffolding command
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

# build.py lives in src/deployment/mcp_server/ — not a Python package, so we
# import it by file path using importlib.
import importlib.util

_BUILD_PY = Path(__file__).parent.parent.parent / "deployment" / "mcp_server" / "build.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build", str(_BUILD_PY))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


_build = _load_build_module()
DomainConfig = _build.DomainConfig
find_domain_configs = _build.find_domain_configs


# ---------------------------------------------------------------------------
# DomainConfig model tests
# ---------------------------------------------------------------------------


def test_domain_config_minimal():
    """DomainConfig with only required fields."""
    cfg = DomainConfig(
        name="example",
        module="domains.example.server.example_server",
        port=8000,
    )
    assert cfg.name == "example"
    assert cfg.service_name == "example-server"
    assert cfg.system_packages == []
    assert cfg.extra_files == []
    assert cfg.extra_env == {}
    assert cfg.gpu is False
    assert cfg.trusted_hosts is True
    assert cfg.memory_limit is None


def test_domain_config_service_name_replaces_underscores():
    """Domain names with underscores produce hyphenated service names."""
    cfg = DomainConfig(name="vitrimer_tg_sim", module="m", port=8010)
    assert cfg.service_name == "vitrimer-tg-sim-server"


def test_domain_config_full():
    """DomainConfig with all optional fields."""
    cfg = DomainConfig(
        name="chemistry",
        module="domains.chemistry.server.chemistry_server",
        port=8003,
        description="Chemistry domain",
        system_packages=["librdkit-dev"],
        extra_files=["states.py", "__init__.py"],
        extra_env={"RDKIT_DATA": "/opt/rdkit"},
        depends_on=["some-sidecar"],
        volumes=["../../data:/data:ro"],
        gpu=False,
        memory_limit="16g",
        trusted_hosts=True,
    )
    assert cfg.service_name == "chemistry-server"
    assert cfg.system_packages == ["librdkit-dev"]
    assert cfg.memory_limit == "16g"


def test_domain_config_from_yaml(tmp_path: Path):
    """DomainConfig can be loaded from a YAML file."""
    data = {
        "name": "example",
        "module": "domains.example.server.example_server",
        "port": 8000,
        "description": "Test domain",
        "extra_files": ["states.py"],
        "trusted_hosts": False,
    }
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(yaml.dump(data))

    raw = yaml.safe_load(yaml_file.read_text())
    cfg = DomainConfig.model_validate(raw)
    assert cfg.name == "example"
    assert cfg.port == 8000
    assert cfg.trusted_hosts is False


# ---------------------------------------------------------------------------
# find_domain_configs tests
# ---------------------------------------------------------------------------


def _make_domain(root: Path, name: str, port: int, **extra) -> Path:
    """Helper: write a minimal domain.yaml and return the domain dir."""
    domain_dir = root / "domains" / name
    domain_dir.mkdir(parents=True)
    data = {"name": name, "module": f"domains.{name}.server.{name}_server", "port": port, **extra}
    (domain_dir / "domain.yaml").write_text(yaml.dump(data))
    return domain_dir


def test_find_domain_configs_empty(tmp_path: Path):
    """Returns empty list when no domains exist."""
    (tmp_path / "domains").mkdir()
    assert find_domain_configs(tmp_path) == []


def test_find_domain_configs_no_domains_dir(tmp_path: Path):
    """Returns empty list when domains/ directory is absent."""
    assert find_domain_configs(tmp_path) == []


def test_find_domain_configs_single(tmp_path: Path):
    _make_domain(tmp_path, "example", 8000)
    configs = find_domain_configs(tmp_path)
    assert len(configs) == 1
    _, cfg = configs[0]
    assert cfg.name == "example"
    assert cfg.port == 8000


def test_find_domain_configs_multiple_sorted(tmp_path: Path):
    """Configs are returned in sorted (alphabetical) order."""
    _make_domain(tmp_path, "process", 8002)
    _make_domain(tmp_path, "example", 8000)
    _make_domain(tmp_path, "foundry", 8003)

    names = [cfg.name for _, cfg in find_domain_configs(tmp_path)]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# generate command — Dockerfile rendering
# ---------------------------------------------------------------------------


def test_generate_creates_dockerfile(tmp_path: Path):
    """generate writes a Dockerfile that starts with base.Dockerfile content."""
    _make_domain(tmp_path, "example", 8000, trusted_hosts=False)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(
            root=str(tmp_path),
            output_dir=str(output_dir),
            func=_build.cmd_generate,
        )
    )

    dockerfile = output_dir / "Dockerfile"
    assert dockerfile.exists()
    content = dockerfile.read_text()
    # Generated file should reference the base stage
    assert "FROM base AS example-server" in content
    # Auto-generated header should be present
    assert "Auto-generated by build.py" in content


def test_generate_renders_system_packages(tmp_path: Path):
    """Domain with system_packages gets USER root + apt-get block."""
    _make_domain(tmp_path, "lammps_domain", 8010, system_packages=["lammps"])
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "Dockerfile").read_text()
    assert "apt-get install" in content
    assert "lammps" in content
    assert "USER root" in content


def test_generate_renders_extra_env(tmp_path: Path):
    """Domain with extra_env gets ENV instructions."""
    _make_domain(tmp_path, "myserver", 8005, extra_env={"MY_KEY": "my_value"})
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "Dockerfile").read_text()
    assert 'ENV MY_KEY="my_value"' in content


def test_generate_skips_missing_fragment(tmp_path: Path, capsys):
    """Domain with dockerfile_fragment that doesn't exist is skipped with a warning."""
    _make_domain(tmp_path, "special", 8001, dockerfile_fragment="Dockerfile.fragment")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "special" in captured.err

    content = (output_dir / "Dockerfile").read_text()
    # The fragment stage should NOT appear since fragment is missing
    assert "special-server" not in content


def test_generate_includes_existing_fragment(tmp_path: Path):
    """Domain with dockerfile_fragment that exists is included verbatim."""
    domain_dir = _make_domain(tmp_path, "special", 8001, dockerfile_fragment="Dockerfile.fragment")
    fragment_content = textwrap.dedent("""\
        FROM base AS special-server
        COPY --chown=appuser:appuser domains/special/server /app/domains/special/server
        CMD ["python", "-m", "domains.special.server.special_server"]
    """)
    (domain_dir / "Dockerfile.fragment").write_text(fragment_content)

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "Dockerfile").read_text()
    assert "FROM base AS special-server" in content


# ---------------------------------------------------------------------------
# generate command — docker-compose rendering
# ---------------------------------------------------------------------------


def test_generate_creates_compose(tmp_path: Path):
    """generate writes a docker-compose.yml with anchors and a service entry."""
    _make_domain(tmp_path, "example", 8000, trusted_hosts=False)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    compose = output_dir / "docker-compose.yml"
    assert compose.exists()
    content = compose.read_text()
    assert "x-common-build" in content
    assert "x-base-env" in content
    assert "x-trusted-hosts" in content
    assert "x-common-healthcheck" in content
    assert "example-server:" in content


def test_generate_trusted_hosts_value(tmp_path: Path):
    """The *trusted-hosts anchor lists services with trusted_hosts=True."""
    _make_domain(tmp_path, "alpha", 8001, trusted_hosts=True)
    _make_domain(tmp_path, "beta", 8002, trusted_hosts=False)
    _make_domain(tmp_path, "gamma", 8003, trusted_hosts=True)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "docker-compose.yml").read_text()
    assert "alpha-server" in content.split("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS")[1]
    assert "gamma-server" in content.split("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS")[1]
    # beta has trusted_hosts=False so should NOT appear in the trusted list
    trusted_line = [line for line in content.splitlines() if "OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS" in line][0]
    assert "beta-server" not in trusted_line


def test_generate_sidecar_service_in_compose(tmp_path: Path):
    """Sidecar services from domain.yaml appear in generated docker-compose.yml."""
    sidecar = {"build": {"context": "./sidecar"}, "ports": ["9090:9090"]}
    _make_domain(tmp_path, "openlca", 8008, sidecar_services={"openlca-ipc": sidecar})
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "docker-compose.yml").read_text()
    assert "openlca-ipc:" in content


def test_generate_named_volumes_in_compose(tmp_path: Path):
    """Named volumes referenced by sidecars appear in the volumes: section."""
    sidecar = {"build": {"context": "./sidecar"}, "volumes": ["mydata:/app/data"]}
    _make_domain(tmp_path, "openlca", 8008, sidecar_services={"openlca-ipc": sidecar})
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _build.cmd_generate(
        _build.argparse.Namespace(root=str(tmp_path), output_dir=str(output_dir), func=_build.cmd_generate)
    )

    content = (output_dir / "docker-compose.yml").read_text()
    assert "volumes:" in content
    assert "mydata:" in content


# ---------------------------------------------------------------------------
# new command
# ---------------------------------------------------------------------------


def test_new_creates_expected_files(tmp_path: Path):
    """build.py new creates domain.yaml, server stub, and __init__.py files."""
    _build.cmd_new(_build.argparse.Namespace(name="chemistry", root=str(tmp_path), func=_build.cmd_new))

    domain_dir = tmp_path / "domains" / "chemistry"
    assert (domain_dir / "domain.yaml").exists()
    assert (domain_dir / "server" / "chemistry_server.py").exists()
    assert (domain_dir / "__init__.py").exists()
    assert (domain_dir / "server" / "__init__.py").exists()


def test_new_domain_yaml_content(tmp_path: Path):
    """The generated domain.yaml is a valid DomainConfig."""
    _build.cmd_new(_build.argparse.Namespace(name="chemistry", root=str(tmp_path), func=_build.cmd_new))

    data = yaml.safe_load((tmp_path / "domains" / "chemistry" / "domain.yaml").read_text())
    cfg = DomainConfig.model_validate(data)
    assert cfg.name == "chemistry"
    assert cfg.module == "domains.chemistry.server.chemistry_server"


def test_new_server_stub_content(tmp_path: Path):
    """The generated server stub imports CodeExecutionServer."""
    _build.cmd_new(_build.argparse.Namespace(name="chemistry", root=str(tmp_path), func=_build.cmd_new))

    content = (tmp_path / "domains" / "chemistry" / "server" / "chemistry_server.py").read_text()
    assert "CodeExecutionServer" in content
    assert "ChemistryServer" in content
    assert "main" in content


def test_new_invalid_name_exits(tmp_path: Path):
    """build.py new with an invalid Python identifier exits with code 1."""
    with pytest.raises(SystemExit) as exc_info:
        _build.cmd_new(_build.argparse.Namespace(name="my-domain", root=str(tmp_path), func=_build.cmd_new))
    assert exc_info.value.code == 1


def test_new_port_auto_assigned(tmp_path: Path):
    """Port is auto-assigned as max(existing_ports) + 1."""
    _make_domain(tmp_path, "existing", 8050)
    _build.cmd_new(_build.argparse.Namespace(name="newdomain", root=str(tmp_path), func=_build.cmd_new))
    data = yaml.safe_load((tmp_path / "domains" / "newdomain" / "domain.yaml").read_text())
    assert data["port"] == 8051


def test_new_does_not_overwrite_existing(tmp_path: Path, capsys):
    """Running new twice on the same domain name warns instead of overwriting."""
    _build.cmd_new(_build.argparse.Namespace(name="chemistry", root=str(tmp_path), func=_build.cmd_new))
    # Modify the file to detect if it gets overwritten
    yaml_path = tmp_path / "domains" / "chemistry" / "domain.yaml"
    original_mtime = yaml_path.stat().st_mtime

    _build.cmd_new(_build.argparse.Namespace(name="chemistry", root=str(tmp_path), func=_build.cmd_new))
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert yaml_path.stat().st_mtime == original_mtime  # file not modified
