"""Tests for the deployment scaffold CLI."""

import glob
import tomllib
from pathlib import Path

import pytest

from agora_workbench.deployment import cli as deploy_cli
from agora_workbench.deployment.cli import (
    available_skills,
    DEFAULT_SKILL,
    init,
    install_skill,
    TEMPLATE_SETS,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
SKILLS_DIR = Path(str(deploy_cli.SKILLS))


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


class TestSkillInstall:
    @pytest.mark.unit
    def test_default_skill_is_bundled(self):
        assert DEFAULT_SKILL in available_skills()

    @pytest.mark.unit
    def test_installs_into_named_subdirectory(self, tmp_path):
        created = install_skill(output_dir=str(tmp_path / "skills"))
        root = tmp_path / "skills" / DEFAULT_SKILL

        assert (root / "SKILL.md").exists()
        assert len(created) == len(list(root.rglob("SKILL.md")))

    @pytest.mark.unit
    def test_nested_subskills_are_installed(self, tmp_path):
        """Sub-skills load on demand, so the nested tree must survive the copy."""
        install_skill(output_dir=str(tmp_path / "skills"))
        nested = tmp_path / "skills" / DEFAULT_SKILL / "skills"

        assert (nested / "artifacts" / "SKILL.md").exists()
        assert (nested / "async-execution" / "SKILL.md").exists()
        assert (nested / "workflow-planning" / "SKILL.md").exists()

    @pytest.mark.unit
    def test_front_matter_is_preserved(self, tmp_path):
        """Agent clients parse the YAML front matter to discover the skill."""
        install_skill(output_dir=str(tmp_path / "skills"))
        content = (tmp_path / "skills" / DEFAULT_SKILL / "SKILL.md").read_text()

        assert content.startswith("---\n")
        assert f"name: {DEFAULT_SKILL}" in content

    @pytest.mark.unit
    def test_output_dir_is_tilde_expanded(self, tmp_path, monkeypatch):
        """``-o ~/.claude/skills`` is the common invocation, so ~ must expand."""
        monkeypatch.setenv("HOME", str(tmp_path))
        created = install_skill(output_dir="~/.claude/skills")

        assert (tmp_path / ".claude" / "skills" / DEFAULT_SKILL / "SKILL.md").exists()
        assert not Path("~/.claude/skills").exists()
        assert all("~" not in path for path in created)

    @pytest.mark.unit
    def test_unknown_skill_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown skill"):
            install_skill(name="not-a-skill", output_dir=str(tmp_path / "skills"))

    @pytest.mark.unit
    def test_reinstall_overwrites_cleanly(self, tmp_path):
        install_skill(output_dir=str(tmp_path / "skills"))
        target = tmp_path / "skills" / DEFAULT_SKILL / "SKILL.md"
        target.write_text("stale")

        install_skill(output_dir=str(tmp_path / "skills"))

        assert target.read_text() != "stale"


class TestSkillPackaging:
    """Guards against the skill being dropped from the built wheel.

    The skill lived at the repository root until it was moved under
    ``agora_workbench.skills`` so that ``pip install agora-workbench`` ships it.
    Its directory names are hyphenated and therefore not importable packages,
    so it reaches the wheel only via ``package-data``.
    """

    @pytest.mark.unit
    def test_no_python_cache_files_are_installed(self, tmp_path):
        created = install_skill(output_dir=str(tmp_path / "skills"))
        assert all(not path.endswith((".pyc", ".pyo")) for path in created)
        assert not any("__pycache__" in path for path in created)

    @pytest.mark.unit
    def test_package_data_patterns_cover_every_skill_file(self):
        """Every bundled skill file must match a ``package-data`` pattern.

        ``**/*`` does not match dotfiles, which is why ``.env.server.example``
        was absent from the 0.1.1 wheel (issue #286). This reproduces
        setuptools' glob expansion so a newly added skill file fails here
        rather than silently vanishing from the published wheel.
        """
        pyproject = REPO_ROOT / "pyproject.toml"
        if not pyproject.is_file():
            pytest.skip("not running from a source checkout")

        config = tomllib.loads(pyproject.read_text())
        patterns = config["tool"]["setuptools"]["package-data"]["agora_workbench.skills"]

        matched = set()
        for pattern in patterns:
            matched.update(glob.glob(pattern, root_dir=SKILLS_DIR, recursive=True))

        uncovered = sorted(
            path.relative_to(SKILLS_DIR).as_posix()
            for path in SKILLS_DIR.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in (".pyc", ".pyo")
            and path.name != "__init__.py"
            and path.relative_to(SKILLS_DIR).as_posix() not in matched
        )
        assert not uncovered, f"skill files not covered by package-data patterns: {uncovered}"
