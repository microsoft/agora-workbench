"""
Blind test fixtures for ToolMaker agent evaluation.

These test cases are NOT shown to the agent - they are used for
post-hoc validation of the generated tools.

Structure:
  {
    "domain_name": {
      "tool_name": [
        {"input": {...}, "expected_output": {...}},
        ...
      ]
    }
  }
"""

BLIND_TEST_FIXTURES = {
    "roman": {
        "to_roman": [
            {"input": {"n": 1}, "expected": {"roman": "I"}},
            {"input": {"n": 4}, "expected": {"roman": "IV"}},
            {"input": {"n": 9}, "expected": {"roman": "IX"}},
            {"input": {"n": 42}, "expected": {"roman": "XLII"}},
            {"input": {"n": 1999}, "expected": {"roman": "MCMXCIX"}},
            {"input": {"n": 3999}, "expected": {"roman": "MMMCMXCIX"}},
        ]
    },
    "humanize": {
        "humanize_number": [
            {"input": {"number": 1234567, "format_type": "intword"}, "expected": {"result": "1.2 million"}},
            {"input": {"number": 1234567, "format_type": "intcomma"}, "expected": {"result": "1,234,567"}},
            {"input": {"number": 4, "format_type": "apnumber"}, "expected": {"result": "four"}},
            {"input": {"number": 42, "format_type": "ordinal"}, "expected": {"result": "42nd"}},
        ]
    },
}
