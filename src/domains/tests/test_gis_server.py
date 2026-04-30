"""Tests for GIS server config, domain prompt, and skills."""

from pathlib import Path

import pytest
import yaml

from domains.gis.server.gis_server import create_gis_config

DOMAINS_GIS_DIR = Path(__file__).resolve().parent.parent / "gis"

REQUIRED_PACKAGES = [
    "geopandas",
    "shapely",
    "rasterio",
    "fiona",
    "pyproj",
    "matplotlib",
]

EXPECTED_SKILLS = [
    "data-loading",
]


class TestGISServer:
    @pytest.mark.unit
    def test_create_gis_config(self):
        config = create_gis_config()
        assert config.name == "gis"
        assert config.type == "uv"
        assert "gis" in config.description.lower() or "geospatial" in config.description.lower()

    @pytest.mark.unit
    def test_required_packages_in_dependencies(self):
        config = create_gis_config()
        for pkg in REQUIRED_PACKAGES:
            assert pkg in config.dependency_file, f"Missing required package: {pkg}"

    @pytest.mark.unit
    def test_domain_prompt_exists(self):
        prompt_path = DOMAINS_GIS_DIR / "domain_prompt" / "gis.jinja"
        assert prompt_path.exists(), f"Domain prompt not found: {prompt_path}"
        content = prompt_path.read_text()
        assert len(content) > 100, "Domain prompt is too short"
        assert "CRS" in content or "crs" in content, "Domain prompt should mention CRS"

    @pytest.mark.unit
    def test_skill_files_exist(self):
        skills_dir = DOMAINS_GIS_DIR / "skills"
        assert skills_dir.is_dir(), f"Skills directory not found: {skills_dir}"

        for skill_name in EXPECTED_SKILLS:
            skill_md = skills_dir / skill_name / "SKILL.md"
            assert skill_md.exists(), f"Missing skill: {skill_md}"

    @pytest.mark.unit
    def test_skill_front_matter(self):
        """Each SKILL.md should have valid YAML front matter with name and description."""
        skills_dir = DOMAINS_GIS_DIR / "skills"

        for skill_name in EXPECTED_SKILLS:
            skill_md = skills_dir / skill_name / "SKILL.md"
            content = skill_md.read_text()

            assert content.startswith("---"), f"{skill_name}/SKILL.md missing front matter"
            end = content.index("---", 3)
            front_matter = yaml.safe_load(content[3:end])

            assert "name" in front_matter, f"{skill_name}/SKILL.md missing 'name' in front matter"
            assert "description" in front_matter, f"{skill_name}/SKILL.md missing 'description'"
            assert front_matter["name"] == skill_name

    @pytest.mark.unit
    def test_skill_references_exist(self):
        """Each skill should have a references/ directory with at least one file."""
        skills_dir = DOMAINS_GIS_DIR / "skills"

        for skill_name in EXPECTED_SKILLS:
            refs_dir = skills_dir / skill_name / "references"
            assert refs_dir.is_dir(), f"Missing references dir: {refs_dir}"
            ref_files = list(refs_dir.glob("*.md"))
            assert len(ref_files) >= 1, f"No reference files in {refs_dir}"
