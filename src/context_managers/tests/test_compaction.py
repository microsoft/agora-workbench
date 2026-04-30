"""Tests for context_managers.compaction.SkillAwareToolCompactionStrategy."""

import pytest

from agent_framework import Message
from agent_framework._compaction import (
    annotate_message_groups,
    _included_group_ids,
    _ordered_group_ids_from_annotations,
    _group_kind_map,
)
from agent_framework._types import Content

from context_managers.compaction import SkillAwareToolCompactionStrategy


def _make_tool_group(call_id: str, tool_name: str, result: str) -> list[Message]:
    """Build an assistant tool-call message + tool result message."""
    return [
        Message(
            role="assistant",
            contents=[
                Content(
                    type="function_call",
                    call_id=call_id,
                    name=tool_name,
                    arguments="{}",
                )
            ],
        ),
        Message(
            role="tool",
            contents=[
                Content(type="function_result", call_id=call_id, result=result)
            ],
        ),
    ]


def _count_included_tool_groups(messages: list[Message]) -> int:
    annotate_message_groups(messages)
    ordered = _ordered_group_ids_from_annotations(messages)
    kinds = _group_kind_map(messages)
    return len(
        [
            gid
            for gid in _included_group_ids(messages, ordered)
            if kinds.get(gid) == "tool_call"
        ]
    )


def _is_message_excluded(msg: Message) -> bool:
    return msg.additional_properties.get("_excluded", False)


def _find_by_call_id(messages: list[Message], call_ids: set[str]) -> list[Message]:
    """Return all messages that contain a function_call or function_result with a matching call_id."""
    result = []
    for msg in messages:
        for content in msg.contents:
            if getattr(content, "call_id", None) in call_ids:
                result.append(msg)
                break
    return result


@pytest.mark.asyncio
async def test_load_skill_group_is_never_compacted():
    """load_skill results survive compaction even when they are the oldest group."""
    messages = [
        *_make_tool_group("c1", "load_skill", "# Skill content\n..."),
        *_make_tool_group("c2", "execute_powergrid_code", "big output 1"),
        *_make_tool_group("c3", "execute_powergrid_code", "big output 2"),
        *_make_tool_group("c4", "execute_powergrid_code", "big output 3"),
    ]
    annotate_message_groups(messages)

    strategy = SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=1)
    changed = await strategy(messages)

    assert changed is True
    # load_skill group (c1) must NOT be excluded
    assert not _is_message_excluded(messages[0]), "load_skill call should be preserved"
    assert not _is_message_excluded(messages[1]), "load_skill result should be preserved"


@pytest.mark.asyncio
async def test_read_skill_resource_group_is_never_compacted():
    """read_skill_resource results survive compaction."""
    messages = [
        *_make_tool_group("c1", "read_skill_resource", "# Reference doc"),
        *_make_tool_group("c2", "execute_gis_code", "gis output"),
        *_make_tool_group("c3", "execute_gis_code", "gis output 2"),
    ]
    annotate_message_groups(messages)

    strategy = SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=1)
    changed = await strategy(messages)

    assert changed is True
    assert not _is_message_excluded(messages[0])
    assert not _is_message_excluded(messages[1])


@pytest.mark.asyncio
async def test_regular_tool_groups_still_compacted():
    """Non-skill tool groups beyond keep_last are collapsed."""
    messages = [
        *_make_tool_group("c1", "execute_powergrid_code", "output 1"),
        *_make_tool_group("c2", "execute_powergrid_code", "output 2"),
        *_make_tool_group("c3", "execute_powergrid_code", "output 3"),
    ]
    annotate_message_groups(messages)

    strategy = SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=1)
    changed = await strategy(messages)

    assert changed is True
    # Find original messages by call_id to avoid index shifts from inserted summaries.
    excluded_calls = _find_by_call_id(messages, {"c1", "c2"})
    kept_calls = _find_by_call_id(messages, {"c3"})
    assert all(_is_message_excluded(m) for m in excluded_calls), "c1/c2 should be compacted"
    assert all(not _is_message_excluded(m) for m in kept_calls), "c3 should be kept"


@pytest.mark.asyncio
async def test_no_change_when_within_budget():
    """If regular groups <= keep_last, nothing happens."""
    messages = [
        *_make_tool_group("c1", "load_skill", "skill content"),
        *_make_tool_group("c2", "execute_powergrid_code", "output"),
    ]
    annotate_message_groups(messages)

    strategy = SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=3)
    changed = await strategy(messages)

    assert changed is False
    for msg in messages:
        assert not _is_message_excluded(msg)


@pytest.mark.asyncio
async def test_custom_protected_tool_names():
    """Custom protected_tool_names override works."""
    messages = [
        *_make_tool_group("c1", "my_custom_tool", "important data"),
        *_make_tool_group("c2", "load_skill", "skill content"),
        *_make_tool_group("c3", "execute_code", "output 1"),
        *_make_tool_group("c4", "execute_code", "output 2"),
    ]
    annotate_message_groups(messages)

    strategy = SkillAwareToolCompactionStrategy(
        keep_last_tool_call_groups=1,
        protected_tool_names=["my_custom_tool"],
    )
    changed = await strategy(messages)

    assert changed is True
    # my_custom_tool (c1) is protected
    protected = _find_by_call_id(messages, {"c1"})
    assert all(not _is_message_excluded(m) for m in protected)
    # load_skill (c2) is NOT protected in this config — it's a regular group and gets compacted
    compacted = _find_by_call_id(messages, {"c2", "c3"})
    assert all(_is_message_excluded(m) for m in compacted)
