"""
Pydantic models for Azure AI Foundry tool definitions and configuration.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class FoundryToolParameters(BaseModel):
    """JSON Schema-style parameters for a Foundry built-in tool."""

    type: str = "object"
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class FoundryBuiltinTool(BaseModel):
    """Definition of an Azure AI Foundry built-in tool."""

    name: str = Field(..., description="Tool name (e.g., 'bing_grounding', 'code_interpreter')")
    description: str = Field(..., description="Human-readable description of what the tool does")
    parameters: FoundryToolParameters = Field(
        default_factory=FoundryToolParameters,
        description="JSON Schema-style parameter definitions",
    )
    tool_class: Any = Field(..., description="Azure SDK tool definition class")
    requires_connection: bool = Field(
        default=False,
        description="Whether the tool requires a connection ID to function",
    )
    connection_env_vars: list[str] = Field(
        default_factory=list,
        description="Environment variables needed for connection configuration",
    )

    model_config = {"arbitrary_types_allowed": True}


class FoundryAgentConfig(BaseModel):
    """Configuration for creating an Azure AI Foundry agent."""

    model_deployment: str = Field(
        default="gpt-4o",
        description="Model deployment name to use for the agent",
    )
    name_template: str = Field(
        default="foundry_agent_{tool_name}",
        description="Template for agent name. Use {tool_name} as placeholder.",
    )
    instructions_template: str = Field(
        default="You are a helpful assistant. Use the {tool_name} tool to answer the user's query.",
        description="Template for agent instructions. Use {tool_name} as placeholder.",
    )

    def get_agent_name(self, tool_name: str) -> str:
        """Generate agent name from template."""
        return self.name_template.format(tool_name=tool_name)

    def get_instructions(self, tool_name: str) -> str:
        """Generate agent instructions from template."""
        return self.instructions_template.format(tool_name=tool_name)


class FoundryToolResult(BaseModel):
    """Result from executing a Foundry tool."""

    success: bool = Field(..., description="Whether the tool execution succeeded")
    result: Optional[str] = Field(default=None, description="The tool output content")
    error: Optional[str] = Field(default=None, description="Error message if execution failed")
    tool: str = Field(..., description="Name of the tool that was executed")
    thread_id: Optional[str] = Field(default=None, description="Azure AI thread ID for conversation continuity")
    run_status: Optional[str] = Field(default=None, description="Status of the agent run")
