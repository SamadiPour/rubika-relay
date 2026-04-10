from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rubpy import Client

from .config import MAX_RETRIES, RELAY_TAG
from .errors import CliError
from .file_ops import sha256_hash

_CAPTION_RE = re.compile(
    re.escape(RELAY_TAG) + r"\s+(.+?)\s+\|\s+(\d+)/(\d+)\s+\|\s+sha256:([0-9a-f]{64})"
)


def _parse_caption(text: str):
    m = _CAPTION_RE.match(text)
    if not m:
        return None
    return {
        "original_name": m.group(1),
        "part": int(m.group(2)),
        "total": int(m.group(3)),
        "sha256": m.group(4),
    }


async def _fetch_relay_messages(client: Client, limit_per_page: int = 50, max_pages: int = 4):
    relay_msgs = []
    max_id = "0"
    object_guid = str(getattr(client, "guid", "") or "")

    if not object_guid:
        raise CliError("Session is missing user GUID; cannot fetch Saved Messages.")

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
        retries: int = MAX_RETRIES,
) -> str:
    for attempt in range(1, retries + 1):
        try:
            await client.download(file_inline, save_as=save_as)
            return save_as
        except Exception as exc:
            if attempt == retries:
                raise CliError(f"Download failed after {retries} attempts: {exc}") from exc
            wait = 2 ** attempt
            print(f"  Download failed (attempt {attempt}/{retries}), retrying in {wait}s... ({exc})")
            await asyncio.sleep(wait)


async def receive_relay_files(client: Client, output_dir: Path) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    object_guid = str(getattr(client, "guid", "") or "")

    if not object_guid:
        raise CliError("Session is missing user GUID; cannot access Saved Messages.")

    print("Fetching messages from Saved Messages...")
    relay_msgs = await _fetch_relay_messages(client)

    if not relay_msgs:
        print("No relay files found.")
        return []

    print(f"Found {len(relay_msgs)} relay file(s).")

    results = []
    delete_ids = []

    for msg in relay_msgs:
        text = getattr(msg, "text", "") or ""
        meta = _parse_caption(text)
        if not meta:
            print(f"  Skipping message with unparseable caption: {text[:60]}")
            continue

        file_inline = msg.file_inline
        file_name = getattr(file_inline, "file_name", None) or f"{meta['original_name']}.part{meta['part']:02d}"
        save_path = output_dir / file_name

        print(f"Downloading {file_name} (part {meta['part']}/{meta['total']})...")
        await _download_with_retry(client, file_inline, str(save_path))

        actual_hash = sha256_hash(save_path)
        if actual_hash == meta["sha256"]:
            print(f"  Hash verified OK")
            delete_ids.append(str(msg.message_id))
            results.append({"file": file_name, "status": "ok", "part": meta["part"], "total": meta["total"],
                            "original_name": meta["original_name"]})
        else:
            print(f"  HASH MISMATCH! Expected {meta['sha256'][:16]}... got {actual_hash[:16]}...")
            results.append({"file": file_name, "status": "hash_mismatch", "part": meta["part"], "total": meta["total"],
                            "original_name": meta["original_name"]})

    if delete_ids:
        print(f"Deleting {len(delete_ids)} verified message(s)...")
        try:
            await client.delete_messages(object_guid, message_ids=delete_ids, type="Global")
        except Exception as exc:
            print(f"  Warning: failed to delete messages: {exc}")

    return results
