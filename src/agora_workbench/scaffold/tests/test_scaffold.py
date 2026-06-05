"""Tests for the deployment scaffold CLI."""

import pytest

from agora_workbench.scaffold.cli import init, TEMPLATE_SETS


class TestScaffoldInit:
    @pytest.mark.unit
    def test_docker_target(self, tmp_path):
        created = init(target="docker", output_dir=str(tmp_path / "out"))
        assert len(created) == 3
        assert (tmp_path / "out" / "Dockerfile").exists()
        assert (tmp_path / "out" / "docker-compose.yml").exists()
        assert (tmp_path / "out" / ".env.server.example").exists()

    @pytest.mark.unit
    def test_azure_target(self, tmp_path):
        created = init(target="azure", output_dir=str(tmp_path / "out"))
        assert len(created) == len(TEMPLATE_SETS["azure"])
        assert (tmp_path / "out" / "container_apps" / "main.bicep").exists()
        assert (tmp_path / "out" / "container_apps" / "deploy-server.sh").exists()

    @pytest.mark.unit
    def test_all_target(self, tmp_path):
        created = init(target="all", output_dir=str(tmp_path / "out"))
        expected_count = sum(len(v) for v in TEMPLATE_SETS.values())
        assert len(created) == expected_count

    @pytest.mark.unit
    def test_shell_scripts_are_executable(self, tmp_path):
        init(target="azure", output_dir=str(tmp_path / "out"))
        deploy_sh = tmp_path / "out" / "container_apps" / "deploy.sh"
        assert deploy_sh.stat().st_mode & 0o111

    @pytest.mark.unit
    def test_invalid_target_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown target"):
            init(target="gcp", output_dir=str(tmp_path / "out"))
