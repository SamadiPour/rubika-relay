from __future__ import annotations

import asyncio
from pathlib import Path

from rubpy import Client

from .config import MAX_RETRIES, RELAY_TAG
from .errors import CliError
from .file_ops import (
    create_encrypted_zip,
    ensure_dir,
    remove_file_safely,
    sha256_hash,
    split_file,
)


async def _send_with_retry(
        client: Client,
        file_path: Path,
        caption: str,
        retries: int = MAX_RETRIES,
):
    for attempt in range(1, retries + 1):
        try:
            return await client.send_document(
                object_guid="me",
                document=str(file_path),
                caption=caption,
            )
        except Exception as exc:
            if attempt == retries:
                raise CliError(f"Failed to send {file_path.name} after {retries} attempts: {exc}") from exc
            wait = 2 ** attempt
            print(f"  Send failed (attempt {attempt}/{retries}), retrying in {wait}s... ({exc})")
            await asyncio.sleep(wait)


async def send_relay_file(
        client: Client,
        file_path: Path,
        tmp_dir: Path,
) -> tuple[list[str], str]:
    """Zip, split, hash, and send a file to Saved Messages.

    Returns (list_of_message_ids, zip_password).
    """
    if not file_path.is_file():
        raise CliError(f"File not found: {file_path}")

    ensure_dir(tmp_dir)
    original_name = file_path.name

    print(f"Zipping {original_name}...")
    zip_path, password = create_encrypted_zip(file_path, tmp_dir)

    temp_files: list[Path] = [zip_path]
    sent_successfully = False
    try:
        parts = split_file(zip_path)
        if parts[0] != zip_path:
            temp_files.extend(parts)
        total = len(parts)

        message_ids: list[str] = []
        for idx, part_path in enumerate(parts, 1):
            file_hash = sha256_hash(part_path)
            caption = f"{RELAY_TAG} {original_name} | {idx}/{total} | sha256:{file_hash}"

            print(f"Sending part {idx}/{total} ({part_path.name})...")
            result = await _send_with_retry(client, part_path, caption)
            mid = _extract_message_id(result)
            message_ids.append(mid)
            print(f"  Sent (message {mid})")

        sent_successfully = True
        return message_ids, password

    finally:
        for f in temp_files:
            remove_file_safely(f)

        if sent_successfully and file_path.suffix.lower() == ".zip":
            remove_file_safely(file_path)
            print(f"Source zip removed after successful send: {file_path.name}")


def _extract_message_id(result) -> str:
    if hasattr(result, "message_update") and hasattr(result.message_update, "message_id"):
        return str(result.message_update.message_id)
    if hasattr(result, "message") and hasattr(result.message, "message_id"):
        return str(result.message.message_id)
    if hasattr(result, "message_id"):
        return str(result.message_id)
    return "unknown"
