from __future__ import annotations

import asyncio
import hashlib
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from rubpy import Client
from rubpy.types import Update

from .config import (
    FILE_CHUNK_SIZE,
    MAX_PARALLEL_UPLOADS,
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
from .progress import MultiProgress
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


def _download_source_url(source_url: str) -> tuple[Path, Path]:
    """Download a URL source to a fresh temp directory.

    Returns (downloaded_file_path, temp_dir_to_cleanup).
    """
    cache_dir = Path(tempfile.mkdtemp(prefix="rubika-relay-url-"))

    request = Request(source_url, headers={"User-Agent": "rubika-relay-cli/0.1"})
    temp_path: Path | None = None
    target_path: Path | None = None
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
        shutil.rmtree(cache_dir, ignore_errors=True)
        raise CliError(f"Failed to download URL source: {source_url} ({exc})") from exc

    assert target_path is not None and temp_path is not None
    temp_path.replace(target_path)
    return target_path, cache_dir


def _resolve_source_file(source: str | Path) -> tuple[Path, Path | None]:
    """Resolve a user-provided source into a local file path.

    Returns (file_path, temp_dir_to_cleanup_after_upload). The second element
    is non-None only when the source was downloaded from a URL into a
    temporary directory that should be removed after a successful upload.
    """
    if isinstance(source, Path):
        candidate = source.expanduser().resolve()
        if not candidate.is_file():
            raise CliError(f"File not found: {candidate}")
        return candidate, None

    source_text = source.strip()
    if _looks_like_direct_url(source_text):
        print(f"Downloading source from URL: {source_text}")
        downloaded_path, temp_dir = _download_source_url(source_text)
        print(f"Saved URL source to: {downloaded_path}")
        return downloaded_path, temp_dir

    candidate = Path(source_text).expanduser().resolve()
    if not candidate.is_file():
        raise CliError(f"File not found: {candidate}")
    return candidate, None


async def _send_with_retry(
    client: Client,
    file_path: Path,
    caption: str,
    retries: int = MAX_RETRIES,
    *,
    progress: "MultiProgress | None" = None,
    slot_idx: int | None = None,
) -> tuple[Update, int]:
    for attempt in range(1, retries + 1):
        try:
            if progress is not None and slot_idx is not None:
                def _cb(total: int, current: int) -> None:
                    progress.update_slot(slot_idx, current)
                result = await client.send_document(
                    object_guid="me",
                    document=str(file_path),
                    caption=caption,
                    callback=_cb,
                )
            else:
                result = await client.send_document(
                    object_guid="me",
                    document=str(file_path),
                    caption=caption,
                )
            return result, attempt
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise CliError(f"Non-retryable error while sending {file_path.name}: {exc}") from exc
            if attempt == retries:
                raise CliError(f"Failed to send {file_path.name} after {retries} attempts: {exc}") from exc
            base_wait = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
            wait = base_wait + random.uniform(0.0, RETRY_JITTER_SECONDS)
            msg = f"  [{file_path.name}] send failed (attempt {attempt}/{retries}), retrying in {wait:.1f}s... ({exc})"
            if progress is not None:
                progress.log(msg)
            else:
                print(msg)
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


def _build_upload_manifest(parts: list[dict[str, Any]]) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for part in parts:
        manifest.append(
            {
                "index": str(part.get("index") or ""),
                "file": str(part.get("name") or ""),
                "sha256": str(part.get("sha256") or ""),
                "message_id": str(part.get("message_id") or "unknown"),
            }
        )
    return manifest


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
    parallel: int = MAX_PARALLEL_UPLOADS,
) -> tuple[list[dict[str, str]], str | None]:
    """Zip, split, hash, and send a file to Saved Messages.

    Returns (upload_manifest, zip_password_or_none).
    """
    file_path, url_temp_dir = _resolve_source_file(source)
    if chunk_size is not None and chunk_size <= 0:
        raise CliError("chunk_size must be a positive integer (bytes).")
    if parallel < 1:
        raise CliError("parallel must be >= 1.")

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

    def _cleanup_url_temp_dir() -> None:
        if url_temp_dir is not None and url_temp_dir.exists():
            shutil.rmtree(url_temp_dir, ignore_errors=True)

    if start_idx > total:
        manifest = _build_upload_manifest(parts)
        print("Upload already completed in local state. Cleaning up local state directory.")
        clear_state_dir(state_dir)
        _cleanup_url_temp_dir()
        return manifest, password_value

    try:
        pending = [p for p in parts[start_idx - 1:] if not p.get("message_id")]
        if not pending:
            state["status"] = "completed"
            save_state(state_dir, state)
            manifest = _build_upload_manifest(parts)
            clear_state_dir(state_dir)
            _cleanup_url_temp_dir()
            return manifest, password_value

        worker_count = max(1, min(parallel, len(pending)))
        if worker_count > 1:
            print(f"Uploading {len(pending)} part(s) with up to {worker_count} concurrent transfer(s)...")
        else:
            print(f"Uploading {len(pending)} part(s)...")

        overall_total = sum(int(p.get("size") or 0) for p in pending)
        progress = MultiProgress(
            overall_label="Upload",
            overall_total=overall_total,
            slot_count=worker_count,
        )

        sem = asyncio.Semaphore(worker_count)
        state_lock = asyncio.Lock()

        async def upload_one(part: dict[str, Any]) -> None:
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
            part_size = int(part.get("size") or part_path.stat().st_size)

            async with sem:
                slot_label = f"part {idx}/{total}"
                slot_idx = progress.acquire_slot(slot_label, part_size)
                try:
                    result, used_attempts = await _send_with_retry(
                        client,
                        part_path,
                        caption,
                        progress=progress,
                        slot_idx=slot_idx,
                    )
                finally:
                    progress.finish_slot(slot_idx)

            mid = _extract_message_id(result)
            async with state_lock:
                part["message_id"] = mid
                part["sent_at"] = time.time()
                part["attempts"] = int(part.get("attempts") or 0) + used_attempts
                state["status"] = "uploading"
                state["last_error"] = None
                save_state(state_dir, state)

        tasks = [asyncio.create_task(upload_one(p)) for p in pending]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            progress.close()
            raise
        progress.close()

        state["status"] = "completed"
        state["last_error"] = None
        save_state(state_dir, state)

        manifest = _build_upload_manifest(parts)
        clear_state_dir(state_dir)
        _cleanup_url_temp_dir()

        return manifest, password_value

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
