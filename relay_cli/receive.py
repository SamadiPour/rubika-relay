from __future__ import annotations

import asyncio
import hashlib
import random
import re
import shutil
from pathlib import Path
from typing import Any

import pyzipper
from rubpy import Client

from .config import FILE_CHUNK_SIZE, MAX_PARALLEL_DOWNLOADS, MAX_RETRIES, RELAY_TAG, RETRY_JITTER_SECONDS
from .errors import CliError
from .file_ops import sha256_hash
from .progress import MultiProgress

_CAPTION_RE = re.compile(
    re.escape(RELAY_TAG) + r"\s+(.+?)\s+\|\s+(\d+)/(\d+)\s+\|\s+sha256:([0-9a-f]{64})"
)

_RECV_WORK_ROOT = ".relay-recv"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_for_path(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return cleaned[:80]


def _recv_work_dir(output_dir: Path, original_name: str, total: int) -> Path:
    """Deterministic per-transfer work dir so partial downloads can be resumed."""
    safe = _sanitize_for_path(original_name)
    digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()[:10]
    return output_dir / _RECV_WORK_ROOT / f"{safe}-{total}-{digest}"


def _recv_part_path(work_dir: Path, idx: int) -> Path:
    return work_dir / f"part_{idx:03d}.bin"


def _fmt_size(num_bytes: int) -> str:
    value = float(max(int(num_bytes), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


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
    *,
    progress: "MultiProgress | None" = None,
    slot_idx: int | None = None,
    retries: int = MAX_RETRIES,
) -> str:
    for attempt in range(1, retries + 1):
        try:
            if progress is not None and slot_idx is not None:
                def _cb(total: int, current: int) -> None:
                    progress.update_slot(slot_idx, current)
                await client.download(file_inline, save_as=save_as, callback=_cb)
            else:
                await client.download(file_inline, save_as=save_as)
            return save_as
        except Exception as exc:
            if attempt == retries:
                raise CliError(f"Download failed after {retries} attempts: {exc}") from exc
            wait = 2 ** attempt + random.uniform(0.0, RETRY_JITTER_SECONDS)
            msg = f"  Download failed (attempt {attempt}/{retries}), retrying in {wait:.1f}s... ({exc})"
            if progress is not None:
                progress.log(msg)
            else:
                print(msg)
            await asyncio.sleep(wait)
    raise CliError(f"Download failed after {retries} attempts.")


async def receive_relay_files(
    client: Client,
    output_dir: Path,
    *,
    keep: bool = False,
    parallel: int = MAX_PARALLEL_DOWNLOADS,
    fresh: bool = False,
) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    object_guid = str(getattr(client, "guid", "") or "")

    if not object_guid:
        raise CliError("Session is missing user GUID; cannot access Saved Messages.")

    if parallel < 1:
        raise CliError("parallel must be >= 1.")

    if fresh:
        fresh_root = output_dir / _RECV_WORK_ROOT
        if fresh_root.exists():
            print(f"--fresh: removing cached partial downloads at {fresh_root}")
            shutil.rmtree(fresh_root, ignore_errors=True)

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

        work_dir = _recv_work_dir(output_dir, original_name, total)
        work_dir.mkdir(parents=True, exist_ok=True)
        group_message_ids: list[str] = []
        part_paths_by_idx: dict[int, Path] = {}
        status = "ok"
        restored_name = _safe_file_name(original_name)

        for idx in range(1, total + 1):
            msg, _meta = part_map[idx]
            group_message_ids.append(str(msg.message_id))

        # Detect already-downloaded parts by verifying on-disk sha256 against the caption.
        already_done: dict[int, Path] = {}
        already_bytes = 0
        for idx in range(1, total + 1):
            _, meta = part_map[idx]
            cached = _recv_part_path(work_dir, idx)
            if cached.is_file():
                try:
                    if sha256_hash(cached) == meta["sha256"]:
                        already_done[idx] = cached
                        already_bytes += cached.stat().st_size
                    else:
                        cached.unlink(missing_ok=True)
                except OSError:
                    cached.unlink(missing_ok=True)

        pending_idxs = [i for i in range(1, total + 1) if i not in already_done]
        if already_done:
            print(
                f"Resuming {original_name}: {len(already_done)}/{total} part(s) "
                f"already downloaded ({_fmt_size(already_bytes)}), "
                f"{len(pending_idxs)} remaining."
            )

        part_paths_by_idx.update(already_done)

        worker_count = max(1, min(parallel, max(1, len(pending_idxs))))
        if pending_idxs:
            if worker_count > 1:
                print(
                    f"Downloading {len(pending_idxs)} part(s) with up to "
                    f"{worker_count} concurrent transfer(s)..."
                )
            else:
                print(f"Downloading {len(pending_idxs)} part(s)...")

        overall_total = 0
        for idx in range(1, total + 1):
            msg, _ = part_map[idx]
            overall_total += int(getattr(msg.file_inline, "size", 0) or 0)

        progress = MultiProgress(
            overall_label=f"Download {original_name}",
            overall_total=overall_total,
            slot_count=worker_count,
        )
        # Credit already-completed parts to the overall bar.
        if already_bytes:
            progress.overall_current = min(overall_total, already_bytes)

        sem = asyncio.Semaphore(worker_count)
        hash_mismatch_detected = asyncio.Event()

        async def download_one(idx: int) -> None:
            if hash_mismatch_detected.is_set():
                return
            msg, meta = part_map[idx]
            file_inline = msg.file_inline
            save_path = _recv_part_path(work_dir, idx)
            part_size = int(getattr(file_inline, "size", 0) or 0)

            async with sem:
                if hash_mismatch_detected.is_set():
                    return
                slot_idx = progress.acquire_slot(f"part {idx}/{total}", part_size)
                try:
                    await _download_with_retry(
                        client,
                        file_inline,
                        str(save_path),
                        progress=progress,
                        slot_idx=slot_idx,
                    )
                finally:
                    progress.finish_slot(slot_idx)

            actual_hash = sha256_hash(save_path)
            if actual_hash != meta["sha256"]:
                progress.log(
                    f"  [part {idx}/{total}] HASH MISMATCH! expected {meta['sha256'][:16]}... "
                    f"got {actual_hash[:16]}..."
                )
                # Remove corrupt part so the next run re-downloads it.
                save_path.unlink(missing_ok=True)
                hash_mismatch_detected.set()
                return

            part_paths_by_idx[idx] = save_path

        try:
            if pending_idxs:
                tasks = [asyncio.create_task(download_one(i)) for i in pending_idxs]
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
                # Only remove the work dir once the file is fully restored.
                shutil.rmtree(work_dir, ignore_errors=True)

        except CliError:
            raise
        except Exception as exc:
            status = "extract_failed"
            print(f"  Failed to restore {original_name}: {exc}")
            # Keep work_dir in place so the next run can resume from verified parts.

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
