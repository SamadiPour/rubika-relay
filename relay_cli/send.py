from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

from rubpy import Client
from rubpy.types import Update

from .config import (
    MAX_RETRIES,
    RELAY_TAG,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_JITTER_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
)
from .errors import CliError
from .file_ops import (
    create_encrypted_zip,
    remove_file_safely,
    sha256_hash,
    split_file,
)
from .progress import TransferProgress
from .send_state import (
    build_new_state,
    clear_state_dir,
    first_unsent_part_index,
    load_state,
    resumable_parts_exist,
    save_state,
    state_dir_for_source,
    state_matches_source,
)


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True

    text = f"{type(exc).__name__}: {exc}".lower()
    non_retryable_markers = (
        "file not found",
        "no such file",
        "permission denied",
        "is a directory",
        "invalid path",
        "invalid file",
        "bad request",
    )
    if any(marker in text for marker in non_retryable_markers):
        return False

    retryable_markers = (
        "timeout",
        "timed out",
        "network",
        "connection",
        "temporar",
        "reset",
        "unavailable",
        "try again",
        "too many requests",
        "429",
        "flood",
    )
    if any(marker in text for marker in retryable_markers):
        return True

    # Unknown remote/upload errors are treated as transient by default.
    return True


async def _send_with_retry(
        client: Client,
        file_path: Path,
        caption: str,
        retries: int = MAX_RETRIES,
) -> tuple[Update, int] | None:
    for attempt in range(1, retries + 1):
        progress = TransferProgress("  Upload")
        try:
            result = await client.send_document(
                object_guid="me",
                document=str(file_path),
                caption=caption,
                callback=progress.callback,
            )
            progress.finish()
            return result, attempt
        except Exception as exc:
            progress.finish()
            if not _is_retryable_error(exc):
                raise CliError(f"Non-retryable error while sending {file_path.name}: {exc}") from exc
            if attempt == retries:
                raise CliError(f"Failed to send {file_path.name} after {retries} attempts: {exc}") from exc
            base_wait = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
            wait = base_wait + random.uniform(0.0, RETRY_JITTER_SECONDS)
            print(f"  Send failed (attempt {attempt}/{retries}), retrying in {wait:.1f}s... ({exc})")
            await asyncio.sleep(wait)
    return None


def _build_part_entries(parts: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for idx, part_path in enumerate(parts, 1):
        entries.append(
            {
                "index": idx,
                "name": part_path.name,
                "size": part_path.stat().st_size,
                "sha256": sha256_hash(part_path),
                "message_id": None,
                "attempts": 0,
                "sent_at": None,
            }
        )
    return entries


def _load_or_prepare_state(
    file_path: Path,
    state_dir: Path,
    fresh: bool,
    with_password: bool,
    chunk_size: int | None = None,
) -> tuple[dict[str, Any], bool]:
    if fresh:
        clear_state_dir(state_dir)

    state = load_state(state_dir)
    if state and state_matches_source(
            state,
            file_path,
            expected_password_protected=with_password,
    ) and resumable_parts_exist(state_dir, state):
        return state, True

    if state:
        print("Existing send state is stale or incomplete. Starting fresh upload state.")
        clear_state_dir(state_dir)

    state_dir.mkdir(parents=True, exist_ok=True)
    print(f"Zipping {file_path.name}...")
    zip_path, password = create_encrypted_zip(
        file_path,
        state_dir,
        with_password=with_password,
    )
    if chunk_size is not None:
        parts = split_file(zip_path, max_size=chunk_size)
    else:
        parts = split_file(zip_path)
    state = build_new_state(
        source_file=file_path,
        zip_name=zip_path.name,
        password=password,
        part_entries=_build_part_entries(parts),
    )
    save_state(state_dir, state)
    return state, False


async def send_relay_file(
    client: Client,
    file_path: Path,
    *,
    fresh: bool = False,
    with_password: bool = False,
    chunk_size: int | None = None,
) -> tuple[list[str], str | None]:
    """Zip, split, hash, and send a file to Saved Messages.

    Returns (list_of_message_ids, zip_password_or_none).
    """
    if not file_path.is_file():
        raise CliError(f"File not found: {file_path}")
    if chunk_size is not None and chunk_size <= 0:
        raise CliError("chunk_size must be a positive integer (bytes).")

    state_dir = state_dir_for_source(file_path)
    state, resumed = _load_or_prepare_state(
        file_path,
        state_dir,
        fresh=fresh,
        with_password=with_password,
        chunk_size=chunk_size,
    )
    original_name = state.get("source", {}).get("name", file_path.name)
    password = state.get("archive", {}).get("password")
    password_value = str(password) if password else None

    parts = state.get("parts") or []
    total = len(parts)
    if total == 0:
        raise CliError("Send state has no parts to upload.")

    start_idx = first_unsent_part_index(state)
    if resumed and start_idx <= total:
        print(f"Resuming upload from part {start_idx}/{total}...")

    if start_idx > total:
        message_ids = [str(part.get("message_id") or "unknown") for part in parts]
        print("Upload already completed in local state. Cleaning up local state directory.")
        clear_state_dir(state_dir)
        if file_path.suffix.lower() == ".zip":
            remove_file_safely(file_path)
            print(f"Source zip removed after successful send: {file_path.name}")
        return message_ids, password_value

    try:
        for part in parts[start_idx - 1:]:
            if part.get("message_id"):
                continue

            idx = int(part.get("index") or 0)
            if idx <= 0:
                raise CliError("Corrupted send state: invalid part index.")

            part_name = part.get("name")
            if not isinstance(part_name, str) or not part_name:
                raise CliError("Corrupted send state: invalid part name.")

            part_path = state_dir / part_name
            if not part_path.is_file():
                raise CliError(
                    f"Missing local part file for resume: {part_path}. "
                    "Re-run with --fresh to rebuild local upload parts."
                )

            file_hash = str(part.get("sha256") or sha256_hash(part_path))
            part["sha256"] = file_hash
            caption = f"{RELAY_TAG} {original_name} | {idx}/{total} | sha256:{file_hash}"

            print(f"Sending part {idx}/{total} ({part_path.name})...")
            result, used_attempts = await _send_with_retry(client, part_path, caption)
            mid = _extract_message_id(result)
            part["message_id"] = mid
            part["sent_at"] = time.time()
            part["attempts"] = int(part.get("attempts") or 0) + used_attempts
            state["status"] = "uploading"
            state["last_error"] = None
            save_state(state_dir, state)
            print(f"  Sent (message {mid})")

        state["status"] = "completed"
        state["last_error"] = None
        save_state(state_dir, state)

        message_ids = [str(part.get("message_id") or "unknown") for part in parts]
        clear_state_dir(state_dir)

        if file_path.suffix.lower() == ".zip":
            remove_file_safely(file_path)
            print(f"Source zip removed after successful send: {file_path.name}")

        return message_ids, password_value

    except Exception as exc:
        state["status"] = "interrupted"
        state["last_error"] = str(exc)
        save_state(state_dir, state)
        raise


def _extract_message_id(result) -> str:
    if hasattr(result, "message_update") and hasattr(result.message_update, "message_id"):
        return str(result.message_update.message_id)
    if hasattr(result, "message") and hasattr(result.message, "message_id"):
        return str(result.message.message_id)
    if hasattr(result, "message_id"):
        return str(result.message_id)
    return "unknown"
