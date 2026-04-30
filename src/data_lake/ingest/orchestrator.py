#!/usr/bin/env python3
"""
Manifest-driven ingestion orchestrator.

Reads an ``IngestionManifest`` (YAML) and executes the full ingestion
pipeline (steps 3-6):

  Step 3 – Register source in Purview, create & configure scan
  Step 4 – Trigger Purview scan  +  create / run blob-details indexer
  Step 5 – Wait for scan & indexer, push semantic descriptions to Purview
  Step 6 – Run Sync to update the Artifact Registry

Usage:
    python -m data_lake.ingest.orchestrator ingest manifest.yaml [--dry-run] [--verbose]
    python -m data_lake.ingest.orchestrator validate manifest.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from jinja2 import Template

from ..manifest.manifest import DataConfigSingle, IngestionManifest
from ..utilities.utilities import move_entities

# Load .env so manifest validators can resolve env-var fallbacks
load_dotenv(verbose=True, override=False)

logger = logging.getLogger(__name__)

# Path to existing deploy scripts' templates
_BLOB_DETAILS_DIR = Path(__file__).resolve().parent.parent / "search" / "blob_details"

# ---------------------------------------------------------------------------
# Polling defaults
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS = 30
_MAX_WAIT_SECONDS = 1800  # 30 min


# ═══════════════════════════════════════════════════════════════════════════
# Step helpers
# ═══════════════════════════════════════════════════════════════════════════


def _get_token() -> str:
    """Acquire a bearer token for Azure AI Search."""
    cred = AzureCliCredential()
    return cred.get_token("https://search.azure.com/.default").token


def _load_template(template_path: Path, substitutions: Dict[str, str]) -> Dict[str, Any]:
    """Render a Jinja2 JSON template."""
    with open(template_path) as f:
        rendered = Template(f.read()).render(**substitutions)
    return json.loads(rendered)


def _deploy_search_resource(
    endpoint: str,
    resource_type: str,
    resource_name: str,
    payload: Dict[str, Any],
    token: str,
    api_version: str = "2025-11-01-preview",
) -> None:
    """PUT a resource into Azure AI Search."""
    url = f"{endpoint}/{resource_type}/{resource_name}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    params = {"api-version": api_version}

    logger.info(f"Deploying {resource_type}: {resource_name}")
    resp = httpx.put(url, json=payload, headers=headers, params=params, timeout=30.0)
    if resp.status_code not in (200, 201, 204):
        logger.error(f"Deploy failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
    logger.info(f"  ✓ {resource_type}/{resource_name}")


def _run_indexer(endpoint: str, indexer_name: str, token: str) -> None:
    """POST to trigger an indexer run."""
    url = f"{endpoint}/indexers/{indexer_name}/run"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"api-version": "2025-11-01-preview"}

    logger.info(f"Running indexer: {indexer_name}")
    resp = httpx.post(url, headers=headers, params=params, timeout=30.0)
    if resp.status_code not in (202, 204):
        logger.warning(f"Indexer run response: {resp.status_code} – {resp.text}")
    else:
        logger.info("  ✓ Indexer run initiated")


def _get_indexer_status(endpoint: str, indexer_name: str, token: str) -> Dict[str, Any]:
    """GET indexer status."""
    url = f"{endpoint}/indexers/{indexer_name}/status"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"api-version": "2025-11-01-preview"}
    resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class IngestionOrchestrator:
    """Chains steps 3-6 from a validated ``IngestionManifest``."""

    def __init__(self, manifest: IngestionManifest, *, dry_run: bool = False):
        self.m = manifest
        self.dry_run = dry_run
        self.credential = AzureCliCredential()
        self._search_endpoint = f"https://{self.m.search.search_service}.search.windows.net"
        # Per-dataset state – set by _apply_dataset before each iteration
        self._current_subfolder: Optional[str] = None
        self._current_data: Optional[DataConfigSingle] = None
        self._current_collection: Optional[str] = None

    def _apply_dataset(
        self,
        subfolder: Optional[str],
        data: DataConfigSingle,
        collection: str,
    ) -> None:
        """Set the per-dataset state for the current iteration."""
        self._current_subfolder = subfolder
        self._current_data = data
        self._current_collection = collection

    # -- Subfolder-aware helpers ----------------------------------------

    @property
    def _scan_name(self) -> str:
        """Scan name including subfolder when present.

        Purview scan names must contain only alphanumerics, hyphens,
        and underscores.  We sanitise the final joined name to avoid
        ``InvalidField`` errors from the Purview scanning API.
        """
        import re

        def _sanitise(s: str) -> str:
            return re.sub(r"[^A-Za-z0-9_-]", "_", s)

        parts = [self.m.source.storage_account, self.m.source.container]
        if self._current_subfolder:
            parts.append(self._current_subfolder.replace("/", "_"))
        raw = "_".join(parts) + "_scan"
        return _sanitise(raw)

    @property
    def _base_blob_url(self) -> str:
        """Base blob URL including subfolder when present."""
        src = self.m.source
        url = f"https://{src.storage_account}.blob.core.windows.net/{src.container}"
        if self._current_subfolder:
            url += f"/{self._current_subfolder}"
        return url

    @property
    def _scan_filter_scopes(self) -> List[str]:
        """URI prefix scopes for the Purview scan filter.

        Always returns the subfolder (or container) scope.  Purview's
        ``includeUriPrefixes`` only accepts folder-level prefixes, so we
        cannot scope to individual files here.  Per-artifact filtering
        is handled downstream in sync (step 6).
        """
        src = self.m.source
        base = src.container
        if self._current_subfolder:
            base += f"/{self._current_subfolder}"
        return [base]

    @property
    def _source_id(self) -> str:
        """Source ID for the current dataset (used for AI Search resources).

        Azure AI Search resource names only allow lowercase letters, digits,
        and dashes, so we normalise all other characters to dashes and
        collapse consecutive dashes.
        """
        import re

        parts = [self.m.source.storage_account, self.m.source.container]
        if self._current_subfolder:
            parts.append(self._current_subfolder.replace("/", "-"))
        raw = "-".join(parts).lower()
        # Replace any character that isn't a lowercase letter, digit, or dash
        sanitised = re.sub(r"[^a-z0-9-]", "-", raw)
        # Collapse consecutive dashes and strip leading/trailing dashes
        return re.sub(r"-{2,}", "-", sanitised).strip("-")

    # ------------------------------------------------------------------
    # Step 3 – Purview: register storage + create scan
    # ------------------------------------------------------------------

    def step3_register_and_create_scan(self) -> None:
        """Register the blob storage in Purview and create a container scan."""
        logger.info("═══ Step 3: Register source in Purview & create scan ═══")

        from data_lake.semantic import PurviewDataSourceManager

        mgr = PurviewDataSourceManager(self.m.governance.purview_account)

        src = self.m.source
        collection = self._current_collection

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would register storage: {src.storage_account} "
                f"(rg={src.resource_group}, sub={src.subscription_id}, collection={collection})"
            )
            logger.info(f"[DRY RUN] Would create scan: {self._scan_name}")
            return

        # Register the storage account as a data source
        max_retries = 6
        retry_delay = 30  # seconds

        for attempt in range(1, max_retries + 1):
            try:
                mgr.register_blob_storage(
                    storage_account=src.storage_account,
                    resource_group=src.resource_group,
                    subscription_id=src.subscription_id,
                    collection_name=collection,
                )

                # Create a scan scoped to the target container (+ subfolder if set)
                scan_name = self._scan_name
                mgr.create_storage_scan(
                    data_source_name=src.storage_account,
                    scan_name=scan_name,
                    container_names=self._scan_filter_scopes,
                    collection_name=collection,
                )
                break  # success
            except Exception as e:
                if "Collection_MovingInProgress" in str(e) and attempt < max_retries:
                    logger.info(
                        f"  Collection move in progress – retrying in {retry_delay}s (attempt {attempt}/{max_retries})"
                    )
                    time.sleep(retry_delay)
                else:
                    raise

        logger.info("✓ Step 3 complete")

    # ------------------------------------------------------------------
    # Step 4a – Purview: trigger scan
    # ------------------------------------------------------------------

    def step4a_trigger_purview_scan(self) -> None:
        """Trigger the Purview scan created in step 3."""
        logger.info("═══ Step 4a: Trigger Purview scan ═══")

        from data_lake.semantic import PurviewDataSourceManager

        mgr = PurviewDataSourceManager(self.m.governance.purview_account)
        scan_name = self._scan_name

        if self.dry_run:
            logger.info(f"[DRY RUN] Would trigger scan: {scan_name}")
            return

        try:
            mgr.trigger_scan(
                data_source_name=self.m.source.storage_account,
                scan_name=scan_name,
            )
            logger.info("✓ Step 4a complete – scan triggered")
        except Exception as e:
            if "ActiveRunExist" in str(e) or "already a scan currently running" in str(e):
                logger.info("✓ Step 4a complete – scan already running, will wait for it")
            else:
                raise

    # ------------------------------------------------------------------
    # Step 4b – AI Search: create datasource + indexer, run indexer
    # ------------------------------------------------------------------

    def step4b_create_and_run_indexer(self) -> tuple[str, Optional[str]]:
        """Deploy AI Search datasource + indexer and trigger a run.

        Returns ``(indexer_name, prev_start_time)`` so that the poller can
        distinguish a stale ``lastResult`` from the freshly triggered run.
        """
        logger.info("═══ Step 4b: Create blob-details data source & indexer ═══")

        src = self.m.source
        source_id = self._source_id
        assert src.managed_identity_id is not None

        storage_resource_id = (
            f"/subscriptions/{src.subscription_id}"
            f"/resourceGroups/{src.resource_group}"
            f"/providers/Microsoft.Storage/storageAccounts/{src.storage_account}"
        )

        token = "" if self.dry_run else _get_token()

        # --- Datasource ---
        ds_payload = _load_template(
            _BLOB_DETAILS_DIR / "datasource.jinja",
            {
                "SOURCE_ID": source_id,
                "STORAGE_RESOURCE_ID": storage_resource_id,
                "USER_ASSIGNED_IDENTITY_RESOURCE_ID": src.managed_identity_id,
                "CONTAINER_NAME": src.container,
                "CONTAINER_QUERY": src.container_query or self._current_subfolder or "",
            },
        )
        ds_name = f"blob-ds-{source_id}"

        if self.dry_run:
            logger.info(f"[DRY RUN] Would deploy datasource: {ds_name}")
        else:
            _deploy_search_resource(self._search_endpoint, "datasources", ds_name, ds_payload, token)

        # --- Indexer ---
        index_path = _BLOB_DETAILS_DIR / "index.json"
        with open(index_path) as f:
            target_index_name = json.load(f)["name"]

        def _norm_ext(exts: Optional[List[str]]) -> str:
            if not exts:
                return ""
            return ",".join(e if e.startswith(".") else f".{e}" for e in exts)

        indexer_payload = _load_template(
            _BLOB_DETAILS_DIR / "indexer.jinja",
            {
                "SOURCE_ID": source_id,
                "TARGET_INDEX_NAME": target_index_name,
                "INCLUDED_EXTENSIONS": _norm_ext(src.included_extensions),
                "EXCLUDED_EXTENSIONS": _norm_ext(src.excluded_extensions),
            },
        )
        indexer_name = f"blob-details-indexer-{source_id}"

        prev_start: Optional[str] = None
        if self.dry_run:
            logger.info(f"[DRY RUN] Would deploy indexer: {indexer_name}")
        else:
            _deploy_search_resource(self._search_endpoint, "indexers", indexer_name, indexer_payload, token)

            # Snapshot previous run's start time so the poller can skip stale results
            status = _get_indexer_status(self._search_endpoint, indexer_name, token)
            prev_result = status.get("lastResult") or {}
            prev_start = prev_result.get("startTime")

            _run_indexer(self._search_endpoint, indexer_name, token)

        logger.info("✓ Step 4b complete")
        return indexer_name, prev_start

    # ------------------------------------------------------------------
    # Polling – wait for indexer to finish
    # ------------------------------------------------------------------

    def wait_for_indexer(
        self,
        indexer_name: str,
        prev_start_time: Optional[str] = None,
        poll_interval: int = _POLL_INTERVAL_SECONDS,
        max_wait: int = _MAX_WAIT_SECONDS,
    ) -> None:
        """Block until the indexer reaches a terminal state.

        *prev_start_time* is the ``lastResult.startTime`` captured **before**
        the new run was triggered.  If the poller sees a ``lastResult`` whose
        ``startTime`` still matches *prev_start_time*, it treats the run as
        not-yet-started and keeps waiting.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would wait for indexer to complete")
            return

        logger.info(f"Waiting for indexer '{indexer_name}' (poll every {poll_interval}s, max {max_wait}s)…")
        token = _get_token()
        elapsed = 0

        while elapsed < max_wait:
            status = _get_indexer_status(self._search_endpoint, indexer_name, token)
            last_result = status.get("lastResult") or {}
            run_status = last_result.get("status", "unknown")
            cur_start = last_result.get("startTime")

            # If lastResult still reflects the previous run, the new run
            # hasn't finished (or started) yet – keep polling.
            if prev_start_time and cur_start == prev_start_time:
                logger.info(f"  Indexer: new run not reflected yet (elapsed {elapsed}s)")
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue

            logger.info(f"  Indexer status: {run_status} (elapsed {elapsed}s)")

            if run_status in (
                "success",
                "transientFailure",
                "persistentFailure",
                "reset",
            ):
                if run_status != "success":
                    logger.warning(f"Indexer finished with status: {run_status}")
                    error_msg = last_result.get("errorMessage", "")
                    if error_msg:
                        logger.warning(f"  Error: {error_msg}")
                else:
                    item_count = last_result.get("itemCount", "?")
                    logger.info(f"  ✓ Indexer succeeded – {item_count} items indexed")
                return

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(f"Indexer did not finish within {max_wait}s – continuing anyway")

    # ------------------------------------------------------------------
    # Polling – wait for Purview scan to finish
    # ------------------------------------------------------------------

    def wait_for_purview_scan(
        self,
        scan_name: str,
        poll_interval: int = _POLL_INTERVAL_SECONDS,
        max_wait: int = _MAX_WAIT_SECONDS,
    ) -> bool:
        """Block until the Purview scan reaches a terminal state.

        Returns True if the scan succeeded, False otherwise.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would wait for Purview scan to complete")
            return True

        logger.info(f"Waiting for Purview scan '{scan_name}' (poll every {poll_interval}s, max {max_wait}s)…")

        from data_lake.semantic import PurviewDataSourceManager

        mgr = PurviewDataSourceManager(self.m.governance.purview_account)
        data_source_name = self.m.source.storage_account
        elapsed = 0

        while elapsed < max_wait:
            try:
                runs = mgr.scanning_client.scan_result.list_scan_history(
                    data_source_name=data_source_name, scan_name=scan_name
                )
                # The SDK returns an iterable; grab the most recent run
                latest = None
                for run in runs:
                    latest = run
                    break  # first entry is the most recent

                if latest:
                    status = latest.get("status", "unknown")
                    logger.info(f"  Purview scan status: {status} (elapsed {elapsed}s)")

                    if status.lower() in (
                        "succeeded",
                        "completed",
                        "failed",
                        "canceled",
                        "cancelled",
                    ):
                        if status.lower() in ("failed", "canceled", "cancelled"):
                            logger.warning(f"Purview scan finished with status: {status}")
                            error = latest.get("error", "")
                            if error:
                                logger.warning(f"  Error: {error}")
                            return False
                        else:
                            logger.info("  ✓ Purview scan succeeded")
                            return True
                else:
                    logger.info(f"  No scan runs found yet (elapsed {elapsed}s)")

            except Exception as e:
                logger.warning(f"  Error checking scan status: {e}")

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(f"Purview scan did not finish within {max_wait}s – continuing anyway")
        return False

    # ------------------------------------------------------------------
    # Step 5 – Push semantic descriptions to Purview
    # ------------------------------------------------------------------

    def step5_push_descriptions(self) -> None:
        """Push the semantic descriptions from the manifest into Purview entities."""
        logger.info("═══ Step 5: Push semantic descriptions to Purview ═══")

        from azure.purview.catalog import PurviewCatalogClient

        purview_endpoint = f"https://{self.m.governance.purview_account}.purview.azure.com"
        catalog_client = PurviewCatalogClient(endpoint=purview_endpoint, credential=self.credential)

        base_url = self._base_blob_url

        # Combine dataset-level description (applied to the scoped folder) with per-artifact descriptions
        all_descriptions: List[Dict[str, str]] = []

        # The dataset description maps to the subfolder (or container) path
        data_config = self._current_data
        assert data_config is not None, "_apply_dataset must be called before step5"

        all_descriptions.append(
            {
                "qualified_name": f"{base_url}/",
                "description": data_config.description,
            }
        )

        for art in data_config.artifacts:
            # Build the full qualified name
            path = art.path.lstrip("/")
            qualified_name = f"{base_url}/{path}"
            all_descriptions.append({"qualified_name": qualified_name, "description": art.description})

        succeeded = 0
        failed = 0

        max_retries = 6
        retry_delay = 30  # seconds between retries for not-found entities

        for entry in all_descriptions:
            qn = entry["qualified_name"]
            desc = entry["description"]

            if self.dry_run:
                logger.info(f"[DRY RUN] Would set description on {qn}: {desc[:80]}…")
                succeeded += 1
                continue

            # Determine entity types to try.  Directory-level paths
            # (trailing slash) may appear in Purview as either
            # azure_blob_container or azure_blob_path depending on the
            # scan.  Try both for any directory; for files only try
            # azure_blob_path.
            if qn.endswith("/"):
                type_names = ["azure_blob_container", "azure_blob_path"]
            else:
                type_names = ["azure_blob_path"]

            for attempt in range(1, max_retries + 1):
                # Step A: look up entity by unique attributes to get its GUID
                entity_result = None
                matched_type = None
                for type_name in type_names:
                    effective_qn = qn.rstrip("/") if type_name == "azure_blob_container" else qn
                    try:
                        entity_result = catalog_client.entity.get_by_unique_attributes(
                            type_name=type_name,
                            attr_qualified_name=effective_qn,
                        )
                        matched_type = type_name
                        break
                    except ResourceNotFoundError:
                        continue
                    except HttpResponseError:
                        continue

                if not entity_result:
                    if attempt < max_retries:
                        logger.info(
                            f"  Entity not found: {qn} – retrying in {retry_delay}s (attempt {attempt}/{max_retries})"
                        )
                        time.sleep(retry_delay)
                        continue
                    else:
                        logger.warning(
                            f"  ✗ Entity not found after {max_retries} attempts: {qn} – "
                            "the Purview scan may not have cataloged this path yet."
                        )
                        failed += 1
                        break

                # Step B: update entity using its GUID
                entity_data = entity_result.get("entity", {})
                guid = entity_data.get("guid")
                entity_name = entity_data.get("attributes", {}).get("name")
                if not guid:
                    logger.warning(f"  ✗ Entity found but missing GUID: {qn}")
                    failed += 1
                    break

                try:
                    effective_qn = qn.rstrip("/") if matched_type == "azure_blob_container" else qn
                    body = {
                        "entity": {
                            "guid": guid,
                            "typeName": matched_type,
                            "attributes": {
                                "qualifiedName": effective_qn,
                                "name": entity_name,
                                "userDescription": desc,
                            },
                        }
                    }
                    catalog_client.entity.create_or_update(entity=body)
                    logger.info(f"  ✓ {qn} (as {matched_type}, guid={guid})")
                    succeeded += 1
                    break
                except (ResourceNotFoundError, HttpResponseError) as e:
                    logger.error(f"  ✗ Failed to update {qn} (guid={guid}): {e}")
                    failed += 1
                    break

        logger.info(f"✓ Step 5 complete – {succeeded} updated, {failed} failed")

    # ------------------------------------------------------------------
    # Step 6 – Sync: blob-details + Purview → artifact registry
    # ------------------------------------------------------------------

    def step6_sync_registry(self) -> None:
        """Run the sync script to populate / update the artifact registry."""
        logger.info("═══ Step 6: Sync artifact registry ═══")

        from data_lake.sync import ArtifactRegistrySync

        emb = self.m.embedding
        search = self.m.search

        if self.dry_run:
            logger.info("[DRY RUN] Would run artifact registry sync")
            return

        assert emb.azure_openai_endpoint is not None, (
            "Azure OpenAI endpoint is required for sync. Set it in the manifest or via DATA_LAKE_VECTORIZER_ENDPOINT."
        )
        assert search.search_service is not None, "search_service is required for sync."
        assert emb.azure_openai_deployment is not None, "azure_openai_deployment is required for sync."
        assert search.blob_details_index is not None, "blob_details_index is required for sync."
        assert search.artifact_registry_index is not None, "artifact_registry_index is required for sync."

        sync = ArtifactRegistrySync(
            search_service=search.search_service,
            purview_account=self.m.governance.purview_account,
            azure_openai_endpoint=emb.azure_openai_endpoint,
            azure_openai_embedding_deployment=emb.azure_openai_deployment,
            blob_details_index=search.blob_details_index,
            artifact_registry_index=search.artifact_registry_index,
        )

        # Filter sync to only the declared artifacts, or fall back to the whole subfolder.
        base = self._base_blob_url
        data = self._current_data
        if data and data.artifacts:
            # Build an OR filter matching each artifact's exact path.
            # Single quotes in OData string literals must be escaped by doubling them.
            escaped_base = base.replace("'", "''")
            clauses = []
            for art in data.artifacts:
                escaped_path = art.path.lstrip("/").replace("'", "''")
                clauses.append(f"metadata_storage_path eq '{escaped_base}/{escaped_path}'")
            filter_expr = " or ".join(clauses)
        else:
            filter_expr = f"metadata_storage_path ge '{base}/' and metadata_storage_path lt '{base}0'"

        stats = sync.sync_artifacts(filter_expression=filter_expr, dry_run=self.dry_run)

        logger.info(
            f"✓ Step 6 complete – "
            f"processed={stats['processed']}, enriched={stats['enriched']}, "
            f"uploaded={stats['uploaded']}, failed={stats['failed']}"
        )

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the full ingestion pipeline (steps 3-6) for all datasets."""
        logger.info("=" * 60)
        logger.info("INGESTION PIPELINE START")
        logger.info(f"  Storage:    {self.m.source.storage_account}/{self.m.source.container}")
        logger.info(f"  Datasets:   {len(self.m.datasets)}")
        logger.info(f"  Dry run:    {self.dry_run}")
        logger.info("=" * 60)

        for subfolder, data, collection in self.m.iter_subfolders():
            self._apply_dataset(subfolder, data, collection)

            logger.info("-" * 60)
            logger.info(f"  Dataset:    {subfolder or '(root)'}")
            logger.info(f"  Collection: {collection}")
            logger.info("-" * 60)

            # Step 3
            self.step3_register_and_create_scan()

            # Step 4a + 4b (trigger scan, then deploy + run indexer)
            self.step4a_trigger_purview_scan()
            indexer_name, prev_start = self.step4b_create_and_run_indexer()

            # Wait for indexer to finish
            self.wait_for_indexer(indexer_name, prev_start_time=prev_start)

            # Wait for Purview scan to complete before pushing descriptions
            scan_ok = self.wait_for_purview_scan(self._scan_name)

            if scan_ok:
                # Step 5 – push semantic descriptions
                self.step5_push_descriptions()

                # Step 6 – sync to artifact registry
                self.step6_sync_registry()
            else:
                logger.warning(
                    "Skipping Steps 5-6: Purview scan did not succeed – "
                    "entities are not available for description push or registry sync."
                )

        logger.info("")
        logger.info("=" * 60)
        logger.info("INGESTION PIPELINE COMPLETE")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Move entities to a different collection
    # ------------------------------------------------------------------

    def move_to_collection(
        self,
        path_prefixes: List[str],
        target_collection: str,
    ) -> None:
        """Move Purview entities matching *path_prefixes* into *target_collection*.

        Each prefix is relative to the container root, e.g. ``"im3_datacenter_atlas"``
        or ``"texas_grid"``.  The method discovers every entity whose
        ``qualifiedName`` starts with
        ``https://<account>.blob.core.windows.net/<container>/<prefix>``
        and moves them all in one API call.
        """
        move_entities(
            purview_account=self.m.governance.purview_account,
            storage_account=self.m.source.storage_account,
            container=self.m.source.container,
            path_prefixes=path_prefixes,
            target_collection=target_collection,
            dry_run=self.dry_run,
        )


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manifest-driven data-lake ingestion orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Validate a manifest without running anything\n"
            "  python -m data_lake.ingest.orchestrator validate manifest.yaml\n\n"
            "  # Dry-run the full pipeline\n"
            "  python -m data_lake.ingest.orchestrator ingest manifest.yaml --dry-run\n\n"
            "  # Execute the full pipeline\n"
            "  python -m data_lake.ingest.orchestrator ingest manifest.yaml --verbose\n\n"
            "  # Move entities under specific subfolders to another collection\n"
            "  python -m data_lake.ingest.orchestrator move \\\n"
            "      --purview agora-purview --storage grid0eastus2 --container demo \\\n"
            "      --prefixes im3_datacenter_atlas texas_grid --to powergrid\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- validate ---
    val_p = sub.add_parser("validate", help="Validate a manifest YAML file")
    val_p.add_argument("manifest", type=Path, help="Path to manifest YAML file")

    # --- ingest ---
    ing_p = sub.add_parser("ingest", help="Run the full ingestion pipeline")
    ing_p.add_argument("manifest", type=Path, help="Path to manifest YAML file")
    ing_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without making changes",
    )
    ing_p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    ing_p.add_argument(
        "--step",
        type=int,
        choices=[3, 4, 5, 6],
        help="Run only a specific step (3=register, 4=scan+index, 5=descriptions, 6=sync)",
    )

    # --- move ---
    mov_p = sub.add_parser(
        "move",
        help="Move Purview entities from subfolders to a different collection",
    )
    mov_p.add_argument("--purview", required=True, help="Purview account name (e.g. agora-purview)")
    mov_p.add_argument("--storage", required=True, help="Storage account name (e.g. grid0eastus2)")
    mov_p.add_argument("--container", required=True, help="Blob container name (e.g. demo)")
    mov_p.add_argument(
        "--prefixes",
        nargs="+",
        required=True,
        help="Subfolder prefixes to match (e.g. im3_datacenter_atlas texas_grid)",
    )
    mov_p.add_argument(
        "--to",
        required=True,
        dest="target_collection",
        help="Target Purview collection to move entities into",
    )
    mov_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only log what would be moved",
    )
    mov_p.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # --- move (no manifest required) ---
    if args.command == "move":
        move_entities(
            purview_account=args.purview,
            storage_account=args.storage,
            container=args.container,
            path_prefixes=args.prefixes,
            target_collection=args.target_collection,
            dry_run=args.dry_run,
        )
        sys.exit(0)

    # Load & validate manifest
    try:
        manifest = IngestionManifest.from_yaml(args.manifest)
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    if args.command == "validate":
        print("✓ Manifest is valid")
        print(json.dumps(manifest.model_dump(mode="json", exclude_none=True), indent=2))
        sys.exit(0)

    # --- ingest ---
    orch = IngestionOrchestrator(manifest, dry_run=args.dry_run)

    if args.step:
        # Initialise per-dataset state so individual steps can run standalone
        for subfolder, data, collection in manifest.iter_subfolders():
            orch._apply_dataset(subfolder, data, collection)
            logger.info("-" * 60)
            logger.info(f"  Dataset:    {subfolder or '(root)'}")
            logger.info(f"  Collection: {collection}")
            logger.info("-" * 60)

            step_map = {
                3: orch.step3_register_and_create_scan,
                4: lambda: (
                    orch.step4a_trigger_purview_scan(),
                    orch.step4b_create_and_run_indexer(),
                ),
                5: orch.step5_push_descriptions,
                6: orch.step6_sync_registry,
            }
            step_map[args.step]()
    else:
        orch.run()


if __name__ == "__main__":
    main()
