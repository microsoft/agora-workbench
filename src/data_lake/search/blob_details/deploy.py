"""
Deploy Azure AI Search resources for blob-details-v1.
This script deploys the index, data source, and indexer for a specific blob storage source.
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
from azure.identity import AzureCliCredential
from jinja2 import Template


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
        print("  Indexer run initiated\n")


def deploy_index(args: argparse.Namespace) -> None:
    """Deploy the blob-details index and update alias."""
    search_endpoint = f"https://{args.search_service}.search.windows.net"
    _, token = get_credential_and_token()

    index_path = Path(__file__).parent / "index.json"
    with open(index_path) as f:
        index_payload = json.load(f)

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


def deploy_datasource(args: argparse.Namespace) -> None:
    """Deploy a data source for blob storage."""
    print(f"=== Deploying data source: blob-ds-{args.source_id} ===\n")

    search_endpoint = f"https://{args.search_service}.search.windows.net"
    _, token = get_credential_and_token()

    datasource_template_path = Path(__file__).parent / "datasource.jinja"
    datasource_payload = load_template(
        datasource_template_path,
        {
            "SOURCE_ID": args.source_id,
            "STORAGE_RESOURCE_ID": args.storage_resource_id,
            "USER_ASSIGNED_IDENTITY_RESOURCE_ID": args.managed_identity_id,
            "CONTAINER_NAME": args.container_name,
            "CONTAINER_QUERY": args.container_query or "",
        },
    )

    deploy_resource(
        endpoint=search_endpoint,
        resource_type="datasources",
        resource_name=f"blob-ds-{args.source_id}",
        payload=datasource_payload,
        token=token,
    )

    print("=== Data source deployment complete ===\n")


def deploy_indexer(args: argparse.Namespace) -> None:
    """Deploy an indexer for blob storage."""
    print(f"=== Deploying indexer: blob-details-indexer-{args.source_id} ===\n")

    search_endpoint = f"https://{args.search_service}.search.windows.net"
    _, token = get_credential_and_token()

    # Get the actual index name from index.json
    index_path = Path(__file__).parent / "index.json"
    with open(index_path) as f:
        index_payload = json.load(f)
    target_index_name = index_payload["name"]

    # Normalize extensions to ensure they have leading dots
    def normalize_extensions(extensions: list[str] | None) -> str:
        if not extensions:
            return ""
        normalized = [ext if ext.startswith(".") else f".{ext}" for ext in extensions]
        return ",".join(normalized)

    indexer_template_path = Path(__file__).parent / "indexer.jinja"
    indexer_payload = load_template(
        indexer_template_path,
        {
            "SOURCE_ID": args.source_id,
            "TARGET_INDEX_NAME": target_index_name,
            "INCLUDED_EXTENSIONS": normalize_extensions(args.included_extensions),
            "EXCLUDED_EXTENSIONS": normalize_extensions(args.excluded_extensions),
        },
    )

    indexer_name = f"blob-details-indexer-{args.source_id}"
    deploy_resource(
        endpoint=search_endpoint,
        resource_type="indexers",
        resource_name=indexer_name,
        payload=indexer_payload,
        token=token,
    )

    print("=== Indexer deployment complete ===\n")

    if not args.deploy_only:
        run_indexer(search_endpoint, indexer_name, token)


def deploy_source(args: argparse.Namespace) -> None:
    """Deploy both a data source and indexer for blob storage."""
    print(f"=== Deploying source: {args.source_id} ===\n")
    deploy_datasource(args)
    deploy_indexer(args)
    print("=== Source deployment complete ===\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Deploy Azure AI Search resources for blob-details-v1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Top-level argument shared by all subcommands
    parser.add_argument(
        "--search-service",
        default=os.getenv("DATA_LAKE_SEARCH_NAME"),
        required=not os.getenv("DATA_LAKE_SEARCH_NAME"),
        help="Azure AI Search service name. Can be set via DATA_LAKE_SEARCH_NAME environment variable.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Resource to deploy: {index, source}")

    # Index subcommand
    subparsers.add_parser(
        "index",
        help="Deploy the blob-details index and automatically update the alias",
    )

    # Source subcommand (combined datasource + indexer)
    source_parser = subparsers.add_parser(
        "source",
        help="Deploy a data source and indexer for blob storage",
    )
    source_parser.add_argument(
        "--source-id",
        required=True,
        help="A unique identifier for this data source and indexer",
    )
    source_parser.add_argument(
        "--storage-resource-id",
        required=True,
        help="Azure resource ID of the storage account",
    )
    source_parser.add_argument(
        "--managed-identity-id",
        default=os.getenv("DEFAULT_IDENTITY_RESOURCE_ID"),
        required=not os.getenv("DEFAULT_IDENTITY_RESOURCE_ID"),
        help="Managed identity resource ID used to authenticate with the storage account. Can be set via DEFAULT_IDENTITY_RESOURCE_ID environment variable.",
    )
    source_parser.add_argument(
        "--container-name",
        required=True,
        help="Blob container name",
    )
    source_parser.add_argument(
        "--container-query",
        default="",
        help="Optional query to filter blobs in the container",
    )
    source_parser.add_argument(
        "--included-extensions",
        default=None,
        nargs="*",
        help="Space-separated list of file extensions to index (e.g.: .json .txt .csv .md .pdf)",
    )
    source_parser.add_argument(
        "--excluded-extensions",
        default=None,
        nargs="*",
        help="Space-separated list of file extensions to exclude (e.g.: .zip .tar .gz)",
    )
    source_parser.add_argument(
        "--deploy-only",
        action="store_true",
        default=False,
        help="Set to prevent the indexer from running immediately after deployment",
    )

    return parser.parse_args()


def main() -> None:
    """Main deployment workflow."""
    args = parse_args()

    if args.command == "index":
        deploy_index(args)
    elif args.command == "source":
        deploy_source(args)
    else:
        raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
