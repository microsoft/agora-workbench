"""Custom catalog storage integration for the Energy Systems demo."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from agora_workbench.code_execution import CatalogIntegration
from agora_workbench.data_lake import (
    ArtifactReference,
    DevelopmentAllowAllCatalogAuthorizer,
    RequestContext,
    ResolvedArtifact,
    ResourceLease,
    ResourceOwnership,
    StorageLocator,
    TransferOptions,
    TransferResult,
)
from agora_workbench.data_lake.catalog import CatalogConfig, ManifestCatalogProvider
from agora_workbench.data_lake.execution import AssetFetcher, LocalFileFetcher

_LOCATOR_SCHEME = "energysystems"
_LOCATOR_AUTHORITY = "datasets"


class EnergySystemsCatalogProvider(ManifestCatalogProvider):
    """Manifest catalog that resolves artifacts to demo-specific locators."""

    def __init__(self, config: CatalogConfig, dataset_root: Path):
        self._dataset_root = dataset_root.resolve()
        super().__init__(config)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        resolved = await super().resolve(reference, context)
        source_path = Path(resolved.locator.uri).resolve()
        try:
            relative_path = source_path.relative_to(self._dataset_root)
        except ValueError as exc:
            raise RuntimeError("Energy Systems catalog resolved an artifact outside its dataset root.") from exc
        locator = StorageLocator(
            f"{_LOCATOR_SCHEME}://{_LOCATOR_AUTHORITY}/{quote(relative_path.as_posix(), safe='/')}"
        )
        return ResolvedArtifact(reference=resolved.reference, locator=locator)


class EnergySystemsDatasetFetcher(AssetFetcher):
    """Fetch ``energysystems://`` locators from the packaged dataset directory."""

    def __init__(self, dataset_root: Path):
        super().__init__(credential=None)
        self._dataset_root = dataset_root.resolve()
        self._local_fetcher = LocalFileFetcher(allowed_roots=[str(self._dataset_root)])

    def can_handle(self, qualified_name: str) -> bool:
        parsed = urlsplit(qualified_name)
        return parsed.scheme == _LOCATOR_SCHEME and parsed.netloc == _LOCATOR_AUTHORITY

    def _source_path(self, qualified_name: str) -> Path:
        parsed = urlsplit(qualified_name)
        if parsed.scheme != _LOCATOR_SCHEME or parsed.netloc != _LOCATOR_AUTHORITY or parsed.query or parsed.fragment:
            raise ValueError("Invalid Energy Systems dataset locator.")

        decoded_path = unquote(parsed.path)
        relative_path = PurePosixPath(decoded_path.lstrip("/"))
        if (
            not decoded_path.startswith("/")
            or not relative_path.parts
            or "\\" in decoded_path
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise ValueError("Invalid Energy Systems dataset path.")

        source_path = self._dataset_root.joinpath(*relative_path.parts).resolve()
        try:
            source_path.relative_to(self._dataset_root)
        except ValueError as exc:
            raise ValueError("Energy Systems dataset locator escapes the configured root.") from exc
        return source_path

    async def fetch(self, qualified_name: str) -> bytes:
        return await self._local_fetcher.fetch(str(self._source_path(qualified_name)))

    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
        result = await self.fetch_to_file_result(
            qualified_name,
            dest_path,
            options=options,
            context=context,
        )
        return result.bytes_transferred

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        return await self._local_fetcher.fetch_to_file_result(
            str(self._source_path(qualified_name)),
            dest_path,
            options=options,
            context=context,
        )

    async def close(self) -> None:
        await self._local_fetcher.close()


def create_catalog_integration(energysystems_dir: Path) -> CatalogIntegration:
    """Create the demo's owned catalog and per-session custom fetcher."""
    config = CatalogConfig.from_yaml(energysystems_dir / "catalog.yaml")
    for source in config.sources:
        source_path = Path(source.path)
        if source.source_type == "local" and not source_path.is_absolute():
            source.path = str((energysystems_dir / source_path).resolve())

    dataset_root = (energysystems_dir / "data").resolve()
    provider = EnergySystemsCatalogProvider(config, dataset_root)
    return CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=DevelopmentAllowAllCatalogAuthorizer(),
        fetcher_factory=lambda _context: EnergySystemsDatasetFetcher(dataset_root),
    )
