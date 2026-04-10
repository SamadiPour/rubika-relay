from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CliError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message
