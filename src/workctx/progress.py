"""Rich progress display for sync operations.

Provides real-time progress bars with ETA, file counts, transfer rates,
and per-source status during sync runs.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()


class SyncProgress:
    """Tracks progress across multiple sources during a sync run."""

    def __init__(self, *, quiet: bool = False) -> None:
        self._quiet = quiet
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
            disable=quiet,
        )
        self._live: Live | None = None
        self._tasks: dict[str, TaskID] = {}
        self._stats: dict[str, dict] = {}
        self._start_time = time.monotonic()
        self._skipped: dict[str, int] = {}

    @contextmanager
    def live(self) -> Generator[SyncProgress, None, None]:
        """Context manager for the live display."""
        if self._quiet:
            yield self
            return

        with self._progress:
            yield self

    def begin_discovery(self, source_name: str) -> None:
        if self._quiet:
            return
        task_id = self._progress.add_task(
            f"[cyan]Discovering {source_name}...", total=None
        )
        self._tasks[f"discover:{source_name}"] = task_id

    def end_discovery(self, source_name: str, total: int, skipped: int = 0) -> None:
        disc_key = f"discover:{source_name}"
        if disc_key in self._tasks:
            self._progress.remove_task(self._tasks.pop(disc_key))
        self._skipped[source_name] = skipped
        self._stats[source_name] = {
            "total": total, "done": 0, "added": 0,
            "updated": 0, "deleted": 0, "failed": 0,
        }
        if self._quiet:
            return
        label = f"[bold]{source_name}"
        if skipped:
            label += f" [dim]({skipped} skipped)"
        task_id = self._progress.add_task(label, total=total)
        self._tasks[source_name] = task_id

    def advance(self, source_name: str, *, added: int = 0, updated: int = 0,
                deleted: int = 0, failed: int = 0) -> None:
        if source_name in self._stats:
            s = self._stats[source_name]
            s["done"] += 1
            s["added"] += added
            s["updated"] += updated
            s["deleted"] += deleted
            s["failed"] += failed
        if source_name in self._tasks and not self._quiet:
            self._progress.advance(self._tasks[source_name])

    def finish_source(self, source_name: str, status: str) -> None:
        if source_name in self._tasks and not self._quiet:
            task = self._tasks[source_name]
            self._progress.update(
                task, description=f"[bold]{source_name} [{status}]"
            )

    def print_summary(self) -> None:
        elapsed = time.monotonic() - self._start_time
        console.print()

        table = Table(title="Sync Summary", show_header=True)
        table.add_column("Source", style="bold")
        table.add_column("Status", justify="center")
        table.add_column("Total", justify="right")
        table.add_column("Added", justify="right", style="green")
        table.add_column("Updated", justify="right", style="yellow")
        table.add_column("Deleted", justify="right", style="red")
        table.add_column("Failed", justify="right", style="red")
        table.add_column("Skipped", justify="right", style="dim")

        for name, s in self._stats.items():
            status = "healthy" if s["failed"] == 0 else "degraded"
            color = "green" if status == "healthy" else "yellow"
            table.add_row(
                name,
                f"[{color}]{status}[/{color}]",
                str(s["total"]),
                str(s["added"]),
                str(s["updated"]),
                str(s["deleted"]),
                str(s["failed"]),
                str(self._skipped.get(name, 0)),
            )

        console.print(table)

        mins = int(elapsed) // 60
        secs = int(elapsed) % 60
        console.print(f"\nCompleted in {mins}m {secs}s")
