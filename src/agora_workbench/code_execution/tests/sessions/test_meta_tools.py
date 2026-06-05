import json

import pytest

from ...sessions import SessionManager
from ...sessions.meta_tools import create_inspect_session_tool


@pytest.mark.asyncio
async def test_create_inspect_session_tool_with_custom_inspector():
    manager = SessionManager()
    session_id = manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )

    async def inspector(sid: str):
        return {"session_id": sid, "status": "idle", "namespace": {"x": {"type": "int", "repr": "1"}}}

    tool = create_inspect_session_tool(manager, inspector=inspector)
    payload = json.loads(await tool(session_id))
    assert payload["success"] is True
    assert payload["session_id"] == session_id
    assert payload["namespace"]["x"]["type"] == "int"
