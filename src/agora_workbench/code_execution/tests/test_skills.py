"""Tests for skill discovery and linked reference expansion."""

import logging

import pytest

from agora_workbench.code_execution.skills import discover_skills, load_skill_content


def _write_skill(skill_dir, content):
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


@pytest.mark.unit
def test_load_skill_content_without_references_is_unchanged(tmp_path):
    skill_path = _write_skill(tmp_path / "plain", "# Plain skill\n\nDo the thing.\n")

    assert load_skill_content(skill_path) == "# Plain skill\n\nDo the thing.\n"


@pytest.mark.unit
def test_discover_skills_expands_only_linked_references(tmp_path):
    skill_dir = tmp_path / "data-loading"
    skill_path = _write_skill(
        skill_dir,
        (
            "---\n"
            "name: data-loading\n"
            "description: Load data.\n"
            "---\n\n"
            "See [supported formats](references/supported-formats.md).\n"
        ),
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "supported-formats.md").write_text(
        "# Supported formats\n\nGeoParquet is supported.\n",
        encoding="utf-8",
    )
    (references_dir / "unlinked.md").write_text("This should stay hidden.\n", encoding="utf-8")

    skills = discover_skills(tmp_path, domain="gis")

    assert len(skills) == 1
    assert skills[0].path == str(skill_path)
    assert "[supported formats](#skill-reference-references-supported-formats)" in skills[0].content
    assert "GeoParquet is supported." in skills[0].content
    assert "This should stay hidden." not in skills[0].content


@pytest.mark.unit
def test_load_skill_content_deduplicates_repeated_references(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_path = _write_skill(
        skill_dir,
        ('Read [the table](references/table.md), then check [the table again](references/table.md "details").\n'),
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "table.md").write_text("UNIQUE REFERENCE BODY\n", encoding="utf-8")

    content = load_skill_content(skill_path)

    assert content.count("#skill-reference-references-table") == 2
    assert content.count("UNIQUE REFERENCE BODY") == 1
    assert content.count("### `references/table.md`") == 1


@pytest.mark.unit
def test_load_skill_content_disambiguates_anchor_collisions(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_path = _write_skill(
        skill_dir,
        "Read [hyphen](references/a-b.md) and [underscore](references/a_b.md).\n",
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "a-b.md").write_text("HYPHEN BODY\n", encoding="utf-8")
    (references_dir / "a_b.md").write_text("UNDERSCORE BODY\n", encoding="utf-8")

    content = load_skill_content(skill_path)

    assert "[hyphen](#skill-reference-references-a-b)" in content
    assert "[underscore](#skill-reference-references-a-b-2)" in content
    assert "HYPHEN BODY" in content
    assert "UNDERSCORE BODY" in content


@pytest.mark.unit
def test_load_skill_content_leaves_external_and_missing_links_unchanged(tmp_path, caplog):
    skill_path = _write_skill(
        tmp_path / "skill",
        ("See [external](https://example.com/reference.md) and [missing](references/missing.md).\n"),
    )

    with caplog.at_level(logging.WARNING):
        content = load_skill_content(skill_path)

    assert "[external](https://example.com/reference.md)" in content
    assert "[missing](references/missing.md)" in content
    assert "Included references" not in content
    assert "Failed to read skill reference references/missing.md" in caplog.text


@pytest.mark.unit
def test_load_skill_content_rejects_parent_traversal(tmp_path, caplog):
    skill_dir = tmp_path / "skill"
    skill_path = _write_skill(skill_dir, "See [secret](references/../secret.md).\n")
    (skill_dir / "secret.md").write_text("SECRET\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        content = load_skill_content(skill_path)

    assert "[secret](references/../secret.md)" in content
    assert "SECRET" not in content
    assert "Ignoring unsafe skill reference" in caplog.text


@pytest.mark.unit
def test_load_skill_content_rejects_symlink_outside_skill_directory(tmp_path, caplog):
    skill_dir = tmp_path / "skill"
    skill_path = _write_skill(skill_dir, "See [secret](references/secret.md).\n")
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET\n", encoding="utf-8")
    (references_dir / "secret.md").symlink_to(secret)

    with caplog.at_level(logging.WARNING):
        content = load_skill_content(skill_path)

    assert "[secret](references/secret.md)" in content
    assert "SECRET" not in content
    assert "Ignoring skill reference outside" in caplog.text


@pytest.mark.unit
def test_load_skill_content_does_not_expand_markdown_images(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_path = _write_skill(skill_dir, "![diagram](references/diagram.md)\n")
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "diagram.md").write_text("NOT AN IMAGE\n", encoding="utf-8")

    assert load_skill_content(skill_path) == "![diagram](references/diagram.md)\n"
