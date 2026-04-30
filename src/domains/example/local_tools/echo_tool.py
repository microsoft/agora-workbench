"""
Echo Tool - A simple local tool for testing local tool execution.

This tool runs in the normal Python environment without requiring
a specialized server or MCP setup.
"""


def echo_with_magic_word(message: str, repeat_count: int = 1) -> dict:
    """
    Echo a message with a magic keyword to verify the tool was executed.

    This tool is useful for testing that local tool execution works correctly.
    The output contains a unique identifier "AGORA_LOCAL_TOOL_SUCCESS" that
    can be searched for in the response to verify the tool ran.

    Args:
        message: The message to echo back
        repeat_count: Number of times to repeat the message (default: 1)

    Returns:
        dict with the echoed message and success indicator
    """
    repeated_message = " | ".join([message] * repeat_count)

    return {
        "status": "success",
        "magic_keyword": "AGORA_LOCAL_TOOL_SUCCESS",
        "original_message": message,
        "repeat_count": repeat_count,
        "echoed_output": f"🔮 AGORA_LOCAL_TOOL_SUCCESS 🔮 - Your message: {repeated_message}",
        "tool_type": "local_execution",
    }
