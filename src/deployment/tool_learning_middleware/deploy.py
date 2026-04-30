"""
Deploy Azure AI Search resources for the tool-learning vignette index.

Deploys the index, data source, skillset, and indexer that populate the
``tool-vignettes`` search index from an Azure Table Storage table.

Uses Azure CLI credentials for authentication (run ``az login`` first).
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

import httpx
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from jinja2 import Template


load_dotenv()


def get_credential_and_token() -> tuple[AzureCliCredential, str]:
    """Authenticate and get access token."""
    credential = AzureCliCredential()
    token = credential.get_token("https://search.azure.com/.default").token
    return credential, token


def load_template(template_path: Path, substitutions: dict[str, str]) -> dict[str, Any]:
    """Load JSON template and render with Jinja2."""
    with open(template_path) as f:
        template_content = f.read()

    template = Template(template_content)
    rendered = template.render(**substitutions)

    return json.loads(rendered)


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


def run_indexer(
    endpoint: str,
    indexer_name: str,
    token: str,
    api_version: str = "2025-11-01-preview",
) -> None:
    """Trigger an immediate run of the indexer."""
    url = f"{endpoint}/indexers/{indexer_name}/run"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"api-version": api_version}

    print(f"Running indexer: {indexer_name}")

    response = httpx.post(url, headers=headers, params=params, timeout=30.0)

    print(f"  HTTP Status: {response.status_code}")

    if response.status_code not in (202, 204):
        print(f"  WARNING: {response.text}", file=sys.stderr)
    else:
        print("  ✓ Indexer run initiated\n")


def deploy_index(args: argparse.Namespace) -> None:
    """Deploy the tool-vignettes index and update alias."""
    search_endpoint = args.search_endpoint.rstrip("/")
    _, token = get_credential_and_token()

    index_path = Path(__file__).parent / "index.jinja"
    index_payload = load_template(
        index_path,
        {
            "azure_openai_endpoint": args.azure_openai_endpoint,
            "azure_openai_embedding_deployment": args.azure_openai_embedding_deployment,
            "user_assigned_identity_resource_id": args.managed_identity_id,
        },
    )

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
    alias_name = re.sub(r"-v\d+$", "", index_name)

    if alias_name != index_name:
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


def deploy_source(args: argparse.Namespace) -> None:
    """Deploy data source, skillset, and indexer for a Table Storage source."""
    print(f"=== Deploying source: {args.source_id} ===\n")

    search_endpoint = args.search_endpoint.rstrip("/")
    _, token = get_credential_and_token()

    # Get the actual index name from index.jinja
    index_path = Path(__file__).parent / "index.jinja"
    index_payload = load_template(
        index_path,
        {
            "azure_openai_endpoint": args.azure_openai_endpoint,
            "azure_openai_embedding_deployment": args.azure_openai_embedding_deployment,
            "user_assigned_identity_resource_id": args.managed_identity_id,
        },
    )
    target_index_name = index_payload["name"]

    # 1. Deploy data source
    datasource_path = Path(__file__).parent / "datasource.jinja"
    datasource_payload = load_template(
        datasource_path,
        {
            "SOURCE_ID": args.source_id,
            "STORAGE_RESOURCE_ID": args.storage_resource_id,
            "USER_ASSIGNED_IDENTITY_RESOURCE_ID": args.managed_identity_id,
            "TABLE_NAME": args.table_name,
        },
    )
    deploy_resource(
        endpoint=search_endpoint,
        resource_type="datasources",
        resource_name=f"vignette-ds-{args.source_id}",
        payload=datasource_payload,
        token=token,
    )

    # 2. Deploy skillset
    skillset_path = Path(__file__).parent / "skillset.jinja"
    skillset_payload = load_template(
        skillset_path,
        {
            "SOURCE_ID": args.source_id,
            "AZURE_OPENAI_ENDPOINT": args.azure_openai_endpoint,
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": args.azure_openai_embedding_deployment,
            "USER_ASSIGNED_IDENTITY_RESOURCE_ID": args.managed_identity_id,
        },
    )
    deploy_resource(
        endpoint=search_endpoint,
        resource_type="skillsets",
        resource_name=f"vignette-skillset-{args.source_id}",
        payload=skillset_payload,
        token=token,
    )

    # 3. Deploy indexer
    indexer_path = Path(__file__).parent / "indexer.jinja"
    indexer_payload = load_template(
        indexer_path,
        {
            "SOURCE_ID": args.source_id,
            "TARGET_INDEX_NAME": target_index_name,
        },
    )
    indexer_name = f"vignette-indexer-{args.source_id}"
    deploy_resource(
        endpoint=search_endpoint,
        resource_type="indexers",
        resource_name=indexer_name,
        payload=indexer_payload,
        token=token,
    )

    print("=== Source deployment complete ===\n")

    if not args.deploy_only:
        run_indexer(search_endpoint, indexer_name, token)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Deploy Azure AI Search resources for tool-learning vignettes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--search-endpoint",
        default=os.getenv("TOOL_LEARNING_SEARCH_ENDPOINT"),
        required=not os.getenv("TOOL_LEARNING_SEARCH_ENDPOINT"),
        help="Azure AI Search endpoint URL (or set TOOL_LEARNING_SEARCH_ENDPOINT).",
    )
    parser.add_argument(
        "--azure-openai-endpoint",
        default=os.getenv("TOOL_LEARNING_VECTORIZER_ENDPOINT"),
        required=not os.getenv("TOOL_LEARNING_VECTORIZER_ENDPOINT"),
        help="Azure OpenAI endpoint for integrated vectorization (or set TOOL_LEARNING_VECTORIZER_ENDPOINT).",
    )
    parser.add_argument(
        "--azure-openai-embedding-deployment",
        default=os.getenv("TOOL_LEARNING_VECTORIZER_DEPLOYMENT", "text-embedding-3-large"),
        help="Azure OpenAI embedding model deployment name (default: text-embedding-3-large).",
    )
    parser.add_argument(
        "--managed-identity-id",
        default=os.getenv("DEFAULT_IDENTITY_RESOURCE_ID"),
        required=not os.getenv("DEFAULT_IDENTITY_RESOURCE_ID"),
        help="User-assigned managed identity resource ID for Azure OpenAI and Table Storage access (or set DEFAULT_IDENTITY_RESOURCE_ID).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Resource to deploy: {index, source}")

    # Index subcommand
    subparsers.add_parser(
        "index",
        help="Deploy the tool-vignettes index and automatically update the alias",
    )

    # Source subcommand (datasource + skillset + indexer)
    source_parser = subparsers.add_parser(
        "source",
        help="Deploy a data source, skillset, and indexer for Table Storage",
    )
    source_parser.add_argument(
        "--source-id",
        default="agora-vignettes",
        required=False,
        help="A unique identifier for this data source and indexer (default: agora-vignettes).",
    )
    source_parser.add_argument(
        "--storage-resource-id",
        default=os.getenv("TOOL_LEARNING_STORAGE_RESOURCE_ID"),
        required=not os.getenv("TOOL_LEARNING_STORAGE_RESOURCE_ID"),
        help="Azure resource ID of the storage account containing the vignette table (or set TOOL_LEARNING_STORAGE_RESOURCE_ID).",
    )
    source_parser.add_argument(
        "--table-name",
        default=os.getenv("TOOL_LEARNING_TABLE_NAME", "ToolVignettes"),
        help="Table Storage table name (default: ToolVignettes, or set TOOL_LEARNING_TABLE_NAME).",
    )
    source_parser.add_argument(
        "--deploy-only",
        action="store_true",
        default=False,
        help="Deploy without running the indexer immediately",
    )

    return parser.parse_args()


def main() -> None:
    """Main deployment workflow."""
    args = parse_args()

    # Azure AI Search resourceUri expects just the base URL (scheme + host),
    # not the full /openai/deployments/... path that TOOL_LEARNING_VECTORIZER_ENDPOINT may contain.
    if args.azure_openai_endpoint:
        parsed = urlparse(args.azure_openai_endpoint)
        args.azure_openai_endpoint = f"{parsed.scheme}://{parsed.netloc}"

    if args.command == "index":
        deploy_index(args)
    elif args.command == "source":
        deploy_source(args)
    else:
        raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
