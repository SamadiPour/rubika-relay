from __future__ import annotations

import sys
import time


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(num_bytes, 0))
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _format_bar(current: int, total: int, width: int) -> str:
    if total <= 0:
        return "-" * width
    filled = int(width * max(0, min(current, total)) / total)
    return ("#" * filled).ljust(width, "-")


class TransferProgress:
    """Single-line progress indicator for sequential transfers."""

    def __init__(self, label: str, width: int = 28):
        self.label = label
        self.width = width
        self.stream = sys.stdout
        self._last_render = 0.0
        self._last_percent_logged = -10
        self._is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self._finished = False

    def callback(self, total: int, current: int) -> None:
        if self._finished or total <= 0:
            return

        current = min(current, total)
        percent = int((current / total) * 100)

        if self._is_tty:
            now = time.monotonic()
            if percent < 100 and (now - self._last_render) < 0.08:
                return
            self._last_render = now

            bar = _format_bar(current, total, self.width)
            msg = (
                f"\r{self.label} [{bar}] {percent:3d}% "
                f"({_format_bytes(current)}/{_format_bytes(total)})"
            )
            self.stream.write(msg)
            self.stream.flush()
            return

        if percent >= self._last_percent_logged + 10 or percent == 100:
            self._last_percent_logged = percent
            self.stream.write(
                f"{self.label} {percent}% ({_format_bytes(current)}/{_format_bytes(total)})\n"
            )
            self.stream.flush()

    def finish(self) -> None:
        if self._finished:
            return
        if self._is_tty:
            self.stream.write("\n")
            self.stream.flush()
        self._finished = True


class MultiProgress:
    """Multi-line progress: one overall bar plus one line per worker slot.

    In a TTY, the block is redrawn in place using ANSI cursor-up/erase-line.
    Use `log(msg)` to print a line above the block (e.g. retry warnings).
    In a non-TTY, falls back to periodic overall log lines (every ~10%).
    """

    def __init__(
        self,
        overall_label: str,
        overall_total: int,
        slot_count: int,
        bar_width: int = 28,
    ) -> None:
        self.overall_label = overall_label
        self.overall_total = max(0, int(overall_total))
        self.overall_current = 0
        self.bar_width = bar_width
        self.stream = sys.stdout
        self._is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self._slots: list[dict | None] = [None] * max(1, slot_count)
        self._rendered = False
        self._last_render = 0.0
        self._last_percent_logged = -10
        self._closed = False
        self._start_time = time.monotonic()

    # ---------- Slot lifecycle ----------

    def acquire_slot(self, label: str, total: int) -> int:
        for i, slot in enumerate(self._slots):
            if slot is None:
                self._slots[i] = {"label": label, "total": max(0, int(total)), "current": 0}
                self._render(force=True)
                return i
        # All slots busy: reuse slot 0 (shouldn't happen when slot_count matches parallelism).
        self._slots[0] = {"label": label, "total": max(0, int(total)), "current": 0}
        self._render(force=True)
        return 0

    def update_slot(self, slot_idx: int, current: int) -> None:
        if not (0 <= slot_idx < len(self._slots)):
            return
        slot = self._slots[slot_idx]
        if slot is None:
            return
        current = max(0, int(current))
        if slot["total"] > 0:
            current = min(current, slot["total"])

        # Handle retry resets (callback reports a smaller value after a retry).
        if current < slot["current"]:
            self.overall_current = max(0, self.overall_current - (slot["current"] - current))
            slot["current"] = current
        else:
            delta = current - slot["current"]
            slot["current"] = current
            self.overall_current += delta
        if self.overall_total > 0:
            self.overall_current = min(self.overall_current, self.overall_total)
        self._render()

    def finish_slot(self, slot_idx: int) -> None:
        if not (0 <= slot_idx < len(self._slots)):
            return
        slot = self._slots[slot_idx]
        if slot is not None and slot["total"] > slot["current"]:
            remaining = slot["total"] - slot["current"]
            slot["current"] = slot["total"]
            if self.overall_total > 0:
                self.overall_current = min(self.overall_total, self.overall_current + remaining)
            else:
                self.overall_current += remaining
        self._slots[slot_idx] = None
        self._render(force=True)

    # ---------- Output helpers ----------

    def log(self, message: str) -> None:
        """Print `message` on its own line above the live progress block."""
        if self._is_tty and self._rendered:
            self._erase()
            self.stream.write(message + "\n")
            self.stream.flush()
            self._draw()
        else:
            self.stream.write(message + "\n")
            self.stream.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._is_tty:
            # Leave the final rendered state on screen with cursor below it.
            self._render(force=True)
        else:
            pct = self._overall_percent()
            self.stream.write(
                f"{self.overall_label} {pct}% "
                f"({_format_bytes(self.overall_current)}/{_format_bytes(self.overall_total)})\n"
            )
            self.stream.flush()

    # ---------- Internals ----------

    def _overall_percent(self) -> int:
        if self.overall_total <= 0:
            return 0
        return int((self.overall_current / self.overall_total) * 100)

    def _format_overall_line(self) -> str:
        pct = self._overall_percent()
        bar = _format_bar(self.overall_current, self.overall_total, self.bar_width)
        elapsed = time.monotonic() - self._start_time
        rate = self.overall_current / elapsed if elapsed > 0 else 0.0
        rate_str = f"{_format_bytes(int(rate))}/s" if rate > 0 else "--"
        return (
            f"{self.overall_label} [{bar}] {pct:3d}% "
            f"({_format_bytes(self.overall_current)}/{_format_bytes(self.overall_total)}) "
            f"{rate_str}"
        )

    def _format_slot_line(self, slot: dict | None) -> str:
        if slot is None:
            return "  (idle)"
        pct = 0
        if slot["total"] > 0:
            pct = int((slot["current"] / slot["total"]) * 100)
        bar = _format_bar(slot["current"], slot["total"], self.bar_width)
        return (
            f"  {slot['label']} [{bar}] {pct:3d}% "
            f"({_format_bytes(slot['current'])}/{_format_bytes(slot['total'])})"
        )

    def _compose_lines(self) -> list[str]:
        lines = [self._format_overall_line()]
        for slot in self._slots:
            lines.append(self._format_slot_line(slot))
        return lines

    def _erase(self) -> None:
        if not (self._is_tty and self._rendered):
            return
        n = len(self._slots) + 1
        for _ in range(n):
            self.stream.write("\x1b[1A\r\x1b[2K")
        self._rendered = False

    def _draw(self) -> None:
        if not self._is_tty:
            return
        for line in self._compose_lines():
            self.stream.write(line + "\n")
        self._rendered = True
        self.stream.flush()

    def _render(self, force: bool = False) -> None:
        if self._is_tty:
            now = time.monotonic()
            if not force and (now - self._last_render) < 0.08:
                return
            self._last_render = now
            self._erase()
            self._draw()
            return

        pct = self._overall_percent()
        if force or pct >= self._last_percent_logged + 10 or pct == 100:
            self._last_percent_logged = pct
            self.stream.write(
                f"{self.overall_label} {pct}% "
                f"({_format_bytes(self.overall_current)}/{_format_bytes(self.overall_total)})\n"
            )
            self.stream.flush()
