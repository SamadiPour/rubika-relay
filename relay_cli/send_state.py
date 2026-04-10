from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .config import SEND_STATE_FILE

STATE_SCHEMA_VERSION = 1


def state_dir_for_source(source_file: Path) -> Path:
    return source_file.resolve().parent / f"{source_file.name}.relay-state"


def state_file_path(state_dir: Path) -> Path:
    return state_dir / SEND_STATE_FILE


def source_identity(source_file: Path) -> dict[str, Any]:
    resolved = source_file.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "name": source_file.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def load_state(state_dir: Path) -> dict[str, Any] | None:
    path = state_file_path(state_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_file_path(state_dir)
    pending_path = path.with_suffix(path.suffix + ".pending")
    state["updated_at"] = time.time()
    payload = json.dumps(state, indent=2, sort_keys=True)
    pending_path.write_text(payload, encoding="utf-8")
    pending_path.replace(path)


def clear_state_dir(state_dir: Path) -> None:
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)


def build_new_state(
        source_file: Path,
        zip_name: str,
        password: str,
        part_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    now = time.time()
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "uploading",
        "created_at": now,
        "updated_at": now,
        "source": source_identity(source_file),
        "archive": {
            "name": zip_name,
            "password": password,
        },
        "total_parts": len(part_entries),
        "parts": part_entries,
        "last_error": None,
    }


def state_matches_source(state: dict[str, Any], source_file: Path) -> bool:
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        return False

    src = state.get("source")
    if not isinstance(src, dict):
        return False

    expected = source_identity(source_file)
    for key in ("path", "size", "mtime_ns"):
        if src.get(key) != expected.get(key):
            return False

    parts = state.get("parts")
    if not isinstance(parts, list) or not parts:
        return False

    if state.get("total_parts") != len(parts):
        return False

    archive = state.get("archive")
    if not isinstance(archive, dict) or not archive.get("password"):
        return False

    return True


def first_unsent_part_index(state: dict[str, Any]) -> int:
    parts = state.get("parts") or []
    for idx, part in enumerate(parts, 1):
        if not part.get("message_id"):
            return idx
    return len(parts) + 1


def resumable_parts_exist(state_dir: Path, state: dict[str, Any]) -> bool:
    parts = state.get("parts") or []
    for part in parts:
        if part.get("message_id"):
            continue
        name = part.get("name")
        if not name:
            return False
        if not (state_dir / name).is_file():
            return False
    return True
