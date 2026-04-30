"""
Test tools for validating handle system patterns.

These tools test the three handle patterns:
1. Creating multiple handles in one call
2. Consuming multiple handles in one call
3. Both consuming and creating handles in one call
"""


def create_pair(first: int = 1, second: int = 2) -> dict:
    """
    Create two counter values and return both as handles.

    Tests: Creating multiple handles simultaneously.

    Args:
        first: Value for the first counter
        second: Value for the second counter

    Returns:
        Dict with 'first_handle' and 'second_handle' keys
    """
    return {
        "first_handle": first,
        "second_handle": second,
    }


def combine_handles(first: int, second: int, operation: str = "add") -> dict:
    """
    Combine two counter values using the specified operation.

    Tests: Consuming multiple handles in one call.

    Args:
        first: First counter value (injected via handle)
        second: Second counter value (injected via handle)
        operation: Operation to perform ('add', 'subtract', 'multiply')

    Returns:
        Dict with 'result' key containing the operation result
    """
    if operation == "add":
        result = first + second
    elif operation == "subtract":
        result = first - second
    elif operation == "multiply":
        result = first * second
    else:
        raise ValueError(f"Unknown operation: {operation}")

    return {"result": result}


def transform_and_create(input_value: int, multiplier: int = 2) -> dict:
    """
    Transform an input value and create a new handle for the result.

    Tests: Tool that both consumes a handle and creates a new handle.

    Args:
        input_value: Input counter value (injected via handle)
        multiplier: Multiplier to apply

    Returns:
        Dict with 'transformed' handle and 'original_value' metadata
    """
    transformed_value = input_value * multiplier

    return {
        "transformed": transformed_value,
        "original_value": input_value,
    }
