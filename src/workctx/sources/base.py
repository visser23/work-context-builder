"""Abstract source protocol for Work Context Mirror."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workctx.models import DiscoveredChange, SourceType, SyncCheckpoint
    from workctx.state import StateDB


class Source(ABC):
    """Base class for all source adapters."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def source_type(self) -> SourceType: ...

    @abstractmethod
    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        """Discover objects that have changed since the last checkpoint.

        If full=True, treat all objects as changed (initial sync).
        """
        ...

    @abstractmethod
    def get_current_ids(self) -> set[str]:
        """Return all currently visible source IDs for reconciliation.

        Used to detect deletions.
        """
        ...

    def validate(self) -> list[str]:
        """Validate source configuration.

        Returns a list of issues (empty if valid).
        """
        return []

    def close(self) -> None:  # noqa: B027
        """Release any resources held by this adapter."""

    def output_subdir(self) -> str:
        """Return the output subdirectory for this source's type."""
        return self.source_type.value
