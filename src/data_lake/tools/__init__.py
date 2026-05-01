"""DataLake runtime tools for artifact discovery and access control."""

from .permissions import check_resource_permissions

__all__ = [
    "check_resource_permissions",
]
