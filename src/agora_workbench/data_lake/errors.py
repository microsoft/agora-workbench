"""Typed errors for the public data-lake contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class DataLakeErrorCode(StrEnum):
    """Stable machine-readable categories for data-lake failures."""

    INTERNAL = "internal"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    PERMISSION_DENIED = "permission_denied"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    TRANSFER_CANCELLED = "transfer_cancelled"
    TRANSFER_TIMEOUT = "transfer_timeout"
    TRANSFER_LIMIT = "transfer_limit"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    UNSAFE_PATH = "unsafe_path"
    CONFLICT = "conflict"
    PRECONDITION_FAILED = "precondition_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    RECONCILIATION_FAILED = "reconciliation_failed"


class DataLakeError(Exception):
    """Base class for errors crossing the public data-lake boundary."""

    code = DataLakeErrorCode.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        resource_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.resource_id = resource_id
        self.operation = operation


class InvalidRequestError(DataLakeError):
    """The request is malformed or internally inconsistent."""

    code = DataLakeErrorCode.INVALID_REQUEST


class ArtifactNotFoundError(DataLakeError):
    """The requested catalog artifact does not exist."""

    code = DataLakeErrorCode.NOT_FOUND


class UnsupportedOperationError(DataLakeError):
    """The provider or effective caller capabilities do not support an operation."""

    code = DataLakeErrorCode.UNSUPPORTED_OPERATION


class PermissionDeniedError(DataLakeError):
    """The current caller is not permitted to perform an operation."""

    code = DataLakeErrorCode.PERMISSION_DENIED


class BackendUnavailableError(DataLakeError):
    """The catalog backend cannot currently serve the request."""

    code = DataLakeErrorCode.BACKEND_UNAVAILABLE


class TransferCancelledError(DataLakeError):
    """A transfer was cancelled before it committed its destination."""

    code = DataLakeErrorCode.TRANSFER_CANCELLED


class TransferTimeoutError(DataLakeError):
    """A transfer exceeded its configured end-to-end timeout."""

    code = DataLakeErrorCode.TRANSFER_TIMEOUT


class TransferLimitError(DataLakeError):
    """A transfer exceeded its object-size or caller-quota bound."""

    code = DataLakeErrorCode.TRANSFER_LIMIT


class TransferChecksumError(DataLakeError):
    """Transferred bytes did not match the required checksum."""

    code = DataLakeErrorCode.CHECKSUM_MISMATCH


class UnsafePathError(DataLakeError):
    """A local or provider path escaped its configured namespace."""

    code = DataLakeErrorCode.UNSAFE_PATH


class ConflictError(DataLakeError):
    """A concurrent mutation conflicted with the requested operation."""

    code = DataLakeErrorCode.CONFLICT


class PreconditionFailedError(DataLakeError):
    """An ownership, generation, or revision precondition was not satisfied."""

    code = DataLakeErrorCode.PRECONDITION_FAILED


class RetryExhaustedError(DataLakeError):
    """Bounded optimistic-concurrency retries were exhausted."""

    code = DataLakeErrorCode.RETRY_EXHAUSTED


class ReconciliationError(DataLakeError):
    """Recovery could not safely classify or clean an interrupted operation."""

    code = DataLakeErrorCode.RECONCILIATION_FAILED

    def __init__(
        self,
        message: str,
        *,
        failures: Mapping[str, str] | None = None,
        resource_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message, resource_id=resource_id, operation=operation)
        self.failures = MappingProxyType(dict(failures or {}))
