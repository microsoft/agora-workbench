"""
Deploy and manage blob storage cataloging in Microsoft Purview.

This script supports scanning blob storage and working with native Purview entities.

Workflows:
1. Register blob storage account as a data source
2. Create scan (optionally filtered to specific containers)
3. Trigger scan to catalog storage contents

Prerequisites:
- User must have appropriate Purview roles:
  - Data Source Administrator (for data source and scan management)
- Purview managed identity must have 'Storage Blob Data Reader' role on storage account
"""

import argparse
import json
import logging
import requests
import sys
import uuid
from typing import Any, Dict, List, Optional

from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
from azure.purview.administration.account import PurviewAccountClient
from azure.purview.scanning import PurviewScanningClient
from auth import get_purview_credential

logger = logging.getLogger(__name__)


class PurviewDataSourceManager:
    """
    Manager for Purview data source scanning and catalog operations.
    """

    def __init__(self, purview_account: str):
        """
        Initialize Purview clients.

        Args:
            purview_account: Purview account name
        """
        self.purview_account = purview_account
        self.endpoint = f"https://{purview_account}.purview.azure.com"
        self.scan_endpoint = f"https://{purview_account}.scan.purview.azure.com"

        credential = get_purview_credential()

        # Initialize clients
        self.scanning_client = PurviewScanningClient(endpoint=self.scan_endpoint, credential=credential)

        self.account_client = PurviewAccountClient(endpoint=self.endpoint, credential=credential)

        logger.info(f"Connected to Purview account: {purview_account}")

    def disable_resource_sets(self) -> None:
        """Disable resource set pattern detection at the account level."""

        try:
            # Acquire a token for the Purview resource
            token = self.account_client._config.credential.get_token("https://purview.azure.net/.default")

            # Endpoint for default resource set rule config
            url = f"{self.endpoint}/account/resourceSetRuleConfigs/defaultResourceSetRuleConfig?api-version=2019-11-01-preview"
            headers = {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}

            # Step 1: GET current configuration
            logger.debug(f"GET {url}")
            resp = requests.get(url, headers=headers)

            logger.debug(f"Response status: {resp.status_code}")
            if resp.content:
                logger.debug(f"Response body:\n{resp.text}")

            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                if resp.status_code == 404:
                    logger.error(
                        "The resource set configuration endpoint is not available for this Purview account.\n"
                        "This often means Advanced Resource Sets are not enabled for your Unified Catalog/region.\n"
                        "Use Pattern rules in Purview Studio if available (Data Map → Source management → Pattern rules)."
                    )
                raise

            # Parse current config
            current_config = resp.json()
            logger.debug("Current configuration retrieved successfully")

            # Step 2: Modify the enableDefaultPatterns field
            if "pathPatternConfig" in current_config:
                current_config["pathPatternConfig"]["enableDefaultPatterns"] = False
            elif (
                "resourceSetRuleConfig" in current_config
                and "pathPatternConfig" in current_config["resourceSetRuleConfig"]
            ):
                current_config["resourceSetRuleConfig"]["pathPatternConfig"]["enableDefaultPatterns"] = False
            else:
                raise ValueError("Unexpected config structure - cannot find pathPatternConfig")

            # Step 3: PUT modified config back
            logger.debug(f"PUT {url}")
            logger.debug(f"Modified config:\n{json.dumps(current_config, indent=2)}")

            resp = requests.put(url, json=current_config, headers=headers)

            logger.debug(f"Response status: {resp.status_code}")
            if resp.content:
                logger.debug(f"Response body:\n{resp.text}")

            resp.raise_for_status()

            logger.info("✓ Resource set pattern detection disabled")

        except Exception as e:
            logger.error(f"Failed to disable resource sets: {e}")
            raise

    # Collection Management

    def get_collection(self, collection_name: str) -> Optional[Dict[str, Any]]:
        """
        Get a collection by name.

        Args:
            collection_name: Collection name

        Returns:
            Collection details or None if not found
        """
        try:
            result = self.account_client.collections.get_collection(collection_name)
            return dict(result) if result else None
        except ResourceNotFoundError:
            logger.debug(f"Collection not found: {collection_name}")
            return None
        except Exception as e:
            logger.debug(f"Collection check failed: {e}")
            return None

    def create_collection(
        self, collection_name: str, parent_collection: Optional[str] = None, description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a collection in Purview.

        Args:
            collection_name: Name for the new collection (friendly name)
            parent_collection: Parent collection name (defaults to root)
            description: Optional description for the collection

        Returns:
            Created collection details
        """
        parent = parent_collection or self.purview_account

        logger.info(f"Creating collection '{collection_name}' under parent '{parent}'")

        collection_def = {"friendlyName": collection_name, "parentCollection": {"referenceName": parent}}

        if description:
            collection_def["description"] = description

        try:
            result = self.account_client.collections.create_or_update_collection(
                collection_name=collection_name, collection=collection_def
            )
            logger.info(f"✓ Created collection: {collection_name}")
            return dict(result)

        except HttpResponseError as e:
            logger.error(f"Failed to create collection: {e}")
            logger.error(f"Collection definition: {json.dumps(collection_def, indent=2)}")
            if "already exists" in str(e).lower():
                logger.info(f"Collection already exists: {collection_name}")
                existing = self.get_collection(collection_name)
                if existing:
                    return existing
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating collection: {e}")
            logger.error(f"Collection definition: {json.dumps(collection_def, indent=2)}")
            raise

    def ensure_collection_exists(self, collection_name: str, auto_create: bool = True) -> None:
        """
        Ensure a collection exists, creating it if necessary.

        Args:
            collection_name: Collection name to check
            auto_create: If True, create collection if it doesn't exist

        Raises:
            ValueError: If collection doesn't exist and auto_create is False
        """
        # Root collection (Purview account name) always exists
        if collection_name == self.purview_account:
            logger.debug(f"Using root collection: {collection_name}")
            return

        logger.info(f"Checking collection: {collection_name}")

        # Check if collection exists
        existing = self.get_collection(collection_name)

        if existing:
            logger.info(f"✓ Collection exists: {collection_name}")
            return

        # Collection doesn't exist
        if auto_create:
            logger.info(f"Collection '{collection_name}' not found, creating it...")
            try:
                self.create_collection(
                    collection_name=collection_name,
                    parent_collection=self.purview_account,
                    description=f"Auto-created collection for {collection_name}",
                )
                # Verify it was created
                verify = self.get_collection(collection_name)
                if not verify:
                    raise ValueError(f"Failed to verify collection '{collection_name}' after creation")
                logger.info(f"✓ Collection '{collection_name}' created and verified")
            except Exception as e:
                logger.error(f"Failed to create collection: {e}")
                raise ValueError(
                    f"Collection '{collection_name}' does not exist and could not be created: {e}. "
                    f"Please create it manually in the Purview Portal."
                )
        else:
            raise ValueError(
                f"Collection '{collection_name}' does not exist. "
                f"Please create it in the Purview Portal or use auto_create=True."
            )

    # Data Source Management

    def register_blob_storage(
        self, storage_account: str, resource_group: str, subscription_id: str, collection_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register an Azure Blob Storage account as a data source in Purview.

        Note: Each storage account can only be registered once in Purview.
        Use scans to target specific containers within the storage account.

        Args:
            storage_account: Storage account name (also used as data source name)
            resource_group: Resource group containing the storage account
            subscription_id: Azure subscription ID
            collection_name: Purview collection to associate with (defaults to root collection)

        Returns:
            Created data source details
        """
        data_source_name = storage_account

        collection_name = collection_name or self.purview_account  # Default to root collection

        # Ensure collection exists
        self.ensure_collection_exists(collection_name)

        logger.info(f"Registering blob storage data source: {data_source_name}")
        logger.info(f"Using collection: {collection_name}")

        # Build data source definition
        data_source = {
            "kind": "AzureStorage",
            "properties": {
                "resourceGroup": resource_group,
                "subscriptionId": subscription_id,
                "resourceName": storage_account,
                "endpoint": f"https://{storage_account}.blob.core.windows.net/",
                "collection": {"referenceName": collection_name, "type": "CollectionReference"},
            },
        }

        logger.debug(f"Data source definition: {json.dumps(data_source, indent=2)}")

        try:
            result = self.scanning_client.data_sources.create_or_update(
                data_source_name=data_source_name, body=data_source
            )
            logger.info(f"✓ Registered data source: {data_source_name}")
            return result

        except HttpResponseError as e:
            logger.error(f"Failed to register data source: {e}")
            logger.error(f"Request body: {json.dumps(data_source, indent=2)}")
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                logger.info("Data source for this storage account already exists, attempting to retrieve it...")
                # Try to get the data source by the name we tried to create
                try:
                    return self.scanning_client.data_sources.get(data_source_name)
                except Exception:
                    # If that fails, the duplicate is under a different name
                    # Since Purview only allows one data source per endpoint, we can't proceed
                    logger.error(
                        f"A data source for storage account '{storage_account}' already exists under a different name. "
                        f"Each storage account can only be registered once as a data source in Purview. "
                        f"Use the existing data source name instead."
                    )
                    raise
            raise

    def create_storage_scan(
        self,
        data_source_name: str,
        scan_name: Optional[str] = None,
        container_names: Optional[List[str]] = None,
        collection_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a scan for a registered Azure Blob Storage data source.

        Args:
            data_source_name: Name of the registered data source
            scan_name: Custom scan name (defaults to "{data_source_name}_{container}_scan" if container specified, else "{data_source_name}-scan")
            container_names: List of container names to scan (None = all containers)
            collection_name: Collection name (must match data source collection)

        Returns:
            Created scan details
        """
        # Generate default scan name based on container
        if scan_name is None:
            if container_names and len(container_names) > 0:
                scan_name = f"{data_source_name}_{container_names[0]}_scan"
            else:
                scan_name = f"{data_source_name}-scan"

        collection_name = collection_name or self.purview_account

        logger.info(f"Creating scan '{scan_name}' for data source: {data_source_name}")

        # Build scan definition for Azure Storage
        scan_def = {
            "kind": "AzureStorageMsi",  # Managed Identity authentication for Azure Storage
            "properties": {
                "scanRulesetName": "AzureStorage",
                "scanRulesetType": "System",
                "collection": {"referenceName": collection_name, "type": "CollectionReference"},
            },
        }

        logger.debug(f"Scan definition: {json.dumps(scan_def, indent=2)}")

        try:
            result = self.scanning_client.scans.create_or_update(
                data_source_name=data_source_name, scan_name=scan_name, body=scan_def
            )
            logger.info(f"\u2713 Created scan: {scan_name}")

            # Apply container filter if specified (separate API call)
            if container_names:
                self._apply_scan_filter(data_source_name, scan_name, container_names)

            return result

        except HttpResponseError as e:
            logger.error(f"Failed to create scan: {e}")
            logger.error(f"Request body: {json.dumps(scan_def, indent=2)}")
            if "already exists" in str(e).lower():
                logger.info(f"Scan already exists: {scan_name}")
                return self.scanning_client.scans.get(data_source_name, scan_name)
            raise

    def _apply_scan_filter(self, data_source_name: str, scan_name: str, container_names: List[str]) -> None:
        """
        Apply container-level filtering to a scan using the filters API.

        Args:
            data_source_name: Name of the data source
            scan_name: Name of the scan
            container_names: List of container names to include
        """
        # Get the storage account name from data source
        storage_account = data_source_name

        # Build URI prefixes for each container
        include_uris = [f"https://{storage_account}.blob.core.windows.net/{container}" for container in container_names]

        filter_body = {
            "properties": {
                "includeUriPrefixes": include_uris,
                "excludeUriPrefixes": [],
            }
        }

        logger.info(f"Applying container filter to scan '{scan_name}': {container_names}")
        logger.debug(f"Filter URI prefixes: {include_uris}")

        try:
            self.scanning_client.filters.create_or_update(
                data_source_name=data_source_name, scan_name=scan_name, body=filter_body
            )
            logger.info("\u2713 Applied container filter successfully")
        except Exception as e:
            logger.error(f"Failed to apply container filter: {e}")
            raise

    def trigger_scan(
        self,
        data_source_name: str,
        scan_name: str = "default-scan",
    ) -> None:
        """
        Trigger a scan to run.

        Args:
            data_source_name: Name of the registered data source
            scan_name: Name of the scan to trigger
        """
        logger.info(f"Triggering scan '{scan_name}' on data source: {data_source_name}")

        # First verify the scan exists
        try:
            scan = self.scanning_client.scans.get(data_source_name, scan_name)
            logger.debug(f"Scan found: {scan.get('name')}")
        except Exception as e:
            logger.error(f"Failed to retrieve scan '{scan_name}': {e}")
            raise ValueError(f"Scan '{scan_name}' not found on data source '{data_source_name}'")

        try:
            run_id = str(uuid.uuid4())
            logger.debug(f"Triggering scan with run_id: {run_id}")

            self.scanning_client.scan_result.run_scan(
                data_source_name=data_source_name, scan_name=scan_name, run_id=run_id
            )

            logger.info(f"✓ Scan triggered successfully with run ID: {run_id}")

        except HttpResponseError as e:
            logger.error(f"Failed to trigger scan: {e}")
            if "InternalServerError" in str(e) or "internal" in str(e).lower():
                logger.warning(
                    "Internal server error when triggering scan. Common causes:\n"
                    "  1. Purview managed identity lacks 'Storage Blob Data Reader' role on the storage account\n"
                    "  2. Storage account firewall is blocking Purview's access\n"
                    "  3. Scan configuration is incomplete or invalid\n"
                    "  4. Purview service is experiencing issues\n"
                    "Try:\n"
                    "  - Verify the managed identity has proper permissions\n"
                    "  - Check storage account firewall settings\n"
                    "  - Test the connection in Purview Portal\n"
                    "  - Wait a few minutes and try again"
                )
            raise

    def get_data_source(self, data_source_name: str) -> Optional[Dict[str, Any]]:
        """Get data source details."""
        try:
            return self.scanning_client.data_sources.get(data_source_name)
        except ResourceNotFoundError:
            return None

    def list_scans(self, data_source_name: str) -> List[str]:
        """List all scans for a data source."""
        try:
            result = self.scanning_client.scans.list_by_data_source(data_source_name)
            scans = result.get("value", []) if isinstance(result, dict) else []
            scan_names = []
            for scan in scans:
                if isinstance(scan, dict) and "name" in scan:
                    scan_names.append(str(scan["name"]))
            return scan_names
        except HttpResponseError:
            return []


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Manage blob storage cataloging in Microsoft Purview")

    parser.add_argument("--account", required=True, help="Purview account name")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Register storage
    register_parser = subparsers.add_parser(
        "register-storage", help="Register an Azure Blob Storage account as a data source"
    )
    register_parser.add_argument("--storage-account", required=True, help="Storage account name")
    register_parser.add_argument("--resource-group", required=True, help="Resource group name")
    register_parser.add_argument("--subscription-id", required=True, help="Azure subscription ID")
    register_parser.add_argument("--collection", help="Purview collection name (defaults to root)")

    # Configure resource sets
    subparsers.add_parser(
        "configure-resource-sets", help="Configure resource set pattern detection at account level (one-time setup)"
    )

    # Create scan
    scan_create_parser = subparsers.add_parser(
        "create-storage-scan", help="Create a scan for a registered Azure Blob Storage data source"
    )
    scan_create_parser.add_argument("--storage-account", required=True, help="Storage account name")
    scan_create_parser.add_argument("--container", help="Container name to scan (default: all)")
    scan_create_parser.add_argument("--collection", required=True, help="Purview collection name")
    scan_create_parser.add_argument("--scan-name", help="Scan name (auto-generated if not provided)")

    # Trigger scan
    scan_trigger_parser = subparsers.add_parser("scan", help="Trigger a scan to catalog blob storage")
    scan_trigger_parser.add_argument("--data-source", "--storage-account", required=True, help="Data source name")
    scan_trigger_parser.add_argument("--scan-name", help="Scan name")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Commands that need scanning client
    if args.command in ["register-storage", "configure-resource-sets", "create-storage-scan", "scan"]:
        manager = PurviewDataSourceManager(args.account)

        if args.command == "register-storage":
            result = manager.register_blob_storage(
                storage_account=args.storage_account,
                resource_group=args.resource_group,
                subscription_id=args.subscription_id,
                collection_name=args.collection,
            )
            print(f"✓ Registered data source: {result.get('name')}")

        elif args.command == "configure-resource-sets":
            manager.disable_resource_sets()

        elif args.command == "create-storage-scan":
            result = manager.create_storage_scan(
                data_source_name=args.storage_account,
                scan_name=args.scan_name,
                container_names=[args.container] if args.container else None,
                collection_name=args.collection,
            )
            print(f"✓ Created scan: {result.get('name')}")

        elif args.command == "scan":
            scan_name = args.scan_name
            manager.trigger_scan(
                data_source_name=args.data_source,
                scan_name=scan_name,
            )
            print("✓ Scan triggered")


if __name__ == "__main__":
    main()
