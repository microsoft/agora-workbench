"""
Shared compaction strategies for AgoraAgentMAF agents.

Provides ``SkillAwareToolCompactionStrategy``, a drop-in replacement for
``ToolResultCompactionStrategy`` that preserves tool-call groups containing
skill-related tool calls (``load_skill``, ``read_skill_resource``).
"""

from __future__ import annotations

import logging
from typing import Sequence

from agent_framework import Message
from agent_framework._compaction import (
    ToolResultCompactionStrategy,
    _group_messages_by_id,
    _group_kind_map,
    _included_group_ids,
    _ordered_group_ids_from_annotations,
)

LOGGER = logging.getLogger(__name__)

# Default tool names whose results are never compacted.
DEFAULT_PROTECTED_TOOL_NAMES: frozenset[str] = frozenset(
    {"load_skill", "read_skill_resource"}
)


class SkillAwareToolCompactionStrategy(ToolResultCompactionStrategy):
    """Like ``ToolResultCompactionStrategy`` but exempts specified tool calls.

    Tool-call groups that contain *any* call to a protected tool name are
    never collapsed, regardless of how old they are.  All other groups
    follow the standard ``keep_last_tool_call_groups`` logic.

    This prevents skill instructions (loaded via ``load_skill`` /
    ``read_skill_resource``) from being replaced with one-line summaries
    while still aggressively compacting large code-execution results.

    Args:
        keep_last_tool_call_groups: Number of newest non-protected
            tool-call groups to keep verbatim.
        protected_tool_names: Tool names whose groups are always kept.
            Defaults to ``{"load_skill", "read_skill_resource"}``.
    """

    def __init__(
        self,
        *,
        keep_last_tool_call_groups: int = 1,
        protected_tool_names: Sequence[str] | None = None,
    ) -> None:
        super().__init__(keep_last_tool_call_groups=keep_last_tool_call_groups)
        self.protected_tool_names: frozenset[str] = (
            frozenset(protected_tool_names)
            if protected_tool_names is not None
            else DEFAULT_PROTECTED_TOOL_NAMES
        )

    def _is_protected_group(self, group_msgs: list[Message]) -> bool:
        """Return ``True`` if any message in the group calls a protected tool."""
        for msg in group_msgs:
            for content in msg.contents:
                if (
                    content.type == "function_call"
                    and content.name in self.protected_tool_names
                ):
                    return True
        return False

    async def __call__(self, messages: list[Message]) -> bool:
        ordered_group_ids = _ordered_group_ids_from_annotations(messages)
        grouped = _group_messages_by_id(messages)
        kinds = _group_kind_map(messages)

        included_tool_group_ids = [
            group_id
            for group_id in _included_group_ids(messages, ordered_group_ids)
            if kinds.get(group_id) == "tool_call"
        ]

        # Split into protected (always kept) and regular (subject to eviction).
        protected_ids: set[str] = set()
        regular_ids: list[str] = []
        for gid in included_tool_group_ids:
            if self._is_protected_group(grouped.get(gid, [])):
                protected_ids.add(gid)
            else:
                regular_ids.append(gid)

        if len(regular_ids) <= self.keep_last_tool_call_groups:
            return False

        # Keep the newest N regular groups + all protected groups.
        keep_regular = (
            set(regular_ids[-self.keep_last_tool_call_groups :])
            if self.keep_last_tool_call_groups > 0
            else set()
        )
        keep_ids = keep_regular | protected_ids

        # Re-use parent's compaction logic for groups that aren't kept.
        # We temporarily patch self.keep_last_tool_call_groups to 0 and
        # filter the parent's view — but it's simpler to just replicate
        # the collapse logic here (it's well-contained).
        from agent_framework._compaction import (
            GROUP_ANNOTATION_KEY,
            SUMMARY_OF_GROUP_IDS_KEY,
            SUMMARY_OF_MESSAGE_IDS_KEY,
            _group_start_indices,
            _set_group_summarized_by_summary_id,
            annotate_message_groups,
            set_excluded,
        )

        starts = _group_start_indices(messages)
        changed = False

        for group_id in included_tool_group_ids:
            if group_id in keep_ids:
                continue
            group_msgs = grouped.get(group_id, [])

            # Build call_id → function_name map.
            call_id_to_name: dict[str, str] = {}
            for msg in group_msgs:
                for content in msg.contents:
                    if content.type == "function_call" and content.call_id and content.name:
                        call_id_to_name[content.call_id] = content.name

            # Collect tool results with the function name for context.
            tool_results: list[str] = []
            for msg in group_msgs:
                for content in msg.contents:
                    if content.type == "function_result":
                        result_text = (
                            content.result
                            if isinstance(content.result, str)
                            else str(content.result)
                        )
                        func_name = call_id_to_name.get(content.call_id or "", "")
                        label = f"{func_name}: {result_text}" if func_name else result_text
                        tool_results.append(label.strip())

            summary_label = "; ".join(tool_results) if tool_results else "no results"
            summary_text = f"[Tool results: {summary_label}]"

            summary_id = f"tool_summary_{group_id}"
            original_message_ids = [msg.message_id for msg in group_msgs if msg.message_id]

            for msg in group_msgs:
                _set_group_summarized_by_summary_id(msg, summary_id)
                changed = set_excluded(msg, excluded=True, reason="tool_result_compaction") or changed

            summary_annotation = {
                SUMMARY_OF_MESSAGE_IDS_KEY: original_message_ids,
                SUMMARY_OF_GROUP_IDS_KEY: [group_id],
            }
            insertion_index = starts.get(group_id, 0)
            summary_message = Message(
                role="assistant",
                text=summary_text,
                message_id=summary_id,
                additional_properties={
                    GROUP_ANNOTATION_KEY: summary_annotation,
                },
            )
            messages.insert(insertion_index, summary_message)
            annotate_message_groups(messages, from_index=insertion_index, force_reannotate=False)
            starts = _group_start_indices(messages)
            grouped = _group_messages_by_id(messages)

        if protected_ids:
            LOGGER.debug(
                "SkillAwareToolCompaction: preserved %d protected group(s), "
                "compacted %d regular group(s)",
                len(protected_ids),
                len(included_tool_group_ids) - len(keep_ids),
            )

        return changed
