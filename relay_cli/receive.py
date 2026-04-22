from __future__ import annotations

import asyncio
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pyzipper
from rubpy import Client

from .config import FILE_CHUNK_SIZE, MAX_PARALLEL_DOWNLOADS, MAX_RETRIES, RELAY_TAG, RETRY_JITTER_SECONDS
from .errors import CliError
from .file_ops import sha256_hash
from .progress import TransferProgress

_CAPTION_RE = re.compile(
    re.escape(RELAY_TAG) + r"\s+(.+?)\s+\|\s+(\d+)/(\d+)\s+\|\s+sha256:([0-9a-f]{64})"
)


def _parse_caption(text: str) -> dict | None:
    m = _CAPTION_RE.match(text)
    if not m:
        return None
    return {
        "original_name": m.group(1),
        "part": int(m.group(2)),
        "total": int(m.group(3)),
        "sha256": m.group(4),
    }


def _safe_file_name(name: str) -> str:
    cleaned = Path(name).name.strip()
    return cleaned or "restored_file"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise CliError(f"Could not find a unique output name for: {path.name}")


def _assemble_parts(part_paths: list[Path], archive_path: Path) -> None:
    with archive_path.open("wb") as out:
        for part_path in part_paths:
            with part_path.open("rb") as src:
                for chunk in iter(lambda: src.read(FILE_CHUNK_SIZE), b""):
                    out.write(chunk)


def _extract_archive_to_original_name(archive_path: Path, output_dir: Path, original_name: str) -> Path:
    target_path = _unique_path(output_dir / _safe_file_name(original_name))

    with pyzipper.AESZipFile(archive_path, "r") as zf:
        entries = [entry for entry in zf.infolist() if not entry.is_dir()]
        if not entries:
            raise CliError("Archive is empty; nothing to extract.")

        # Sender writes one file per archive; extracting the first file is sufficient.
        first_entry = entries[0]
        with zf.open(first_entry, "r") as src, target_path.open("wb") as dest:
            for chunk in iter(lambda: src.read(FILE_CHUNK_SIZE), b""):
                dest.write(chunk)

    return target_path


async def _fetch_relay_messages(client: Client, object_guid: str, limit_per_page: int = 50, max_pages: int = 4) -> list:
    relay_msgs = []
    max_id = "0"

    for _ in range(max_pages):
        result = await client.get_messages(object_guid, max_id=max_id, limit=str(limit_per_page))
        messages = getattr(result, "messages", None)
        if not messages:
            break

        for msg in messages:
            text = getattr(msg, "text", None) or ""
            if text.startswith(RELAY_TAG) and getattr(msg, "file_inline", None) is not None:
                relay_msgs.append(msg)

        ids = [getattr(m, "message_id", "0") for m in messages]
        min_id = min(ids, key=lambda x: int(x) if str(x).isdigit() else 0)
        if min_id == max_id or len(messages) < limit_per_page:
            break
        max_id = str(min_id)

    return relay_msgs


async def _download_with_retry(
    client: Client,
    file_inline,
    save_as: str,
    progress: TransferProgress | None,
    retries: int = MAX_RETRIES,
) -> str:
    for attempt in range(1, retries + 1):
        try:
            await client.download(
                file_inline,
                save_as=save_as,
                callback=progress.callback if progress else None,
            )
            return save_as
        except Exception as exc:
            if progress:
                progress.finish()
            if attempt == retries:
                raise CliError(f"Download failed after {retries} attempts: {exc}") from exc
            wait = 2 ** attempt + random.uniform(0.0, RETRY_JITTER_SECONDS)
            print(f"  Download failed (attempt {attempt}/{retries}), retrying in {wait:.1f}s... ({exc})")
            await asyncio.sleep(wait)
    raise CliError(f"Download failed after {retries} attempts.")


async def receive_relay_files(
    client: Client,
    output_dir: Path,
    *,
    keep: bool = False,
    parallel: int = MAX_PARALLEL_DOWNLOADS,
) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    object_guid = str(getattr(client, "guid", "") or "")

    if not object_guid:
        raise CliError("Session is missing user GUID; cannot access Saved Messages.")

    if parallel < 1:
        raise CliError("parallel must be >= 1.")

    print("Fetching messages from Saved Messages...")
    relay_msgs = await _fetch_relay_messages(client, object_guid)

    if not relay_msgs:
        print("No relay files found.")
        return []

    print(f"Found {len(relay_msgs)} relay file(s).")

    results = []
    delete_ids = []

    grouped: dict[tuple[str, int], dict[int, tuple[Any, dict[str, Any]]]] = {}
    for msg in relay_msgs:
        text = getattr(msg, "text", "") or ""
        meta = _parse_caption(text)
        if not meta:
            print(f"  Skipping message with unparseable caption: {text[:60]}")
            continue

        total = int(meta["total"])
        part = int(meta["part"])
        if part < 1 or total < 1 or part > total:
            print(f"  Skipping invalid relay metadata: part={part}, total={total}")
            continue

        key = (str(meta["original_name"]), total)
        per_part = grouped.setdefault(key, {})
        # Keep the first occurrence (newest message page order) when duplicates exist.
        if part not in per_part:
            per_part[part] = (msg, meta)

    for (original_name, total), part_map in grouped.items():
        missing = [idx for idx in range(1, total + 1) if idx not in part_map]
        if missing:
            print(
                f"Skipping {original_name}: missing {len(missing)} part(s) "
                f"({', '.join(str(x) for x in missing[:10])}{'...' if len(missing) > 10 else ''})."
            )
            results.append(
                {
                    "file": _safe_file_name(original_name),
                    "status": "missing_parts",
                    "part": 0,
                    "total": total,
                    "original_name": original_name,
                }
            )
            continue

        work_dir = Path(tempfile.mkdtemp(prefix="relay_parts_", dir=str(output_dir)))
        group_message_ids: list[str] = []
        part_paths_by_idx: dict[int, Path] = {}
        status = "ok"
        restored_name = _safe_file_name(original_name)

        for idx in range(1, total + 1):
            msg, _meta = part_map[idx]
            group_message_ids.append(str(msg.message_id))

        worker_count = max(1, min(parallel, total))
        show_progress = worker_count == 1
        if worker_count > 1:
            print(f"Downloading {total} part(s) with up to {worker_count} concurrent transfer(s)...")

        sem = asyncio.Semaphore(worker_count)
        hash_mismatch_detected = asyncio.Event()

        async def download_one(idx: int) -> None:
            if hash_mismatch_detected.is_set():
                return
            msg, meta = part_map[idx]
            file_inline = msg.file_inline
            raw_name = getattr(file_inline, "file_name", None) or f"{original_name}.{idx:03d}"
            file_name = Path(raw_name).name or f"part_{idx:03d}"
            save_path = work_dir / f"{idx:03d}_{file_name}"

            async with sem:
                if hash_mismatch_detected.is_set():
                    return
                print(f"Downloading {file_name} (part {idx}/{total})...")
                start_time = time.time()
                progress = TransferProgress("  Download") if show_progress else None
                await _download_with_retry(client, file_inline, str(save_path), progress)
                if progress:
                    progress.finish()
                elapsed = time.time() - start_time

            actual_hash = sha256_hash(save_path)
            if actual_hash != meta["sha256"]:
                print(f"  [part {idx}/{total}] HASH MISMATCH! expected {meta['sha256'][:16]}... got {actual_hash[:16]}...")
                hash_mismatch_detected.set()
                return

            part_paths_by_idx[idx] = save_path
            if worker_count > 1:
                print(f"  [part {idx}/{total}] downloaded and verified in {elapsed:.1f}s")
            else:
                print("  Hash verified OK")

        try:
            tasks = [asyncio.create_task(download_one(i)) for i in range(1, total + 1)]
            try:
                await asyncio.gather(*tasks)
            except Exception:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            if hash_mismatch_detected.is_set():
                status = "hash_mismatch"
            else:
                part_paths = [part_paths_by_idx[i] for i in range(1, total + 1)]
                archive_path = work_dir / "relay_archive.zip"
                _assemble_parts(part_paths, archive_path)

                restored_path = _extract_archive_to_original_name(
                    archive_path=archive_path,
                    output_dir=output_dir,
                    original_name=original_name,
                )
                restored_name = restored_path.name
                print(f"Restored: {restored_name}")
                delete_ids.extend(group_message_ids)

        except CliError:
            raise
        except Exception as exc:
            status = "extract_failed"
            print(f"  Failed to restore {original_name}: {exc}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        results.append(
            {
                "file": restored_name,
                "status": status,
                "part": total if status == "ok" else 0,
                "total": total,
                "original_name": original_name,
            }
        )

    if delete_ids and not keep:
        print(f"Deleting {len(delete_ids)} verified message(s)...")
        try:
            await client.delete_messages(object_guid, message_ids=delete_ids, type="Global")
        except Exception as exc:
            print(f"  Warning: failed to delete messages: {exc}")

    return results
