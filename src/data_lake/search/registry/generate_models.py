#!/usr/bin/env python3
"""
Generate Pydantic models from Azure AI Search index schema (index.jinja).

This script reads the index.jinja file and generates corresponding Pydantic models
to ensure type safety and validation when working with search documents.

Usage:
    python generate_models.py [--validate]

    --validate: Only validate that models.py matches index.jinja (don't regenerate)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict


# Type mapping from Azure Search EDM types to Python types
EDM_TO_PYTHON = {
    "Edm.String": "str",
    "Edm.Int32": "int",
    "Edm.Int64": "int",
    "Edm.Double": "float",
    "Edm.Boolean": "bool",
    "Edm.DateTimeOffset": "datetime",
    "Collection(Edm.String)": "List[str]",
    "Collection(Edm.Int32)": "List[int]",
    "Collection(Edm.Single)": "List[float]",
    "Collection(Edm.Double)": "List[float]",
}


def get_python_type(edm_type: str) -> str:
    """Convert EDM type to Python type annotation."""
    return EDM_TO_PYTHON.get(edm_type, "str")


def generate_field_definition(field: Dict, is_key: bool = False) -> str:
    """
    Generate a Pydantic field definition from an index field.

    Args:
        field: Field definition from index.jinja
        is_key: Whether this is the key field (required)

    Returns:
        String representation of the Pydantic field
    """
    name = field["name"]
    edm_type = field["type"]
    python_type = get_python_type(edm_type)

    # Key field is required, others are optional
    if is_key:
        type_annotation = python_type
        default = "..."
    else:
        type_annotation = f"Optional[{python_type}]"
        default = "None"

    # Build Field() arguments
    field_args = [default]

    # Add description
    description = f"{name.replace('_', ' ').title()}"
    if "dimensions" in field:
        description += f" (vector with {field['dimensions']} dimensions)"
    field_args.append(f'description="{description}"')

    # Add alias if field name uses camelCase
    if name[0].islower() and any(c.isupper() for c in name):
        field_args.append(f'alias="{name}"')

    # Add vector dimension constraints
    if edm_type == "Collection(Edm.Single)" and "dimensions" in field:
        dims = field["dimensions"]
        field_args.append(f"min_length={dims}")
        field_args.append(f"max_length={dims}")

    field_def = f"    {name}: {type_annotation} = Field({', '.join(field_args)})"

    return field_def


def generate_models_file(index_path: Path) -> str:
    """
    Generate Pydantic models from index.jinja.

    Args:
        index_path: Path to index.jinja

    Returns:
        Generated Python code as string
    """
    with open(index_path, "r") as f:
        schema = json.load(f)

    index_name = schema["name"]
    fields = schema["fields"]

    # Find key field
    key_field = next((f for f in fields if f.get("key")), None)
    if not key_field:
        raise ValueError("No key field found in index schema")

    # Generate imports
    imports = [
        '"""',
        "Pydantic models for the artifact registry index.",
        "",
        "This file is AUTO-GENERATED from index.jinja by generate_models.py.",
        "Do not edit manually - regenerate using: python generate_models.py",
        f"Generated: {datetime.now().isoformat()}",
        '"""',
        "",
        "from datetime import datetime",
        "from typing import List, Optional",
        "",
        "from pydantic import BaseModel, Field, field_serializer",
        "",
    ]

    # Generate main model class
    class_name = "ArtifactRegistryDocument"
    model_lines = [
        f"class {class_name}(BaseModel):",
        '    """',
        f"    Document model for the {index_name} index.",
        "    ",
        "    Represents an artifact with enriched metadata from both blob-details",
        "    and semantic dataset registry.",
        '    """',
        "",
    ]

    # Group fields by category for better organization
    key_fields = [f for f in fields if f.get("key")]
    datetime_fields = [f for f in fields if f["type"] == "Edm.DateTimeOffset"]
    other_fields = [f for f in fields if not f.get("key") and f["type"] != "Edm.DateTimeOffset"]

    # Generate key field
    model_lines.append("    # Key field - required")
    for field in key_fields:
        model_lines.append(generate_field_definition(field, is_key=True))
    model_lines.append("")

    # Generate other fields
    model_lines.append("    # Document fields")
    for field in other_fields:
        model_lines.append(generate_field_definition(field))
    model_lines.append("")

    # Generate datetime fields
    if datetime_fields:
        model_lines.append("    # Timestamp fields")
        for field in datetime_fields:
            model_lines.append(generate_field_definition(field))
        model_lines.append("")

    # Add datetime serializer if there are datetime fields
    if datetime_fields:
        datetime_field_names = ", ".join(f'"{f["name"]}"' for f in datetime_fields)
        model_lines.extend(
            [
                "    @field_serializer(" + datetime_field_names + ")",
                "    def serialize_datetime(self, dt: Optional[datetime]) -> Optional[str]:",
                '        """Serialize datetime to ISO 8601 format with Z suffix for Azure Search."""',
                "        if dt is None:",
                "            return None",
                "        # Ensure UTC and format with Z suffix",
                "        if dt.tzinfo is None:",
                '            return dt.isoformat() + "Z"',
                '        return dt.isoformat().replace("+00:00", "Z")',
                "",
            ]
        )

    # Add Config class
    model_lines.extend(
        [
            "    class Config:",
            '        """Pydantic configuration."""',
            "        populate_by_name = True",
            "",
        ]
    )

    # Combine all parts
    code = "\n".join(imports + model_lines)

    return code


def validate_models_match(index_path: Path, models_path: Path) -> bool:
    """
    Validate that models.py is in sync with index.jinja.

    Args:
        index_path: Path to index.jinja
        models_path: Path to models.py

    Returns:
        True if in sync, False otherwise
    """
    if not models_path.exists():
        print(f"ERROR: {models_path} does not exist")
        return False

    # Read index schema
    with open(index_path, "r") as f:
        schema = json.load(f)

    # Read models file
    with open(models_path, "r") as f:
        models_content = f.read()

    # Check that all fields from schema are present in models
    fields = schema["fields"]
    missing_fields = []

    for field in fields:
        field_name = field["name"]
        # Check if field name appears in the models file
        if f"{field_name}:" not in models_content and f'"{field_name}"' not in models_content:
            missing_fields.append(field_name)

    if missing_fields:
        print(f"ERROR: Models missing fields from index.jinja: {', '.join(missing_fields)}")
        return False

    # Check type mappings
    for field in fields:
        field_name = field["name"]
        edm_type = field["type"]
        python_type = get_python_type(edm_type)

        # Check if the type appears near the field definition
        # This is a simple heuristic - could be made more robust
        if field_name in models_content:
            # Find the line with the field definition
            for line in models_content.split("\n"):
                if f"{field_name}:" in line:
                    if python_type not in line:
                        print(f"WARNING: Field '{field_name}' may have incorrect type")
                    break

    print("✓ Models appear to be in sync with index.jinja")
    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate Pydantic models from Azure AI Search index schema")
    parser.add_argument(
        "--validate", action="store_true", help="Only validate that models.py matches index.jinja (don't regenerate)"
    )
    parser.add_argument("--index", default="index.jinja", help="Path to index.jinja file (default: index.jinja)")
    parser.add_argument("--output", default="models.py", help="Path to output models.py file (default: models.py)")

    args = parser.parse_args()

    # Resolve paths
    script_dir = Path(__file__).parent
    index_path = script_dir / args.index
    output_path = script_dir / args.output

    if not index_path.exists():
        print(f"ERROR: Index file not found: {index_path}")
        sys.exit(1)

    if args.validate:
        # Validation mode
        success = validate_models_match(index_path, output_path)
        sys.exit(0 if success else 1)
    else:
        # Generation mode
        print(f"Generating Pydantic models from {index_path}...")

        try:
            code = generate_models_file(index_path)

            # Write to file
            with open(output_path, "w") as f:
                f.write(code)

            print(f"✓ Successfully generated {output_path}")

            # Validate Python syntax
            try:
                compile(code, str(output_path), "exec")
                print("✓ Generated code is valid Python")
            except SyntaxError as e:
                print(f"ERROR: Generated code has syntax errors: {e}")
                sys.exit(1)

        except Exception as e:
            print(f"ERROR: Failed to generate models: {e}")
            import traceback

            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
