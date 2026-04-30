"""
Blind test validator for ToolMaker-generated tools.

Validates generated tools against hidden test fixtures that the
agent never sees during exploration or implementation.

Usage:
    from agent_bot.toolmaker.tests.blind_validator import run_blind_tests
    results = await run_blind_tests("roman", port=8010)
"""

import json
import os

from auth import BearerTokenAuth, create_entra_token_provider

from .blind_test_fixtures import BLIND_TEST_FIXTURES


async def run_blind_tests(
    domain_name: str,
    port: int = 8010,
    verbose: bool = True,
) -> dict:
    """
    Run blind tests against a running MCP server.

    Args:
        domain_name: Name of the domain to test
        port: Port the MCP server is running on
        verbose: Print results as they run

    Returns:
        {
            "domain": domain_name,
            "total": N,
            "passed": M,
            "failed": N-M,
            "results": [
                {"tool": "...", "input": {...}, "expected": {...}, "actual": {...}, "passed": bool},
                ...
            ]
        }
    """
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if domain_name not in BLIND_TEST_FIXTURES:
        return {
            "domain": domain_name,
            "error": f"No blind test fixtures defined for domain '{domain_name}'",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
        }

    fixtures = BLIND_TEST_FIXTURES[domain_name]

    # Get auth token
    mcp_server_scope = os.getenv("MCP_SERVER_SCOPE")
    if not mcp_server_scope:
        return {
            "domain": domain_name,
            "error": "MCP_SERVER_SCOPE not set",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
        }

    token_provider = create_entra_token_provider(mcp_server_scope)

    results = []
    total = 0
    passed = 0

    url = f"http://localhost:{port}/mcp"
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"BLIND TEST: {domain_name}")
        print(f"{'=' * 60}")

    try:
        async with httpx.AsyncClient(
            auth=BearerTokenAuth(token_provider),
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    for tool_name, test_cases in fixtures.items():
                        for i, test_case in enumerate(test_cases, 1):
                            total += 1
                            input_args = test_case["input"]
                            expected = test_case["expected"]

                            try:
                                result = await session.call_tool(tool_name, input_args)

                                # Extract result text
                                actual_text = ""
                                for content in result.content:
                                    if hasattr(content, "text"):
                                        actual_text += content.text

                                # Parse as JSON
                                try:
                                    actual = json.loads(actual_text)
                                except json.JSONDecodeError:
                                    actual = {"_raw": actual_text}

                                # Compare
                                test_passed = _compare_output(actual, expected)
                                if test_passed:
                                    passed += 1

                                result_entry = {
                                    "tool": tool_name,
                                    "input": input_args,
                                    "expected": expected,
                                    "actual": actual,
                                    "passed": test_passed,
                                }
                                results.append(result_entry)

                                if verbose:
                                    status = "✓ PASS" if test_passed else "✗ FAIL"
                                    print(f"  [{i}] {tool_name}({input_args}) → {status}")
                                    if not test_passed:
                                        print(f"      Expected: {expected}")
                                        print(f"      Actual:   {actual}")

                            except Exception as e:
                                results.append(
                                    {
                                        "tool": tool_name,
                                        "input": input_args,
                                        "expected": expected,
                                        "actual": None,
                                        "error": str(e),
                                        "passed": False,
                                    }
                                )
                                if verbose:
                                    print(f"  [{i}] {tool_name}({input_args}) → ✗ ERROR: {e}")

    except Exception as e:
        return {
            "domain": domain_name,
            "error": str(e),
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "results": results,
        }

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"RESULTS: {passed}/{total} passed ({100 * passed / total:.0f}%)" if total > 0 else "No tests run")
        print(f"{'=' * 60}\n")

    return {
        "domain": domain_name,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }


def _compare_output(actual: dict, expected: dict) -> bool:
    """Compare actual output to expected, allowing for flexible matching."""
    # Direct comparison of the key fields
    for key, expected_value in expected.items():
        if key not in actual:
            return False
        actual_value = actual[key]

        # String comparison (case-sensitive)
        if isinstance(expected_value, str) and isinstance(actual_value, str):
            if actual_value != expected_value:
                return False
        # Numeric comparison
        elif isinstance(expected_value, (int, float)) and isinstance(actual_value, (int, float)):
            if actual_value != expected_value:
                return False
        # Other types: direct equality
        elif actual_value != expected_value:
            return False

    return True
