"""Compatibility exports for execution-integrated data-lake implementations."""

from agora_workbench.code_execution.data_access.credentials import MsalCacheCredential, create_storage_credential
from agora_workbench.code_execution.data_access.fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher
from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
from agora_workbench.code_execution.data_access.publishers import (
    AssetPublisher,
    BlobPublisher,
    GuiPublisher,
    LocalFilePublisher,
    ServerPublisher,
    publish_compat,
)
from .transfer import TransferDiagnostic, TransferOptions, TransferResult

__all__ = [
    "AssetFetcher",
    "AssetPublisher",
    "BlobFetcher",
    "BlobPublisher",
    "DataLakeDataManager",
    "GuiPublisher",
    "LocalFileFetcher",
    "LocalFilePublisher",
    "MsalCacheCredential",
    "ServerPublisher",
    "TransferDiagnostic",
    "TransferOptions",
    "TransferResult",
    "create_storage_credential",
    "publish_compat",
]
