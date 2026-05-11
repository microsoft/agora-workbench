"""
Deploy Azure AI Search registry index.
This script deploys the artifact-registry index for data lake governance and discovery.
Uses Azure Identity (managed identity or default credential chain) for authentication.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from utilities.auth import get_token_provider
from jinja2 import Template


def get_token() -> str:
    """Acquire a bearer token for Azure AI Search."""
    return get_token_provider("https://search.azure.com/.default")()


def deploy_resource(
    endpoint: str,
    resource_type: str,
    resource_name: str,
    payload: dict[str, Any],
    token: str,
    api_version: str = "2025-11-01-preview",
) -> None:
    """Deploy (create or update) an Azure AI Search resource."""
    url = f"{endpoint}/{resource_type}/{resource_name}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    params = {"api-version": api_version}

    print(f"Deploying {resource_type}: {resource_name}")

    response = httpx.put(url, json=payload, headers=headers, params=params, timeout=30.0)

    print(f"  HTTP Status: {response.status_code}")

    if response.status_code not in (200, 201, 204):
        print(f"  ERROR: {response.text}", file=sys.stderr)
        response.raise_for_status()

    print("  ✓ Success\n")


def deploy_index(args: argparse.Namespace) -> None:
    """Deploy the artifact-registry index and update alias."""
    search_endpoint = f"https://{args.search_service}.search.windows.net"
    token = get_token()

    index_path = Path(__file__).parent / "index.jinja"
    with open(index_path) as f:
        index_template_str = f.read()

    # Render Jinja template with Azure OpenAI parameters
    template = Template(index_template_str)
    index_json = template.render(
        azure_openai_endpoint=args.azure_openai_endpoint,
        azure_openai_embedding_deployment=args.azure_openai_embedding_deployment,
    )
    index_payload = json.loads(index_json)

    # Get index name from the payload
    index_name = index_payload["name"]
    print(f"=== Deploying index: {index_name} ===\n")

    deploy_resource(
        endpoint=search_endpoint,
        resource_type="indexes",
        resource_name=index_name,
        payload=index_payload,
        token=token,
    )

    # Automatically create/update alias to point to this version
    # Extract base name (remove version suffix like -v1, -v2, etc.)
    alias_name = re.sub(r"-v\d+$", "", index_name)

    if alias_name != index_name:  # Only create alias if there's a version suffix
        print(f"Updating alias: {alias_name} -> {index_name}")
        alias_payload = {"name": alias_name, "indexes": [index_name]}
        deploy_resource(
            endpoint=search_endpoint,
            resource_type="aliases",
            resource_name=alias_name,
            payload=alias_payload,
            token=token,
        )

    print("=== Index deployment complete ===\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Deploy Azure AI Search artifact-registry index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--search-service",
        default=os.getenv("DATA_LAKE_SEARCH_NAME"),
        required=not os.getenv("DATA_LAKE_SEARCH_NAME"),
        help="Azure AI Search service name. Can be set via DATA_LAKE_SEARCH_NAME environment variable.",
    )

    parser.add_argument(
        "--azure-openai-endpoint",
        default=os.getenv("DATA_LAKE_VECTORIZER_ENDPOINT"),
        required=not os.getenv("DATA_LAKE_VECTORIZER_ENDPOINT"),
        help="Azure OpenAI endpoint (e.g., https://your-openai.openai.azure.com). Can be set via DATA_LAKE_VECTORIZER_ENDPOINT environment variable.",
    )

    parser.add_argument(
        "--azure-openai-embedding-deployment",
        default=os.getenv("DATA_LAKE_VECTORIZER_DEPLOYMENT"),
        required=not os.getenv("DATA_LAKE_VECTORIZER_DEPLOYMENT"),
        help="Azure OpenAI embedding model deployment name (e.g., text-embedding-ada-002). Can be set via DATA_LAKE_VECTORIZER_DEPLOYMENT environment variable.",
    )

    return parser.parse_args()


def main() -> None:
    """Main deployment workflow."""
    args = parse_args()
    deploy_index(args)


if __name__ == "__main__":
    main()
