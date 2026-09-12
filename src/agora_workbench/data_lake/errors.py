"""Typed errors for the public data-lake contracts."""

from __future__ import annotations

from enum import StrEnum


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
