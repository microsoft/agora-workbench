"""Tests for the deployment scaffold CLI."""

import pytest

from agora_workbench.deployment.cli import init, TEMPLATE_SETS


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
