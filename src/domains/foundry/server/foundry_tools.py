"""
Foundry tool callable functions for kernel execution.

These functions are imported and called by the code execution kernel when
domain tools like bing_grounding, code_interpreter, etc. are invoked.

Each function creates a temporary Azure AI Agent with the appropriate built-in
tool attached, runs the query, and returns the result.

This module is intentionally self-contained — it does NOT import
server-side dependencies (uvicorn, fastapi), or core.* modules, so it can safely
be imported inside the isolated kernel environment. It depends on packages
listed in requirements.yaml (azure-ai-projects, azure-identity, azure-ai-agents)
and the code_execution.auth module (available via PYTHONPATH in the container).
"""

import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Cache for reuse across calls within the same kernel session
_cached_clients = {}
_cached_agents = {}


def _load_obo_credential_class():
    """Load OBOCredentialProvider via importlib to avoid server-side imports.

    The class is cached in ``_cached_clients["_obo_class"]`` so the file-system
    lookup only happens once per kernel lifetime.
    """
    if "_obo_class" in _cached_clients:
        return _cached_clients["_obo_class"]

    import importlib.util

    workspace_root = Path(__file__).resolve().parents[3]
    candidate_paths = [
        Path("/app/code_execution/auth/obo_credential.py"),
        workspace_root / "code_execution" / "code_execution" / "auth" / "obo_credential.py",
        workspace_root / "code_execution" / "auth" / "obo_credential.py",
    ]

    obo_path = next((path for path in candidate_paths if path.exists()), None)
    if obo_path is None:
        raise RuntimeError(
            "Could not locate obo_credential.py. Checked: " + ", ".join(str(path) for path in candidate_paths)
        )

    spec = importlib.util.spec_from_file_location("obo_credential", obo_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load OBO credential module from {obo_path}")
    obo_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obo_module)

    _cached_clients["_obo_class"] = obo_module.OBOCredentialProvider
    return obo_module.OBOCredentialProvider


def _get_agents_client():
    """Get or create an Azure AI Agents client (cached per kernel session).

    Uses OBOCredentialProvider for authentication:
    - Simulation mode (OBO_SIMULATION_MODE=true): Uses AzureCliCredential
    - Production mode: Uses OBO flow with the user's assertion token

    The client is rebuilt automatically when ``USER_ASSERTION_TOKEN`` changes
    in the environment (e.g. after a token refresh preamble injected by
    ``SessionManager.execute_code_for_session``).

    Note: We load OBOCredentialProvider via importlib.util to bypass
    code_execution/__init__.py, which imports server-side deps (uvicorn, fastapi)
    not available in the isolated kernel environment.
    """
    current_token = os.environ.get("USER_ASSERTION_TOKEN", "")
    cached_token = _cached_clients.get("_last_token")

    if "client" not in _cached_clients or current_token != cached_token:
        if current_token != cached_token and "client" in _cached_clients:
            LOGGER.info("USER_ASSERTION_TOKEN changed — rebuilding credential and client")

        endpoint = os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT")
        if not endpoint:
            raise RuntimeError("AZURE_AI_FOUNDRY_ENDPOINT environment variable is required")

        AgentsClient = globals().get("AgentsClient")
        if AgentsClient is None:
            from azure.ai.agents import AgentsClient as _AgentsClient

            AgentsClient = _AgentsClient

        OBOCredentialProvider = _load_obo_credential_class()

        provider = OBOCredentialProvider(user_assertion=current_token)
        _cached_clients["credential_provider"] = provider
        _cached_clients["client"] = AgentsClient(endpoint=endpoint, credential=provider._credential)
        _cached_clients["_last_token"] = current_token

    return _cached_clients["client"]


def _call_foundry_tool(tool_name: str, query: str, tool_instances: list) -> str:
    """
    Generic helper to call a Foundry built-in tool via a temporary Azure AI Agent.

    Args:
        tool_name: Name of the Foundry tool (for caching/logging)
        query: The query/prompt to pass to the tool
        tool_instances: List of tool definition objects to attach to the agent

    Returns:
        Tool result as a string
    """
    from azure.ai.agents.models import MessageRole

    client = _get_agents_client()

    # Get or create cached agent for this tool
    if tool_name not in _cached_agents:
        model = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
        agent = client.create_agent(
            model=model,
            name=f"foundry-{tool_name}",
            instructions=f"You are a helpful assistant. Use the {tool_name} tool to answer the user's query. Return the tool's output directly.",
            tools=tool_instances,
        )
        _cached_agents[tool_name] = str(agent.id)
        LOGGER.info(f"Created agent for tool '{tool_name}': {agent.id}")

    agent_id = _cached_agents[tool_name]

    # Create thread, add message, run
    thread = client.threads.create()
    client.messages.create(thread_id=thread.id, role=MessageRole.USER, content=query)
    run = client.runs.create_and_process(thread_id=thread.id, agent_id=agent_id)

    # Extract response
    messages = client.messages.list(thread_id=thread.id)
    result_parts = []
    for msg in messages:
        role = str(msg.role).upper() if msg.role else ""
        if "AGENT" in role or "ASSISTANT" in role:
            for content in msg.content:
                if hasattr(content, "text") and content.text:
                    result_parts.append(content.text.value)

    if not result_parts:
        # Check run steps for tool outputs
        try:
            run_steps = client.run_steps.list(thread_id=thread.id, run_id=run.id)
            for step in run_steps:
                if hasattr(step, "step_details"):
                    details = step.step_details
                    if hasattr(details, "message_creation"):
                        msg = client.messages.get(
                            thread_id=thread.id,
                            message_id=details.message_creation.message_id,
                        )
                        for content in msg.content:
                            if hasattr(content, "text") and content.text:
                                result_parts.append(content.text.value)
                    if hasattr(details, "tool_calls"):
                        for tool_call in details.tool_calls or []:
                            if hasattr(tool_call, "code_interpreter") and tool_call.code_interpreter:
                                for output in tool_call.code_interpreter.outputs or []:
                                    if hasattr(output, "logs") and output.logs:
                                        result_parts.append(output.logs)
                            if hasattr(tool_call, "deep_research") and tool_call.deep_research:
                                dr = tool_call.deep_research
                                if hasattr(dr, "output") and dr.output:
                                    result_parts.append(str(dr.output))
        except Exception as e:
            LOGGER.debug(f"Error getting run steps: {e}")

    if not result_parts:
        return f"Tool '{tool_name}' completed but returned no content. Run status: {run.status}"

    return "\n".join(result_parts)


def bing_grounding(query: str) -> str:
    """Search the web using Bing to ground responses with real-time information."""
    from azure.ai.agents.models import BingGroundingTool

    connection_id = os.getenv("BING_GROUNDING_CONNECTION_ID")
    if not connection_id:
        raise RuntimeError("BING_GROUNDING_CONNECTION_ID environment variable is required")
    tool = BingGroundingTool(connection_id=connection_id)
    return _call_foundry_tool("bing_grounding", query, tool.definitions)


def code_interpreter(query: str) -> str:
    """Execute Python code in a sandboxed environment for calculations, data analysis, and file processing."""
    from azure.ai.agents.models import CodeInterpreterToolDefinition

    return _call_foundry_tool("code_interpreter", query, [CodeInterpreterToolDefinition()])


def file_search(query: str) -> str:
    """Search through uploaded files using semantic search."""
    from azure.ai.agents.models import FileSearchToolDefinition

    return _call_foundry_tool("file_search", query, [FileSearchToolDefinition()])


def azure_ai_search(query: str) -> str:
    """Query Azure AI Search indexes for relevant information."""
    from azure.ai.agents.models import AzureAISearchTool

    connection_id = os.getenv("AZURE_AI_SEARCH_CONNECTION_ID")
    index_name = os.getenv("AZURE_AI_SEARCH_INDEX_NAME")
    if not connection_id or not index_name:
        raise RuntimeError("AZURE_AI_SEARCH_CONNECTION_ID and AZURE_AI_SEARCH_INDEX_NAME are required")
    tool = AzureAISearchTool(index_connection_id=connection_id, index_name=index_name)
    return _call_foundry_tool("azure_ai_search", query, tool.definitions)


def deep_research(query: str) -> str:
    """Perform multi-step web research to answer complex questions with comprehensive analysis."""
    from azure.ai.agents.models import DeepResearchTool

    bing_connection_id = os.getenv("BING_GROUNDING_CONNECTION_ID")
    model = os.getenv("DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME")
    if not bing_connection_id or not model:
        raise RuntimeError("BING_GROUNDING_CONNECTION_ID and DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME are required")
    tool = DeepResearchTool(bing_grounding_connection_id=bing_connection_id, deep_research_model=model)
    return _call_foundry_tool("deep_research", query, tool.definitions)


def microsoft_fabric(query: str) -> str:
    """Query and analyze data from Microsoft Fabric data sources."""
    from azure.ai.agents.models import FabricTool

    connection_id = os.getenv("MICROSOFT_FABRIC_CONNECTION_ID")
    if not connection_id:
        raise RuntimeError("MICROSOFT_FABRIC_CONNECTION_ID environment variable is required")
    tool = FabricTool(connection_id=connection_id)
    return _call_foundry_tool("microsoft_fabric", query, tool.definitions)


def sharepoint_grounding(query: str) -> str:
    """Search and retrieve information from SharePoint sites and documents."""
    from azure.ai.agents.models import SharepointTool

    connection_id = os.getenv("SHAREPOINT_CONNECTION_ID")
    if not connection_id:
        raise RuntimeError("SHAREPOINT_CONNECTION_ID environment variable is required")
    tool = SharepointTool(connection_id=connection_id)
    return _call_foundry_tool("sharepoint_grounding", query, tool.definitions)
