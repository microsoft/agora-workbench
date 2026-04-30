"""Unit tests for PlanStore."""

import pytest

from ..models import StepStatus
from ..store import PlanStore


@pytest.fixture
def store():
    """In-memory PlanStore for fast unit tests."""
    s = PlanStore()
    yield s
    s.close()


class TestPlanStoreBasics:
    """Core CRUD operations matching the original Plan interface."""

    @pytest.mark.unit
    def test_empty_plan(self, store):
        assert store.steps == []
        assert store.finalized is False
        assert "empty" in store.view().lower()

    @pytest.mark.unit
    def test_add_steps(self, store):
        s1 = store.add_step("Step one")
        s2 = store.add_step("Step two")
        assert s1.step_id is not None
        assert s2.step_id is not None
        assert s1.step_id != s2.step_id
        assert len(store.steps) == 2

    @pytest.mark.unit
    def test_step_defaults(self, store):
        step = store.add_step("Do something")
        assert step.description == "Do something"
        assert step.status == StepStatus.PENDING
        assert step.notes == ""
        assert step.tags == ()
        assert step.depends_on == ()

    @pytest.mark.unit
    def test_step_to_dict(self, store):
        step = store.add_step("Analyze data")
        d = step.to_dict()
        assert d["description"] == "Analyze data"
        assert d["status"] == "pending"
        assert d["notes"] == ""

    @pytest.mark.unit
    def test_insert_step_after(self, store):
        s1 = store.add_step("First")
        store.add_step("Third")
        inserted = store.insert_step(after_step_id=s1.step_id, description="Second")
        descriptions = [s.description for s in store.steps]
        assert descriptions == ["First", "Second", "Third"]
        assert inserted.description == "Second"

    @pytest.mark.unit
    def test_insert_step_at_beginning(self, store):
        store.add_step("Was first")
        inserted = store.insert_step(after_step_id=0, description="Now first")
        descriptions = [s.description for s in store.steps]
        assert descriptions == ["Now first", "Was first"]
        assert inserted.description == "Now first"

    @pytest.mark.unit
    def test_insert_step_invalid_id(self, store):
        store.add_step("Only step")
        with pytest.raises(ValueError, match="not found"):
            store.insert_step(after_step_id=9999, description="Orphan")

    @pytest.mark.unit
    def test_update_step(self, store):
        s = store.add_step("Original")
        updated = store.update_step(s.step_id, description="Revised", notes="Changed")
        assert updated.description == "Revised"
        assert updated.notes == "Changed"

    @pytest.mark.unit
    def test_update_step_partial(self, store):
        s = store.add_step("Original")
        store.update_step(s.step_id, notes="Just a note")
        step = store.steps[0]
        assert step.description == "Original"
        assert step.notes == "Just a note"

    @pytest.mark.unit
    def test_update_step_not_found(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.update_step(9999, description="No such step")

    @pytest.mark.unit
    def test_set_step_status(self, store):
        s = store.add_step("Do thing")
        store.set_step_status(s.step_id, StepStatus.IN_PROGRESS)
        assert store.steps[0].status == StepStatus.IN_PROGRESS
        store.set_step_status(s.step_id, StepStatus.COMPLETED, notes="Done!")
        assert store.steps[0].status == StepStatus.COMPLETED
        assert store.steps[0].notes == "Done!"

    @pytest.mark.unit
    def test_remove_step(self, store):
        store.add_step("Keep")
        s2 = store.add_step("Remove me")
        removed = store.remove_step(s2.step_id)
        assert removed.description == "Remove me"
        assert len(store.steps) == 1

    @pytest.mark.unit
    def test_remove_step_not_found(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.remove_step(9999)

    @pytest.mark.unit
    def test_view(self, store):
        s1 = store.add_step("Gather data")
        store.add_step("Run analysis")
        store.set_step_status(s1.step_id, StepStatus.COMPLETED, notes="OK")
        view = store.view()
        assert "Gather data" in view
        assert "Run analysis" in view
        assert "[✓]" in view
        assert "[ ]" in view

    @pytest.mark.unit
    def test_finalize(self, store):
        store.add_step("One step")
        result = store.finalize()
        assert store.finalized is True
        assert "finalized" in result.lower()

    @pytest.mark.unit
    def test_finalize_empty_plan(self, store):
        result = store.finalize()
        assert store.finalized is False
        assert "empty" in result.lower()

    @pytest.mark.unit
    def test_is_complete(self, store):
        assert store.is_complete() is False
        s1 = store.add_step("A")
        s2 = store.add_step("B")
        assert store.is_complete() is False
        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        assert store.is_complete() is False
        store.set_step_status(s2.step_id, StepStatus.SKIPPED)
        assert store.is_complete() is True

    @pytest.mark.unit
    def test_is_complete_with_failed(self, store):
        s = store.add_step("A")
        store.set_step_status(s.step_id, StepStatus.FAILED)
        assert store.is_complete() is True


class TestPlanStoreMetadata:
    """Plan metadata and serialization."""

    @pytest.mark.unit
    def test_plan_id_is_uuid(self, store):
        import uuid

        assert uuid.UUID(store.plan_id)

    @pytest.mark.unit
    def test_title(self):
        s = PlanStore(title="My Research Plan")
        assert s.title == "My Research Plan"
        s.close()

    @pytest.mark.unit
    def test_summary(self, store):
        s1 = store.add_step("A")
        s2 = store.add_step("B")
        store.add_step("C")
        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        store.set_step_status(s2.step_id, StepStatus.IN_PROGRESS)
        summary = store.summary()
        assert summary["total"] == 3
        assert summary["completed"] == 1
        assert summary["in_progress"] == 1
        assert summary["pending"] == 1

    @pytest.mark.unit
    def test_to_dict(self, store):
        store.add_step("Step A")
        d = store.to_dict()
        assert "plan_id" in d
        assert "steps" in d
        assert "summary" in d
        assert len(d["steps"]) == 1

    @pytest.mark.unit
    def test_to_json(self, store):
        import json

        store.add_step("Step A")
        j = store.to_json()
        d = json.loads(j)
        assert d["steps"][0]["description"] == "Step A"


class TestPlanStoreFileBacked:
    """File-backed persistence tests."""

    @pytest.mark.unit
    def test_persist_and_reload(self, tmp_path):
        db_path = str(tmp_path / "plan.db")
        plan_id = None

        with PlanStore(db_path, title="Persisted") as s:
            s.add_step("Step one")
            s.add_step("Step two")
            plan_id = s.plan_id

        # Reload from disk
        with PlanStore.load(db_path, plan_id) as s2:
            assert len(s2.steps) == 2
            assert s2.title == "Persisted"
            assert s2.steps[0].description == "Step one"

    @pytest.mark.unit
    def test_load_nonexistent_plan(self, tmp_path):
        db_path = str(tmp_path / "plan.db")
        PlanStore(db_path).close()  # create DB
        with pytest.raises(ValueError, match="not found"):
            PlanStore.load(db_path, "00000000-0000-0000-0000-000000000000")


class TestPlanStoreDependencies:
    """Dependency (DAG) management."""

    @pytest.mark.unit
    def test_add_dependency(self, store):
        s1 = store.add_step("First")
        s2 = store.add_step("Second")
        store.add_dependency(s2.step_id, s1.step_id)
        step = store.steps[1]
        assert s1.step_id in step.depends_on

    @pytest.mark.unit
    def test_remove_dependency(self, store):
        s1 = store.add_step("First")
        s2 = store.add_step("Second")
        store.add_dependency(s2.step_id, s1.step_id)
        store.remove_dependency(s2.step_id, s1.step_id)
        step = store.steps[1]
        assert step.depends_on == ()

    @pytest.mark.unit
    def test_self_dependency_rejected(self, store):
        s = store.add_step("Solo")
        with pytest.raises(ValueError, match="itself"):
            store.add_dependency(s.step_id, s.step_id)

    @pytest.mark.unit
    def test_cycle_detection(self, store):
        s1 = store.add_step("A")
        s2 = store.add_step("B")
        s3 = store.add_step("C")
        store.add_dependency(s2.step_id, s1.step_id)
        store.add_dependency(s3.step_id, s2.step_id)
        with pytest.raises(ValueError, match="cycle"):
            store.add_dependency(s1.step_id, s3.step_id)

    @pytest.mark.unit
    def test_ready_steps(self, store):
        s1 = store.add_step("Prerequisite")
        s2 = store.add_step("Dependent")
        store.add_dependency(s2.step_id, s1.step_id)

        ready = store.ready_steps()
        ids = [s.step_id for s in ready]
        assert s1.step_id in ids
        assert s2.step_id not in ids

        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        ready = store.ready_steps()
        ids = [s.step_id for s in ready]
        assert s2.step_id in ids

    @pytest.mark.unit
    def test_dep_on_nonexistent_step(self, store):
        s = store.add_step("Step")
        with pytest.raises(ValueError, match="not found"):
            store.add_dependency(s.step_id, 9999)


class TestPlanStoreTags:
    """Tag/label management."""

    @pytest.mark.unit
    def test_tag_step(self, store):
        s = store.add_step("Research phase")
        store.tag_step(s.step_id, "research")
        step = store.steps[0]
        assert "research" in step.tags

    @pytest.mark.unit
    def test_untag_step(self, store):
        s = store.add_step("Research phase")
        store.tag_step(s.step_id, "research")
        store.untag_step(s.step_id, "research")
        step = store.steps[0]
        assert step.tags == ()

    @pytest.mark.unit
    def test_tag_idempotent(self, store):
        s = store.add_step("Step")
        store.tag_step(s.step_id, "research")
        store.tag_step(s.step_id, "research")  # duplicate — should not raise
        step = store.steps[0]
        assert step.tags.count("research") == 1

    @pytest.mark.unit
    def test_query_by_tag(self, store):
        s1 = store.add_step("Research")
        store.add_step("Implementation")
        store.tag_step(s1.step_id, "research")
        results = store.query_steps(tag="research")
        assert len(results) == 1
        assert results[0].step_id == s1.step_id


class TestPlanStoreHistory:
    """History log."""

    @pytest.mark.unit
    def test_history_recorded(self, store):
        s = store.add_step("Step A")
        store.set_step_status(s.step_id, StepStatus.COMPLETED)
        history = store.get_history()
        assert len(history) >= 2
        actions = [h.action for h in history]
        assert "add_step" in actions
        assert "set_step_status" in actions

    @pytest.mark.unit
    def test_history_by_step(self, store):
        s1 = store.add_step("A")
        store.add_step("B")
        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        history = store.get_history(step_id=s1.step_id)
        for h in history:
            assert h.step_id == s1.step_id

    @pytest.mark.unit
    def test_dependency_history_attributed_to_step(self, store):
        s1 = store.add_step("A")
        s2 = store.add_step("B")
        store.add_dependency(s2.step_id, s1.step_id)
        store.remove_dependency(s2.step_id, s1.step_id)
        history = store.get_history(step_id=s2.step_id)
        actions = [h.action for h in history]
        assert "add_dependency" in actions
        assert "remove_dependency" in actions
        for h in history:
            assert h.step_id == s2.step_id

    @pytest.mark.unit
    def test_history_to_dict(self, store):
        store.add_step("Step")
        record = store.get_history()[0]
        d = record.to_dict()
        assert "action" in d
        assert "timestamp" in d


class TestQuerySteps:
    """query_steps filtering."""

    @pytest.mark.unit
    def test_filter_by_status(self, store):
        s1 = store.add_step("A")
        store.add_step("B")
        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        completed = store.query_steps(status=StepStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].step_id == s1.step_id

    @pytest.mark.unit
    def test_filter_by_status_and_tag(self, store):
        s1 = store.add_step("Research A")
        s2 = store.add_step("Research B")
        store.tag_step(s1.step_id, "research")
        store.tag_step(s2.step_id, "research")
        store.set_step_status(s1.step_id, StepStatus.COMPLETED)
        results = store.query_steps(status=StepStatus.COMPLETED, tag="research")
        assert len(results) == 1
        assert results[0].step_id == s1.step_id
