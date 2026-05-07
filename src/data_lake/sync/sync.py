#!/usr/bin/env python3
"""
Sync script to populate the artifact registry index from blob-details index and semantic dataset registry.

This script:
1. Queries the blob-details index for all artifacts
2. Enriches each artifact with metadata from Purview catalog entities
3. Uploads the combined data to the artifact registry index

Usage:
    python sync.py --search-service <service-name> --purview-account <account-name> [options]
"""

import argparse
import datetime
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

import httpx

from azure.core.exceptions import ResourceNotFoundError
from azure.purview.catalog import PurviewCatalogClient
from azure.search.documents import SearchClient
from openai import AzureOpenAI

from auth import get_purview_credential, get_search_credential, get_token_provider
from data_lake.search.registry import ArtifactRegistryDocument

logger = logging.getLogger(__name__)


class StaleArtifactError(Exception):
    """Raised when an artifact's Purview entity no longer exists (stale index entry)."""


class PurviewLookupError(Exception):
    """Raised when a Purview API call fails due to a transient/unexpected error."""


class ArtifactRegistrySync:
    """Synchronizes artifact data from blob-details index and Purview catalog to artifact registry."""

    def __init__(
        self,
        search_service: str,
        purview_account: str,
        azure_openai_endpoint: str,
        azure_openai_embedding_deployment: str,
        blob_details_index: str = "blob-details",
        artifact_registry_index: str = "artifact-registry",
    ):
        """
        Initialize the sync service.

        Args:
            search_service: Azure AI Search service name
            purview_account: Purview account name
            azure_openai_endpoint: Azure OpenAI endpoint for embeddings
            azure_openai_embedding_deployment: Azure OpenAI embedding deployment name
            blob_details_index: Name of the blob-details index
            artifact_registry_index: Name of the artifact registry index
        """
        self.search_service = search_service
        self.search_endpoint = f"https://{search_service}.search.windows.net"
        self.purview_account = purview_account
        self.purview_endpoint = f"https://{purview_account}.purview.azure.com"
        self.blob_details_index = blob_details_index
        self.artifact_registry_index = artifact_registry_index
        self.azure_openai_endpoint = azure_openai_endpoint
        self.azure_openai_embedding_deployment = azure_openai_embedding_deployment

        # Set up credentials using the centralized auth provider chain
        self._search_credential = get_search_credential()
        self._purview_credential = get_purview_credential()

        # Initialize clients
        self._init_clients()

        # Initialize OpenAI client
        self.openai_client = AzureOpenAI(
            api_version="2023-05-15",
            azure_endpoint=azure_openai_endpoint,
            azure_ad_token_provider=get_token_provider("https://cognitiveservices.azure.com/.default"),
        )
        logger.info(f"Azure OpenAI embeddings enabled: {azure_openai_embedding_deployment}")

        # Cache for Purview entities to avoid repeated queries
        self._entity_cache: Dict[str, Dict[str, Any]] = {}

        # Lazily-initialized httpx client for blob HEAD checks (connection pooling)
        self._http_client: Optional[httpx.Client] = None

    def _init_clients(self):
        """Initialize Azure service clients."""
        logger.info(f"Connecting to Azure AI Search: {self.search_endpoint}")

        self.blob_details_client = SearchClient(
            endpoint=self.search_endpoint,
            index_name=self.blob_details_index,
            credential=self._search_credential,
        )

        self.artifact_registry_client = SearchClient(
            endpoint=self.search_endpoint,
            index_name=self.artifact_registry_index,
            credential=self._search_credential,
        )

        logger.info(f"Connecting to Purview: {self.purview_endpoint}")
        self.catalog_client = PurviewCatalogClient(endpoint=self.purview_endpoint, credential=self._purview_credential)

    def get_purview_entity(self, qualified_name: str, entity_type: str = "azure_blob_path") -> Optional[Dict[str, Any]]:
        """
        Retrieve entity metadata from Purview catalog.

        Args:
            qualified_name: Qualified name of the entity (usually the blob URL)
            entity_type: Entity type (default: azure_blob_path)

        Returns:
            Dictionary with entity metadata, or None if the entity does not exist.

        Raises:
            PurviewLookupError: On transient or unexpected failures (timeouts,
                500s, auth errors).  Callers should treat this as a retriable
                error rather than a confirmed-missing entity.
        """
        # Use qualified name and type as cache key
        cache_key = f"{entity_type}:{qualified_name}"

        # Check cache first
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]

        try:
            # Query Purview by qualified name - include relationships
            result = self.catalog_client.entity.get_by_unique_attributes(
                type_name=entity_type,
                attr_qualified_name=qualified_name,
                min_ext_info=True,
                ignore_relationships=False,
            )

            entity = dict(result) if result else None

            # Cache the result
            if entity:
                self._entity_cache[cache_key] = entity
                logger.debug(f"Retrieved Purview entity: {qualified_name}")

            return entity

        except ResourceNotFoundError:
            logger.debug(f"Purview entity not found: {qualified_name}")
            return None
        except Exception as e:
            logger.error(f"Error retrieving Purview entity {qualified_name}: {e}")
            raise PurviewLookupError(f"Transient error retrieving Purview entity {qualified_name}: {e}") from e

    def get_entity_by_guid(self, guid: str) -> Optional[Dict[str, Any]]:
        """
        Get an entity by its GUID.

        Args:
            guid: Entity GUID

        Returns:
            Entity details or None if not found
        """
        # Check cache first
        cache_key = f"guid:{guid}"
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]

        try:
            result = self.catalog_client.entity.get_by_guid(guid=guid, min_ext_info=True, ignore_relationships=False)
            entity = dict(result) if result else None

            if entity:
                self._entity_cache[cache_key] = entity
                logger.debug(f"Retrieved entity by GUID: {guid}")

            return entity
        except ResourceNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error retrieving entity by GUID {guid}: {e}")
            return None

    def find_semantic_parent(self, entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Walk up the Purview entity hierarchy to find the nearest parent with a description.

        Uses a path-based approach: strips back the qualified name (URL) layer by layer
        and checks if each parent path exists as a Purview entity with a description.
        Stops at the container level (first path segment).

        Args:
            entity: Starting entity (blob entity)

        Returns:
            Parent entity with description, or None if not found
        """
        entity_data = entity.get("entity", {})
        attributes = entity_data.get("attributes", {})
        qualified_name = attributes.get("qualifiedName")

        if not qualified_name:
            logger.debug("Entity has no qualifiedName, cannot find parent")
            return None

        logger.debug(f"Looking for semantic parent by walking up path from: {qualified_name}")

        # Parse the URL to get the path components
        # Format: https://account.blob.core.windows.net/container/folder/subfolder/file.ext
        from urllib.parse import urlparse

        parsed = urlparse(qualified_name)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.strip("/")

        if not path:
            logger.debug("No path components to walk up")
            return None

        # Split path into components
        path_parts = path.split("/")

        # Walk up the path, removing one component at a time.
        # When we reach the container level (i == 1), also try azure_blob_container
        # since Purview stores container entities under that type.
        for i in range(len(path_parts) - 1, 0, -1):
            parent_path = "/".join(path_parts[:i])
            parent_qualified_name = f"{base_url}/{parent_path}/"

            logger.debug(f"Checking parent path: {parent_qualified_name}")

            # Determine which entity types to check
            entity_types = ["azure_blob_path"]
            if i == 1:
                # Container level – try container type as well
                entity_types.append("azure_blob_container")

            parent_entity = None
            for etype in entity_types:
                # azure_blob_container uses qualified name without trailing slash
                qn = parent_qualified_name.rstrip("/") if etype == "azure_blob_container" else parent_qualified_name
                parent_entity = self.get_purview_entity(qn, entity_type=etype)
                if parent_entity:
                    break

            if parent_entity:
                parent_data = parent_entity.get("entity", {})
                parent_attrs = parent_data.get("attributes", {})

                logger.info(
                    f"Found parent entity: name={parent_attrs.get('name')}, typeName={parent_data.get('typeName')}, has_userDescription={bool(parent_attrs.get('userDescription'))}, has_description={bool(parent_attrs.get('description'))}"
                )

                # Check if this parent has a description
                if parent_attrs.get("userDescription") or parent_attrs.get("description"):
                    logger.info(f"Found semantic parent with description: {parent_attrs.get('name')}")
                    return parent_entity
            else:
                logger.debug(f"Parent entity not found: {parent_qualified_name}")

        logger.debug("No semantic parent with description found")
        return None

    def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding vector for text using Azure OpenAI.

        Args:
            text: Text to generate embedding for

        Returns:
            List of floats representing the embedding vector

        Raises:
            ValueError: If text is empty or None
            Exception: If embedding generation fails
        """
        if not text:
            raise ValueError("Cannot generate embedding for empty text")

        try:
            response = self.openai_client.embeddings.create(input=text, model=self.azure_openai_embedding_deployment)
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            raise

    def strip_html(self, text: Optional[str]) -> Optional[str]:
        """Strip HTML tags from text.

        Args:
            text: Text that may contain HTML tags

        Returns:
            Plain text with HTML tags removed, or None if input is None
        """
        if not text:
            return None

        # Remove HTML tags
        clean = re.sub(r"<[^>]+>", "", text)
        # Clean up whitespace
        clean = " ".join(clean.split())
        return clean if clean else None

    def get_collection_domain(self, entity: Dict[str, Any]) -> Optional[str]:
        """
        Extract the domain from the entity's collection path (lowest collection).

        Args:
            entity: Purview entity

        Returns:
            Collection name as domain, or None
        """
        entity_data = entity.get("entity", {})
        attributes = entity_data.get("attributes", {})

        # Try to get collection from various places
        collection_ref = attributes.get("collection") or entity_data.get("collectionId")

        # If collection_ref is a dict with referenceName, extract it
        if isinstance(collection_ref, dict) and "referenceName" in collection_ref:
            return collection_ref["referenceName"]
        elif isinstance(collection_ref, str):
            return collection_ref

        return None

    def enrich_artifact(self, blob_detail: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enrich a blob-details document with Purview entity metadata.

        Strategy:
        - Get the direct blob entity by qualified name (from metadata_storage_path)
        - Use blob entity's name and description for artifact name/summary
        - Walk up hierarchy to find nearest parent with description
        - Use that parent for semantic_dataset fields
        - Extract domain from collection

        Args:
            blob_detail: Document from blob-details index

        Returns:
            Enriched document for artifact registry.

        Raises:
            StaleArtifactError: If the Purview entity no longer exists.
            ValueError: If required fields are missing or descriptions are absent.
            ValidationError: If the enriched document fails Pydantic validation.
        """
        # The qualified_name in Purview is the full blob URL (metadata_storage_path)
        qualified_name = blob_detail.get("metadata_storage_path")
        artifact_id = blob_detail.get("artifact_id")

        if not qualified_name or not artifact_id:
            raise ValueError(
                f"Blob detail missing required fields: metadata_storage_path={qualified_name}, artifact_id={artifact_id}"
            )

        # Get direct Purview entity for this blob
        blob_entity = self.get_purview_entity(qualified_name)

        if not blob_entity:
            raise StaleArtifactError(f"Purview entity not found for artifact {artifact_id} ({qualified_name})")

        # Extract blob entity attributes
        blob_entity_data = blob_entity.get("entity", {})
        blob_attributes = blob_entity_data.get("attributes", {})

        # Get name from the blob entity (use metadata_storage_name as fallback)
        artifact_name = blob_attributes.get("name") or blob_detail.get("metadata_storage_name")

        # Get summary from Purview entity userDescription (user-edited description)
        # userDescription is stored as HTML, so we need to strip tags
        user_description = blob_attributes.get("userDescription")
        artifact_description = self.strip_html(user_description) if user_description else None

        if not artifact_description:
            raise ValueError(f"Artifact {artifact_id} (URL: {qualified_name}) missing user description")

        # Generate embedding for description
        artifact_description_vector = self.generate_embedding(artifact_description)

        # Artifact type is "blob" for all blob storage artifacts
        artifact_type = "blob"

        # Find semantic parent (nearest ancestor with description)
        semantic_parent = self.find_semantic_parent(blob_entity)

        # Extract semantic dataset fields from parent (or leave empty if no parent found)
        if semantic_parent:
            parent_data = semantic_parent.get("entity", {})
            parent_attrs = parent_data.get("attributes", {})

            logger.info(f"Found semantic parent: {parent_attrs.get('name')} (guid={parent_data.get('guid')})")

            semantic_dataset_id = parent_data.get("guid")
            semantic_dataset_name = parent_attrs.get("name")

            # Get description from parent
            parent_user_description = parent_attrs.get("userDescription")
            semantic_dataset_description = self.strip_html(parent_user_description)

        else:
            # No semantic parent found - leave fields empty
            semantic_dataset_id = None
            semantic_dataset_name = None
            semantic_dataset_description = None
            logger.debug("No semantic parent found, leaving semantic_dataset fields empty")

        # Get domain from collection
        domain = self.get_collection_domain(blob_entity)

        # Generate embedding for semantic dataset description
        if not semantic_dataset_description:
            raise ValueError(f"The parent of artifact {artifact_id} (URL: {qualified_name}) missing user description")

        semantic_dataset_description_vector = self.generate_embedding(semantic_dataset_description)

        # Build artifact registry document
        artifact_data = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "name": artifact_name,
            "description": artifact_description,
            "description_vector": artifact_description_vector,
            "semantic_dataset_id": semantic_dataset_id,
            "semantic_dataset_name": semantic_dataset_name,
            "semantic_dataset_description": semantic_dataset_description,
            "semantic_dataset_description_vector": semantic_dataset_description_vector,
            "domain": domain,
            "rbacScope": blob_detail.get("rbacScope"),
            "detail_index": self.blob_details_index,
            "detail_key": artifact_id,
            "source": blob_attributes.get("accountName") or qualified_name.split(".")[0].replace("https://", ""),
            "created_at": blob_detail.get("metadata_storage_last_modified") or blob_attributes.get("createTime"),
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        }

        # Validate with Pydantic model
        validated_doc = ArtifactRegistryDocument(**artifact_data)
        # Convert to dict for Azure Search (with serialized datetimes)
        artifact_doc = validated_doc.model_dump(mode="json", by_alias=True, exclude_none=True)
        return artifact_doc

    def sync_artifacts(
        self,
        filter_expression: Optional[str] = None,
        batch_size: int = 100,
        dry_run: bool = False,
        cleanup: bool = False,
        verify_blobs: bool = False,
        max_cleanup: int = 50,
        cleanup_threshold: float = 0.2,
    ) -> Dict[str, int]:
        """
        Sync artifacts from blob-details to artifact registry.

        Args:
            filter_expression: OData filter to apply to blob-details query
            batch_size: Number of documents to process in each batch
            dry_run: If True, don't upload to artifact registry
            cleanup: If True, delete stale artifact-registry entries
                when the Purview entity is not found
            verify_blobs: If True, perform a HEAD request against the blob URL to
                verify the blob exists before enrichment. Requires an Azure bearer
                token and adds one HTTP request per artifact.
            max_cleanup: Maximum number of stale entries to delete per run
                (default: 50). Prevents runaway mass-deletion.
            cleanup_threshold: Maximum ratio of cleaned-to-processed before
                aborting (default: 0.2, i.e. 20%). Acts as a circuit breaker
                to protect against Purview outages causing false positives.

        Returns:
            Dictionary with statistics (processed, enriched, uploaded, skipped,
            cleaned, failed)
        """
        stats: Dict[str, int] = {
            "processed": 0,
            "enriched": 0,
            "uploaded": 0,
            "skipped": 0,
            "cleaned": 0,
            "failed": 0,
        }

        logger.info(f"Starting sync from {self.blob_details_index} to {self.artifact_registry_index}")
        if filter_expression:
            logger.info(f"Using filter: {filter_expression}")

        try:
            # Query blob-details index
            search_kwargs = {
                "search_text": "*",
                "top": 1000,  # Maximum allowed by Azure Search
            }

            if filter_expression:
                search_kwargs["filter"] = filter_expression

            results = self.blob_details_client.search(**search_kwargs)

            cleanup_halted = False
            batch = []
            for result in results:
                stats["processed"] += 1

                artifact_id = result.get("artifact_id") or result.get("metadata_storage_path", "unknown")
                blob_url = result.get("metadata_storage_path")

                # Circuit breaker: abort if too many artifacts are being cleaned
                if (
                    cleanup
                    and not cleanup_halted
                    and stats["processed"] >= 10
                    and stats["cleaned"] / stats["processed"] > cleanup_threshold
                ):
                    logger.error(
                        f"Circuit breaker tripped: {stats['cleaned']}/{stats['processed']} "
                        f"({stats['cleaned'] / stats['processed']:.0%}) artifacts marked stale, "
                        f"exceeding {cleanup_threshold:.0%} threshold. "
                        f"Halting further deletions — check Purview health."
                    )
                    cleanup_halted = True

                # Optional blob existence check (adds one HTTP request per artifact)
                if verify_blobs and blob_url:
                    if not self._blob_exists(blob_url):
                        logger.info(f"Blob not found (HEAD 404) for artifact {artifact_id}: {blob_url}")
                        if cleanup and not dry_run and not cleanup_halted:
                            if stats["cleaned"] >= max_cleanup:
                                logger.warning(f"Max cleanup cap ({max_cleanup}) reached — skipping further deletions")
                                stats["skipped"] += 1
                            elif self._delete_stale_entries(artifact_id):
                                stats["cleaned"] += 1
                            else:
                                stats["failed"] += 1
                        else:
                            stats["skipped"] += 1
                        continue

                # Enrich with semantic dataset metadata
                try:
                    enriched = self.enrich_artifact(result)
                except StaleArtifactError as e:
                    logger.info(f"Skipping stale artifact {artifact_id}: {e}")
                    if cleanup and not dry_run and not cleanup_halted:
                        if stats["cleaned"] >= max_cleanup:
                            logger.warning(f"Max cleanup cap ({max_cleanup}) reached — skipping further deletions")
                            stats["skipped"] += 1
                        elif self._delete_stale_entries(artifact_id):
                            stats["cleaned"] += 1
                        else:
                            stats["failed"] += 1
                    else:
                        stats["skipped"] += 1
                    continue
                except Exception as e:
                    logger.error(f"Failed to enrich {artifact_id}: {e}")
                    stats["failed"] += 1
                    continue

                stats["enriched"] += 1
                batch.append(enriched)

                # Upload in batches
                if len(batch) >= batch_size:
                    if not dry_run:
                        uploaded, failed = self._upload_batch(batch)
                        stats["uploaded"] += uploaded
                        stats["failed"] += failed
                    else:
                        logger.info(f"[DRY RUN] Would upload batch of {len(batch)} documents")
                        stats["uploaded"] += len(batch)

                    batch = []

                # Log progress
                if stats["processed"] % 100 == 0:
                    logger.info(
                        f"Progress: {stats['processed']} processed, {stats['enriched']} enriched, {stats['uploaded']} uploaded"
                    )

            # Upload remaining documents
            if batch:
                if not dry_run:
                    uploaded, failed = self._upload_batch(batch)
                    stats["uploaded"] += uploaded
                    stats["failed"] += failed
                else:
                    logger.info(f"[DRY RUN] Would upload final batch of {len(batch)} documents")
                    stats["uploaded"] += len(batch)

            logger.info(f"Sync complete: {stats}")
            return stats

        except Exception as e:
            logger.error(f"Error during sync: {e}", exc_info=True)
            raise

    def _upload_batch(self, documents: List[Dict[str, Any]]) -> tuple[int, int]:
        """
        Upload a batch of documents to artifact registry.

        Args:
            documents: List of documents to upload

        Returns:
            Tuple of (successful uploads, failed uploads)
        """
        try:
            result = self.artifact_registry_client.upload_documents(documents=documents)

            uploaded = sum(1 for r in result if r.succeeded)
            failed = sum(1 for r in result if not r.succeeded)

            if failed > 0:
                logger.warning(f"Batch upload: {uploaded} succeeded, {failed} failed")
                for r in result:
                    if not r.succeeded:
                        logger.error(f"Failed to upload {r.key}: {r.error_message}")
            else:
                logger.debug(f"Uploaded batch of {uploaded} documents")

            return uploaded, failed

        except Exception as e:
            logger.error(f"Error uploading batch: {e}")
            return 0, len(documents)

    def _delete_stale_entries(self, artifact_id: str) -> bool:
        """
        Delete stale entry for an artifact from the artifact-registry index.

        Only artifact-registry is cleaned; blob-details is the source-of-truth
        populated by the indexer and must not be modified by the sync process.

        Args:
            artifact_id: The document key to delete

        Returns:
            True if the delete succeeded, False otherwise.
        """
        try:
            results = self.artifact_registry_client.delete_documents(documents=[{"artifact_id": artifact_id}])
            if results and results[0].succeeded:
                logger.info(f"Deleted stale entry {artifact_id} from {self.artifact_registry_index}")
                return True
            else:
                error_msg = results[0].error_message if results else "no result returned"
                logger.warning(f"Delete failed for {artifact_id} in {self.artifact_registry_index}: {error_msg}")
                return False
        except Exception as e:
            logger.warning(f"Could not delete {artifact_id} from {self.artifact_registry_index}: {e}")
            return False

    def _get_http_client(self) -> httpx.Client:
        """Return a shared httpx.Client, creating it on first use."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.Client(timeout=10.0, follow_redirects=True)
        return self._http_client

    def _blob_exists(self, blob_url: str) -> bool:
        """
        Check whether a blob URL is accessible via an authenticated HEAD request.

        Args:
            blob_url: Full blob storage URL to check

        Returns:
            True if the blob returns a 200-range response or the check is inconclusive,
            False only if the blob returns 404 (definitively gone)
        """
        try:
            # The token provider uses a credential chain (CLI → Managed Identity) with internal caching
            token = get_token_provider("https://storage.azure.com/.default")()
            client = self._get_http_client()
            response = client.head(
                blob_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            # For other HTTP errors (auth issues, server errors), fail open:
            # don't delete blobs we can't definitively confirm are missing.
            logger.warning(f"Unexpected HTTP status checking blob {blob_url}: {e}")
            return True
        except Exception as e:
            # Network errors etc. — fail open to avoid false-positive deletions
            logger.warning(f"Error checking blob existence for {blob_url}: {e}")
            return True

    def sync_single_artifact(self, artifact_id: str, dry_run: bool = False) -> bool:
        """
        Sync a single artifact by ID.

        Args:
            artifact_id: ID of the artifact to sync
            dry_run: If True, don't upload to artifact registry

        Returns:
            True if successful, False otherwise
        """
        try:
            # Get the artifact from blob-details
            result = self.blob_details_client.get_document(key=artifact_id)

            # Enrich with semantic dataset metadata
            enriched = self.enrich_artifact(result)

            if not enriched:
                logger.error(f"Failed to enrich artifact {artifact_id}")
                return False

            if dry_run:
                logger.info(f"[DRY RUN] Would upload artifact: {enriched}")
                return True

            # Upload to artifact registry
            upload_result = self.artifact_registry_client.upload_documents(documents=[enriched])

            if upload_result[0].succeeded:
                logger.info(f"Successfully synced artifact {artifact_id}")
                return True
            else:
                logger.error(f"Failed to upload artifact {artifact_id}: {upload_result[0].error_message}")
                return False

        except Exception as e:
            logger.error(f"Error syncing artifact {artifact_id}: {e}")
            return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync artifacts from blob-details index and Purview catalog to artifact registry"
    )

    # Required arguments
    parser.add_argument("--search-service", required=True, help="Azure AI Search service name")
    parser.add_argument("--purview-account", required=True, help="Purview account name")

    # Optional arguments
    parser.add_argument(
        "--blob-details-index",
        default="blob-details",
        help="Name of the blob-details index (default: blob-details)",
    )
    parser.add_argument(
        "--artifact-registry-index",
        default="artifact-registry",
        help="Name of the artifact registry index (default: artifact-registry)",
    )
    parser.add_argument("--filter", help="OData filter expression to apply to blob-details query")
    parser.add_argument("--artifact-id", help="Sync only a single artifact by ID")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of documents to process in each batch (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't actually upload to artifact registry, just log what would be done",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete stale artifact-registry entries when the Purview entity is not found",
    )
    parser.add_argument(
        "--max-cleanup",
        type=int,
        default=50,
        help="Maximum number of stale entries to delete per run (default: 50)",
    )
    parser.add_argument(
        "--cleanup-threshold",
        type=float,
        default=0.2,
        help="Maximum cleaned/processed ratio before aborting (default: 0.2, i.e. 20%%)",
    )
    parser.add_argument(
        "--verify-blobs",
        action="store_true",
        help="Perform a HEAD request against each blob URL to verify it exists before enrichment (adds latency)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--azure-openai-endpoint",
        default=os.getenv("DATA_LAKE_VECTORIZER_ENDPOINT"),
        required=not os.getenv("DATA_LAKE_VECTORIZER_ENDPOINT"),
        help="Base Azure OpenAI resource endpoint URL (e.g., https://your-resource.cognitiveservices.azure.com). Can be set via DATA_LAKE_VECTORIZER_ENDPOINT environment variable.",
    )
    parser.add_argument(
        "--azure-openai-embedding-deployment",
        default=os.getenv("DATA_LAKE_VECTORIZER_DEPLOYMENT", "text-embedding-3-large"),
        help="Azure OpenAI embedding model deployment name (default: text-embedding-3-large). Can be set via DATA_LAKE_VECTORIZER_DEPLOYMENT environment variable.",
    )

    args = parser.parse_args()

    # Validate CLI arguments
    if args.max_cleanup < 1:
        parser.error("--max-cleanup must be at least 1")
    if not 0 < args.cleanup_threshold <= 1:
        parser.error("--cleanup-threshold must be in the range (0, 1]")

    # Configure logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    try:
        # Initialize sync service
        sync = ArtifactRegistrySync(
            search_service=args.search_service,
            purview_account=args.purview_account,
            azure_openai_endpoint=args.azure_openai_endpoint,
            azure_openai_embedding_deployment=args.azure_openai_embedding_deployment,
            blob_details_index=args.blob_details_index,
            artifact_registry_index=args.artifact_registry_index,
        )

        # Sync single artifact or all artifacts
        if args.artifact_id:
            success = sync.sync_single_artifact(artifact_id=args.artifact_id, dry_run=args.dry_run)
            sys.exit(0 if success else 1)
        else:
            stats = sync.sync_artifacts(
                filter_expression=args.filter,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                cleanup=args.cleanup,
                verify_blobs=args.verify_blobs,
                max_cleanup=args.max_cleanup,
                cleanup_threshold=args.cleanup_threshold,
            )

            # Print summary
            print("\n" + "=" * 60)
            print("SYNC SUMMARY")
            print("=" * 60)
            print(f"Processed:  {stats['processed']}")
            print(f"Enriched:   {stats['enriched']}")
            print(f"Uploaded:   {stats['uploaded']}")
            print(f"Skipped:    {stats['skipped']}")
            if stats.get("cleaned"):
                print(f"Cleaned:    {stats['cleaned']}")
            print(f"Failed:     {stats['failed']}")
            print("=" * 60)

            # Exit with error if there were actual failures
            sys.exit(0 if stats["failed"] == 0 else 1)

    except KeyboardInterrupt:
        logger.info("Sync interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
