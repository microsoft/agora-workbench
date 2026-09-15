"""Administrative CLI for the public data-lake API."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import (
    ArtifactMetadata,
    CatalogArtifact,
    AuthorizedManagedCatalogWriter,
    CatalogAuthorizer,
    CatalogManifest,
    DataLakeError,
    DevelopmentAllowAllCatalogAuthorizer,
    LocalManagedStorage,
    ManagedCatalogWriter,
    ReconciliationError,
    RegisterArtifactRequest,
    RequestContext,
)
from .catalog import CatalogConfig, CatalogDB, CatalogIndexer, SQLiteCatalogProvider
from .identity import canonicalize_azure_uri, sanitize_uri_for_display, stable_source_id
from .models import PageRequest, SearchRequest

DEFAULT_CONFIG = Path("catalog.yaml")
DEFAULT_DATABASE = Path("catalog.db")


def _effective_source_ids(config: CatalogConfig) -> tuple[str, ...]:
    source_ids = []
    for source in config.sources:
        root = canonicalize_azure_uri(source.path) if source.source_type == "blob" else str(Path(source.path).resolve())
        source_ids.append(source.source_id or stable_source_id(source.source_type, root))
    return tuple(source_ids)


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


async def _close_optional_resource(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "aclose", None)
    if not callable(close):
        close = getattr(resource, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        _ = await result


def _paths_alias(first: Path, second: Path) -> bool:
    if first == second:
        return True
    try:
        return first.exists() and second.exists() and first.samefile(second)
    except OSError:
        return False


def _stage_text(destination: Path, content: str, token: str) -> Path:
    stage = destination.parent / f".{destination.name}.{token}.stage"
    with stage.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return stage


def _install_files(outputs: tuple[tuple[Path, str], ...], *, force: bool) -> None:
    token = uuid.uuid4().hex
    stages: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    committed = False
    try:
        for destination, _ in outputs:
            if destination.exists() and not destination.is_file():
                raise FileExistsError(f"{destination} exists and is not a regular file.")
            if destination.exists() and not force:
                raise FileExistsError(f"{destination} already exists; pass --force to replace it.")
            destination.parent.mkdir(parents=True, exist_ok=True)
        for destination, content in outputs:
            stages[destination] = _stage_text(destination, content, token)
        for destination, _ in outputs:
            if destination.exists():
                backup = destination.parent / f".{destination.name}.{token}.backup"
                os.replace(destination, backup)
                backups[destination] = backup
        for destination, _ in outputs:
            os.replace(stages[destination], destination)
            installed.add(destination)
        committed = True
    except BaseException:
        for destination in reversed(tuple(installed)):
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        for destination, backup in reversed(tuple(backups.items())):
            try:
                os.replace(backup, destination)
            except OSError:
                pass
        raise
    finally:
        for stage in stages.values():
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        if committed:
            for backup in backups.values():
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass


def _artifact_json(artifact: CatalogArtifact) -> dict[str, object]:
    reference = artifact.reference
    presentation = artifact.presentation
    locator = artifact.locator
    return {
        "artifact_id": reference.artifact_id,
        "source_id": reference.source_id,
        "revision": reference.revision,
        "name": presentation.name,
        "description": presentation.description,
        "media_type": presentation.media_type,
        "size_bytes": presentation.size_bytes,
        "metadata": dict(artifact.metadata),
        "score": artifact.score,
        "storage_uri": sanitize_uri_for_display(locator.uri) if locator is not None else None,
    }


def _safe_refresh_error(error: str | None) -> str | None:
    if error is None:
        return None
    safe_categories = (
        "credential_or_access_failure:",
        "manifest_invalid:",
        "manifest_missing:",
        "optional_dependency_missing:",
        "source_unavailable:",
    )
    if error.startswith(safe_categories):
        return error
    if error.startswith("FileNotFoundError:"):
        return "source_missing: configured source was not found"
    if error.startswith("PermissionError:"):
        return "credential_or_access_failure: source refresh failed"
    return "source_unavailable: source refresh failed"


def _load_authorizer(spec: str | None, allow_development_writes: bool) -> CatalogAuthorizer:
    if spec and allow_development_writes:
        raise ValueError("Choose either --authorization-factory or --allow-development-writes, not both.")
    if allow_development_writes:
        return DevelopmentAllowAllCatalogAuthorizer()
    if not spec:
        raise ValueError(
            "Managed registration requires application authorization. Pass --authorization-factory module:factory "
            "or, for local development only, --allow-development-writes."
        )
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("Authorization factory must use module.path:factory syntax.")
    module = importlib.import_module(module_name)
    if not hasattr(module, attribute):
        raise ValueError(f"Authorization factory {spec!r} was not found.")
    factory = getattr(module, attribute)
    if not callable(factory):
        raise ValueError(f"Authorization factory {spec!r} is not callable.")
    authorizer = factory()
    if not isinstance(authorizer, CatalogAuthorizer):
        raise ValueError("Authorization factory did not return a CatalogAuthorizer.")
    return authorizer


async def _run_validate(args: argparse.Namespace) -> None:
    config = CatalogConfig.from_yaml(args.config)
    db = CatalogDB(":memory:", vec_dimensions=config.search.embedding_dimensions)
    db.open()
    indexer = CatalogIndexer(config, db)
    embedding_provider = None
    try:
        embedding_provider = indexer.embedding_provider
        report = await indexer.dry_run()
    finally:
        await _close_optional_resource(embedding_provider)
        db.close()
    _print_json(
        {"configuration_valid": report.configuration_valid, "sources": [asdict(item) for item in report.sources]}
    )
    if report.has_errors:
        raise ValueError("Catalog validation failed; fix the reported source or manifest errors before refresh.")


async def _run_refresh(args: argparse.Namespace) -> None:
    config = CatalogConfig.from_yaml(args.config)
    db = CatalogDB(args.database, vec_dimensions=config.search.embedding_dimensions)
    db.open()
    indexer = CatalogIndexer(config, db)
    embedding_provider = None
    changed: int | None = None
    refresh_error: Exception | None = None
    source_states = []
    states = []
    try:
        embedding_provider = indexer.embedding_provider
        try:
            changed = await indexer.index()
        except Exception as exc:
            refresh_error = exc
        source_states = db.list_source_refresh_states()
        states = [{**asdict(state), "error": _safe_refresh_error(state.error)} for state in source_states]
    finally:
        await _close_optional_resource(embedding_provider)
        db.close()
    failed_states = [state for state in source_states if state.status == "error" or state.error is not None]
    output: dict[str, object] = {"database": str(args.database), "changed": changed, "sources": states}
    if failed_states:
        if any((state.error or "").startswith("optional_dependency_missing:") for state in failed_states):
            output["hint"] = (
                "Azure catalog sources require optional dependencies. Install with: uv add 'agora-workbench[azure]'."
            )
        elif any((state.error or "").startswith(("FileNotFoundError:", "PermissionError:")) for state in failed_states):
            output["hint"] = "Check that each configured local source exists and is readable by this process."
        _print_json(output)
        raise ValueError("Catalog refresh failed for one or more sources; inspect the structured source states.")
    if refresh_error is not None:
        raise refresh_error
    _print_json(output)


async def _run_search(args: argparse.Namespace) -> None:
    config = CatalogConfig.from_yaml(args.config)
    if not args.database.is_file():
        raise FileNotFoundError(f"Catalog database not found: {args.database}. Run refresh first.")
    db = CatalogDB(args.database, vec_dimensions=config.search.embedding_dimensions)
    db.open()
    embedding_provider = None
    try:
        embedding_provider = CatalogIndexer(config, db).embedding_provider

        async def embed_query(query: str) -> list[float] | None:
            if embedding_provider is None:
                return None
            embedded = await embedding_provider.embed([query])
            return embedded[0] if embedded else None

        provider = SQLiteCatalogProvider(
            db,
            _effective_source_ids(config),
            query_embedder=embed_query if embedding_provider is not None else None,
            hybrid_alpha=config.search.hybrid_alpha,
        )
        page = await provider.search(
            SearchRequest(
                args.query,
                source_ids=tuple(args.source_id),
                page=PageRequest(limit=args.limit),
            ),
            RequestContext(caller_id=args.caller_id),
        )
    finally:
        await _close_optional_resource(embedding_provider)
        db.close()
    _print_json({"items": [_artifact_json(item) for item in page.items], "next_cursor": page.next_cursor})


async def _run_register(args: argparse.Namespace) -> None:
    authorizer = _load_authorizer(args.authorization_factory, args.allow_development_writes)
    if not args.root.is_dir():
        raise FileNotFoundError(f"Managed storage root not found: {args.root}")
    writer = AuthorizedManagedCatalogWriter(
        ManagedCatalogWriter(args.source_id, LocalManagedStorage(args.root)),
        authorizer,
    )
    result = await writer.register(
        RegisterArtifactRequest(
            operation_id=args.operation_id,
            path=args.path,
            storage_path=args.storage_path,
            artifact_id=args.artifact_id,
            metadata=ArtifactMetadata(
                name=args.name,
                description=args.description,
                domain=args.domain,
                media_type=args.media_type,
                aliases=tuple(args.alias),
            ),
            content_revision=args.content_revision,
            checksum_sha256=args.checksum_sha256,
            size_bytes=args.size_bytes,
            expected_generation=args.expected_generation,
            expected_revision_id=args.expected_revision_id,
        ),
        RequestContext(caller_id=args.caller_id),
    )
    _print_json(asdict(result))


async def _run_reconcile(args: argparse.Namespace) -> None:
    if not args.root.is_dir():
        raise FileNotFoundError(f"Managed storage root not found: {args.root}")
    writer = ManagedCatalogWriter(args.source_id, LocalManagedStorage(args.root))
    try:
        report = await writer.reconcile(grace_seconds=args.grace_seconds)
    except ReconciliationError as exc:
        _print_json(
            {
                "recovered": [],
                "removed_orphans": [],
                "deferred": [],
                "removed_staging": [],
                "deferred_staging": [],
                "failures": dict(exc.failures),
            }
        )
        raise ValueError("Reconciliation completed with failures; inspect the reported operation IDs.") from None
    value = asdict(report)
    value["failures"] = dict(report.failures)
    _print_json(value)
    if report.failures:
        raise ValueError("Reconciliation completed with failures; inspect the reported operation IDs.")


def _run_init(args: argparse.Namespace) -> None:
    source: dict[str, Any] = {
        "path": args.source,
        "discovery": args.discovery,
    }
    if args.source_id:
        source["source_id"] = args.source_id
    if args.discovery == "manifest":
        if not args.source_id:
            raise ValueError("--source-id is required for manifest discovery.")
        if not args.manifest:
            raise ValueError("--manifest is required for manifest discovery.")
        source["manifest"] = args.manifest
    config_path = args.config.resolve()
    manifest_path = None
    if args.create_empty_manifest:
        if args.discovery != "manifest":
            raise ValueError("--create-empty-manifest requires --discovery manifest.")
        if "://" in args.source:
            raise ValueError("--create-empty-manifest is local-only and never provisions cloud resources.")
        source_root = Path(args.source).resolve()
        configured_manifest = Path(args.manifest)
        manifest_path = (
            configured_manifest.resolve()
            if configured_manifest.is_absolute()
            else (source_root / configured_manifest).resolve()
        )
        if not manifest_path.is_relative_to(source_root):
            raise ValueError("The empty manifest path must stay within the resolved local source root.")
        if _paths_alias(config_path, manifest_path):
            raise ValueError("Catalog config and manifest paths must refer to different files.")
    config_value = {"version": 1, "sources": [source]}
    CatalogConfig.model_validate(config_value)
    config_content = yaml.safe_dump(config_value, sort_keys=False)
    outputs: list[tuple[Path, str]] = []
    if manifest_path is not None:
        manifest = CatalogManifest(version=1, generation=1, artifacts=())
        outputs.append((manifest_path, json.dumps(manifest.to_mapping(), indent=2) + "\n"))
    outputs.append((config_path, config_content))
    _install_files(tuple(outputs), force=args.force)
    created = [str(config_path)]
    if manifest_path is not None:
        created.append(str(manifest_path))
    _print_json({"created": created, "scan_policy": args.discovery == "scan"})


def _add_catalog_files(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Catalog YAML (default: catalog.yaml).")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="SQLite catalog (default: catalog.db).")


def _add_managed_backend(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True, help="Local managed-storage root.")
    parser.add_argument("--source-id", required=True, help="Stable managed catalog source ID.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agora-workbench-data-lake",
        description="Validate, refresh, search, and administer Agora Workbench data-lake catalogs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an explicit version-1 catalog configuration.")
    init_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    init_parser.add_argument("--source", default="data", help="Local root or Azure Blob prefix.")
    init_parser.add_argument("--source-id", help="Stable source ID (required for manifest discovery).")
    init_parser.add_argument("--discovery", choices=("scan", "manifest"), default="scan")
    init_parser.add_argument("--manifest", help="Authoritative manifest path.")
    init_parser.add_argument(
        "--create-empty-manifest", action="store_true", help="Create a local generation-1 manifest."
    )
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=_run_init)

    validate_parser = subparsers.add_parser("validate", help="Validate config and enumerate sources without mutation.")
    validate_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    validate_parser.set_defaults(handler=_run_validate)

    refresh_parser = subparsers.add_parser("refresh", help="Refresh the SQLite catalog from configured sources.")
    _add_catalog_files(refresh_parser)
    refresh_parser.set_defaults(handler=_run_refresh)

    search_parser = subparsers.add_parser("search", help="Search a refreshed SQLite catalog.")
    _add_catalog_files(search_parser)
    search_parser.add_argument("query")
    search_parser.add_argument("--source-id", action="append", default=[], help="Restrict to a source; repeatable.")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--caller-id", default="data-lake-cli")
    search_parser.set_defaults(handler=_run_search)

    register_parser = subparsers.add_parser("register", help="Register existing bytes in a local managed catalog.")
    _add_managed_backend(register_parser)
    register_parser.add_argument("--operation-id", required=True)
    register_parser.add_argument("--path", required=True, help="Logical catalog path.")
    register_parser.add_argument("--storage-path", required=True, help="Existing path relative to the managed root.")
    register_parser.add_argument("--checksum-sha256", required=True)
    register_parser.add_argument("--size-bytes", type=int)
    register_parser.add_argument("--artifact-id")
    register_parser.add_argument("--content-revision")
    register_parser.add_argument("--expected-generation", type=int)
    register_parser.add_argument("--expected-revision-id")
    register_parser.add_argument("--name")
    register_parser.add_argument("--description")
    register_parser.add_argument("--domain")
    register_parser.add_argument("--media-type")
    register_parser.add_argument("--alias", action="append", default=[])
    register_parser.add_argument("--caller-id", default="data-lake-cli")
    register_parser.add_argument("--authorization-factory", help="Application authorizer as module.path:factory.")
    register_parser.add_argument(
        "--allow-development-writes", action="store_true", help="Development-only allow-all policy."
    )
    register_parser.set_defaults(handler=_run_register)

    reconcile_parser = subparsers.add_parser("reconcile", help="Recover abandoned local managed-write operations.")
    _add_managed_backend(reconcile_parser)
    reconcile_parser.add_argument("--grace-seconds", type=float, default=300.0)
    reconcile_parser.set_defaults(handler=_run_reconcile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except (
        DataLakeError,
        FileNotFoundError,
        FileExistsError,
        ImportError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValidationError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
