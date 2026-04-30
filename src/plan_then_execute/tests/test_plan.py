"""Unit tests for the Plan data structure."""

import pytest

from ..plan import Plan, PlanStep, StepStatus


class TestPlanStep:
    """Tests for individual PlanStep objects."""

    @pytest.mark.unit
    def test_step_defaults(self):
        step = PlanStep(description="Do something", step_id=1)
        assert step.step_id == 1
        assert step.description == "Do something"
        assert step.status == StepStatus.PENDING
        assert step.notes == ""

    @pytest.mark.unit
    def test_step_to_dict(self):
        step = PlanStep(description="Analyze data", step_id=3)
        step.status = StepStatus.COMPLETED
        step.notes = "All good"
        d = step.to_dict()
        assert d == {
            "step_id": 3,
            "description": "Analyze data",
            "status": "completed",
            "notes": "All good",
        }


class TestPlan:
    """Tests for Plan operations."""

    @pytest.mark.unit
    def test_empty_plan(self):
        plan = Plan()
        assert plan.steps == []
        assert plan.finalized is False
        assert "empty" in plan.view().lower()

    @pytest.mark.unit
    def test_add_steps(self):
        plan = Plan()
        s1 = plan.add_step("Step one")
        s2 = plan.add_step("Step two")
        assert s1.step_id == 1
        assert s2.step_id == 2
        assert len(plan.steps) == 2

    @pytest.mark.unit
    def test_insert_step_after(self):
        plan = Plan()
        plan.add_step("First")
        plan.add_step("Third")
        inserted = plan.insert_step(after_step_id=1, description="Second")
        assert inserted.step_id == 3
        descriptions = [s.description for s in plan.steps]
        assert descriptions == ["First", "Second", "Third"]

    @pytest.mark.unit
    def test_insert_step_at_beginning(self):
        plan = Plan()
        plan.add_step("Was first")
        inserted = plan.insert_step(after_step_id=0, description="Now first")
        descriptions = [s.description for s in plan.steps]
        assert descriptions == ["Now first", "Was first"]
        assert inserted.step_id == 2

    @pytest.mark.unit
    def test_insert_step_invalid_id(self):
        plan = Plan()
        plan.add_step("Only step")
        with pytest.raises(ValueError, match="not found"):
            plan.insert_step(after_step_id=999, description="Orphan")

    @pytest.mark.unit
    def test_update_step(self):
        plan = Plan()
        plan.add_step("Original")
        updated = plan.update_step(1, description="Revised", notes="Changed my mind")
        assert updated.description == "Revised"
        assert updated.notes == "Changed my mind"

    @pytest.mark.unit
    def test_update_step_partial(self):
        plan = Plan()
        plan.add_step("Original")
        plan.update_step(1, notes="Just a note")
        step = plan.steps[0]
        assert step.description == "Original"
        assert step.notes == "Just a note"

    @pytest.mark.unit
    def test_update_step_not_found(self):
        plan = Plan()
        with pytest.raises(ValueError, match="not found"):
            plan.update_step(42, description="No such step")

    @pytest.mark.unit
    def test_set_step_status(self):
        plan = Plan()
        plan.add_step("Do thing")
        plan.set_step_status(1, StepStatus.IN_PROGRESS)
        assert plan.steps[0].status == StepStatus.IN_PROGRESS
        plan.set_step_status(1, StepStatus.COMPLETED, notes="Done!")
        assert plan.steps[0].status == StepStatus.COMPLETED
        assert plan.steps[0].notes == "Done!"

    @pytest.mark.unit
    def test_remove_step(self):
        plan = Plan()
        plan.add_step("Keep")
        plan.add_step("Remove me")
        removed = plan.remove_step(2)
        assert removed.description == "Remove me"
        assert len(plan.steps) == 1

    @pytest.mark.unit
    def test_remove_step_not_found(self):
        plan = Plan()
        with pytest.raises(ValueError, match="not found"):
            plan.remove_step(99)

    @pytest.mark.unit
    def test_view(self):
        plan = Plan()
        plan.add_step("Gather data")
        plan.add_step("Run analysis")
        plan.set_step_status(1, StepStatus.COMPLETED, notes="OK")
        view = plan.view()
        assert "Gather data" in view
        assert "Run analysis" in view
        assert "[✓]" in view
        assert "[ ]" in view

    @pytest.mark.unit
    def test_finalize(self):
        plan = Plan()
        plan.add_step("One step")
        result = plan.finalize()
        assert plan.finalized is True
        assert "finalized" in result.lower()

    @pytest.mark.unit
    def test_finalize_empty_plan(self):
        plan = Plan()
        result = plan.finalize()
        assert plan.finalized is False
        assert "empty" in result.lower()

    @pytest.mark.unit
    def test_is_complete(self):
        plan = Plan()
        assert plan.is_complete() is False

        plan.add_step("A")
        plan.add_step("B")
        assert plan.is_complete() is False

        plan.set_step_status(1, StepStatus.COMPLETED)
        assert plan.is_complete() is False

        plan.set_step_status(2, StepStatus.SKIPPED)
        assert plan.is_complete() is True

    @pytest.mark.unit
    def test_is_complete_with_failed(self):
        plan = Plan()
        plan.add_step("A")
        plan.set_step_status(1, StepStatus.FAILED)
        assert plan.is_complete() is True
