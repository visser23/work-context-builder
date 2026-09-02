"""Progress display for sync operations.

Uses Rich live progress bars when connected to a terminal.
Falls back to periodic plain-text status lines otherwise.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Generator
from contextlib import contextmanager

from rich.console import Console
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

_is_tty = sys.stderr.isatty()
console = Console(stderr=True)

# How often to print a status line in non-TTY mode (seconds)
_PRINT_INTERVAL = 15


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
            disable=quiet or not _is_tty,
        )
        self._tasks: dict[str, TaskID] = {}
        self._stats: dict[str, dict] = {}
        self._start_time = time.monotonic()
        self._skipped: dict[str, int] = {}
        self._last_print: float = 0
        self._current_source: str = ""

    @contextmanager
    def live(self) -> Generator[SyncProgress, None, None]:
        """Context manager for the live display."""
        if self._quiet or not _is_tty:
            yield self
            return

        with self._progress:
            yield self

    def begin_discovery(self, source_name: str) -> None:
        self._current_source = source_name
        self._print_status(f"Discovering {source_name}...")
        if self._quiet or not _is_tty:
            return
        task_id = self._progress.add_task(f"[cyan]Discovering {source_name}...", total=None)
        self._tasks[f"discover:{source_name}"] = task_id

    def end_discovery(self, source_name: str, total: int, skipped: int = 0) -> None:
        disc_key = f"discover:{source_name}"
        if disc_key in self._tasks:
            self._progress.remove_task(self._tasks.pop(disc_key))
        self._skipped[source_name] = skipped
        self._stats[source_name] = {
            "total": total,
            "done": 0,
            "added": 0,
            "updated": 0,
            "deleted": 0,
            "failed": 0,
        }
        self._current_source = source_name
        skip_msg = f" ({skipped} skipped)" if skipped else ""
        self._print_status(f"{source_name}: {total} items to process{skip_msg}")
        if self._quiet or not _is_tty:
            return
        label = f"[bold]{source_name}"
        if skipped:
            label += f" [dim]({skipped} skipped)"
        task_id = self._progress.add_task(label, total=total)
        self._tasks[source_name] = task_id

    def advance(
        self,
        source_name: str,
        *,
        added: int = 0,
        updated: int = 0,
        deleted: int = 0,
        failed: int = 0,
    ) -> None:
        if source_name in self._stats:
            s = self._stats[source_name]
            s["done"] += 1
            s["added"] += added
            s["updated"] += updated
            s["deleted"] += deleted
            s["failed"] += failed
            self._maybe_print_progress(source_name)
        if source_name in self._tasks and _is_tty and not self._quiet:
            self._progress.advance(self._tasks[source_name])

    def finish_source(self, source_name: str, status: str) -> None:
        if source_name in self._stats:
            s = self._stats[source_name]
            elapsed = time.monotonic() - self._start_time
            self._print_status(
                f"{source_name}: done — {s['added']} added, {s['updated']} updated, "
                f"{s['deleted']} deleted, {s['failed']} failed "
                f"({_format_time(elapsed)})"
            )
        if source_name in self._tasks and _is_tty and not self._quiet:
            task = self._tasks[source_name]
            self._progress.update(task, description=f"[bold]{source_name} [{status}]")

    def print_summary(self) -> None:
        elapsed = time.monotonic() - self._start_time

        if _is_tty and not self._quiet:
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
            console.print(f"\nCompleted in {_format_time(elapsed)}")
        else:
            self._print_status(f"Sync complete in {_format_time(elapsed)}")
            for name, s in self._stats.items():
                status = "healthy" if s["failed"] == 0 else "degraded"
                self._print_status(
                    f"  {name}: [{status}] {s['total']} total, "
                    f"{s['added']} added, {s['updated']} updated, "
                    f"{s['deleted']} deleted, {s['failed']} failed"
                )

    def _maybe_print_progress(self, source_name: str) -> None:
        """In non-TTY mode, print a progress line every _PRINT_INTERVAL seconds."""
        if _is_tty or self._quiet:
            return
        now = time.monotonic()
        if now - self._last_print < _PRINT_INTERVAL:
            return
        self._last_print = now
        s = self._stats[source_name]
        elapsed = now - self._start_time
        pct = (s["done"] / s["total"] * 100) if s["total"] else 0
        remaining = ""
        if s["done"] > 0 and s["total"] > s["done"]:
            rate = s["done"] / max(elapsed, 1)
            eta = (s["total"] - s["done"]) / rate
            remaining = f", ~{_format_time(eta)} remaining"
        self._print_status(
            f"{source_name}: {s['done']}/{s['total']} ({pct:.0f}%) "
            f"— {s['added']}+ {s['updated']}~ {s['failed']}! "
            f"[{_format_time(elapsed)}{remaining}]"
        )

    def _print_status(self, msg: str) -> None:
        """Print a status line to stderr (always visible, even non-TTY)."""
        if self._quiet:
            return
        sys.stderr.write(f"[workctx] {msg}\n")
        sys.stderr.flush()


def _format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"
