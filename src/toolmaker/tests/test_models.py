"""Tests for ToolMaker agent models."""


from toolmaker.models import (
    ArgumentSpec,
    ReturnFieldSpec,
    TaskSpec,
    ImplementationState,
    BuildStatus,
    GeneratedFile,
    TestResult,
    ToolPersistence,
)


# ── TaskSpec ─────────────────────────────────────────────────────────────


class TestTaskSpec:
    """Tests for TaskSpec model."""

    def test_empty_spec_is_incomplete(self):
        spec = TaskSpec()
        assert not spec.is_complete
        assert "repo_url" in spec.missing_fields
        assert "tool_name" in spec.missing_fields

    def test_complete_spec(self):
        spec = TaskSpec(
            repo_url="https://github.com/user/repo",
            task_description="Does something useful",
            tool_name="do_thing",
            domain_name="myrepo",
            arguments=[
                ArgumentSpec(name="input", type="str", description="Input data"),
            ],
            returns=[
                ReturnFieldSpec(name="result", type="str", description="Output data"),
            ],
        )
        assert spec.is_complete
        assert spec.missing_fields == []

    def test_python_signature(self):
        spec = TaskSpec(
            tool_name="calculate",
            arguments=[
                ArgumentSpec(name="expression", type="str", description="Math expression"),
                ArgumentSpec(name="precision", type="int", description="Decimal places", default="2"),
            ],
        )
        sig = spec.python_signature()
        assert sig == "def calculate(expression: str, precision: int = 2) -> dict:"

    def test_view_output(self):
        spec = TaskSpec(
            repo_url="https://github.com/user/repo",
            tool_name="my_tool",
            domain_name="myrepo",
        )
        view = spec.view()
        assert "Task Specification" in view
        assert "https://github.com/user/repo" in view
        assert "my_tool" in view
        assert "INCOMPLETE" in view

    def test_view_complete(self):
        spec = TaskSpec(
            repo_url="https://github.com/user/repo",
            task_description="Does something",
            tool_name="do_thing",
            domain_name="myrepo",
            arguments=[ArgumentSpec(name="x", type="int", description="value")],
            returns=[ReturnFieldSpec(name="result", type="int", description="output")],
        )
        view = spec.view()
        assert "COMPLETE" in view

    def test_missing_fields_partial(self):
        spec = TaskSpec(repo_url="https://github.com/user/repo", tool_name="my_tool")
        missing = spec.missing_fields
        assert "task_description" in missing
        assert "domain_name" in missing
        assert "repo_url" not in missing
        assert "tool_name" not in missing


# ── ArgumentSpec ─────────────────────────────────────────────────────────


class TestArgumentSpec:
    def test_repr_no_default(self):
        arg = ArgumentSpec(name="x", type="int", description="a number")
        assert "x: int" in repr(arg)
        assert "=" not in repr(arg)

    def test_repr_with_default(self):
        arg = ArgumentSpec(name="x", type="int", description="a number", default="42")
        assert "x: int = 42" in repr(arg)


# ── ImplementationState ──────────────────────────────────────────────────


class TestImplementationState:
    def test_initial_state(self):
        state = ImplementationState()
        assert state.iteration == 0
        assert state.build_status == BuildStatus.NOT_STARTED
        assert state.persistence == ToolPersistence.UNDECIDED
        assert state.generated_files == []
        assert state.test_results == []

    def test_view(self):
        state = ImplementationState(
            iteration=3,
            build_status=BuildStatus.PASSED,
            generated_files=[
                GeneratedFile(relative_path="server.py", content="..."),
                GeneratedFile(relative_path="tool_registry.py", content="..."),
            ],
            test_results=[
                TestResult(tool_name="calc", arguments={"x": "1"}, success=True, output="1"),
            ],
        )
        view = state.view()
        assert "3/30" in view
        assert "passed" in view
        assert "2" in view  # 2 files
        assert "PASS" in view

    def test_view_with_failure(self):
        state = ImplementationState(
            iteration=1,
            build_status=BuildStatus.TEST_FAILED,
            test_results=[
                TestResult(
                    tool_name="calc",
                    arguments={"x": "1"},
                    success=False,
                    error="ModuleNotFoundError: No module named 'foo'",
                ),
            ],
        )
        view = state.view()
        assert "FAIL" in view
        assert "ModuleNotFoundError" in view


# ── ToolPersistence ──────────────────────────────────────────────────────


class TestToolPersistence:
    def test_values(self):
        assert ToolPersistence.UNDECIDED == "undecided"
        assert ToolPersistence.SESSION_ONLY == "session_only"
        assert ToolPersistence.REUSABLE == "reusable"

    def test_persistence_field_on_impl_state(self):
        state = ImplementationState(persistence=ToolPersistence.REUSABLE)
        assert state.persistence == ToolPersistence.REUSABLE

    def test_persistence_in_view(self):
        """Persistence doesn't appear in view — just verify it doesn't crash."""
        state = ImplementationState(persistence=ToolPersistence.SESSION_ONLY)
        view = state.view()
        assert "Implementation State" in view
