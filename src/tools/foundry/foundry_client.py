"""
Azure AI Foundry Built-in Tools Client.

Provides centralized authentication and client initialization for
Azure AI Foundry built-in tools (bing_grounding, code_interpreter, deep_research, etc.).

NOTE: This module only supports Azure AI Foundry's built-in tools, not custom tools.
Built-in tools are executed by creating a temporary Azure AI Agent with the tool attached,
running a query through the agent, and returning the result.

Built-in tools supported:
    - bing_grounding: Web search via Bing (optional: BING_GROUNDING_CONNECTION_ID)
    - code_interpreter: Execute Python code in a sandbox (no connection required)
    - file_search: Semantic search over uploaded files (no connection required)
    - azure_ai_search: Query Azure AI Search indexes (requires: AZURE_AI_SEARCH_CONNECTION_ID, AZURE_AI_SEARCH_INDEX_NAME)
    - deep_research: Multi-step web research (requires: BING_GROUNDING_CONNECTION_ID, DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME)
    - microsoft_fabric: Query Microsoft Fabric data (requires: MICROSOFT_FABRIC_CONNECTION_ID)
    - sharepoint_grounding: Search SharePoint sites (requires: SHAREPOINT_CONNECTION_ID)

For custom tools, use MCP servers or local tool registration instead.
"""

import logging
import os
import threading
from typing import Optional

from azure.ai.projects import AIProjectClient
from azure.identity import ChainedTokenCredential

from auth import create_azure_credential
from .foundry_models import FoundryAgentConfig, FoundryBuiltinTool, FoundryToolParameters, FoundryToolResult

LOGGER = logging.getLogger(__name__)


class FoundryClientManager:
    """Manages Azure AI Foundry client instances with shared authentication."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        agent_config: Optional[FoundryAgentConfig] = None,
    ):
        """
        Initialize Foundry client manager.

        Args:
            endpoint: Azure AI Foundry endpoint URL (e.g., https://xxx.services.ai.azure.com/)
            agent_config: Optional configuration for agent creation. Uses defaults if not provided.

        Raises:
            ValueError: If endpoint is not provided via constructor or environment variable.
        """
        self.endpoint = endpoint or os.getenv("AZURE_AI_FOUNDRY_ENDPOINT")

        if not self.endpoint:
            raise ValueError(
                "Must provide endpoint. Can be set via constructor or environment variable: AZURE_AI_FOUNDRY_ENDPOINT"
            )

        self.agent_config = agent_config or FoundryAgentConfig(
            model_deployment=os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o"),
        )
        self._client: Optional[AIProjectClient] = None
        self._credential: Optional[ChainedTokenCredential] = None
        self._cached_agents: dict[str, str] = {}  # tool_name -> agent_id

    @property
    def credential(self) -> ChainedTokenCredential:
        """Get or create Azure credential using shared auth utilities."""
        if self._credential is None:
            self._credential = create_azure_credential()
        return self._credential

    @property
    def client(self) -> AIProjectClient:
        """Get or create AI Project client."""
        if self._client is None:
            LOGGER.info(f"Initializing AI Foundry client for endpoint: {self.endpoint}")
            self._client = AIProjectClient(
                endpoint=self.endpoint,  # type: ignore[arg-type]  # endpoint narrowed by __init__ guard
                credential=self.credential,
            )

        return self._client

    @property
    def list_builtin_tools(self) -> list[FoundryBuiltinTool]:
        """
        List all available built-in tools from Azure AI Agents.

        These are Azure AI Foundry's built-in capabilities, NOT custom tools.
        Each tool is executed by attaching it to a temporary Azure AI Agent.

        Returns:
            List of FoundryBuiltinTool definitions
        """
        from azure.ai.agents.models import (
            BingGroundingToolDefinition,
            CodeInterpreterToolDefinition,
            FileSearchToolDefinition,
            AzureAISearchToolDefinition,
            DeepResearchToolDefinition,
            MicrosoftFabricToolDefinition,
            SharepointToolDefinition,
        )

        # Built-in Azure AI Foundry tools with their metadata
        # Some tools require connection IDs from environment variables
        builtin_tools = [
            FoundryBuiltinTool(
                name="bing_grounding",
                description="Search the web using Bing to ground responses with real-time information",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The search query"}},
                    required=["query"],
                ),
                tool_class=BingGroundingToolDefinition,
                requires_connection=True,
                connection_env_vars=["BING_GROUNDING_CONNECTION_ID"],
            ),
            FoundryBuiltinTool(
                name="code_interpreter",
                description="Execute Python code in a sandboxed environment for data analysis, calculations, and file processing",
                parameters=FoundryToolParameters(
                    properties={"code": {"type": "string", "description": "Python code to execute"}},
                    required=["code"],
                ),
                tool_class=CodeInterpreterToolDefinition,
                requires_connection=False,
            ),
            FoundryBuiltinTool(
                name="file_search",
                description="Search through uploaded files using semantic search",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The search query"}},
                    required=["query"],
                ),
                tool_class=FileSearchToolDefinition,
                requires_connection=False,
            ),
            FoundryBuiltinTool(
                name="azure_ai_search",
                description="Search Azure AI Search indexes for relevant information",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The search query"}},
                    required=["query"],
                ),
                tool_class=AzureAISearchToolDefinition,
                requires_connection=True,
                connection_env_vars=["AZURE_AI_SEARCH_CONNECTION_ID"],
            ),
            FoundryBuiltinTool(
                name="deep_research",
                description="Perform multi-step web research to answer complex questions with comprehensive analysis",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The research query or question"}},
                    required=["query"],
                ),
                tool_class=DeepResearchToolDefinition,
                requires_connection=True,
                connection_env_vars=["BING_GROUNDING_CONNECTION_ID", "DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME"],
            ),
            FoundryBuiltinTool(
                name="microsoft_fabric",
                description="Query and analyze data from Microsoft Fabric data sources",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The data query"}},
                    required=["query"],
                ),
                tool_class=MicrosoftFabricToolDefinition,
                requires_connection=True,
                connection_env_vars=["MICROSOFT_FABRIC_CONNECTION_ID"],
            ),
            FoundryBuiltinTool(
                name="sharepoint_grounding",
                description="Search and retrieve information from SharePoint sites and documents",
                parameters=FoundryToolParameters(
                    properties={"query": {"type": "string", "description": "The search query"}},
                    required=["query"],
                ),
                tool_class=SharepointToolDefinition,
                requires_connection=True,
                connection_env_vars=["SHAREPOINT_CONNECTION_ID"],
            ),
        ]

        return builtin_tools

    def get_tool(self, tool_name: str) -> FoundryBuiltinTool:
        """
        Get a specific tool definition by name.

        Args:
            tool_name: Name of the tool to retrieve (case-insensitive)

        Returns:
            FoundryBuiltinTool definition

        Raises:
            ValueError: If tool not found
        """
        tool_name_lower = tool_name.lower()
        for tool in self.list_builtin_tools:
            if tool.name.lower() == tool_name_lower:
                return tool
        raise ValueError(f"Tool '{tool_name}' not found in available tools")

    def create_tool_instance(self, tool_name: str):
        """
        Create a properly configured tool instance for the given tool name.

        Some tools require connection IDs or other configuration from environment variables.

        Args:
            tool_name: Name of the tool to instantiate

        Returns:
            Configured tool instance ready for use with an agent

        Raises:
            ValueError: If required environment variables are missing
        """
        from azure.ai.agents.models import (
            BingGroundingTool,
            AzureAISearchToolDefinition,
            AzureAISearchTool,
            DeepResearchTool,
            MicrosoftFabricToolDefinition,
            FabricTool,
            SharepointToolDefinition,
            SharepointTool,
        )

        tool_def = self.get_tool(tool_name)
        tool_class = tool_def.tool_class

        if not tool_class:
            raise ValueError(f"Tool '{tool_name}' does not have a tool_class defined")

        # Handle tools that require connection configuration
        if tool_name == "bing_grounding":
            connection_id = os.getenv("BING_GROUNDING_CONNECTION_ID")
            if not connection_id:
                raise ValueError(
                    "bing_grounding tool requires BING_GROUNDING_CONNECTION_ID environment variable. "
                    "This is the connection ID for Bing grounding in your Azure AI Foundry project."
                )
            # Return BingGroundingTool directly - call_tool will use .definitions
            # to get the serializable format for the SDK
            return BingGroundingTool(connection_id=connection_id)

        if tool_name == "azure_ai_search":
            connection_id = os.getenv("AZURE_AI_SEARCH_CONNECTION_ID")
            index_name = os.getenv("AZURE_AI_SEARCH_INDEX_NAME")

            if not connection_id:
                raise ValueError(
                    "azure_ai_search tool requires AZURE_AI_SEARCH_CONNECTION_ID environment variable. "
                    "This is the connection ID for Azure AI Search in your Azure AI Foundry project."
                )
            if not index_name:
                raise ValueError(
                    "azure_ai_search tool requires AZURE_AI_SEARCH_INDEX_NAME environment variable. "
                    "This is the name of the search index to query."
                )

            search_tool = AzureAISearchTool(
                index_connection_id=connection_id,
                index_name=index_name,
            )
            return AzureAISearchToolDefinition(azure_ai_search=search_tool)  # type: ignore[call-overload]

        if tool_name == "deep_research":
            bing_connection_id = os.getenv("BING_GROUNDING_CONNECTION_ID")
            deep_research_model = os.getenv("DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME")

            if not bing_connection_id:
                raise ValueError(
                    "deep_research tool requires BING_GROUNDING_CONNECTION_ID environment variable. "
                    "This is the connection ID for Bing grounding in your Azure AI Foundry project."
                )
            if not deep_research_model:
                raise ValueError(
                    "deep_research tool requires DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME environment variable. "
                    "This is the model deployment name (e.g., 'o3-deep-research')."
                )

            # DeepResearchTool is a helper that generates definitions
            # Return the tool object - call_tool will use .definitions when passing to agent
            deep_research_tool = DeepResearchTool(
                bing_grounding_connection_id=bing_connection_id,
                deep_research_model=deep_research_model,
            )
            return deep_research_tool

        if tool_name == "microsoft_fabric":
            connection_id = os.getenv("MICROSOFT_FABRIC_CONNECTION_ID")

            if not connection_id:
                raise ValueError(
                    "microsoft_fabric tool requires MICROSOFT_FABRIC_CONNECTION_ID environment variable. "
                    "This is the connection ID for Microsoft Fabric in your Azure AI Foundry project."
                )

            fabric_tool = FabricTool(connection_id=connection_id)
            return MicrosoftFabricToolDefinition(microsoft_fabric=fabric_tool)  # type: ignore[call-overload]

        if tool_name == "sharepoint_grounding":
            connection_id = os.getenv("SHAREPOINT_CONNECTION_ID")

            if not connection_id:
                raise ValueError(
                    "sharepoint_grounding tool requires SHAREPOINT_CONNECTION_ID environment variable. "
                    "This is the connection ID for SharePoint in your Azure AI Foundry project."
                )

            sharepoint_tool = SharepointTool(connection_id=connection_id)
            return SharepointToolDefinition(sharepoint_grounding=sharepoint_tool)  # type: ignore[arg-type]

        # Tools without special configuration (code_interpreter, file_search)
        return tool_class()

    def get_agents_client(self):
        """Get or create the Azure AI Agents client."""
        from azure.ai.agents import AgentsClient

        if not hasattr(self, "_agents_client") or self._agents_client is None:
            self._agents_client = AgentsClient(
                endpoint=self.endpoint,  # type: ignore[arg-type]  # endpoint narrowed by __init__ guard
                credential=self.credential,
            )
        return self._agents_client

    def call_tool(
        self,
        tool_name: str,
        parameters: dict,
        agent_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> FoundryToolResult:
        """
        Execute a built-in tool via an Azure AI Agent.

        Built-in tools (bing_grounding, code_interpreter, etc.) are executed
        by running a query through an agent with the tool attached.

        Args:
            tool_name: Name of the tool to call
            parameters: Tool parameters as dictionary
            agent_id: Optional existing agent ID to use. If not provided, creates a temp agent.
            thread_id: Optional existing thread ID to continue conversation. If not provided, creates new thread.

        Returns:
            FoundryToolResult with success, result, thread_id, and run_status
        """
        from azure.ai.agents.models import MessageRole

        try:
            agents_client = self.get_agents_client()

            # Use existing agent, cached agent, or create a new one
            if agent_id:
                # Use explicitly provided agent - caller is responsible for ensuring it has the tool
                pass
            elif tool_name in self._cached_agents:
                # Reuse cached agent for this tool
                agent_id = self._cached_agents[tool_name]
                LOGGER.debug(f"Reusing cached agent for tool '{tool_name}': {agent_id}")
            else:
                # Create properly configured tool instance
                tool_instance = self.create_tool_instance(tool_name)

                # Some tools (like DeepResearchTool) have a .definitions property
                # that returns the serializable tool definitions
                if hasattr(tool_instance, "definitions"):
                    tools_for_agent = tool_instance.definitions
                else:
                    tools_for_agent = [tool_instance]

                agent = agents_client.create_agent(
                    model=self.agent_config.model_deployment,
                    name=self.agent_config.get_agent_name(tool_name),
                    instructions=self.agent_config.get_instructions(tool_name),
                    tools=tools_for_agent,
                )
                agent_id = str(agent.id)
                self._cached_agents[tool_name] = agent_id
                LOGGER.info(f"Created and cached agent for tool '{tool_name}': {agent_id}")

            # Use existing thread or create new one
            if thread_id:
                thread = agents_client.threads.get(thread_id)
            else:
                thread = agents_client.threads.create()

            # Build query from parameters
            query = parameters.get("query", str(parameters))

            # Add user message
            agents_client.messages.create(
                thread_id=thread.id,
                role=MessageRole.USER,
                content=query,
            )

            # Run the agent
            run = agents_client.runs.create_and_process(
                thread_id=thread.id,
                agent_id=agent_id,
            )

            # Get the response messages
            messages = agents_client.messages.list(thread_id=thread.id)

            # Extract assistant/agent response
            # Note: msg.role can be MESSAGEROLE.AGENT or ASSISTANT depending on SDK version
            result_content = []
            for msg in messages:
                role = str(msg.role).upper() if msg.role else ""
                LOGGER.debug(f"Message role: {role}, content count: {len(msg.content) if msg.content else 0}")
                if "AGENT" in role or "ASSISTANT" in role:
                    for content in msg.content:
                        LOGGER.debug(f"Content type: {type(content).__name__}")
                        # Try multiple content types
                        if hasattr(content, "text") and content.text:
                            result_content.append(content.text.value)
                        elif hasattr(content, "value"):
                            # Some content types have value directly
                            result_content.append(str(content.value))

            # If still no content, check run steps for tool outputs
            if not result_content:
                LOGGER.debug("No text content found, checking run steps...")
                try:
                    run_steps = agents_client.run_steps.list(thread_id=thread.id, run_id=run.id)
                    for step in run_steps:
                        if hasattr(step, "step_details"):
                            details = step.step_details
                            LOGGER.debug(f"Step type: {type(details).__name__}")

                            # Check for message creation (contains the final response)
                            if hasattr(details, "message_creation"):
                                msg_id = details.message_creation.message_id
                                msg = agents_client.messages.get(thread_id=thread.id, message_id=msg_id)
                                for content in msg.content:
                                    if hasattr(content, "text") and content.text:
                                        result_content.append(content.text.value)

                            # Check for tool call outputs
                            if hasattr(details, "tool_calls"):
                                for tool_call in details.tool_calls or []:
                                    # Code interpreter output
                                    if hasattr(tool_call, "code_interpreter") and tool_call.code_interpreter:
                                        ci = tool_call.code_interpreter
                                        if hasattr(ci, "outputs"):
                                            for output in ci.outputs or []:
                                                if hasattr(output, "logs") and output.logs:
                                                    result_content.append(output.logs)
                                    # Deep research output
                                    if hasattr(tool_call, "deep_research") and tool_call.deep_research:
                                        dr = tool_call.deep_research
                                        if hasattr(dr, "output") and dr.output:
                                            result_content.append(str(dr.output))
                except Exception as e:
                    LOGGER.debug(f"Error getting run steps: {e}")

            if not result_content:
                LOGGER.warning(f"Tool '{tool_name}' completed but returned no content. Run status: {run.status}")
                return FoundryToolResult(
                    success=False,
                    error=f"Tool execution completed but returned no content. Run status: {run.status}",
                    tool=tool_name,
                    thread_id=thread.id,
                    run_status=run.status,
                )

            return FoundryToolResult(
                success=True,
                result="\n".join(result_content),
                tool=tool_name,
                thread_id=thread.id,
                run_status=run.status,
            )

        except Exception as e:
            LOGGER.error(f"Failed to call tool '{tool_name}': {e}")
            return FoundryToolResult(
                success=False,
                error=str(e),
                tool=tool_name,
            )

    def cleanup_cached_agents(self) -> None:
        """
        Delete all cached agents.

        Call this when shutting down the session or when agents are no longer needed.
        """
        if not self._cached_agents:
            return

        agents_client = self.get_agents_client()
        for tool_name, agent_id in list(self._cached_agents.items()):
            try:
                agents_client.delete_agent(agent_id)
                LOGGER.info(f"Deleted cached agent for tool '{tool_name}': {agent_id}")
            except Exception as e:
                LOGGER.warning(f"Failed to delete cached agent '{agent_id}' for tool '{tool_name}': {e}")
            finally:
                del self._cached_agents[tool_name]


# Global singleton instance (thread-safe)
_foundry_client_manager: Optional[FoundryClientManager] = None
_foundry_client_lock = threading.Lock()


def get_foundry_client(
    endpoint: Optional[str] = None,
) -> FoundryClientManager:
    """
    Get or create the global Foundry client manager instance.

    Args:
        endpoint: Azure AI Foundry endpoint URL

    Returns:
        FoundryClientManager instance
    """
    global _foundry_client_manager

    if _foundry_client_manager is None:
        with _foundry_client_lock:
            if _foundry_client_manager is None:  # Double-check after acquiring lock
                _foundry_client_manager = FoundryClientManager(
                    endpoint=endpoint,
                )

    return _foundry_client_manager


def reset_foundry_client():
    """Reset the global client instance (mainly for testing). Cleans up cached agents first."""
    global _foundry_client_manager
    with _foundry_client_lock:
        if _foundry_client_manager is not None:
            _foundry_client_manager.cleanup_cached_agents()
        _foundry_client_manager = None
