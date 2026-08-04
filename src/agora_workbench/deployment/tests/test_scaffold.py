"""Tests for the deployment scaffold CLI."""

import glob
import tomllib
from pathlib import Path

import pytest

from agora_workbench.deployment import cli as deploy_cli
from agora_workbench.deployment.cli import init, TEMPLATE_SETS


REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES_DIR = Path(str(deploy_cli.DEPLOYMENT_TEMPLATES))


class TestScaffoldInit:
    @pytest.mark.unit
    def test_docker_target(self, tmp_path):
        created = init(target="docker", output_dir=str(tmp_path / "out"))
        assert len(created) == len(TEMPLATE_SETS["docker"])
        assert (tmp_path / "out" / "docker" / "base.Dockerfile").exists()
        assert (tmp_path / "out" / "docker" / "Dockerfile").exists()
        assert (tmp_path / "out" / "docker" / "docker-compose.yml").exists()
        assert (tmp_path / "out" / "docker" / ".env.server.example").exists()

    @pytest.mark.unit
    def test_azure_target(self, tmp_path):
        created = init(target="azure", output_dir=str(tmp_path / "out"))
        assert len(created) == len(TEMPLATE_SETS["azure"])
        assert (tmp_path / "out" / "azure" / "main.bicep").exists()
        assert (tmp_path / "out" / "azure" / "deploy-server.sh").exists()
        assert (tmp_path / "out" / "azure" / "README.md").exists()

    @pytest.mark.unit
    def test_activity_ui_target(self, tmp_path):
        created = init(target="activity-ui", output_dir=str(tmp_path / "out"))
        assert len(created) == len(TEMPLATE_SETS["activity-ui"])
        assert (tmp_path / "out" / "activity_ui" / "Dockerfile").exists()
        assert (tmp_path / "out" / "activity_ui" / "docker-compose.yml").exists()
        assert (tmp_path / "out" / "activity_ui" / "server.py").exists()
        assert (tmp_path / "out" / "activity_ui" / "static" / "index.html").exists()
        assert (tmp_path / "out" / "activity_ui" / "README.md").exists()
        compose = (tmp_path / "out" / "activity_ui" / "docker-compose.yml").read_text()
        assert "context: .." in compose
        assert "dockerfile: activity_ui/Dockerfile" in compose

    @pytest.mark.unit
    def test_all_target(self, tmp_path):
        created = init(target="all", output_dir=str(tmp_path / "out"))
        expected_count = sum(len(v) for v in TEMPLATE_SETS.values())
        assert len(created) == expected_count

    @pytest.mark.unit
    def test_shell_scripts_are_executable(self, tmp_path):
        init(target="azure", output_dir=str(tmp_path / "out"))
        deploy_sh = tmp_path / "out" / "azure" / "deploy.sh"
        assert deploy_sh.stat().st_mode & 0o111

    @pytest.mark.unit
    def test_invalid_target_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown target"):
            init(target="gcp", output_dir=str(tmp_path / "out"))

    @pytest.mark.unit
    def test_base_dockerfile_included(self, tmp_path):
        init(target="docker", output_dir=str(tmp_path / "out"))
        base = tmp_path / "out" / "docker" / "base.Dockerfile"
        assert base.exists()
        content = base.read_text()
        assert "mcp-server-base" in content or "miniforge" in content

    @pytest.mark.unit
    def test_base_dockerfile_builds_without_a_workbench_checkout(self, tmp_path):
        """The scaffolded base image must not depend on the build context.

        Consumers scaffold into their own project, where ``src/agora_workbench``
        and the workbench ``pyproject.toml`` do not exist. The default build
        installs the published package instead. See issue #286.
        """
        init(target="docker", output_dir=str(tmp_path / "out"))
        content = (tmp_path / "out" / "docker" / "base.Dockerfile").read_text()

        assert "ARG AGORA_WORKBENCH_SOURCE=pypi" in content
        assert 'pip install --no-input "agora-workbench==${AGORA_WORKBENCH_VERSION}"' in content

        # Anything read from the build context must be confined to the opt-in
        # workbench-local stage, never the default path.
        stages = content.split("\nFROM ")
        local_stage = [s for s in stages if s.startswith("base AS workbench-local")]
        assert len(local_stage) == 1
        for stage in stages:
            if stage.startswith("base AS workbench-local"):
                continue
            assert "COPY src/agora_workbench" not in stage
            assert "COPY pyproject.toml" not in stage


class TestMissingTemplateHandling:
    @pytest.mark.unit
    def test_missing_template_is_skipped_not_fatal(self, tmp_path, monkeypatch, capsys):
        """A template absent from the installed package must not abort the scaffold.

        Regression test for issue #286, where ``.env.server.example`` was missing
        from the published wheel and raised ``FileNotFoundError`` partway through
        ``init``, after other files had already been written.
        """
        monkeypatch.setitem(TEMPLATE_SETS, "docker", ["docker/Dockerfile", "docker/does-not-exist"])

        created = init(target="docker", output_dir=str(tmp_path / "out"))

        assert created == [str(tmp_path / "out" / "docker" / "Dockerfile")]
        assert (tmp_path / "out" / "docker" / "Dockerfile").exists()
        assert not (tmp_path / "out" / "docker" / "does-not-exist").exists()
        assert "does-not-exist" in capsys.readouterr().err


class TestTemplatePackaging:
    """Guards against templates being silently dropped from the built wheel."""

    @pytest.mark.unit
    def test_declared_templates_exist_in_source_tree(self):
        for names in TEMPLATE_SETS.values():
            for name in names:
                source = deploy_cli._template_source(name)
                assert source.is_file(), f"{name} is declared in TEMPLATE_SETS but missing"

    @pytest.mark.unit
    def test_package_data_patterns_cover_every_template(self):
        """Every template file must match a ``package-data`` pattern.

        ``**/*`` does not match dotfiles, which is why ``.env.server.example``
        was absent from the 0.1.1 wheel (issue #286). This reproduces setuptools'
        glob expansion so a newly added dotfile fails here rather than at runtime
        for someone who installed from PyPI.
        """
        pyproject = REPO_ROOT / "pyproject.toml"
        if not pyproject.is_file():
            pytest.skip("not running from a source checkout")

        config = tomllib.loads(pyproject.read_text())
        patterns = config["tool"]["setuptools"]["package-data"]["agora_workbench.deployment.templates"]

        matched = set()
        for pattern in patterns:
            matched.update(glob.glob(pattern, root_dir=TEMPLATES_DIR, recursive=True))

        uncovered = sorted(
            path.relative_to(TEMPLATES_DIR).as_posix()
            for path in TEMPLATES_DIR.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.relative_to(TEMPLATES_DIR).as_posix() not in matched
        )
        assert not uncovered, f"templates not covered by package-data patterns: {uncovered}"
