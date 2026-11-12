"""Shared, stateless contracts for independently deployed agents."""

from .identity import Identity, get_identity
from .middleware import install_request_context
from .storage import LocalObjectStorage
from .tasks import JobStatus, ThreadTaskDispatcher

__all__ = [
    "Identity",
    "JobStatus",
    "LocalObjectStorage",
    "ThreadTaskDispatcher",
    "get_identity",
    "install_request_context",
]
