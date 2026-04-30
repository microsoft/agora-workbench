"""Tests for prompt rendering with Jinja2."""

from pathlib import Path
import tempfile

import pytest
from jinja2.exceptions import TemplateNotFound
from ..prompts.renderer import (
    jinja_env,
    render_system_prompt,
    BASE_DIR,
)


class TestPromptRenderer:
    """Test cases for Jinja2 prompt rendering."""

    @pytest.mark.unit
    def test_render_with_domain_template(self):
        """Test rendering system prompt with a domain template."""
        # Create a temporary domain template in a known location
        temp_dir = Path(tempfile.mkdtemp(dir=BASE_DIR))
        domain_template = temp_dir / "test_domain.jinja"

        try:
            domain_template.write_text("""You are a test domain agent.

Your goal is to help with testing.""")

            # Use relative path from BASE_DIR
            relative_path = str(domain_template.relative_to(BASE_DIR))
            result = render_system_prompt(domain_prompt_path=relative_path)

            # Should include domain-specific content
            assert isinstance(result, str)
            assert len(result) > 0
            assert "test domain agent" in result.lower()

        finally:
            # Cleanup
            if domain_template.exists():
                domain_template.unlink()
            if temp_dir.exists():
                temp_dir.rmdir()

    @pytest.mark.unit
    def test_render_nonexistent_template(self):
        """Test rendering nonexistent template raises error."""
        with pytest.raises(Exception):
            render_system_prompt(domain_prompt_path="nonexistent/path/template.jinja")

    @pytest.mark.unit
    def test_render_with_multiple_domain_templates(self):
        """Test rendering system prompt with multiple domain templates."""
        temp_dir = Path(tempfile.mkdtemp(dir=BASE_DIR))
        domain_a = temp_dir / "domain_a.jinja"
        domain_b = temp_dir / "domain_b.jinja"

        try:
            domain_a.write_text("You are a domain A agent.")
            domain_b.write_text("You are a domain B agent.")

            path_a = str(domain_a.relative_to(BASE_DIR))
            path_b = str(domain_b.relative_to(BASE_DIR))
            result = render_system_prompt(domain_prompt_paths=[path_a, path_b])

            assert "domain A agent" in result
            assert "domain B agent" in result

        finally:
            for f in [domain_a, domain_b]:
                if f.exists():
                    f.unlink()
            if temp_dir.exists():
                temp_dir.rmdir()

    @pytest.mark.unit
    def test_render_with_single_and_list_deduplicates(self):
        """Test that providing both domain_prompt_path and domain_prompt_paths deduplicates."""
        temp_dir = Path(tempfile.mkdtemp(dir=BASE_DIR))
        domain_a = temp_dir / "domain_a.jinja"
        domain_b = temp_dir / "domain_b.jinja"

        try:
            domain_a.write_text("Domain A instructions.")
            domain_b.write_text("Domain B instructions.")

            path_a = str(domain_a.relative_to(BASE_DIR))
            path_b = str(domain_b.relative_to(BASE_DIR))

            # Pass the same path as both singular and in list — should not duplicate
            result = render_system_prompt(domain_prompt_path=path_a, domain_prompt_paths=[path_a, path_b])

            assert "Domain A instructions" in result
            assert "Domain B instructions" in result
            # Domain A content should appear only once
            assert result.count("Domain A instructions") == 1

        finally:
            for f in [domain_a, domain_b]:
                if f.exists():
                    f.unlink()
            if temp_dir.exists():
                temp_dir.rmdir()

    @pytest.mark.unit
    def test_render_without_any_domain_template(self):
        """Test rendering system prompt without any domain template still works."""
        result = render_system_prompt()

        assert isinstance(result, str)
        assert len(result) > 0
        # Should still include base agent instructions
        assert "---" in result

    @pytest.mark.unit
    def test_render_with_empty_domain_prompt_paths(self):
        """Test rendering with an empty list is equivalent to no domain prompt."""
        result_none = render_system_prompt()
        result_empty = render_system_prompt(domain_prompt_paths=[])

        assert result_none == result_empty


class TestPromptTemplates:
    """Test cases for existance of prompt templates."""

    @pytest.mark.unit
    def test_base_agent_instructions_exists(self):
        """Test that base agent instructions template exists."""
        try:
            template = jinja_env.get_template("base_agent_instructions.jinja")
            assert template is not None
        except TemplateNotFound:
            pytest.fail("base_agent_instructions.jinja template not found")

    @pytest.mark.unit
    def test_base_system_prompt_exists(self):
        """Test that base system prompt template exists."""
        try:
            template = jinja_env.get_template("base_system_prompt.jinja")
            assert template is not None
        except TemplateNotFound:
            pytest.fail("base_system_prompt.jinja template not found")
