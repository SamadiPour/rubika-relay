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


class TransferProgress:
    """Render a simple progress indicator that works in interactive and non-interactive terminals."""

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

            filled = int(self.width * current / total)
            bar = ("#" * filled).ljust(self.width, "-")
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
