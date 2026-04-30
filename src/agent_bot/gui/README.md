# GUIAgent

`GUIAgent` is the conversational agent used by the interactive GIS GUI.

## Constructor extension points

`GUIAgent(...)` accepts caller-supplied context/middleware hooks:

- `context_providers: Optional[list]`
- `middleware: Optional[list]`

Built-in providers (skills, history, compaction, experience) are created first, then `context_providers` are appended. `middleware` is passed through to the underlying MAF `Agent`.

```python
from agent_bot.gui.agent import GUIAgent
from middleware import DecisionLogChatMiddleware, DecisionLogContextProvider
from middleware.decision_log import DecisionLog

decision_log = DecisionLog()
chat_middleware = DecisionLogChatMiddleware(
    decision_log=decision_log,
    agent_name="gui",
    chat_client=...,
)

agent = GUIAgent(
    llm="gpt-4o",
    context_providers=[DecisionLogContextProvider(decision_log, chat_middleware=chat_middleware)],
    middleware=[chat_middleware],
)
```

## Experience injection

`ExperienceContextProvider` reads `agent_bot/gui/experiences/default.md` each turn and injects persistent user preferences/lessons into agent instructions.

## Visual map inspection tool

GUIAgent registers `capture_map_view(latitude, longitude, zoom, purpose)`:

- Emits a capture request over SSE
- Frontend captures a screenshot with `html2canvas`
- Backend stores image bytes and injects them into the next agent turn for multimodal reasoning

Prompt guidance (`prompts/system_prompt.jinja`) tells the agent to use this selectively:

- **Use** for visual checks (render quality, site context, user-reported map issues)
- **Avoid** when vector/tabular data already answers the question
