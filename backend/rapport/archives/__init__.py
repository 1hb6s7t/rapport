"""Long-lived encrypted archives, independent of temporary import retention."""

from rapport.archives.store import ArchiveStore, get_service

__all__ = ["ArchiveStore", "get_service"]
