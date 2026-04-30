"""Fibonacci calculation tool (stateless)."""


def calculate_fibonacci(n: int) -> dict:
    """
    Calculate Fibonacci sequence up to n terms.

    This tool demonstrates computation without session state.

    Args:
        n: Number of Fibonacci terms to calculate

    Returns:
        dict: Fibonacci sequence
    """
    if n <= 0:
        return {"sequence": [], "count": 0}
    elif n == 1:
        return {"sequence": [0], "count": 1}

    fib = [0, 1]
    for i in range(2, n):
        fib.append(fib[i - 1] + fib[i - 2])

    return {"sequence": fib, "count": len(fib)}
