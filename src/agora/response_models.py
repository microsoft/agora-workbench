"""
Pydantic models for structured LLM responses.
"""

import json
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Discriminator, Field, Tag, field_validator, model_validator

# Common LLM mistakes: using class names instead of literal values
_ACTION_ALIASES = {
    "solutionresponse": "solution",
    "helpresponse": "help",
    "continueresponse": "continue",
}


class HelpResponse(BaseModel):
    """Response when the agent needs clarification or assistance."""

    action: Literal["help"] = "help"
    question: str = Field(description="What the agent needs help with or clarification on")

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> str:
        return _ACTION_ALIASES.get(str(v).lower(), v) if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def accept_help_as_question(cls, data: Any) -> Any:
        """LLMs sometimes use 'help' instead of 'question' as the key."""
        if isinstance(data, dict) and "help" in data and "question" not in data:
            data["question"] = data.pop("help")
        return data


class SolutionResponse(BaseModel):
    """Response when the agent has a final solution."""

    action: Literal["solution"] = "solution"
    solution: str = Field(description="The final solution or answer")
    provenance: Optional[str] = Field(default=None, description="What tools and data were used")

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> str:
        return _ACTION_ALIASES.get(str(v).lower(), v) if isinstance(v, str) else v

    @field_validator("solution", mode="before")
    @classmethod
    def coerce_solution_to_str(cls, v: Any) -> str:
        """LLMs often return structured data instead of a plain string — serialize it."""
        if isinstance(v, (dict, list)):
            return json.dumps(v, indent=2)
        return v


class ContinueResponse(BaseModel):
    """Response when the agent needs to keep working (made progress but task is not complete)."""

    action: Literal["continue"] = "continue"
    reasoning: str = Field(description="What was done, what went wrong, and what to do next")

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> str:
        return _ACTION_ALIASES.get(str(v).lower(), v) if isinstance(v, str) else v


def get_response_discriminator(v: Any) -> str:
    """Discriminator function for response types based on 'action' field.

    Normalizes common LLM mistakes like using class names
    ('SolutionResponse', 'HelpResponse') instead of the expected
    literal values ('solution', 'help').
    """
    if isinstance(v, dict):
        raw = v.get("action", "help")
    else:
        raw = getattr(v, "action", "help")

    # Normalize class-name variants the LLM sometimes emits
    return _ACTION_ALIASES.get(raw.lower(), raw) if isinstance(raw, str) else raw


# Discriminated union for response types
ResponseUnion = Annotated[
    Union[
        Annotated[HelpResponse, Tag("help")],
        Annotated[SolutionResponse, Tag("solution")],
        Annotated[ContinueResponse, Tag("continue")],
    ],
    Discriminator(get_response_discriminator),
]


class AgentResponse(BaseModel):
    """Wrapper for all agent responses with common fields."""

    explanation: str = Field(description="The agent's reasoning and thought process before taking action")
    response: ResponseUnion = Field(description="The specific action the agent wants to take")
    plan: Optional[str] = Field(default=None, description="Updated plan of steps")
    status: Optional[str] = Field(
        default=None, description="Status message for UI (e.g., 'Running optimal power flow')"
    )

    @model_validator(mode="before")
    @classmethod
    def strip_markdown_wrapper(cls, data: Any) -> Any:
        """
        Strip markdown code block wrappers from JSON input.

        LLMs sometimes return JSON wrapped in ```json ... ``` blocks,
        which breaks parsing. This validator handles that automatically.
        """
        if isinstance(data, str):
            text = data.strip()
            # Handle ```json ... ``` or ``` ... ```
            if text.startswith("```"):
                # Remove opening fence (```json or ```)
                match = re.match(r"^```(?:json)?\s*\n?", text)
                if match:
                    text = text[match.end() :]
                # Remove closing fence
                text = re.sub(r"\n?```\s*$", "", text)
                text = text.strip()
            # Parse the cleaned JSON string into a dict.
            # If the model outputs multiple concatenated JSON objects
            # (e.g., a valid response followed by hallucinated tool calls),
            # extract just the first object via JSONDecodeError.pos.
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e) and e.pos > 0:
                    return json.loads(text[: e.pos])
                raise
        return data
