from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from rubpy import Client
from rubpy.types import Update

from .config import (
    FILE_CHUNK_SIZE,
    MAX_RETRIES,
    RELAY_TAG,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_JITTER_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
)
from .errors import CliError
from .file_ops import (
    create_encrypted_zip,
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

    # Treat all unknown remote/upload errors as transient by default.
    return True


def _looks_like_direct_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _filename_from_content_disposition(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None

    # Support common Content-Disposition formats, including RFC 5987 filename*=.
    rfc5987 = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
    if rfc5987:
        return unquote(rfc5987.group(1).strip().strip('"'))

    basic = re.search(r"filename=([^;]+)", content_disposition, flags=re.IGNORECASE)
    if basic:
        return basic.group(1).strip().strip('"')

    return None


def _clean_file_name(name: str | None) -> str:
    cleaned = Path(name or "").name.strip()
    return cleaned or "downloaded_file"


def _download_source_url(source_url: str, data_dir: Path) -> Path:
    cache_dir = data_dir / "url-sources"
    cache_dir.mkdir(parents=True, exist_ok=True)

    request = Request(source_url, headers={"User-Agent": "rubika-relay-cli/0.1"})
    temp_path: Path | None = None
    try:
        with urlopen(request, timeout=120) as response:
            remote_name = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
            if not remote_name:
                remote_name = Path(unquote(urlparse(source_url).path)).name

            safe_name = _clean_file_name(remote_name)
            url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
            target_path = cache_dir / f"{url_hash}_{safe_name}"
            temp_path = target_path.with_suffix(target_path.suffix + ".part")

            with temp_path.open("wb") as fh:
                for chunk in iter(lambda: response.read(FILE_CHUNK_SIZE), b""):
                    fh.write(chunk)
    except Exception as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise CliError(f"Failed to download URL source: {source_url} ({exc})") from exc

    if target_path.exists():
        target_path.unlink(missing_ok=True)
    temp_path.replace(target_path)
    return target_path


def _resolve_source_file(source: str | Path, data_dir: Path | None) -> Path:
    if isinstance(source, Path):
        candidate = source.expanduser().resolve()
        if not candidate.is_file():
            raise CliError(f"File not found: {candidate}")
        return candidate

    source_text = source.strip()
    if _looks_like_direct_url(source_text):
        if data_dir is None:
            data_dir = Path.home() / ".rubika-relay"
        print(f"Downloading source from URL: {source_text}")
        downloaded_path = _download_source_url(source_text, data_dir.resolve())
        print(f"Saved URL source to: {downloaded_path}")
        return downloaded_path

    candidate = Path(source_text).expanduser().resolve()
    if not candidate.is_file():
        raise CliError(f"File not found: {candidate}")
    return candidate


async def _send_with_retry(
    client: Client,
    file_path: Path,
    caption: str,
    retries: int = MAX_RETRIES,
) -> tuple[Update, int]:
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
    raise CliError(f"Failed to send {file_path.name} after {retries} attempts.")


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
    orig_suffix = file_path.suffix.lstrip(".")
    part_stem = f"{zip_path.stem}.{orig_suffix}" if orig_suffix else zip_path.stem
    split_kwargs = {"part_stem": part_stem}
    if chunk_size is not None:
        split_kwargs["max_size"] = chunk_size

    parts = split_file(zip_path, **split_kwargs)
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
    source: str | Path,
    *,
    fresh: bool = False,
    with_password: bool = False,
    chunk_size: int | None = None,
    data_dir: Path | None = None,
) -> tuple[list[str], str | None]:
    """Zip, split, hash, and send a file to Saved Messages.

    Returns (list_of_message_ids, zip_password_or_none).
    """
    file_path = _resolve_source_file(source, data_dir)
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
