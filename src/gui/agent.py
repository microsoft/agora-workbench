"""
GUIAgent — simplified agent for interactive GUI use.

Direct loop with no structured output constraint:
  1. Call LLM (with streaming for real-time tool events)
  2. LLM produces free-text reasoning
  3. Return the final response text directly

Reuses AgoraAgent's tool setup, prompt rendering, and context management.
No WorkflowBuilder, no executor classes, no graph nodes.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Type, TYPE_CHECKING

from agent_framework import (
    Agent,
    BaseContextProvider,
    CharacterEstimatorTokenizer,
    CompactionProvider,
    Content,
    InMemoryHistoryProvider,
    Message,
    SkillsProvider,
    SlidingWindowStrategy,
    SummarizationStrategy,
    TokenBudgetComposedStrategy,
)

from compaction import SkillAwareToolCompactionStrategy
from agent_framework.azure import AzureOpenAIChatClient

from auth import create_entra_token_provider
from data_lake.tools.data_lake import create_data_lake_search_tool, is_data_lake_configured
from domains.domain_registry import get_domain_registry
from tools.mcp import create_mcp_tools, get_mcp_registry
from tools.search import BM25ToolSearchBackend, create_search_tools_function
from gui.capture_tool import create_capture_map_view_function
from gui.story_map_tool import create_story_map_function

if TYPE_CHECKING:
    from tools import ToolSearchBackend

LOGGER = logging.getLogger(__name__)
USER_LOGGER = logging.getLogger("user")
STATUS_LOGGER = logging.getLogger("status")

# Skill discovery — same as executors.py
_DOMAINS_DIR = Path(__file__).resolve().parent.parent.parent / "domains"
_PLANNING_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "planning" / "skills"

_MAX_EXPERIENCE_CHARS = 4000


class ExperienceContextProvider(BaseContextProvider):
    """Injects persistent user preferences into the system prompt.

    Reads the experience file each turn so mid-session updates are picked
    up.  The content is appended to the system-level instructions — clearly
    separated from user messages — so the LLM treats it as background
    preferences rather than part of the current user utterance.
    """

    def __init__(self):
        super().__init__(source_id="experience")

    async def before_run(self, *, agent, session, context, state) -> None:
        from gui.experience import read_experience

        experience = read_experience()
        if not experience:
            return
        if len(experience) > _MAX_EXPERIENCE_CHARS:
            cut = experience.rfind("\n", 0, _MAX_EXPERIENCE_CHARS)
            if cut <= 0:
                cut = _MAX_EXPERIENCE_CHARS
            experience = experience[:cut] + "\n...(truncated)"
        context.instructions.append(
            "[Persistent user preferences — apply these when the topic is "
            "relevant, but they are NOT part of the user's current message.]\n" + experience
        )


def _discover_skill_paths(domains_dir: Path = _DOMAINS_DIR) -> list[str]:
    paths: list[str] = []
    if domains_dir.is_dir():
        for child in sorted(domains_dir.iterdir()):
            skills_dir = child / "skills"
            if skills_dir.is_dir():
                paths.append(str(skills_dir))
    if _PLANNING_SKILLS_DIR.is_dir():
        paths.append(str(_PLANNING_SKILLS_DIR))
    return paths


class GUIAgent:
    """Simplified agent for GUI — direct loop, no workflow graph.

    Same tool setup and prompt rendering as AgoraAgent.
    Streams tool events in real-time via ``event_callback``.
    """

    def __init__(
        self,
        domain_prompt_path: Optional[str] = None,
        llm: str = "gpt-4o",
        max_iterations: int = 500,
        user_token: str = "",
        search_backend: Optional[Type["ToolSearchBackend"]] = None,
        context_providers: Optional[list] = None,
        middleware: Optional[list] = None,
    ):
        self._loaded_domain_prompts: list[str] = []
        if domain_prompt_path:
            self._loaded_domain_prompts.append(domain_prompt_path)

        self._search_backend_cls = search_backend
        self._user_token = user_token
        self._max_iterations = max_iterations

        # Extra context providers supplied by the caller (e.g. DecisionLogContextProvider)
        self._extra_context_providers: list = context_providers or []

        # Extra middleware supplied by the caller (e.g. DecisionLogChatMiddleware)
        self._extra_middleware: list = middleware or []

        # Chat client
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            raise EnvironmentError("AZURE_OPENAI_ENDPOINT not found.")
        scope = os.getenv("AOAI_SCOPE")
        if not scope:
            raise EnvironmentError("AOAI_SCOPE not found.")
        api_version = os.getenv("API_VERSION")
        if not api_version:
            raise EnvironmentError("API_VERSION not found.")

        token_provider = create_entra_token_provider(scope)
        self._client = AzureOpenAIChatClient(
            endpoint=endpoint,
            api_version=api_version,
            deployment_name=llm,
            credential=token_provider,
        )

        # Render system prompt from GUI-specific template
        self._system_prompt = self._render_prompt(domain_prompt_path)

        # State — initialized lazily
        self._agent: Agent | None = None
        self._session = None
        self._iteration = 0
        self._tools_built = False

        # Mutable holder for the current SSE event callback — the
        # capture_map_view tool reads this to emit capture requests.
        self._event_callback_holder: dict[str, Any] = {"callback": None}

        # Mutable holder for captured map images — the capture_map_view
        # tool stores PNG bytes here; run() injects them as user messages.
        self._image_holder: dict[str, Any] = {"pending_images": None}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_map_context(self) -> str:
        """Build a short summary of the current map layers for context injection.

        This ensures the agent always knows what data is displayed on the
        map, even after conversation compaction drops the tool calls that
        originally created the layers.
        """
        from gui.map_state import read_map_state

        state = read_map_state()
        if not state:
            return ""
        layers = state.get("layers", [])
        if not layers:
            return ""

        lines = ["[Current map layers]"]
        for layer in layers:
            name = layer.get("name", layer.get("id", "unknown"))
            desc = layer.get("description", "")
            source = layer.get("source", "")
            feat_count = layer.get("feature_count", "")
            parts = [f"- {name}"]
            if desc:
                parts.append(f"  ({desc})")
            if source:
                parts.append(f"  source: {source}")
            if feat_count:
                parts.append(f"  features: {feat_count}")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def warm_up(self) -> None:
        """Eagerly initialize tools, MCP connections, and data lake.

        Call this after ``__aenter__()`` to move the heavy startup work
        out of the first ``run()`` call.
        """
        await self._ensure_agent()

    async def close(self):
        """Clean up MCP connections and clear map state in the sandbox."""
        try:
            from tools.mcp.mcp_server_registry import get_mcp_registry, reset_mcp_registry

            # Write an empty map_state.json inside the Docker sandbox before
            # disconnecting.  The sandbox blocks delete operations (unlink,
            # rmdir, etc.) but allows writing to /tmp/maps/.  Overwriting
            # with an empty state is sufficient — orphaned GeoJSON files
            # won't render without a map_state entry referencing them.
            registry = get_mcp_registry()
            gis_tool = registry.get_mcp_tool("gis")
            if gis_tool and getattr(gis_tool, "load_tools_flag", False):
                try:
                    await gis_tool.call_tool(
                        "execute_gis_code",
                        code=(
                            "import json\n"
                            "with open('/tmp/maps/map_state.json', 'w') as f:\n"
                            "    json.dump({'view': None, 'layers': []}, f)\n"
                            "print('map_state.json reset to empty')\n"
                        ),
                    )
                    LOGGER.info("Reset map_state.json in GIS sandbox on session reset")
                except Exception as e:
                    LOGGER.warning("Failed to reset map_state.json in GIS sandbox: %s", e)

            await registry.aclose()
            reset_mcp_registry()
        except Exception as e:
            LOGGER.warning("Error closing MCP registry: %s", e)

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _render_prompt(
        domain_prompt_path: str | None = None,
        domain_prompt_paths: list[str] | None = None,
    ) -> str:
        """Render the GUI agent system prompt from its own Jinja template."""
        from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

        base_dir = Path(__file__).resolve().parent.parent.parent  # AgoraAgentMAF/
        env = Environment(
            loader=ChoiceLoader(
                [
                    FileSystemLoader(str(Path(__file__).resolve().parent / "prompts")),  # gui/prompts/
                    FileSystemLoader(str(base_dir)),  # AgoraAgentMAF/ for domain templates
                ]
            ),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("system_prompt.jinja")

        all_paths: list[str] = []
        if domain_prompt_path:
            all_paths.append(domain_prompt_path)
        if domain_prompt_paths:
            for p in domain_prompt_paths:
                if p not in all_paths:
                    all_paths.append(p)

        return template.render(
            domain_prompt_path=all_paths[0] if len(all_paths) == 1 else None,
            domain_prompt_paths=all_paths if all_paths else None,
        )

    # ------------------------------------------------------------------
    # Tool setup
    # ------------------------------------------------------------------

    def _build_tools(self) -> list:
        tools: list = []
        domain_registry = get_domain_registry()

        mcp_registry = get_mcp_registry()
        for server_name in mcp_registry.list_servers():
            mcp_tool = create_mcp_tools(server_name)
            if mcp_tool is not None:
                tools.append(mcp_tool)
                LOGGER.info("Added MCP tools for server '%s'", server_name)
                prompt_path = domain_registry.get_domain_prompt_path(server_name)
                if prompt_path and prompt_path not in self._loaded_domain_prompts:
                    self._loaded_domain_prompts.append(prompt_path)
                    LOGGER.info("Domain prompt '%s' from server '%s'", prompt_path, server_name)

        # Re-render system prompt if new domain prompts were discovered
        if self._loaded_domain_prompts:
            self._system_prompt = self._render_prompt(
                domain_prompt_paths=list(self._loaded_domain_prompts),
            )

        if self._search_backend_cls is None:
            search_backend = BM25ToolSearchBackend()
        else:
            search_backend = self._search_backend_cls(user_token=self._user_token)
        tools.append(create_search_tools_function(search_backend))
        LOGGER.info("Created search_tools FunctionTool")

        return tools

    async def _ensure_agent(self) -> None:
        """Lazily build tools and create the MAF Agent."""
        if self._agent is not None:
            return

        agent_tools = self._build_tools()

        if is_data_lake_configured():
            data_lake_tool = await create_data_lake_search_tool(user_token=self._user_token)
            agent_tools.append(data_lake_tool)
            LOGGER.info("Created search_data_lake_catalog tool")

        # Map capture tool — lets the agent screenshot the map for VLM analysis
        capture_tool = create_capture_map_view_function(
            self._event_callback_holder,
            self._image_holder,
        )
        agent_tools.append(capture_tool)
        LOGGER.info("Created capture_map_view FunctionTool")

        # Story map tool — guided spatial walkthrough
        story_map_tool = create_story_map_function(self._event_callback_holder)
        agent_tools.append(story_map_tool)
        LOGGER.info("Created present_story_map FunctionTool")

        context_providers = []
        skill_paths = _discover_skill_paths()
        if skill_paths:
            context_providers.append(SkillsProvider(skill_paths))

        # MAF-native history management and context compaction
        history_provider = InMemoryHistoryProvider(skip_excluded=True)
        tokenizer = CharacterEstimatorTokenizer()
        pipeline = TokenBudgetComposedStrategy(
            token_budget=40_000,
            tokenizer=tokenizer,
            strategies=[
                SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=3),
                SummarizationStrategy(client=self._client, target_count=10, threshold=11),
                SlidingWindowStrategy(keep_last_groups=20),
            ],
        )
        compaction_provider = CompactionProvider(
            before_strategy=pipeline,
            history_source_id=history_provider.source_id,
        )
        context_providers.extend([history_provider, compaction_provider])
        context_providers.append(ExperienceContextProvider())

        # Append any caller-supplied providers (e.g. DecisionLogContextProvider)
        context_providers.extend(self._extra_context_providers)

        self._agent = Agent(
            client=self._client,
            name="agora_gui_agent",
            instructions=self._system_prompt,
            tools=agent_tools,
            context_providers=context_providers,
            middleware=self._extra_middleware or None,
        )
        self._session = self._agent.create_session()
        LOGGER.info("GUIAgent initialised with %d tools", len(agent_tools))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        event_callback: Callable[[dict], Any] | None = None,
        viewport: tuple[tuple[float, float], float] | None = None,
    ) -> str:
        """Run the agent on a user prompt.

        Streams tool events via ``event_callback`` in real-time.
        Returns the final response text.

        Raises HelpRequested (via the caller) if the agent needs clarification.
        """
        await self._ensure_agent()
        assert self._agent is not None
        assert self._session is not None

        self._iteration += 1

        USER_LOGGER.info("LLM processing iteration %d", self._iteration)

        if self._iteration > self._max_iterations:
            LOGGER.warning("Max iterations reached (%d)", self._max_iterations)
            return "I've reached the maximum number of iterations. Please try a simpler request."

        # Pass only the latest user message; InMemoryHistoryProvider and
        # CompactionProvider manage accumulated history automatically.
        #
        # Prepend a summary of the current map state so the agent always
        # knows what layers are displayed, even after compaction drops the
        # tool calls that created them.
        context_parts: list[str] = []

        map_context = self._get_map_context()
        if map_context:
            context_parts.append(map_context)

        if viewport:
            context_parts.append(
                f"[User viewport] center: [{viewport[0][0]:.4f}, {viewport[0][1]:.4f}], zoom: {viewport[1]:.0f}"
            )

        context_block = "\n".join(context_parts)
        user_text = f"{context_block}\n\n{prompt}" if context_block else prompt
        new_message = [Message(role="user", text=user_text)]

        # Set the event callback so capture_map_view can emit SSE events
        self._event_callback_holder["callback"] = event_callback
        self._image_holder["pending_images"] = None

        # Call LLM — stream if we have a callback
        try:
            if event_callback:
                response = await self._call_llm_streaming(new_message, event_callback)
            else:
                response = await self._agent.run(
                    messages=new_message,
                    session=self._session,
                )

            # If capture_map_view stored images, inject them as a user
            # message and re-run so the LLM can "see" them.  Images cannot
            # go in tool-result messages (OpenAI API limitation), so we
            # send them as a follow-up user message with multimodal content.
            pending_images = self._image_holder.pop("pending_images", None)
            if pending_images:
                total_bytes = sum(img["image"].__len__() for img in pending_images)
                LOGGER.info(
                    "Injecting %d captured map image(s) (%d bytes total) as user message",
                    len(pending_images),
                    total_bytes,
                )
                # Build multipart contents: text description + all images
                contents: list[str | Content] = []
                if len(pending_images) == 1:
                    img = pending_images[0]
                    contents.append(
                        f"Here is the map screenshot you requested. "
                        f"Your purpose was: {img['purpose']}. "
                        f"Focus your analysis on that purpose — do not list generic observations. "
                        f"If the zoom level is wrong (too far or too close to see what you need), "
                        f"say so and capture again at a better zoom."
                    )
                    contents.append(Content.from_data(img["image"], "image/png"))
                else:
                    descriptions = []
                    for i, img in enumerate(pending_images, 1):
                        descriptions.append(
                            f"Image {i}: center ({img['center'][0]:.4f}, {img['center'][1]:.4f}), "
                            f"zoom {img['zoom']}, purpose: {img['purpose']}"
                        )
                    contents.append(
                        f"Here are {len(pending_images)} map screenshots you requested.\n"
                        + "\n".join(descriptions)
                        + "\n\nAnalyze ALL images. Address each purpose separately."
                    )
                    for img in pending_images:
                        contents.append(Content.from_data(img["image"], "image/png"))

                # Mark as _excluded so InMemoryHistoryProvider (skip_excluded=True)
                # drops this message from future turns.  The raw images would
                # otherwise consume most of the 40K token budget and cause
                # compaction to evict important earlier context.
                image_message = [
                    Message(
                        role="user",
                        contents=contents,
                        additional_properties={"_excluded": True},
                    )
                ]
                if event_callback:
                    response = await self._call_llm_streaming(image_message, event_callback)
                else:
                    response = await self._agent.run(
                        messages=image_message,
                        session=self._session,
                    )
        finally:
            # Clear callback after the run completes
            self._event_callback_holder["callback"] = None
            self._image_holder["pending_images"] = None

        # Return the final text directly.
        # With no response_format, the last message might be a tool result
        # or have empty text — walk backwards to find the last assistant text.
        last_text = ""
        if response.messages:
            for msg in reversed(response.messages):
                if msg.role == "assistant" and msg.text and msg.text.strip():
                    last_text = msg.text.strip()
                    break
        if not last_text:
            last_text = (response.text or "").strip()
        if not last_text:
            last_text = "Done."

        USER_LOGGER.info("Response: %s", last_text[:200])
        return last_text

    # ------------------------------------------------------------------
    # LLM call with streaming
    # ------------------------------------------------------------------

    async def _call_llm_streaming(self, messages, event_callback):
        """Call the LLM with stream=True and emit tool events in real-time."""
        stream = self._agent.run(
            messages=messages,
            session=self._session,
            stream=True,
        )
        seen_calls: dict[str, str] = {}
        # Accumulate raw argument strings across streaming deltas
        call_args_raw: dict[str, str] = {}
        # Track current call_id (deltas after the first chunk have empty call_id)
        current_call_id: str = ""

        async for update in stream:
            if not update.contents:
                continue
            for content in update.contents:
                if content.type == "function_call":
                    call_id = getattr(content, "call_id", None) or ""
                    name = getattr(content, "name", None) or ""
                    # First chunk carries call_id+name; subsequent deltas don't
                    if call_id:
                        current_call_id = call_id
                    # Concatenate argument deltas under the current call
                    raw_args = getattr(content, "arguments", None)
                    if current_call_id and raw_args is not None:
                        call_args_raw[current_call_id] = call_args_raw.get(current_call_id, "") + (
                            raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
                        )
                    # Emit tool_call event only on first appearance
                    if name and call_id and call_id not in seen_calls:
                        seen_calls[call_id] = name
                        # Emit best-effort args (may be partial on first chunk)
                        partial_raw = call_args_raw.get(call_id, "")
                        try:
                            parsed_args = json.loads(partial_raw) if partial_raw else {}
                            best_effort_args: dict = (
                                parsed_args if isinstance(parsed_args, dict) else {"raw": partial_raw}
                            )
                        except (json.JSONDecodeError, TypeError):
                            best_effort_args = {"raw": partial_raw}
                        event_callback(
                            {
                                "event": "tool_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": best_effort_args,
                            }
                        )
                elif content.type == "function_result":
                    call_id = getattr(content, "call_id", None)
                    name = (
                        seen_calls.get(call_id)  # type: ignore
                        or getattr(content, "name", None)
                        or (call_id or "")
                    )
                    exc = getattr(content, "exception", None)
                    raw_result = getattr(content, "result", None)
                    result_str = _format_tool_result(name, raw_result)
                    # Log the full tool call (args are now complete) + result
                    accumulated = call_args_raw.get(call_id, "") if call_id else ""  # type: ignore
                    try:
                        full_args = json.loads(accumulated) if accumulated else {}
                    except (json.JSONDecodeError, TypeError):
                        full_args = {"raw": accumulated}
                    code = full_args.get("code") if isinstance(full_args, dict) else None
                    if code:
                        LOGGER.info(
                            "TOOL CALL  [%s]\n--- code ---\n%s\n--- end ---",
                            name,
                            code,
                        )
                    else:
                        LOGGER.info("TOOL CALL  [%s]  args=%s", name, full_args)
                    if exc is not None:
                        LOGGER.warning("TOOL FAIL  [%s]  error=%s", name, exc)
                    else:
                        preview = (result_str or "")[:500]
                        LOGGER.info("TOOL OK    [%s]  result=%s", name, preview)
                    # Determine success: framework exception OR tool-level failure
                    tool_success = exc is None
                    if tool_success and name.startswith("execute_") and name.endswith("_code"):
                        obj = raw_result
                        if isinstance(obj, str):
                            try:
                                obj = json.loads(obj)
                            except (json.JSONDecodeError, TypeError):
                                obj = None
                        if isinstance(obj, dict) and obj.get("success") is False:
                            tool_success = False
                    payload: dict = {
                        "event": "tool_result",
                        "call_id": call_id or "",
                        "name": name,
                        "success": tool_success,
                        "arguments": full_args if isinstance(full_args, dict) else {},
                    }
                    if result_str:
                        payload["result"] = result_str
                    if exc is not None:
                        payload["error"] = str(exc)
                    event_callback(payload)
                    # Clean up per-call state to avoid unbounded memory growth
                    if call_id:
                        call_args_raw.pop(call_id, None)
                        seen_calls.pop(call_id, None)

        return await stream.get_final_response()

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    async def reset(self):
        """Reset conversation for a new session."""
        self._iteration = 0
        if self._agent is not None:
            self._session = self._agent.create_session()


# ---------------------------------------------------------------------------
# Tool result formatting (shared with executors.py)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _format_tool_result(name: str, raw_result) -> str:
    """Format a tool result for GUI display, keeping it compact."""
    if raw_result is None:
        return ""

    if name.startswith("execute_") and name.endswith("_code"):
        if isinstance(raw_result, (dict, str)):
            obj = raw_result
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(obj, dict):
                error = obj.get("error")
                if error:
                    stderr = _strip_ansi((obj.get("stderr") or "").strip())
                    # Extract the last meaningful line (e.g. "ModuleNotFoundError: ...")
                    if stderr:
                        last = [line for line in stderr.splitlines() if line.strip()]
                        if last:
                            return f"Error: {last[-1].strip()}"
                    return f"Error: {error}"
                stdout = _strip_ansi((obj.get("stdout") or "").strip())
                stderr = _strip_ansi((obj.get("stderr") or "").strip())
                if stderr:
                    last = [line for line in stderr.splitlines() if line.strip()]
                    suffix = f"\n\n--- stderr ---\n{last[-1].strip()}" if last else ""
                    return (stdout + suffix).strip()[:2000] or "(no output)"
                return stdout[:2000] or "(no output)"

    if name == "search_data_lake_catalog":
        items = raw_result
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(items, dict):
            items = items.get("results") or items.get("artifacts") or []
        if isinstance(items, list):
            lines = []
            for r in items:
                if not isinstance(r, dict):
                    continue
                n = r.get("name") or r.get("artifact_name") or ""
                desc = r.get("semantic_dataset_description") or r.get("description") or ""
                lines.append(f"{n} — {desc}" if desc else str(n))
            return "\n".join(lines) if lines else "(no results)"

    if isinstance(raw_result, (dict, list)):
        result_str = json.dumps(raw_result, default=str)
    else:
        result_str = str(raw_result)
    if len(result_str) > 2000:
        result_str = result_str[:2000] + "\u2026"
    return result_str
