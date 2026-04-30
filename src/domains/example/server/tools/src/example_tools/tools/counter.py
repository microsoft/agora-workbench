"""Counter tools demonstrating session-based persistence."""


def create_counter(initial_value: int = 0) -> dict:
    """
    Create a new counter with an initial value.

    Args:
        initial_value: Starting value for the counter

    Returns:
        Dict with 'counter' key containing the counter value
    """
    return {"counter": initial_value}


def increment_counter(counter: int, amount: int = 1) -> dict:
    """
    Increment a counter by a specified amount.

    Args:
        counter: The current counter value
        amount: Amount to increment by

    Returns:
        Dict with 'result' key containing the new counter value
    """
    return {"result": counter + amount}


def get_counter_value(counter: int) -> dict:
    """
    Get the current value of a counter.

    Args:
        counter: The counter value

    Returns:
        Dict with 'result' key containing the current counter value
    """
    return {"result": counter}
